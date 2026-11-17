"""Bot 4, Executor: buys on the dip, takes profit (spec 5.4). PAPER only.

A paper buy goes up to the FOMO quote and stops: the primary button is never pressed (hard rule 2). The fill is the
quote minus fees. Paper sells open the Sell tab for a quote and fill at the DexScreener price that fired them, minus
fees. Anything missing (warnings, creator balance, Jupiter, quote) is a skip, never a pass.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque

import fees
from bots.common import Deps, activity, decide, push, subscribe_queue
from book import DipOrder, Position

log = logging.getLogger("desk.executor")


def parse_amount(text: str | None) -> float | None:
    """'44K paws' -> 44000.0, '1.2M' -> 1200000.0."""
    m = re.match(r"\s*([\d][\d.,]*)\s?([KMBkmb]?)", text or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()]


# ---- pure rules ------------------------------------------------------------------------------------------------------
def dip_decision(price: float | None, high10: float | None, buys5: float | None, sells5: float | None,
                 waited_min: float, s) -> tuple[str, str]:
    """('buy'|'wait'|'skip', reason). Buy only at >= dip% below the 10-min high with sellers <= 1.5x buyers.
    Deeper than the crash limit is a skip; no dip in time is a skip."""
    if price is None or not high10:
        return ("skip", "no dip") if waited_min >= s["dip_wait_min"] else ("wait", "no data: price")
    drop = 100.0 * (1 - price / high10)
    if drop > s["crash_pct"]:
        return "skip", f"crash −{drop:.0f}%"
    verdict = ("wait", f"waiting for a dip (−{max(drop, 0):.1f}%)")
    if drop >= s["dip_pct"] - 1e-9:
        if buys5 is None or sells5 is None:
            verdict = ("wait", "no data: 5m buyers/sellers")
        elif sells5 <= s["sellers_ratio"] * buys5:
            return "buy", f"dip −{drop:.1f}% with buyers back"
        else:
            verdict = ("wait", f"falling −{drop:.1f}% with sellers still running")
    if waited_min >= s["dip_wait_min"]:
        return "skip", "no dip"
    return verdict


def recheck(*, snap: dict | None, order: DipOrder, free_cash: float, warnings: dict | None, creator_balance_now: float | None,
            jup: dict | None, s) -> str | None:
    """Right before the click. Returns a skip reason, or None when everything checks out."""
    if snap is None or snap.get("price_usd") is None:
        return "no data: price"
    liq = snap.get("liquidity_usd")
    if order.scanner_liq and liq is not None and 100.0 * (order.scanner_liq - liq) / order.scanner_liq > s["recheck_liq_drop_pct"]:
        return f"liquidity down {100.0 * (order.scanner_liq - liq) / order.scanner_liq:.0f}% since scan"
    if order.analyst_price and 100.0 * (snap["price_usd"] / order.analyst_price - 1) > s["recheck_run_pct"]:
        return "ran before the click"
    if free_cash < order.size_usd:
        return "free cash < size"
    if warnings is None:
        return "no data: FOMO warnings"
    if warnings.get("disable_selling") or warnings.get("disable_buying"):
        return "FOMO: selling/buying off"
    if warnings.get("list"):
        return "FOMO warns"
    if order.creator_balance_analyst is not None:
        if creator_balance_now is None:
            return "no data: dev balance"
        if creator_balance_now < order.creator_balance_analyst * (1 - 1e-6):
            return "dev sold while we waited"
    if jup is None:
        return "no data: Jupiter round trip"
    if jup["rt_loss_pct"] > s["jupiter_roundtrip_max_pct"]:
        return f"Jupiter round trip {jup['rt_loss_pct']:.1f}% > {s['jupiter_roundtrip_max_pct']:g}%"
    if jup.get("sell_impact_pct") is not None and jup["sell_impact_pct"] > s["jupiter_sell_impact_max_pct"]:
        return f"Jupiter sell impact {jup['sell_impact_pct']:.1f}% > {s['jupiter_sell_impact_max_pct']:g}%"
    return None


def tp_decision(pos: Position, price: float, s) -> tuple[int, str] | None:
    """Take profit at tp_mult x entry (sell tp_sell_pct once); then trail trail% below the peak (sell the rest)."""
    eps = 1e-9  # a price exactly on the line counts (floating point must not decide)
    if not pos.tp_done and price >= pos.entry_price * s["tp_mult"] * (1 - eps):
        return int(s["tp_sell_pct"]), f"take profit {s['tp_mult']:g}x"
    if pos.tp_done and price <= pos.peak_price * (1 - s["trail_pct"] / 100.0) * (1 + eps):
        return 100, f"trail −{s['trail_pct']:g}% from peak"
    return None


class Executor:
    def __init__(self, deps: Deps):
        self.d = deps
        self.q = subscribe_queue(deps.hub, "order_request")
        self.q_sell = subscribe_queue(deps.hub, "manual_sell")
        self.hist: dict[str, deque] = {}  # mint -> (ts, price) over the dip window
        self._busy: set[str] = set()

    # ---- orders in ----------------------------------------------------------------------------------------------------
    async def place_dip_order(self, req: dict) -> bool:
        d, mint = self.d, req["mint"]
        if mint in d.book.positions:
            await decide(d, "executor", mint, req.get("symbol"), "skip", "already held: never a 2nd buy on an open position")
            return False
        if mint in d.book.waiting:
            return False
        if d.book.free_cash() < req["size_usd"]:
            await decide(d, "executor", mint, req.get("symbol"), "skip", "free cash < size")
            return False
        order = DipOrder(mint, req.get("symbol"), req["size_usd"], d.clock(), req.get("price") or 0.0, req.get("scanner_liq"),
                         req.get("creator"), req.get("creator_balance"), req.get("pool"), req.get("pool_liq"), req["score"],
                         {"curve_min": req.get("curve_min")})
        d.book.waiting[mint] = order
        h = self.hist.setdefault(mint, deque())
        rows = await d.db.fetchall("SELECT ts, price FROM prices WHERE mint=? AND ts>? ORDER BY ts",
                                   (mint, d.clock() - d.settings["dip_window_min"] * 60))
        for r in rows:
            h.append((r["ts"], r["price"]))
        if order.analyst_price:
            h.append((d.clock(), order.analyst_price))
        await decide(d, "executor", mint, order.symbol, "wait", f"dip order ${order.size_usd:.2f} waits up to {d.settings['dip_wait_min']:g} min")
        return True

    def high10(self, mint: str, now: float) -> float | None:
        cut = now - self.d.settings["dip_window_min"] * 60
        h = self.hist.get(mint)
        if not h:
            return None
        while h and h[0][0] < cut:
            h.popleft()
        return max((p for _, p in h), default=None)

    # ---- each Risk poll ---------------------------------------------------------------------------------------------------
    async def on_prices(self, snaps: dict[str, dict]) -> None:
        d, now = self.d, self.d.clock()
        for mint, snap in snaps.items():
            if snap and snap.get("price_usd") is not None:
                self.hist.setdefault(mint, deque()).append((now, snap["price_usd"]))
                d.book.last_prices[mint] = snap["price_usd"]
        for mint in list(d.book.positions):
            pos, snap = d.book.positions.get(mint), snaps.get(mint)
            if pos is None or not snap or snap.get("price_usd") is None:
                continue
            price = snap["price_usd"]
            if price > pos.peak_price:
                pos.peak_price = price
                await d.book.persist(pos)
            act = tp_decision(pos, price, d.settings)
            if act:
                await self.close(mint, act[0], act[1], price, bot="executor")
        for mint in list(d.book.waiting):
            order, snap = d.book.waiting.get(mint), snaps.get(mint)
            if order is None or mint in self._busy:
                continue
            waited = (now - order.created) / 60.0
            price = snap.get("price_usd") if snap else None
            verdict, why = dip_decision(price, self.high10(mint, now), (snap or {}).get("buys_m5"), (snap or {}).get("sells_m5"),
                                        waited, d.settings)
            if verdict == "skip":
                del d.book.waiting[mint]
                await decide(d, "executor", mint, order.symbol, "skip", why)
            elif verdict == "buy":
                self._busy.add(mint)
                try:
                    await self.buy(order, snap, why)
                finally:
                    self._busy.discard(mint)

    # ---- buy -------------------------------------------------------------------------------------------------------------
    async def buy(self, order: DipOrder, snap: dict, why: str) -> bool:
        d, mint, sym = self.d, order.mint, order.symbol

        async def skip(reason: str, push_text: str | None = None) -> bool:
            d.book.waiting.pop(mint, None)
            await decide(d, "executor", mint, sym, "skip", reason)
            if push_text:
                push(d, push_text, mint)
            return False

        free = d.book.cash - (d.book.reserved() - order.size_usd)  # this order's own hold is not 'someone else's'
        warnings = await self._warnings(mint)
        bal = await self._creator_balance(order)
        jup = await self._jupiter(mint, order.size_usd)
        bad = recheck(snap=snap, order=order, free_cash=free, warnings=warnings, creator_balance_now=bal, jup=jup, s=d.settings)
        if bad:
            return await skip(bad)
        if d.browser is None:
            return await skip("FOMO browser unavailable: no quote, no paper fill")
        try:
            q = await d.browser.buy_to_quote(mint, order.size_usd, bot="executor")
        except Exception as e:  # noqa: BLE001 - LoggedOutError, NeedsHuman, ClickDenied, CoinNotOpened ...
            name = type(e).__name__
            return await skip(f"browser: {name}: {e}", f"{name}: {sym or mint[:6]} not bought ({e})" if name == "LoggedOutError" else None)
        if not q.ok:
            return await skip(f"FOMO: {q.reason}")
        price = snap["price_usd"]
        tokens_quote = parse_amount(q.tokens_text)
        jup_extra = max(0.0, jup["rt_loss_pct"] / 2.0)  # real route worse than the model: extra cost per leg, never a benefit
        fee_model = fees.side_cost(order.size_usd, curve=False, jupiter_extra_pct=jup_extra, params=d.settings.fee_params()).total
        fee = max(fee_model, (q.fee_usd or 0.0) + order.size_usd * jup_extra / 100.0)
        if tokens_quote:
            implied = (order.size_usd - fee) / tokens_quote
            if abs(implied / price - 1) > 0.10:
                return await skip(f"page price vs DexScreener {100 * (implied / price - 1):+.0f}% apart (> 10%)")
        tokens = (order.size_usd - fee) / price
        d.book.waiting.pop(mint, None)
        order_id = await self._order("buy", mint, sym, order.size_usd, None, "executor", why, q, fill=order.size_usd)
        pos = await d.book.open_position(
            mint, sym, order.size_usd, tokens, price, fee, d.settings["stop_pct"],
            {"pool": order.pool, "creator": order.creator, "creator_balance_buy": bal, "analyst_price": order.analyst_price,
             "entry_liq": snap.get("liquidity_usd"), "scanner_liq": order.scanner_liq, "jup_extra_pct": jup_extra,
             "score": order.score, "quote": q.quote_text, "fee_line": q.fee_text, "order_id": order_id})
        if d.session:
            d.session.trades += 1
        await decide(d, "executor", mint, sym, "buy", why, value=f"${order.size_usd:.2f} @ {price:.8g}",
                     data={"quote": q.quote_text, "fee": fee, "paper": True})
        activity(d, "buy", "executor", mint, f"Bought {sym or mint[:6]} · ${order.size_usd:.2f}")
        d.hub.bus.publish("trade_open", {"mint": mint, "symbol": sym, "usd": order.size_usd, "price": price, "strategy": "prompt"})
        push(d, f"PAPER · Bought {sym or mint[:6]} · ${order.size_usd:.2f} · Executor", mint)
        return pos is not None

    async def _warnings(self, mint: str):
        d = self.d
        if d.fomo is None or d.body_for is None:
            return None
        try:
            from fields import get

            raw = await d.fomo.token_warnings(d.body_for("tokenWarnings", mint), bot="executor")
            w = {"disable_buying": get("warnings.disable_buying", raw), "disable_selling": get("warnings.disable_selling", raw),
                 "list": get("warnings.list", raw)}
            return None if all(v is None for v in w.values()) else w
        except Exception as e:  # noqa: BLE001
            log.info("warnings unavailable: %s", e)
            return None

    async def _creator_balance(self, order: DipOrder):
        d = self.d
        if not order.creator or d.rpc is None:
            return None
        try:
            return await d.rpc.token_balance(order.creator, order.mint)
        except Exception:  # noqa: BLE001
            return None

    async def _jupiter(self, mint: str, usd: float):
        d = self.d
        if d.jupiter is None or d.coinbase is None:
            return None
        try:
            return await d.jupiter.round_trip(mint, usd, await d.coinbase.spot())
        except Exception as e:  # noqa: BLE001
            log.info("jupiter unavailable: %s", e)
            return None

    async def _order(self, side, mint, sym, usd, pct, bot, reason, q, fill=None) -> int:
        d = self.d
        r = await d.db.execute(
            "INSERT INTO orders(ts, mint, symbol, side, usd, pct, bot, reason, status, quote_text, fee_text, fill_usd, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.clock(), mint, sym, side, usd, pct, bot, reason, "done", getattr(q, "quote_text", None),
             getattr(q, "fee_text", None), fill, "prompt"))
        return r.lastrowid

    # ---- sells ---------------------------------------------------------------------------------------------------------
    async def close(self, mint: str, pct: int, reason: str, price: float | None = None, bot: str = "executor") -> dict | None:
        """Sell pct% of what is held. PAPER: open the Sell tab for a quote (best effort), fill at the real DexScreener price
        that fired the sell, minus fees. A stop never waits for the browser."""
        d = self.d
        pos = d.book.positions.get(mint)
        if pos is None:
            return None
        price = price or d.book.last_prices.get(mint)
        if not price:
            raise RuntimeError("no real price to sell at")  # never invent one
        q = None
        if d.browser is not None:
            try:
                q = await d.browser.sell_to_quote(mint, pct if pct in (10, 25, 50, 100) else 100, bot=bot, urgent=bot == "risk")
            except Exception as e:  # noqa: BLE001
                log.info("paper sell quote skipped (%s): %s", type(e).__name__, e)
        frac = pct / 100.0
        gross = pos.tokens * frac * price
        fee = fees.side_cost(gross, curve=False, jupiter_extra_pct=pos.meta.get("jup_extra_pct", 0.0),
                             params=d.settings.fee_params()).total
        res = await d.book.reduce(mint, frac, price, fee, reason)
        await self._order("sell", mint, pos.symbol, gross, pct, bot, reason, q, fill=res["proceeds"])
        if not res["closed"]:
            pos.tp_done, pos.trail_active = True, True
            pos.peak_price = max(pos.peak_price, price)
            await d.book.persist(pos)
        txt = (f"Sold {pos.symbol or mint[:6]} · {res.get('pnl_usd', res['proceeds'] - pos.size_usd * frac):+.2f}"
               + (f" ({res['pnl_pct']:+.0f}%)" if res["closed"] else " (partial)"))
        await decide(d, bot, mint, pos.symbol, "sell", reason, value=f"{pct}% @ {price:.8g}", data={"paper": True, "fee": fee})
        activity(d, "sell", bot, mint, txt + f" · {reason}")
        d.hub.bus.publish("trade_close" if res["closed"] else "trade_partial", {**res, "strategy": "prompt"})
        stopped = reason.startswith("stop")
        push(d, f"PAPER · {'Stopped' if stopped else 'Sold'} {pos.symbol or mint[:6]} · {txt.split(' · ', 1)[-1]} · {reason}", mint)
        return res

    async def sell_all(self, reason: str) -> int:
        """Lock time: sell every open position 100%, cancel waiting dip orders."""
        d, n = self.d, 0
        for mint in list(d.book.waiting):
            order = d.book.waiting.pop(mint)
            await decide(d, "executor", mint, order.symbol, "skip", reason)
        for mint in list(d.book.positions):
            try:
                if await self.close(mint, 100, reason, None, bot="executor"):
                    n += 1
            except RuntimeError as e:
                log.warning("lock-time sell of %s skipped: %s", mint[:6], e)
        return n

    async def manual_sell(self, req: dict) -> dict | None:
        """Owner's 'Sell now' from the dashboard (25/50/100%). PAPER: simulated at the last real price, minus fees."""
        d, mint = self.d, req["mint"]
        if mint not in d.book.positions:
            return None
        return await self.close(mint, int(req["pct"]), "manual sell", None, bot="executor")

    async def run(self) -> None:
        from bus import beating_sleep, wait_if_paused

        while True:
            await wait_if_paused(self.d.hub, "executor")
            self.d.hub.heartbeats.beat("executor")
            did = False
            for q, handler in ((self.q, self.place_dip_order), (self.q_sell, self.manual_sell)):
                try:
                    req = q.get_nowait()
                except asyncio.QueueEmpty:
                    continue
                did = True
                try:
                    await handler(req)
                except Exception as e:  # noqa: BLE001
                    log.exception("executor failed on %s: %s", req.get("mint"), e)
            if not did:
                await beating_sleep(self.d.hub, "executor", 0.5, step=0.5)
