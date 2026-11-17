"""Bot 5, Risk: cuts losers, never bends (spec 5.5).

One DexScreener call for all open (and dip-waiting) mints every 5 s. Rules in order: stop, time, pulled, dev, fomo.
Risk sells jump the browser lane. The stop is set at the buy and only ever tightens. Risk cannot be paused while a
position is open. No price for 12 polls = sell 'no price' (at the last real price, never an invented one).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from bots.common import Deps, push
from book import Position, tighten_stop

log = logging.getLogger("desk.risk")

NO_PRICE_POLLS = 12
PULLED_DROP_PCT = 80.0
DEV_DUMP_FRACTION = 0.5
SLOW_CHECK_S = 60.0


@dataclass
class MintState:
    no_price_polls: int = 0
    prev_liq: float | None = None
    last_slow: float = 0.0
    selling_off_pushed: bool = False
    sell_failures: int = 0
    retry_after: float = 0.0


@dataclass
class Outcome:
    sell: str | None = None
    pushes: list[str] = field(default_factory=list)


def check_position(pos: Position, price: float | None, liquidity: float | None, now: float, state: MintState, s, *,
                   creator_balance: float | None = None, warnings: dict | None = None) -> Outcome:
    """Evaluate one open position. Mutates state and tightens pos.stop_price; returns the first sell reason."""
    out = Outcome()
    pos.stop_price = tighten_stop(pos.stop_price, pos.entry_price, s["stop_pct"])
    if price is None:
        state.no_price_polls += 1
        if state.no_price_polls >= NO_PRICE_POLLS:
            out.sell = "no price"
        return out
    state.no_price_polls = 0
    if price <= pos.stop_price * (1 + 1e-9):
        out.sell = f"stop {100.0 * (price / pos.entry_price - 1):+.1f}%"
        return out
    if now - pos.opened_at >= s["dead_hours"] * 3600 and price < pos.entry_price * (1 + s["dead_gain_pct"] / 100.0):
        out.sell = "dead money"
        return out
    if liquidity is not None:
        gone = liquidity <= 0 or (state.prev_liq and 100.0 * (state.prev_liq - liquidity) / state.prev_liq >= PULLED_DROP_PCT)
        state.prev_liq = liquidity
        if gone:
            out.sell = "pulled"
            return out
    bal_buy = pos.meta.get("creator_balance_buy")
    if creator_balance is not None and bal_buy and creator_balance <= bal_buy * DEV_DUMP_FRACTION:
        out.sell = "dev dumping"
        return out
    if warnings:
        if warnings.get("disable_selling") and not state.selling_off_pushed:
            state.selling_off_pushed = True
            out.pushes.append(f"FOMO turned selling off for {pos.symbol or pos.mint[:6]}")
        if warnings.get("list"):
            out.sell = "FOMO warns"
    return out


class Risk:
    def __init__(self, deps: Deps, executor):
        self.d, self.ex = deps, executor
        self.state: dict[str, MintState] = {}

    def pausable(self) -> bool:
        return not self.d.book.positions  # Risk cannot be paused while a position is open

    async def poll(self) -> list[tuple[str, str]]:
        """One cycle. Returns [(mint, sell_reason)] for the sells it ordered."""
        d, now = self.d, self.d.clock()
        mints = list(dict.fromkeys(list(d.book.positions) + list(d.book.waiting)))
        if not mints:
            return []
        try:
            snaps = await d.dex.snapshots(mints)
        except Exception as e:  # noqa: BLE001
            log.warning("risk poll: DexScreener failed: %s", e)
            snaps = {}
        for mint, snap in snaps.items():
            if snap and snap.get("price_usd") is not None:
                d.book.last_prices[mint] = snap["price_usd"]
        sold: list[tuple[str, str]] = []
        for mint in list(d.book.positions):
            pos, st = d.book.positions[mint], self.state.setdefault(mint, MintState())
            snap = snaps.get(mint) or {}
            bal = warnings = None
            if now - st.last_slow >= SLOW_CHECK_S:  # every 60 s: dev balance and FOMO warnings
                st.last_slow = now
                bal, warnings = await self._slow_checks(pos)
            out = check_position(pos, snap.get("price_usd"), snap.get("liquidity_usd"), now, st, d.settings,
                                 creator_balance=bal, warnings=warnings)
            for text in out.pushes:
                push(d, text, mint)
            if out.sell and now >= st.retry_after:
                try:
                    await self.ex.close(mint, 100, out.sell, snap.get("price_usd"), bot="risk")
                    sold.append((mint, out.sell))
                    self.state.pop(mint, None)
                except Exception as e:  # noqa: BLE001
                    st.sell_failures += 1
                    st.retry_after = now + 10.0  # retry every 10 s
                    log.warning("risk sell of %s failed (%d): %s", mint[:6], st.sell_failures, e)
                    if st.sell_failures == 2:
                        push(d, f"Sell {pos.symbol or mint[:6]} by hand now", mint)
        await self.ex.on_prices(snaps)  # take profit, trail and dip orders ride on the same poll
        return sold

    async def _slow_checks(self, pos: Position):
        d, bal, warnings = self.d, None, None
        if d.rpc is not None and pos.meta.get("creator"):
            try:
                bal = await d.rpc.token_balance(pos.meta["creator"], pos.mint)
            except Exception:  # noqa: BLE001
                pass
        if d.fomo is not None and d.body_for is not None:
            try:
                from fields import get

                raw = await d.fomo.token_warnings(d.body_for("tokenWarnings", pos.mint), bot="risk")
                w = {"disable_buying": get("warnings.disable_buying", raw), "disable_selling": get("warnings.disable_selling", raw),
                     "list": get("warnings.list", raw)}
                warnings = None if all(v is None for v in w.values()) else w
            except Exception:  # noqa: BLE001
                pass
        return bal, warnings

    async def run(self) -> None:
        from bus import beating_sleep

        while True:
            self.d.hub.heartbeats.beat("risk")
            t0 = time.monotonic()
            try:
                if self.d.session and self.d.session.lock_due():
                    n = await self.ex.sell_all("lock time")
                    await self.d.session.close(self.d.book.equity({}))
                    await self.d.session.open()
                    self.d.hub.bus.publish("session_roll", {"sold": n})
                await self.poll()
            except Exception as e:  # noqa: BLE001
                log.exception("risk cycle failed: %s", e)
            await beating_sleep(self.d.hub, "risk", max(0.5, self.d.settings["risk_poll_s"] - (time.monotonic() - t0)),
                                step=self.d.settings["risk_poll_s"])
