"""Dashboard endpoints (spec section 7). Mounted on the token-guarded /api router by api.create_app.

Everything returned comes from the database or live in-process state: a missing value is null, never invented.
FOMO links are built only by links.coin_url (hard rule 8), so the page never constructs one itself.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import config
import gates as gates_mod
import links
import settings as st
import stats

AGENTS = ["scanner", "analyst", "brain", "executor", "risk", "orion"]
MINT_RE = re.compile(r"^[A-Za-z0-9]{6,64}$")
POSITIVE = ("pass", "buy", "size")
NEGATIVE = ("kill", "reject", "skip", "drop")
TICK_S = 1.0  # with nothing moving, the stream still sends the heartbeats this often
TICK_LOOK_S = 0.25  # how often it looks for a moved number; a change is pushed at once, so the page is at most this far behind
RANGES = {"night": None, "live": 900, "1h": 3600, "4h": 4 * 3600, "1d": 86400, "1w": 7 * 86400, "all": 0}


def session_end(started_at: float, lock_hm: str) -> float | None:
    """When the running session is due to roll over: the first lock_time (local HH:MM) after it started."""
    import datetime as dt

    try:
        h, m = (int(x) for x in str(lock_hm).split(":"))
        start = dt.datetime.fromtimestamp(started_at)
        end = start.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, OverflowError, OSError):
        return None
    if end <= start:
        end += dt.timedelta(days=1)
    return end.timestamp()
STREAM_TOPICS = {"decision", "audit_layer", "trade_open", "trade_close", "trade_partial", "room_add", "scan_pass", "audit_pass",
                 "order_request", "push", "restart", "wake", "session_roll", "new_token", "migration", "fomo_logged_out", "fomo_resumed"}
_BUCKETS = [
    (r"^liq\b", "liquidity"), (r"^top 10", "top 10"), (r"^1h \+", "pumped 1h"), (r"^copycat", "copycat"), (r"^FOMO", "FOMO warning"),
    (r"^holders leaving", "holders leaving"), (r"^layer (\d) ([A-Za-z0-9 ]+):", None), (r"^score ", "score too low"),
    (r"^under \$", "under min ticket"), (r"^night loss", "night loss"), (r"^max trades", "max trades"), (r"^no dip", "no dip"),
    (r"^crash", "crash"), (r"^liquidity down", "liquidity pulled"), (r"^ran before", "ran before click"), (r"^dev sold", "dev sold"),
    (r"^Jupiter", "Jupiter route"), (r"^free cash", "no free cash"), (r"^no data", "no data"), (r"^not passed", "never passed"),
    (r"^already", "already held/rejected"), (r"^entries paused", "feed too slow"), (r"^daily loss", "daily loss stop"),
    (r"^honeypot", "honeypot check"), (r"^unsafe", "unsafe"), (r"^age ", "age window"), (r"^cooldown", "cooldown"),
]


def bucket(reason: str | None) -> str:
    r = reason or ""
    for pat, name in _BUCKETS:
        m = re.search(pat, r)
        if m:
            return name or f"layer {m.group(1)} {m.group(2).strip().lower()}"
    return " ".join(r.split()[:3]) or "other"


class SettingBody(BaseModel):
    key: str
    value: object


class SellBody(BaseModel):
    pct: int
    confirm: bool = False


class ModeBody(BaseModel):
    mode: str
    confirm: str = ""


def _clean_mint(mint: str) -> str:
    if not MINT_RE.match(mint):
        raise HTTPException(400, "bad mint")
    return mint


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"


def shape_event(ev: dict) -> dict | None:
    """What the browser may see of a bus event (trimmed, no secrets, links only from links.py)."""
    topic, d = ev["topic"], ev["data"]
    if topic not in STREAM_TOPICS:
        return None
    keep = {
        "decision": ("ts", "bot", "mint", "symbol", "decision", "reason", "value", "strategy"),
        "audit_layer": ("mint", "layer", "name", "value", "pass"),
        "trade_open": ("mint", "symbol", "usd", "price", "strategy"),
        "trade_close": ("mint", "symbol", "pnl_usd", "pnl_pct", "reason", "strategy"),
        "trade_partial": ("mint", "symbol", "proceeds", "reason", "strategy"),
        "room_add": ("mint", "symbol", "room"),
        "scan_pass": ("mint", "symbol", "name"),
        "audit_pass": ("mint", "symbol", "score"),
        "order_request": ("mint", "symbol", "size_usd", "score"),
        "push": ("text", "link", "strategy"),
        "restart": ("bot", "reason"),
        "new_token": ("mint", "symbol", "name", "late"),
        "migration": ("mint",),
    }.get(topic)
    out = {k: d.get(k) for k in keep} if keep else {}
    return {"seq": ev["seq"], "ts": ev["ts"], "topic": topic, "data": out}


def mount(api: APIRouter, db, hub, supervisor=None) -> None:
    objects = hub.objects

    async def settings_obj() -> st.Settings:
        return objects.get("settings") or await st.Settings.load(db)

    async def has_open_position(mint: str) -> bool:
        marks = objects["marks"]() if "marks" in objects else {}
        if mint in marks:
            return True
        for table in ("positions", "strat_positions"):
            if await db.fetchone(f"SELECT id FROM {table} WHERE mint=? AND closed_at IS NULL LIMIT 1", (mint,)):
                return True
        return False

    # ---- live ------------------------------------------------------------------------------------------------------------
    @api.get("/live")
    async def live():
        """One payload for the whole dashboard: both strategies side by side, agents, feed, speed, radar, kills, night."""
        now = time.time()
        s = await settings_obj()
        marks = objects["marks"]() if "marks" in objects else {}
        cards = await stats.strategy_cards(db, float(s["paper_start_cash"]), now, marks)
        decisions = await db.fetchall("SELECT ts, bot, mint, symbol, decision, reason, value, strategy FROM decisions ORDER BY id DESC LIMIT 60")
        recent = await db.fetchall("SELECT ts, bot, decision, mint, symbol, reason FROM decisions WHERE ts > ? ORDER BY id DESC LIMIT 900", (now - 3600,))
        ages = hub.heartbeats.ages()
        per_hour = {r["bot"]: r["n"] for r in await db.fetchall("SELECT bot, COUNT(*) n FROM decisions WHERE ts > ? GROUP BY bot", (now - 3600,))}
        agents = []
        for name in AGENTS:
            rows = [r for r in recent if r["bot"] == name]
            last = rows[0] if rows else None
            bins = [0] * 10  # decisions per minute, oldest -> newest
            for r in rows:
                i = int((now - r["ts"]) // 60)
                if 0 <= i < 10:
                    bins[9 - i] += 1
            pos = sum(1 for r in rows[:50] if r["decision"] in POSITIVE)
            neg = sum(1 for r in rows[:50] if r["decision"] in NEGATIVE)
            agents.append({"name": name, "heartbeat_age_s": None if name not in ages else round(ages[name], 1),
                           "running": name in ages and ages[name] < 95, "paused": name in hub.paused, "last": last,
                           "rate": bins, "per_hour": per_hour.get(name, 0), "pass_rate": (100.0 * pos / (pos + neg)) if pos + neg else None})
        radar = await db.fetchall(
            "SELECT ts, bot, mint, symbol, decision, reason FROM decisions WHERE bot IN ('scanner','analyst') "
            "AND decision IN ('pass','kill','reject','drop') AND ts > ? ORDER BY id DESC LIMIT 80", (now - 5400,))
        killed = await db.fetchall("SELECT bot, reason FROM decisions WHERE decision IN ('kill','reject','skip','drop') AND ts > ?", (now - 86400,))
        counts: dict[tuple, int] = {}
        for r in killed:
            k = (r["bot"], bucket(r["reason"]))
            counts[k] = counts.get(k, 0) + 1
        kills = sorted(({"bot": b, "bucket": bk, "count": c} for (b, bk), c in counts.items()), key=lambda x: -x["count"])[:14]
        last_audit = await db.fetchone("SELECT a.mint, (SELECT symbol FROM coins c WHERE c.mint = a.mint) AS symbol FROM audits a ORDER BY a.id DESC LIMIT 1")
        night = await db.fetchone("SELECT id, started_at, start_cash, trades FROM nights ORDER BY id DESC LIMIT 1")
        recap = None
        if night:
            since = night["started_at"]
            by = {r["decision"]: r["n"] for r in await db.fetchall(
                "SELECT decision, COUNT(*) n FROM decisions WHERE ts >= ? GROUP BY decision", (since,))}
            recap = {"started_at": since, "start_equity": night["start_cash"], "trades": night["trades"],
                     "passes": by.get("pass", 0), "kills": by.get("kill", 0) + by.get("reject", 0), "buys": by.get("buy", 0),
                     "sells": by.get("sell", 0)}
        # the coin tape: the newest scan of each recently scanned coin, with the 1 h change the scanner read (never computed here)
        ticker = await db.fetchall(
            "SELECT s.mint, s.ts, s.h1_change_pct, s.verdict, COALESCE((SELECT symbol FROM coins c WHERE c.mint = s.mint), "
            "(SELECT symbol FROM decisions d WHERE d.mint = s.mint AND d.symbol IS NOT NULL ORDER BY d.id DESC LIMIT 1)) AS symbol "
            "FROM scans s WHERE s.id IN (SELECT MAX(id) FROM scans WHERE ts > ? GROUP BY mint) ORDER BY s.ts DESC LIMIT 16", (now - 3 * 3600,))
        # change over the last hour: today's equity minus the last reading that is at least an hour old (no such reading = no data)
        delta_1h, have = 0.0, False
        for c, sql in zip(cards, ("SELECT equity_usd e FROM balance WHERE strategy='prompt' AND ts <= ? ORDER BY ts DESC LIMIT 1",
                                  "SELECT equity_usd e FROM strat_balance WHERE strategy='orion_fast' AND ts <= ? ORDER BY ts DESC LIMIT 1")):
            old = await db.fetchone(sql, (now - 3600,))
            if c["balance"] is not None and old and old["e"] is not None:
                delta_1h += c["balance"] - old["e"]
                have = True
        if recap:
            recap["ends_at"] = session_end(recap["started_at"], s["lock_time"])
        return {
            "mode": await db.get_setting("mode", config.DEFAULT_MODE), "ts": now,
            "ticker": ticker, "delta_1h": delta_1h if have else None,
            "strategies": cards, "agents": agents, "decisions": decisions, "radar": radar, "kills": kills, "night": recap,
            "feed": hub.status["feed"]() if "feed" in hub.status else None,
            "stages": hub.metrics.snapshot(), "loop_lag_ms": hub.lag.snapshot(),
            "bots": {k: round(v, 1) for k, v in ages.items()},
            "engines": {k: fn() for k, fn in hub.status.items() if k in ("prompt", "orion_fast")},
            "restarts": list(getattr(supervisor, "restarts", []))[-10:],
            "lane": hub.status["lane"]() if "lane" in hub.status else None,
            "audit": last_audit,
        }

    def live_tick() -> dict:
        """The live part of the dashboard, straight from memory: each strategy's equity, every open position's last real price,
        the agents' heartbeats and the feed. A value the desk does not hold is None (the page then keeps its last good number)."""
        ages = hub.heartbeats.ages()
        marks = objects["marks"]() if "marks" in objects else {}
        feed = hub.status["feed"]() if "feed" in hub.status else None
        return {"equity": hub.status["equity"]() if "equity" in hub.status else None,
                "marks": {m: [v.get("price"), v.get("tokens"), v.get("realized")] for m, v in marks.items()},
                "ages": {k: round(ages[k], 1) for k in AGENTS if k in ages}, "paused": [k for k in AGENTS if k in hub.paused],
                "feed": feed["badge"] if feed else None}

    # ---- stream (server-sent events; the page reads it with fetch + the token header) -------------------------------------------
    @api.get("/stream")
    async def stream(request: Request):
        async def gen():
            with hub.bus.subscribe() as q:
                yield sse({"seq": 0, "ts": time.time(), "topic": "hello", "data": {}})
                window_start, window_n = 0.0, 0
                next_look, last_sent, last_key = 0.0, 0.0, None
                while True:
                    if await request.is_disconnected():
                        return
                    now_m = time.monotonic()
                    if now_m >= next_look:  # four looks a second at what the desk holds in memory (no database read)
                        next_look = now_m + TICK_LOOK_S
                        tick = live_tick()
                        key = (tick["equity"], tick["marks"], tick["feed"], tick["paused"])
                        if key != last_key or now_m - last_sent >= TICK_S:  # a number moved: sent at once. Nothing moved: once a second, for the heartbeats
                            last_key, last_sent = key, now_m
                            yield sse({"seq": 0, "ts": time.time(), "topic": "tick", "data": tick})
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=max(0.02, next_look - time.monotonic()))
                    except asyncio.TimeoutError:
                        continue
                    out = shape_event(ev)
                    if out is None:
                        continue
                    if out["topic"] == "new_token":  # sampled: at most 4 a second reach the browser
                        t = time.monotonic()
                        if t - window_start > 1.0:
                            window_start, window_n = t, 0
                        window_n += 1
                        if window_n > 4:
                            continue
                    yield sse(out)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---- chart ---------------------------------------------------------------------------------------------------------------------
    @api.get("/chart")
    async def chart(range: str = "1d"):
        if range not in RANGES:
            raise HTTPException(400, f"range must be one of {sorted(RANGES)}")
        now, span = time.time(), RANGES[range]
        s = await settings_obj()
        window = None
        if range == "night":  # the running session, drawn across its whole planned length (start -> lock time)
            night = await db.fetchone("SELECT started_at FROM nights ORDER BY id DESC LIMIT 1")
            cut = night["started_at"] if night else now - 86400
            end = session_end(cut, s["lock_time"]) if night else None
            window = [cut, max(end, now)] if end else None
        else:
            cut = now - span if span else 0.0
        series = []
        for name, sql, start in (
            ("prompt", "SELECT ts, equity_usd e FROM balance WHERE strategy='prompt' AND ts >= ? ORDER BY ts", float(s["paper_start_cash"])),
            ("orion_fast", "SELECT ts, equity_usd e FROM strat_balance WHERE strategy='orion_fast' AND ts >= ? ORDER BY ts", 150.0),
        ):
            rows = [[r["ts"], r["e"]] for r in await db.fetchall(sql, (cut,)) if r["e"] is not None]
            if len(rows) > 400:
                step = len(rows) / 400.0
                rows = [rows[int(i * step)] for i in range(400)] + [rows[-1]]
            series.append({"name": name, "start_cash": start, "points": rows})
        markers = []
        for strat, table in (("prompt", "positions"), ("orion_fast", "strat_positions")):
            for r in await db.fetchall(f"SELECT symbol, mint, closed_at, pnl_usd FROM {table} WHERE strategy=? AND closed_at >= ? AND pnl_usd IS NOT NULL ORDER BY closed_at", (strat, cut)):
                markers.append({"strategy": strat, "ts": r["closed_at"], "pnl": r["pnl_usd"], "symbol": r["symbol"], "mint": r["mint"]})
        return {"range": range, "series": series, "markers": markers[-80:], "now": now, "window": window}

    # ---- activity / orders ---------------------------------------------------------------------------------------------------------
    @api.get("/activity")
    async def activity(limit: int = 60):
        limit = max(1, min(limit, 200))
        closed = []
        for strat, table, why_col in (("prompt", "positions", "exit_reason"), ("orion_fast", "strat_positions", "exit_rule")):
            for r in await db.fetchall(
                    f"SELECT mint, symbol, opened_at, closed_at, size_usd, pnl_usd, fees_usd, {why_col} AS exit_reason FROM {table} "
                    "WHERE strategy=? AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?", (strat, limit)):
                closed.append({**r, "strategy": strat, "pnl_pct": 100.0 * r["pnl_usd"] / r["size_usd"] if r["pnl_usd"] is not None and r["size_usd"] else None})
        closed.sort(key=lambda x: -(x["closed_at"] or 0))
        rows = await db.fetchall("SELECT ts, kind, bot, mint, text FROM activity ORDER BY id DESC LIMIT ?", (limit,))
        return {"closed": closed[:limit], "activity": rows}

    @api.get("/orders")
    async def orders(limit: int = 50):
        limit = max(1, min(limit, 200))
        return {"orders": await db.fetchall(
            "SELECT id, ts, mint, symbol, side, usd, pct, bot, reason, status, quote_text, fee_text, fill_usd, error, strategy "
            "FROM orders ORDER BY id DESC LIMIT ?", (limit,))}

    # ---- coin / audit / intel --------------------------------------------------------------------------------------------------------
    @api.get("/coin/{mint}")
    async def coin(mint: str):
        mint = _clean_mint(mint)
        row = await db.fetchone("SELECT mint, name, symbol, pool, created_at, migrated_at, curve_min, creator FROM coins WHERE mint=?", (mint,))
        if row is None:
            row = await db.fetchone("SELECT mint, name, symbol, NULL pool, ts created_at, NULL migrated_at, NULL curve_min, creator FROM new_tokens WHERE mint=?", (mint,))
        scan = await db.fetchone("SELECT * FROM scans WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        pos = None
        for strat, table in (("prompt", "positions"), ("orion_fast", "strat_positions")):
            p = await db.fetchone(f"SELECT * FROM {table} WHERE mint=? AND closed_at IS NULL LIMIT 1", (mint,))
            if p:
                pos = {"strategy": strat, **{k: p.get(k) for k in ("symbol", "entry_price", "size_usd", "tokens", "opened_at", "stop_price") if k in p}}
                break
        if row is None and scan is None and pos is None:
            raise HTTPException(404, "unknown coin")
        buckets: dict[int, list] = {}
        for r in await db.fetchall("SELECT ts, price FROM prices WHERE mint=? AND ts > ? ORDER BY ts", (mint, time.time() - 6 * 3600)):
            if r["price"] is None:
                continue
            b = buckets.setdefault(int(r["ts"] // 60), [r["price"], r["price"], r["price"], r["price"]])
            b[1], b[2], b[3] = max(b[1], r["price"]), min(b[2], r["price"]), r["price"]
        candles = [[k * 60, *v] for k, v in sorted(buckets.items())][-180:]
        return {"coin": row, "scan": scan, "position": pos, "candles": candles, "fomo_url": links.coin_url(mint)}

    @api.get("/audit/{mint}")
    async def audit(mint: str):
        mint = _clean_mint(mint)
        rows = await db.fetchall('SELECT layer, name, value_text, "pass" p, source, ts FROM audits WHERE mint=? ORDER BY id DESC LIMIT 32', (mint,))
        latest: dict[int, dict] = {}
        for r in rows:
            latest.setdefault(r["layer"], r)
        score = await db.fetchone('SELECT alive, organic, flush, chase, social, social_pts, fresh_pct, score, "pass" p, reason, ts FROM scores WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,))
        return {"mint": mint, "layers": [latest[k] for k in sorted(latest)], "score": score}

    @api.get("/intel/{mint}")
    async def intel(mint: str):
        mint = _clean_mint(mint)
        p = await db.fetchone("SELECT data FROM strat_positions WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        council = None
        if p and p["data"]:
            try:
                council = json.loads(p["data"]).get("council")
            except ValueError:
                council = None
        score = await db.fetchone('SELECT score, social_pts, reason FROM scores WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,))
        return {"mint": mint, "council": council, "score": score, "gmgn": None, "xai_judge": None}

    # ---- bots ------------------------------------------------------------------------------------------------------------------------
    @api.get("/bots/{name}/decisions")
    async def bot_decisions(name: str, limit: int = 60):
        if name not in AGENTS:
            raise HTTPException(404, "unknown bot")
        rows = await db.fetchall("SELECT ts, bot, mint, symbol, decision, reason, value, strategy FROM decisions WHERE bot=? ORDER BY id DESC LIMIT ?",
                                 (name, max(1, min(limit, 200))))
        return {"bot": name, "decisions": rows}

    @api.post("/bots/{name}/pause")
    async def pause(name: str):
        if name not in AGENTS:
            raise HTTPException(404, "unknown bot")
        if name == "risk":
            can = objects["risk_pausable"]() if "risk_pausable" in objects else True
            if not can:
                raise HTTPException(409, "Risk cannot be paused while a position is open")
        hub.paused.add(name)
        return {"bot": name, "paused": True}

    @api.post("/bots/{name}/resume")
    async def resume(name: str):
        if name not in AGENTS:
            raise HTTPException(404, "unknown bot")
        hub.paused.discard(name)
        return {"bot": name, "paused": False}

    # ---- sell now (PAPER: simulated by the Executor / orion engine) -----------------------------------------------------------------------
    @api.post("/sell/{mint}")
    async def sell(mint: str, body: SellBody):
        mint = _clean_mint(mint)
        if body.pct not in (25, 50, 100):
            raise HTTPException(400, "pct must be 25, 50 or 100")
        if not body.confirm:
            raise HTTPException(400, "confirm must be true")
        if not await has_open_position(mint):
            raise HTTPException(404, "no open position in that coin")
        hub.bus.publish("manual_sell", {"mint": mint, "pct": body.pct})
        return {"queued": True, "mint": mint, "pct": body.pct, "mode": "PAPER", "note": "simulated at the last real price, minus fees"}

    # ---- screen frame ----------------------------------------------------------------------------------------------------------------------
    @api.get("/screen.jpg")
    async def screen():
        # A JPEG when there is a frame, otherwise a small JSON status (200): a 404/423 would be logged as a console error.
        if (config.DATA_DIR / "screen.paused").exists():
            return JSONResponse({"state": "paused", "message": "Paused · private"})
        p = config.DATA_DIR / "screen.jpg"
        if not p.exists():
            return JSONResponse({"state": "waiting", "message": "Waiting for the desk Chrome"})
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---- reports (behind the token like everything else) ----------------------------------------------------------------------------------
    @api.get("/reports")
    async def list_reports():
        d = config.ROOT / "reports"
        return {"reports": sorted((p.stem.split("_", 1)[1] for p in d.glob("night_*.html")), reverse=True) if d.exists() else []}

    @api.get("/report/{date}")
    async def get_report(date: str):
        import report

        if not report.NIGHT_RE.match(date):
            raise HTTPException(400, "date must look like 2026-11-18")
        p = config.ROOT / "reports" / f"night_{date}.html"
        if not p.exists():
            raise HTTPException(404, "no report for that night")
        from fastapi.responses import HTMLResponse

        return HTMLResponse(p.read_text(encoding="utf-8"))

    @api.post("/report/generate")
    async def make_report():
        import report

        gen = objects.get("report_fn")
        res = await (gen() if gen else report.build_night_report(db, hub))
        return {k: res[k] for k in ("date", "summary", "coverage_pct", "trades")}

    # ---- account / settings / mode ---------------------------------------------------------------------------------------------------------
    @api.get("/account")
    async def account():
        handle = await db.get_setting("fomo_handle", None)
        addr = await db.get_setting("fomo_address", None)
        nights = await db.fetchall("SELECT id, started_at, ended_at, start_cash, end_cash, trades FROM nights ORDER BY id DESC LIMIT 7")
        s = await settings_obj()
        return {"handle": handle, "address": f"{addr[:4]}...{addr[-4:]}" if addr and len(addr) > 10 else None,
                "mode": await db.get_setting("mode", config.DEFAULT_MODE), "nights": nights,
                "strategies": await stats.strategy_cards(db, float(s["paper_start_cash"]), time.time(), objects["marks"]() if "marks" in objects else {})}

    @api.get("/settings")
    async def get_settings():
        s = await settings_obj()
        return {"settings": [{"key": x.key, "label": x.label, "group": x.group, "value": s[x.key], "default": x.default,
                              "lo": x.lo, "hi": x.hi, "kind": "bool" if isinstance(x.default, bool) else "time" if isinstance(x.default, str) else "number"}
                             for x in st.ALL]}

    @api.post("/settings")
    async def post_settings(body: SettingBody):
        s = await settings_obj()
        try:
            old, new = await s.set(db, body.key, body.value, st.OWNER)  # the token holder is the owner
        except st.SettingError as e:
            raise HTTPException(400, str(e)) from None
        return {"key": body.key, "old": old, "new": new}

    async def gates() -> list[dict]:
        return await gates_mod.live_gates(db)

    @api.get("/mode")
    async def get_mode():
        return {"mode": await db.get_setting("mode", config.DEFAULT_MODE), "gates": await gates()}

    @api.post("/mode")
    async def post_mode(body: ModeBody):
        if body.mode == "PAPER":
            return {"mode": "PAPER"}
        if body.mode != "LIVE" or body.confirm != "LIVE":
            raise HTTPException(400, 'to ask for LIVE send {"mode":"LIVE","confirm":"LIVE"}')
        g = await gates()
        if not all(x["met"] for x in g):
            return JSONResponse({"error": "LIVE is locked: gates not met", "gates": g}, status_code=409)
        # Even with every gate met this build cannot go live: LIVE clicking is not implemented (hard rule 1).
        return JSONResponse({"error": "LIVE is not implemented in this build", "gates": g}, status_code=501)
