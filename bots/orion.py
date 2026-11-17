"""orion_fast, the live side: feeds the pure engine and persists what it does. PAPER only (orion_core.MODE).

This bot never clicks anything on FOMO web: its fills are simulated on real data. It
  * turns feed events into ticks, RECORDS each tick before the engine sees it (so any hour replays exactly),
  * prefetches the hard safety card for coins that look interesting (authorities, RugCheck danger, Jupiter round trip),
  * pauses entries, with the reason, whenever the feed is too slow to trade on (GeckoTerminal, or down),
  * hot-reloads strategies/orion_fast.json, keeps its own $150 balance, and writes strat_* tables.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import fees
import orion_core as oc
from bots.common import Deps, decide, push, subscribe_queue
from recorder import TickRecorder

log = logging.getLogger("desk.orion")
STRATEGY = "orion_fast"
FAST_BADGES = ("PumpPortal live", "DexScreener snapshots")  # anything else is too slow to trade on


class Orion:
    def __init__(self, deps: Deps, fallbacks=None, feed=None, params_path: Path | None = None):
        self.d, self.fb, self.feed = deps, fallbacks, feed
        self.params_path = Path(params_path) if params_path else oc.PARAMS_FILE
        self.params = oc.load_params(self.params_path)
        self._mtime = self.params_path.stat().st_mtime
        self.engine = oc.Engine(self.params)
        self.rec = TickRecorder(deps.db)
        self.q_new = subscribe_queue(deps.hub, "new_token")
        self.q_trade = subscribe_queue(deps.hub, "trade")
        self.q_snap = subscribe_queue(deps.hub, "snapshot")
        self.names: dict[str, dict] = {}
        self.rows: dict[str, int] = {}  # mint -> strat_positions id
        self.watch: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._retry_at: dict[str, float] = {}
        self._dev_checked: dict[str, float] = {}
        self._skip_logged: dict[tuple, float] = {}
        self._last_mark_write = 0.0
        self.ticks = 0
        self.sem = asyncio.Semaphore(3)
        deps.hub.status[STRATEGY] = self.status

    # ---- status / pause ------------------------------------------------------------------------------------------------------
    def status(self) -> dict:
        e = self.engine
        return {"mode": oc.MODE, "cash": round(e.cash, 2), "equity": round(e.equity(), 2), "open": len(e.positions),
                "pending": len(e.pending), "trades": len(e.trades), "halted": e.halted, "paused": e.pause_reason,
                "ticks": self.ticks, "watching": len(self.watch), "trade_stream": bool(self.feed and self.feed.api_key)}

    def update_pause(self) -> None:
        badge = self.fb.badge() if self.fb is not None else "feed down"
        self.engine.pause_reason = None if badge in FAST_BADGES else f"{badge}: too slow to trade on"

    # ---- start: restore the bank, close what a restart left open -----------------------------------------------------------------------
    async def start(self) -> None:
        d = self.d
        row = await d.db.fetchone("SELECT cash_usd FROM strat_balance WHERE strategy=? ORDER BY id DESC LIMIT 1", (STRATEGY,))
        if row and row["cash_usd"] is not None:
            self.engine.cash = float(row["cash_usd"])
        for r in await d.db.fetchall("SELECT * FROM strat_positions WHERE strategy=? AND closed_at IS NULL", (STRATEGY,)):
            await self._close_row_at_last_price(r)
        await self._balance_row()

    async def _close_row_at_last_price(self, r: dict) -> None:
        d, data = self.d, json.loads(r["data"] or "{}")
        price = data.get("last_price") or r["entry_price"]  # last real price we stored (entry only if none was ever stored)
        gross = r["tokens"] * price
        cost = fees.side_cost(gross, curve=data.get("venue") == "curve", jupiter_extra_pct=data.get("jup_extra_pct", 0.0),
                              params=self.engine.fee_params())
        proceeds = gross - cost.total
        self.engine.cash += proceeds
        pnl = proceeds - r["size_usd"]
        rule = "desk restart · marked at last real price"
        await d.db.execute("UPDATE strat_positions SET closed_at=?, exit_price=?, pnl_usd=?, fees_usd=?, exit_rule=? WHERE id=?",
                           (d.clock(), price, pnl, (r["fees_usd"] or 0.0) + cost.total, rule, r["id"]))
        await d.db.execute("INSERT INTO strat_events(ts, strategy, mint, kind, data) VALUES (?,?,?,?,?)",
                           (d.clock(), STRATEGY, r["mint"], "restart_close", json.dumps({"price": price, "pnl_usd": pnl})))
        log.info("closed %s left open across a restart at its last real price", r["mint"][:6])

    # ---- tick plumbing -----------------------------------------------------------------------------------------------------------------
    async def handle(self, tick: dict) -> list[dict]:
        self.rec.record(tick)  # recorded first: a replay sees exactly what the engine saw
        self.ticks += 1
        events = self.engine.on_tick(tick)
        if events:
            await self._persist(events)
        return events

    def tick_from_new_token(self, ev: dict) -> dict | None:
        if ev.get("late"):
            return None  # GeckoTerminal sighting: already 20-50 s old
        return {"kind": "create", "ts": ev["seen_at"], "mint": ev["mint"], "creator": ev.get("creator"), "v_sol": ev.get("v_sol"),
                "v_tokens": ev.get("v_tokens"), "initial_buy": ev.get("initial_buy"),
                "watching_trades": bool(self.feed and self.feed.api_key)}

    @staticmethod
    def tick_from_trade(ev: dict) -> dict | None:
        if not ev.get("mint") or ev.get("txType") not in ("buy", "sell"):
            return None
        return {"kind": "trade", "ts": ev["seen_at"], "mint": ev["mint"], "side": ev["txType"], "sol": ev.get("solAmount") or 0.0,
                "tokens": ev.get("tokenAmount"), "wallet": ev.get("traderPublicKey"), "v_sol": ev.get("vSolInBondingCurve"),
                "v_tokens": ev.get("vTokensInBondingCurve")}

    @staticmethod
    def tick_from_snapshot(s: dict) -> dict | None:
        if s.get("price_usd") is None:
            return None
        return {"kind": "snap", "ts": s["ts"], "mint": s["mint"], "price_usd": s["price_usd"], "liquidity_usd": s.get("liquidity_usd"),
                "buys_m5": s.get("buys_m5"), "sells_m5": s.get("sells_m5"), "dex": s.get("dex")}

    # ---- safety / dev prefetch --------------------------------------------------------------------------------------------------------------
    async def prefetch_safety(self, mint: str) -> dict | None:
        """The hard safety card. Any source that is down is 'no data' (no tick, retry later): never a pass."""
        d, sp = self.d, self.params.get("safety", {})
        reasons: list[str] = []
        try:
            info = await d.rpc.mint_info(mint)
            if info["mint_authority"] is not None:
                reasons.append("mint authority")
            if info["freeze_authority"] is not None:
                reasons.append("freeze authority")
            rep = await d.rugcheck.report(mint)
            if any((r or {}).get("level") == "danger" for r in rep.get("risks") or []):
                reasons.append("RugCheck danger")
            size = max(self.engine.equity() * self.params["size_pct"] / 100.0, self.params["min_ticket"])
            jup = await d.jupiter.round_trip(mint, size, await d.coinbase.spot())
        except Exception as e:  # noqa: BLE001
            log.info("safety card for %s unavailable: %s", mint[:6], e)
            return None
        rt, imp = jup["rt_loss_pct"], jup.get("sell_impact_pct")
        if rt > sp.get("max_round_trip_pct", 12.0):
            reasons.append(f"round trip {rt:.1f}%")
        if imp is not None and imp > sp.get("max_sell_impact_pct", 15.0):
            reasons.append(f"sell impact {imp:.1f}%")
        return {"kind": "safety", "ts": d.clock(), "mint": mint, "ok": not reasons, "reasons": reasons, "rt_loss_pct": rt,
                "sell_impact_pct": imp}

    async def _safety_job(self, mint: str) -> None:
        async with self.sem:
            try:
                tick = await self.prefetch_safety(mint)
                if tick is None:
                    self._retry_at[mint] = self.d.clock() + self.params.get("safety", {}).get("retry_s", 30)
                else:
                    await self.handle(tick)
            finally:
                self._inflight.discard(mint)

    async def dev_check(self, mint: str) -> dict | None:
        """Without a trade stream: has the creator sold? Compare the creator's token balance with what they bought."""
        m = self.engine.mints.get(mint)
        if m is None or not m.creator or not m.initial_buy:
            return None
        try:
            bal = await self.d.rpc.token_balance(m.creator, mint)
        except Exception:  # noqa: BLE001
            return None
        if bal is None:
            return None
        return {"kind": "dev", "ts": self.d.clock(), "mint": mint, "dev_sold": bal < m.initial_buy * 0.999}

    async def _dev_job(self, mint: str) -> None:
        try:
            tick = await self.dev_check(mint)
            if tick:
                await self.handle(tick)
        finally:
            self._inflight.discard("dev:" + mint)

    def schedule_prefetch(self, now: float) -> None:
        for mint in list(self.engine.mints):
            if mint in self._inflight or self._retry_at.get(mint, 0) > now:
                continue
            if self.engine.wants_safety(mint, now):
                self._inflight.add(mint)
                asyncio.create_task(self._safety_job(mint))
        for mint in list(self.engine.mints):  # creator checks when we do not watch the trade stream, and for open positions
            m = self.engine.mints[mint]
            need = (m.dev_sold is None and m.safety is not None) or mint in self.engine.positions
            if need and "dev:" + mint not in self._inflight and now - self._dev_checked.get(mint, 0) >= self.params.get("safety", {}).get("dev_check_s", 30):
                if not (self.feed and self.feed.api_key and m.dev_sold is not None):
                    self._dev_checked[mint] = now
                    self._inflight.add("dev:" + mint)
                    asyncio.create_task(self._dev_job(mint))

    # ---- persistence -----------------------------------------------------------------------------------------------------------------------------
    async def _balance_row(self) -> None:
        e = self.engine
        await self.d.db.execute("INSERT INTO strat_balance(ts, strategy, cash_usd, equity_usd) VALUES (?,?,?,?)",
                                (self.d.clock(), STRATEGY, e.cash, e.equity()))

    def _sym(self, mint: str) -> str:
        return (self.names.get(mint) or {}).get("symbol") or mint[:6]

    def _event_row(self, kind: str, mint: str | None, data: dict) -> None:
        self.d.db.execute_nowait("INSERT INTO strat_events(ts, strategy, mint, kind, data) VALUES (?,?,?,?,?)",
                                 (self.d.clock(), STRATEGY, mint, kind, json.dumps(data, default=str)))

    async def _persist(self, events: list[dict]) -> None:
        d = self.d
        for ev in events:
            typ, mint = ev["type"], ev.get("mint")
            if typ == "entry_signal":
                self._event_row(typ, mint, ev)
            elif typ == "entry_cancel":
                self._event_row(typ, mint, ev)
                await decide(d, "orion", mint, self._sym(mint), "skip", ev["reason"], strategy=STRATEGY)
            elif typ == "skip":
                key = (mint, ev["reason"])
                if d.clock() - self._skip_logged.get(key, 0) >= 60:  # one line per coin and reason per minute
                    self._skip_logged[key] = d.clock()
                    await decide(d, "orion", mint, self._sym(mint), "skip", ev["reason"], strategy=STRATEGY, data={"mode": ev.get("mode")})
            elif typ == "fill_buy":
                pos = self.engine.positions[mint]
                r = await d.db.execute(
                    "INSERT INTO strat_positions(strategy, mint, symbol, opened_at, entry_price, size_usd, tokens, fees_usd, data) VALUES (?,?,?,?,?,?,?,?,?)",
                    (STRATEGY, mint, self._sym(mint), d.clock(), ev["price"], ev["size"], ev["tokens"], ev["fees"],
                     json.dumps({"signal": ev["signal"], "council": ev["council"], "venue": pos["venue"], "jup_extra_pct": pos["jup_extra_pct"],
                                 "last_price": ev["price"], "engine_ts": ev["ts"]}, default=str)))
                self.rows[mint] = r.lastrowid
                await self._balance_row()
                await decide(d, "orion", mint, self._sym(mint), "buy", f"{ev['signal']['mode']} signal", value=f"${ev['size']:.2f} @ {ev['price']:.8g}",
                             strategy=STRATEGY, data={"council": ev["council"], "fees": ev["fees"], "latency_s": ev["latency_s"], "paper": True})
                d.hub.bus.publish("trade_open", {"mint": mint, "symbol": self._sym(mint), "usd": ev["size"], "price": ev["price"], "strategy": STRATEGY})
                push(d, f"PAPER · Bought {self._sym(mint)} · ${ev['size']:.2f} · {STRATEGY}", mint, strategy=STRATEGY)
            elif typ == "exit_signal":
                self._event_row(typ, mint, ev)
            elif typ == "fill_sell":
                self._event_row(typ, mint, ev)
                await self._balance_row()
            elif typ == "trade_closed":
                await self._on_closed(ev["trade"])
            elif typ == "halt":
                self._event_row(typ, None, ev)
                push(d, f"PAPER · Daily loss stop hit ({ev['loss_pct']:.1f}%) · {STRATEGY}", strategy=STRATEGY)
            elif typ == "day_start":
                self._event_row(typ, None, ev)

    async def _on_closed(self, tr: dict) -> None:
        d, mint = self.d, tr["mint"]
        last = tr["legs"][-1]["price"] if tr["legs"] else tr["entry_price"]
        row = self.rows.pop(mint, None)
        if row is not None:
            await d.db.execute("UPDATE strat_positions SET closed_at=?, exit_price=?, pnl_usd=?, fees_usd=?, exit_rule=?, data=? WHERE id=?",
                               (d.clock(), last, tr["pnl_usd"], tr["fees_total"], tr["exit_rule"], json.dumps(tr, default=str), row))
        await self._balance_row()
        await decide(d, "orion", mint, self._sym(mint), "sell", tr["exit_rule"], value=f"{tr['pnl_usd']:+.2f} ({tr['pnl_pct']:+.1f}%)",
                     strategy=STRATEGY, data={"fees": tr["fees_total"], "hold_s": tr["hold_s"], "paper": True})
        d.hub.bus.publish("trade_close", {"mint": mint, "symbol": self._sym(mint), "pnl_usd": tr["pnl_usd"], "pnl_pct": tr["pnl_pct"],
                                          "reason": tr["exit_rule"], "strategy": STRATEGY, "closed": True})
        word = "Stopped" if tr["exit_rule"].startswith("stop") else "Sold"
        push(d, f"PAPER · {word} {self._sym(mint)} · {tr['pnl_usd']:+.2f} ({tr['pnl_pct']:+.0f}%) · {tr['exit_rule']} · {STRATEGY}",
             mint, strategy=STRATEGY)

    async def _write_marks(self, now: float) -> None:
        """Keep each open position's last real price on disk, so a restart can mark it honestly."""
        if now - self._last_mark_write < 10:
            return
        self._last_mark_write = now
        for mint, row in list(self.rows.items()):
            m, pos = self.engine.mints.get(mint), self.engine.positions.get(mint)
            if m and pos and m.price_usd:
                self.d.db.execute_nowait("UPDATE strat_positions SET data=json_set(COALESCE(data,'{}'),'$.last_price',?) WHERE id=?", (m.price_usd, row))

    # ---- the loop ---------------------------------------------------------------------------------------------------------------------------------
    def _maybe_reload_params(self) -> None:
        try:
            mt = self.params_path.stat().st_mtime
            if mt != self._mtime:
                new = oc.load_params(self.params_path)
                for key in ("burst", "snapshot", "gates", "exits", "fill", "experiments", "size_pct", "min_ticket", "max_open"):
                    if key not in new:
                        raise ValueError(f"missing {key}")
                self.params, self.engine.p, self._mtime = new, new, mt
                log.info("orion params reloaded")
        except Exception as e:  # noqa: BLE001 - a bad edit never takes the strategy down: the old params stay
            log.warning("orion params reload failed, keeping the old ones: %s", e)

    def _refresh_watch(self, now: float) -> None:
        max_age = self.params["max_age_s"]
        self.watch = {m: t for m, t in self.watch.items() if now - t <= max_age}
        newest = sorted(self.watch, key=lambda m: -self.watch[m])
        if self.fb is not None:
            self.fb.set_watch(newest[:90])
        if self.feed is not None and self.feed.api_key:
            asyncio.create_task(self.feed.set_trade_watch(newest[:50]))

    async def step(self, now: float | None = None) -> None:
        """Drain what has arrived and do the periodic work. Separate from run() so tests can drive it."""
        d = self.d
        now = d.clock() if now is None else now
        for q, conv in ((self.q_new, self.tick_from_new_token), (self.q_trade, self.tick_from_trade), (self.q_snap, self.tick_from_snapshot)):
            while True:
                try:
                    ev = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                tick = conv(ev)
                if tick is None:
                    continue
                if tick["kind"] == "create":
                    self.names[tick["mint"]] = {"symbol": ev.get("symbol"), "name": ev.get("name")}
                    self.watch[tick["mint"]] = tick["ts"]
                await self.handle(tick)
        self.update_pause()
        if self.engine.positions or self.engine.pending:
            await self.handle({"kind": "clock", "ts": now})
        self.schedule_prefetch(now)
        await self._write_marks(now)

    async def run(self) -> None:
        from bus import beating_sleep

        await self.start()
        last_sol = last_watch = 0.0
        while True:
            self.d.hub.heartbeats.beat("orion")
            now = self.d.clock()
            try:
                if now - last_sol >= 30 and self.d.coinbase is not None:
                    try:
                        usd = await self.d.coinbase.spot()
                        last_sol = now
                        self.d.db.execute_nowait("INSERT OR REPLACE INTO sol_price(ts, usd) VALUES (?,?)", (now, usd))
                        await self.handle({"kind": "sol", "ts": now, "usd": usd})
                    except Exception as e:  # noqa: BLE001
                        last_sol = now - 20
                        log.info("SOL price unavailable: %s", e)
                if now - last_watch >= 5:
                    last_watch = now
                    self._maybe_reload_params()
                    self._refresh_watch(now)
                await self.step(now)
            except Exception as e:  # noqa: BLE001
                log.exception("orion step failed: %s", e)
            await beating_sleep(self.d.hub, "orion", 1.0, step=1.0)
