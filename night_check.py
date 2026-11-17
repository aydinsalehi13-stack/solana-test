"""Was this a FULL PAPER night? (Phase 9.) Reads only what the desk wrote to its own database.

  python night_check.py            the last closed night (or the open one)
  python night_check.py --night 3  a specific night
  python night_check.py --json     the same, machine readable

A night counts as "full" only when all three are true, each from data:
  coverage   the desk actually ran >= MIN_COVERAGE_PCT of the night (uptime_segments; sleep and downtime do not count)
  loop lag   the event-loop lag p95 over the night is < LAG_P95_MAX_MS (metrics_log, one row a minute; spec section 11)
  restarts   the restart log exists for the night (restart_log: component restarts, wakes, desk starts); the list is shown, never hidden
No data is "no data", never a pass. MIN_COVERAGE_PCT is my proposed threshold: the spec asks for coverage to be reported, not for a
number; change it here if you want a stricter or looser definition of "full".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import config
import osutil
from bus import percentile

LAG_P95_MAX_MS = 250.0  # Phase 9 acceptance
MIN_COVERAGE_PCT = 90.0  # proposed, not from the spec (see the module doc)
MAX_LISTED = 25


async def pick(db, night_id: int | None = None) -> dict | None:
    if night_id is not None:
        return await db.fetchone("SELECT * FROM nights WHERE id=?", (night_id,))
    row = await db.fetchone("SELECT * FROM nights WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 1")
    return row or await db.fetchone("SELECT * FROM nights ORDER BY id DESC LIMIT 1")


def lag_summary(rows: list[dict]) -> dict:
    p95s = sorted(r["lag_p95_ms"] for r in rows if r["lag_p95_ms"] is not None)
    maxes = [r["lag_max_ms"] for r in rows if r["lag_max_ms"] is not None]
    if not p95s:
        return {"status": "no data", "minutes": 0, "p95_ms": None, "worst_minute_p95_ms": None, "max_ms": None, "limit_ms": LAG_P95_MAX_MS}
    p95 = percentile(p95s, 95)  # the p95 of the per-minute p95s: a few bad minutes cannot hide, one stall cannot fail a night
    return {"status": "pass" if p95 < LAG_P95_MAX_MS else "fail", "minutes": len(p95s), "p95_ms": round(p95, 1), "worst_minute_p95_ms": round(p95s[-1], 1),
            "max_ms": round(max(maxes), 1) if maxes else None, "limit_ms": LAG_P95_MAX_MS}


async def restart_summary(db, start: float, end: float) -> dict:
    ever = await db.fetchone("SELECT COUNT(*) n FROM restart_log WHERE ts <= ?", (end,))
    rows = await db.fetchall("SELECT ts, kind, bot, reason, down_s FROM restart_log WHERE ts BETWEEN ? AND ? ORDER BY ts", (start, end))
    by_bot: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "component":
            by_bot[r["bot"] or "?"] = by_bot.get(r["bot"] or "?", 0) + 1
    count = lambda k: sum(1 for r in rows if r["kind"] == k)  # noqa: E731
    return {"status": "logged" if (ever["n"] or 0) > 0 else "no data", "components": count("component"), "wakes": count("wake"), "desk_starts": count("desk_start"),
            "by_bot": by_bot, "listed": rows[:MAX_LISTED], "more": max(0, len(rows) - MAX_LISTED)}


async def evaluate(db, night_id: int | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    n = await pick(db, night_id)
    if n is None:
        return {"night": None, "full": False, "reasons": ["no night in the database yet"]}
    start, end = n["started_at"], n["ended_at"] or now
    window = max(end - start, 1.0)
    covered = await db.coverage_seconds(start, end)
    pct = 100.0 * covered / window
    lag = lag_summary(await db.fetchall("SELECT lag_p95_ms, lag_max_ms FROM metrics_log WHERE ts BETWEEN ? AND ?", (start, end)))
    restarts = await restart_summary(db, start, end)
    reasons = []
    if n["ended_at"] is None:
        reasons.append("the night is still open")
    if covered <= 0:
        reasons.append("no coverage data for this night")
    elif pct < MIN_COVERAGE_PCT:
        reasons.append(f"coverage {pct:.0f}% is under {MIN_COVERAGE_PCT:.0f}%")
    if lag["status"] != "pass":
        reasons.append("loop lag: no data" if lag["status"] == "no data" else f"loop lag p95 {lag['p95_ms']} ms is not under {LAG_P95_MAX_MS:.0f} ms")
    if restarts["status"] != "logged":
        reasons.append("restarts: no restart log")
    return {"night": {"id": n["id"], "start": start, "end": end, "open": n["ended_at"] is None},
            "window_s": window, "coverage_s": covered, "coverage_pct": round(pct, 1), "coverage_ok": covered > 0 and pct >= MIN_COVERAGE_PCT,
            "lag": lag, "restarts": restarts, "full": not reasons, "reasons": reasons}


async def full_nights(db) -> int:
    """How many closed nights meet all three checks. This is what the LIVE gate counts."""
    n = 0
    for r in await db.fetchall("SELECT id FROM nights WHERE ended_at IS NOT NULL"):
        if (await evaluate(db, r["id"]))["full"]:
            n += 1
    return n


def render(res: dict) -> str:
    if res["night"] is None:
        return "night check: " + res["reasons"][0]
    nt, lag, rs = res["night"], res["lag"], res["restarts"]
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))  # noqa: E731
    out = [f"night #{nt['id']}  {fmt(nt['start'])} to {fmt(nt['end'])}{' (still open)' if nt['open'] else ''}",
           f"  coverage   {res['coverage_s'] / 3600:.1f} of {res['window_s'] / 3600:.1f} h = {res['coverage_pct']:.0f}%  need {MIN_COVERAGE_PCT:.0f}%  {'ok' if res['coverage_ok'] else 'NOT ok'}",
           "  loop lag   " + ("no data" if lag["status"] == "no data" else f"p95 {lag['p95_ms']} ms (worst minute {lag['worst_minute_p95_ms']} ms, max {lag['max_ms']} ms) limit {lag['limit_ms']:.0f} ms  {lag['status']}"),
           f"  restarts   {rs['components']} component, {rs['wakes']} wake, {rs['desk_starts']} desk start  [{rs['status']}]"]
    out += [f"    {fmt(r['ts'])} {r['kind']:<10} {r['bot'] or '-':<12} {r['reason']}" for r in rs["listed"]]
    if rs["more"]:
        out.append(f"    ... and {rs['more']} more")
    out.append("  verdict    FULL PAPER NIGHT" if res["full"] else "  verdict    not a full night: " + "; ".join(res["reasons"]))
    return "\n".join(out)


async def _amain(night: int | None, as_json: bool) -> int:
    from db import Database

    if not config.DB_PATH.exists():
        osutil.say("night check: no database yet (the desk has never run here)")
        return 1
    db = Database(config.DB_PATH)
    await db.start()
    try:
        res = await evaluate(db, night)
    finally:
        await db.stop()
    osutil.say(json.dumps(res, indent=2, default=str) if as_json else render(res))
    return 0 if res["full"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--night", type=int)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    config.load_env()
    return asyncio.run(_amain(a.night, a.json))


if __name__ == "__main__":
    sys.exit(main())
