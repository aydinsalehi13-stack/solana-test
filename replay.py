"""Replay, statistics, latency sweep, walk-forward and the promotion rule for orion_fast (spec 6.3).

    python replay.py run            replay everything recorded in the database with the current params
    python replay.py sweep          latency sweep 0/2/5/20/40 s: how much edge disappears
    python replay.py walk           walk-forward over the recorded days, experiment grid, promotion verdict

The engine is deterministic, so two runs over the same ticks are identical. Nothing here trades or clicks.
"""
from __future__ import annotations

import asyncio
import math
import sys

import config
import orion_core as oc

LATENCIES = (0, 2, 5, 20, 40)


def replay(ticks: list[dict], params: dict, start_equity: float | None = None, latency_s: float | None = None) -> dict:
    p = oc.merge(params, {"fill": {"latency_s": latency_s}}) if latency_s is not None else params
    eng = oc.Engine(p, start_equity)
    events: list[dict] = []
    for t in ticks:
        events += eng.on_tick(t)
    return {"trades": eng.trades, "events": events, "equity_curve": eng.equity_curve, "final_equity": eng.equity(),
            "open_positions": len(eng.positions), "start_equity": eng.start_equity, "engine": eng,
            "signals": sum(1 for e in events if e["type"] == "entry_signal"),
            "cancelled": sum(1 for e in events if e["type"] == "entry_cancel"),
            "stats": stats(eng.trades, eng.start_equity, _span_days(ticks))}


def _span_days(ticks: list[dict]) -> float | None:
    if len(ticks) < 2:
        return None
    return max((ticks[-1]["ts"] - ticks[0]["ts"]) / 86400.0, 1.0 / 24)


def stats(trades: list[dict], start_equity: float, days: float | None = None) -> dict:
    """Net after ALL fees, win rate, avg win/loss, profit factor, max drawdown, trades/day, share of P&L from the top 3."""
    pnl = [t["pnl_usd"] for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    net = sum(pnl)
    equity, peak, dd = start_equity, start_equity, 0.0
    for x in pnl:
        equity += x
        peak = max(peak, equity)
        dd = max(dd, 100.0 * (peak - equity) / peak if peak else 0.0)
    gross_loss = -sum(losses)
    top3 = sum(sorted(pnl, reverse=True)[:3])
    return {"trades": len(pnl), "net_usd": net, "net_pct": 100.0 * net / start_equity if start_equity else 0.0,
            "win_rate": 100.0 * len(wins) / len(pnl) if pnl else None,
            "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
            "profit_factor": (sum(wins) / gross_loss if gross_loss > 0 else (math.inf if wins else None)),
            "max_drawdown_pct": dd, "trades_per_day": (len(pnl) / days) if days and pnl else None,
            "top3_share_pct": (100.0 * top3 / net if net > 0 else None),
            "fees_usd": sum(t["fees_total"] for t in trades)}


def latency_sweep(ticks: list[dict], params: dict, latencies=LATENCIES, start_equity: float | None = None) -> list[dict]:
    """The same recorded hour traded with simulated latency 0/2/5/20/40 s: shows how much edge disappears."""
    rows = []
    for lat in latencies:
        r = replay(ticks, params, start_equity, latency_s=float(lat))
        rows.append({"latency_s": lat, "signals": r["signals"], "cancelled": r["cancelled"], **r["stats"]})
    return rows


def split_days(ticks: list[dict]) -> list[list[dict]]:
    days: dict[int, list[dict]] = {}
    for t in ticks:
        days.setdefault(int(t["ts"] // 86400), []).append(t)
    return [days[k] for k in sorted(days)]


def walk_forward(ticks: list[dict], candidates: dict[str, dict], baseline: str = "baseline") -> dict:
    """Pick the params on day N, report on day N+1 only. Candidates are named full param sets.
    Returns the out-of-sample trades/stats of the walk-forward choice and of the baseline."""
    days = split_days(ticks)
    oos_trades, base_trades, picks = [], [], []
    for i in range(len(days) - 1):
        train, test = days[i], days[i + 1]
        scored = {name: replay(train, p)["stats"] for name, p in candidates.items()}
        eligible = {n: s for n, s in scored.items() if s["trades"] > 0}
        best = max(eligible, key=lambda n: (eligible[n]["net_usd"], n)) if eligible else baseline
        picks.append({"day": i, "train_best": best, "train_net": scored.get(best, {}).get("net_usd")})
        oos_trades += replay(test, candidates[best])["engine"].trades
        base_trades += replay(test, candidates[baseline])["engine"].trades
    start = candidates[baseline].get("start_cash", 150.0)
    span = max(len(days) - 1, 1)
    return {"picks": picks, "oos": stats(oos_trades, start, span), "baseline_oos": stats(base_trades, start, span), "days_tested": len(days) - 1}


def evaluate_promotion(oos: dict) -> tuple[bool, list[str]]:
    """A param set becomes a new PAPER strategy (orion_v2.json) only if out-of-sample PF > 1.3 with >= 50 trades and
    max drawdown < 15%. The owner approves any switch. Otherwise: no edge found, keep testing."""
    why = []
    pf = oos.get("profit_factor")
    if pf is None or not pf > 1.3:
        why.append(f"profit factor {'n/a' if pf is None else f'{pf:.2f}'} is not > 1.3")
    if oos.get("trades", 0) < 50:
        why.append(f"only {oos.get('trades', 0)} out-of-sample trades (need >= 50)")
    if not oos.get("max_drawdown_pct", 100.0) < 15.0:
        why.append(f"max drawdown {oos.get('max_drawdown_pct', 0):.1f}% is not < 15%")
    return (not why), why


def verdict_text(oos: dict) -> str:
    ok, why = evaluate_promotion(oos)
    if ok:
        return "PASSES the promotion rule: may run as a new PAPER strategy (orion_v2.json) beside the others. Needs the owner's ok."
    return "no edge found: " + "; ".join(why) + ". Keep testing. Nothing is promoted."


def fmt(x, nd=2):
    if x is None:
        return "n/a"
    if x == math.inf:
        return "inf"
    return f"{x:.{nd}f}"


def table(rows: list[dict], cols: list[str]) -> str:
    out = ["  ".join(f"{c:>14}" for c in cols)]
    for r in rows:
        out.append("  ".join(f"{fmt(r.get(c)) if not isinstance(r.get(c), str) else r.get(c):>14}" for c in cols))
    return "\n".join(out)


async def _load():
    from db import Database
    from recorder import load_ticks

    db = Database(config.DB_PATH)
    await db.start()
    try:
        return await load_ticks(db)
    finally:
        await db.stop()


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "run"
    ticks = asyncio.run(_load())
    if not ticks:
        print("no recorded ticks yet: run the desk with the trade feed or snapshots on, then replay")
        return 1
    params = oc.load_params()
    print(f"{len(ticks)} ticks, {len(split_days(ticks))} day(s)")
    if cmd == "sweep":
        print(table(latency_sweep(ticks, params), ["latency_s", "signals", "cancelled", "trades", "net_usd", "win_rate", "profit_factor", "fees_usd"]))
    elif cmd == "walk":
        import experiments

        res = walk_forward(ticks, experiments.candidates(params))
        print(experiments.report(res))
    else:
        r = replay(ticks, params)
        print({k: fmt(v) if not isinstance(v, str) else v for k, v in r["stats"].items()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
