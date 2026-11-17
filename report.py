"""Night report and weekly review (spec section 9). Everything comes from the database; coverage is the time the desk REALLY ran.

* Night report: red trades first, then green; every order with its checks; Scanner kills by reason and Analyst refusals by layer;
  fees paid and the extra cost Jupiter added; up to 6 frames; coverage; footer is the plain signup link only (links.SIGNUP_URL).
* Weekly review: per metric the median on green vs red trades vs skipped coins that later ran 2x, and AT MOST 3 setting suggestions with
  the numbers behind each. Suggestions are never applied: the owner approves each one in the app.
"""
from __future__ import annotations

import html
import json
import re
import statistics
import time
from pathlib import Path

import api_dash
import config
import links
import night_check
import settings as st

NIGHT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
METRICS = ["alive", "organic", "flush", "chase", "social_pts", "fresh_pct", "score", "curve_min", "insiders_pct", "holders_growth", "liq_mcap", "accel", "dip_depth_pct"]


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def money(v) -> str:
    if v is None:
        return "no data"
    r = round(v, 2)
    return ("−" if r < 0 else "+" if r > 0 else "") + f"${abs(r):,.2f}"


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


async def pick_night(db, night_id: int | None = None) -> dict | None:
    if night_id is not None:
        return await db.fetchone("SELECT * FROM nights WHERE id=?", (night_id,))
    row = await db.fetchone("SELECT * FROM nights WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 1")
    return row or await db.fetchone("SELECT * FROM nights ORDER BY id DESC LIMIT 1")


async def closed_trades(db, start: float, end: float) -> list[dict]:
    out = []
    for strat, table, why in (("prompt", "positions", "exit_reason"), ("orion_fast", "strat_positions", "exit_rule")):
        for r in await db.fetchall(
                f"SELECT mint, symbol, opened_at, closed_at, size_usd, entry_price, exit_price, pnl_usd, fees_usd, {why} AS rule, "
                f"{'meta' if table == 'positions' else 'data'} AS extra FROM {table} WHERE strategy=? AND closed_at BETWEEN ? AND ?", (strat, start, end)):
            try:
                extra = json.loads(r["extra"] or "{}")
            except ValueError:
                extra = {}
            out.append({**r, "strategy": strat, "jup_extra_pct": extra.get("jup_extra_pct") or 0.0,
                        "pnl_pct": 100.0 * r["pnl_usd"] / r["size_usd"] if r["pnl_usd"] is not None and r["size_usd"] else None})
    return out


def usage_text(hub) -> str:
    rpc = (getattr(hub, "objects", {}) or {}).get("rpc") if hub is not None else None
    if rpc is None:
        return "no data (the report was not built inside the running desk)"
    credits = rpc.credits_today()
    return f"RPC calls cost {credits} credits today ({rpc.label}). LLM spend: $0.00 (the Grok judge is off)."


async def build_night_report(db, hub=None, night_id: int | None = None, out_dir: Path | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    night = await pick_night(db, night_id)
    if night is None:
        start, end, nid, start_eq, end_eq = now - 8 * 3600, now, None, None, None
    else:
        start, end, nid = night["started_at"], night["ended_at"] or now, night["id"]
        start_eq, end_eq = night["start_cash"], night["end_cash"]
    date = time.strftime("%Y-%m-%d", time.localtime(end))
    trades = await closed_trades(db, start, end)
    red = sorted([t for t in trades if (t["pnl_usd"] or 0) <= 0], key=lambda t: t["pnl_usd"] or 0)
    green = sorted([t for t in trades if (t["pnl_usd"] or 0) > 0], key=lambda t: -(t["pnl_usd"] or 0))
    orders = await db.fetchall("SELECT ts, mint, symbol, side, usd, pct, bot, reason, status, quote_text, fee_text, screen_ok, api_ok, chain_ok, fill_usd, strategy "
                               "FROM orders WHERE ts BETWEEN ? AND ? ORDER BY id", (start, end))
    dec = await db.fetchall("SELECT bot, decision, reason FROM decisions WHERE ts BETWEEN ? AND ?", (start, end))
    scanner_kills: dict[str, int] = {}
    analyst_no: dict[str, int] = {}
    for d in dec:
        if d["bot"] == "scanner" and d["decision"] in ("kill", "drop"):
            scanner_kills[api_dash.bucket(d["reason"])] = scanner_kills.get(api_dash.bucket(d["reason"]), 0) + 1
        if d["bot"] == "analyst" and d["decision"] == "reject":
            analyst_no[api_dash.bucket(d["reason"])] = analyst_no.get(api_dash.bucket(d["reason"]), 0) + 1
    fees = sum(t["fees_usd"] or 0.0 for t in trades)
    jup_extra = sum((t["size_usd"] or 0) * (t["jup_extra_pct"] or 0) / 100.0 * 2 for t in trades)
    covered = await db.coverage_seconds(start, end)
    window = max(end - start, 1.0)
    health = await night_check.evaluate(db, nid, now) if night else None
    net = sum(t["pnl_usd"] or 0.0 for t in trades)
    summary = f"{len(trades)} trades, net {money(net)}, fees {money(fees).lstrip('+')}, the desk ran {100 * covered / window:.0f}% of the night"

    out_dir = Path(out_dir) if out_dir else config.ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = _embed_frames(start, end)

    def trade_rows(rows):
        if not rows:
            return '<tr><td colspan="8">none</td></tr>'
        return "".join(
            f"<tr><td>{esc(t['strategy'])}</td><td><a href=\"{esc(links.coin_url(t['mint']))}\">{esc(t['symbol'] or t['mint'][:6])}</a></td>"
            f"<td>{esc(time.strftime('%H:%M', time.localtime(t['opened_at'])))}</td><td>{esc(time.strftime('%H:%M', time.localtime(t['closed_at'])))}</td>"
            f"<td>{esc(money(t['size_usd']).lstrip('+'))}</td><td class=\"{'up' if (t['pnl_usd'] or 0) > 0 else 'down'}\">{esc(money(t['pnl_usd']))} ({esc('no data' if t['pnl_pct'] is None else f'{t['pnl_pct']:+.1f}%')})</td>"
            f"<td>{esc(t['rule'])}</td><td>{esc(money(t['fees_usd']).lstrip('+'))}</td></tr>" for t in rows)

    def order_rows():
        if not orders:
            return '<tr><td colspan="8">no orders</td></tr>'
        chk = lambda v: "n/a (paper)" if v is None else ("yes" if v else "NO")  # noqa: E731
        return "".join(
            f"<tr><td>{esc(time.strftime('%H:%M:%S', time.localtime(o['ts'])))}</td><td>{esc(o['strategy'])}</td><td>{esc(o['side'])} {esc(o['symbol'] or o['mint'][:6])}</td>"
            f"<td>{esc(o['usd'])}</td><td>{esc(o['quote_text'])} {esc(o['fee_text'])}</td><td>{esc(o['status'])}</td>"
            f"<td>screen {chk(o['screen_ok'])}, api {chk(o['api_ok'])}, chain {chk(o['chain_ok'])}</td><td>{esc(o['reason'])}</td></tr>" for o in orders)

    def health_html(h):
        if not h or h.get("night") is None:
            return "<p>no data</p>"
        lag, rs = h["lag"], h["restarts"]
        lag_t = "no data" if lag["status"] == "no data" else f"p95 {lag['p95_ms']} ms over {lag['minutes']} minutes (limit {lag['limit_ms']:.0f} ms, {lag['status']}); worst minute {lag['worst_minute_p95_ms']} ms"
        items = "".join(f"<li>{esc(time.strftime('%H:%M', time.localtime(r['ts'])))} {esc(r['kind'])} {esc(r['bot'] or '')} {esc(r['reason'])}</li>" for r in rs["listed"])
        verdict = "a full PAPER night" if h["full"] else "not a full night: " + "; ".join(h["reasons"])
        return (f"<p>Loop lag: {esc(lag_t)}.</p><p>Restarts: {rs['components']} component, {rs['wakes']} wake, {rs['desk_starts']} desk start.</p>"
                f"{('<ul>' + items + '</ul>') if items else ''}<p><b>{esc(verdict)}</b></p>")

    def bars(d):
        return "<ul>" + "".join(f"<li>{esc(k)}: {v}</li>" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) + "</ul>" if d else "<p>none</p>"

    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Night report {esc(date)}</title><style>body{{font:14px/1.5 system-ui,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#111}}
table{{border-collapse:collapse;width:100%;margin:8px 0 22px}}td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;font-size:13px}}
.up{{color:#15803d}}.down{{color:#dc2626}}h2{{margin-top:28px}}img{{width:31%;margin:1%;border:1px solid #ddd}}footer{{margin-top:36px;color:#666}}</style></head><body>
<h1>Night report {esc(date)}</h1><p><b>PAPER.</b> {esc(summary)}.</p>
<p>Coverage: the desk actually ran {covered / 3600:.1f} of {window / 3600:.1f} hours ({100 * covered / window:.0f}%). Hours the laptop slept or the desk was down are not counted.</p>
<p>Balance: start {esc(money(start_eq).lstrip('+'))}, end {esc(money(end_eq).lstrip('+') if end_eq is not None else 'night still open')}.</p>
<h2>Trades that lost ({len(red)})</h2><table><tr><th>strategy</th><th>coin</th><th>in</th><th>out</th><th>size</th><th>P&amp;L</th><th>rule</th><th>fees</th></tr>{trade_rows(red)}</table>
<h2>Trades that won ({len(green)})</h2><table><tr><th>strategy</th><th>coin</th><th>in</th><th>out</th><th>size</th><th>P&amp;L</th><th>rule</th><th>fees</th></tr>{trade_rows(green)}</table>
<h2>Orders and their checks</h2><table><tr><th>time</th><th>strategy</th><th>order</th><th>$</th><th>quote</th><th>status</th><th>checks</th><th>why</th></tr>{order_rows()}</table>
<h2>Scanner kills by reason</h2>{bars(scanner_kills)}<h2>Analyst refusals by layer</h2>{bars(analyst_no)}
<h2>Health</h2>{health_html(health)}
<h2>Costs</h2><p>Fees paid: {esc(money(fees).lstrip('+'))}. Extra cost added where the real Jupiter route was worse than the fill model: about {esc(money(jup_extra).lstrip('+'))} across both legs.</p>
<h2>Usage</h2><p>{esc(usage_text(hub))}</p>
<h2>Frames</h2>{''.join(f'<img src="{esc(f)}" alt="desk frame">' for f in frames) or '<p>no frames were recorded</p>'}
<footer>{esc(links.SIGNUP_URL)}</footer></body></html>"""
    path = out_dir / f"night_{date}.html"
    path.write_text(body, encoding="utf-8")
    return {"date": date, "path": str(path), "summary": summary, "night_id": nid, "coverage_pct": 100 * covered / window, "trades": len(trades)}


def _embed_frames(start: float, end: float, n: int = 6) -> list[str]:
    """Up to 6 evenly spaced frames from the night, embedded in the page itself (one self-contained file, no extra URLs behind the token)."""
    import base64

    src = config.DATA_DIR / "frames"
    if not src.exists():
        return []
    files = [f for f in sorted(src.glob("*.jpg")) if start <= _frame_ts(f) <= end]
    if not files:
        return []
    picks = [files[int(i * (len(files) - 1) / max(1, n - 1))] for i in range(min(n, len(files)))]
    return ["data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode() for f in dict.fromkeys(picks)]


def _frame_ts(p: Path) -> float:
    try:
        return float(p.stem)
    except ValueError:
        return 0.0


# ---- weekly review ---------------------------------------------------------------------------------------------------------------------
async def trade_metrics(db, mint: str, at: float) -> dict:
    """The metrics the Brain and Analyst had at the moment of a decision (latest rows before `at`)."""
    sc = await db.fetchone('SELECT alive, organic, flush, chase, social_pts, fresh_pct, score FROM scores WHERE mint=? AND ts<=? ORDER BY id DESC LIMIT 1', (mint, at))
    scan = await db.fetchone("SELECT liq_mcap, accel, holders_growth FROM scans WHERE mint=? AND ts<=? ORDER BY id DESC LIMIT 1", (mint, at))
    coin = await db.fetchone("SELECT curve_min FROM coins WHERE mint=?", (mint,))
    l7 = await db.fetchone("SELECT value_text FROM audits WHERE mint=? AND layer=7 ORDER BY id DESC LIMIT 1", (mint,))
    dip = await db.fetchone("SELECT reason FROM decisions WHERE mint=? AND bot='executor' AND decision='buy' ORDER BY id DESC LIMIT 1", (mint,))
    m = {k: None for k in METRICS}
    m.update(sc or {})
    m.update(scan or {})
    m["curve_min"] = (coin or {}).get("curve_min")
    ins = re.search(r"insiders\s+(\d+(?:\.\d+)?)%", (l7 or {}).get("value_text") or "")
    m["insiders_pct"] = float(ins.group(1)) if ins else None
    dd = re.search(r"dip\s*[-−]\s*(\d+(?:\.\d+)?)%", (dip or {}).get("reason") or "")
    m["dip_depth_pct"] = float(dd.group(1)) if dd else None
    return m


# metric -> (setting it would move, True when a higher value is BETTER (so the setting is a minimum), label)
SUGGEST = {
    "score": ("pass_score", True), "fresh_pct": ("fresh_pct_max", False), "curve_min": ("sniped_curve_min", True),
    "holders_growth": ("holders_growth_min_pct", True), "insiders_pct": ("insiders_max_pct", False), "dip_depth_pct": ("dip_pct", True),
}


async def weekly_review(db, settings=None, now: float | None = None, days: int = 7) -> dict:
    now = time.time() if now is None else now
    settings = settings or st.Settings()
    since = now - days * 86400
    green, red = [], []
    for t in await closed_trades(db, since, now):
        if t["strategy"] != "prompt":
            continue
        m = await trade_metrics(db, t["mint"], t["opened_at"])
        (green if (t["pnl_usd"] or 0) > 0 else red).append(m)
    ran2x = []
    for r in await db.fetchall("SELECT DISTINCT mint, MIN(ts) ts FROM decisions WHERE decision IN ('kill','reject','skip','drop') AND ts>=? AND mint IS NOT NULL GROUP BY mint", (since,)):
        p0 = await db.fetchone("SELECT price FROM prices WHERE mint=? AND ts>=? ORDER BY ts LIMIT 1", (r["mint"], r["ts"]))
        pm = await db.fetchone("SELECT MAX(price) p FROM prices WHERE mint=? AND ts>=?", (r["mint"], r["ts"]))
        if p0 and pm and p0["price"] and pm["p"] and pm["p"] >= 2 * p0["price"]:
            ran2x.append(await trade_metrics(db, r["mint"], r["ts"]))
    table = {k: {"green": median([g[k] for g in green]), "red": median([x[k] for x in red]), "ran2x": median([x[k] for x in ran2x])} for k in METRICS}
    notes, suggestions = [], []
    if len(green) < 5 or len(red) < 5:
        notes.append(f"Too few trades to suggest anything: {len(green)} green and {len(red)} red (need at least 5 of each).")
    else:
        cand = []
        for metric, (setting, higher_better) in SUGGEST.items():
            g, r = table[metric]["green"], table[metric]["red"]
            if g is None or r is None or max(abs(g), abs(r)) == 0:
                continue
            gap = abs(g - r) / max(abs(g), abs(r))
            if gap < 0.2:
                continue
            if (g > r) != higher_better:
                continue  # the greens are on the wrong side for this setting to help
            cur = settings[setting]
            mid = round((g + r) / 2, 1)
            if (higher_better and mid <= cur) or (not higher_better and mid >= cur):
                continue  # the setting is already at least that strict
            cand.append((gap, {"setting": setting, "current": cur, "proposed": mid, "metric": metric, "green_median": g, "red_median": r,
                               "why": f"{metric}: median {g:g} on {len(green)} winning trades vs {r:g} on {len(red)} losing ones"}))
        suggestions = [c for _, c in sorted(cand, key=lambda x: -x[0])[:3]]  # at most 3, never applied
        if not suggestions:
            notes.append("No setting change is supported by this week's numbers.")
    orion = "not enough recorded data for a walk-forward table (needs at least 2 days of ticks)"
    try:
        import experiments
        import orion_core
        import replay
        from recorder import load_ticks

        ticks = await load_ticks(db, since=since)
        if len(replay.split_days(ticks)) >= 2:
            orion = experiments.report(replay.walk_forward(ticks, experiments.candidates(orion_core.load_params())))
    except Exception as e:  # noqa: BLE001
        orion = f"walk-forward could not run: {type(e).__name__}"
    return {"trades_green": len(green), "trades_red": len(red), "skipped_ran_2x": len(ran2x), "metrics": table, "suggestions": suggestions,
            "notes": notes, "orion_walk_forward": orion, "applied": []}


def write_weekly(res: dict, date: str, out_dir: Path | None = None) -> str:
    out_dir = Path(out_dir) if out_dir else config.ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"weekly_{date}.html"
    p.write_text(render_weekly(res, date), encoding="utf-8")
    return str(p)


def render_weekly(res: dict, date: str) -> str:
    rows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v['green'])}</td><td>{esc(v['red'])}</td><td>{esc(v['ran2x'])}</td></tr>" for k, v in res["metrics"].items())
    sug = "".join(f"<li>Change <b>{esc(s['setting'])}</b> from {esc(s['current'])} to {esc(s['proposed'])}. {esc(s['why'])}.</li>" for s in res["suggestions"]) or "<li>none</li>"
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Weekly review {esc(date)}</title></head><body style=\"font:14px system-ui;max-width:900px;margin:24px auto\">"
            f"<h1>Weekly review {esc(date)}</h1><p>PAPER. {res['trades_green']} winning and {res['trades_red']} losing trades; {res['skipped_ran_2x']} skipped coins later ran 2x.</p>"
            f"<table border=\"0\" cellpadding=\"6\"><tr><th>metric (median)</th><th>green</th><th>red</th><th>skipped, ran 2x</th></tr>{rows}</table>"
            f"<h2>Suggestions (at most 3, none applied: you approve each in the app)</h2><ul>{sug}</ul><p>{esc(' '.join(res['notes']))}</p>"
            f"<h2>orion_fast, out of sample</h2><pre>{esc(res['orion_walk_forward'])}</pre><footer>{esc(links.SIGNUP_URL)}</footer></body></html>")
