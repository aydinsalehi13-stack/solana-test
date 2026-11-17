"""Bot 3, Brain: sizes the trade. Never picks a coin, never clicks (spec 5.3)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from bots.common import Deps, cents, decide, subscribe_queue

log = logging.getLogger("desk.brain")


@dataclass
class Sizing:
    skip: bool
    size_usd: float
    pct_of_cash: float
    cap_hit: str  # pool | ticket | none
    reason: str


def size_trade(free_cash: float, score: float, pool_liq: float | None, s, *, shill: bool = False,
               trades_tonight: int = 0, night_loss_pct: float = 0.0) -> Sizing:
    """risk size = free cash x risk% / stop% (3% / 25% = 12% of cash). Full size at score >= 80, else 50%;
    shill ring halves again. Caps: 2% of pool liquidity, max ticket. Under the min ticket = skip."""
    if night_loss_pct >= s["night_loss_pct"]:
        return Sizing(True, 0.0, 0.0, "none", f"night loss {night_loss_pct:.0f}% >= {s['night_loss_pct']:g}%")
    if trades_tonight >= s["max_trades_night"]:
        return Sizing(True, 0.0, 0.0, "none", f"max trades tonight ({trades_tonight})")
    if pool_liq is None or pool_liq <= 0:
        return Sizing(True, 0.0, 0.0, "none", "no data: pool liquidity")
    if free_cash <= 0:
        return Sizing(True, 0.0, 0.0, "none", "no free cash")
    size = free_cash * (s["risk_pct"] / 100.0) / (s["stop_pct"] / 100.0)
    why = ["risk size"]
    if score < s["full_score"]:
        size *= s["reduced_size_pct"] / 100.0
        why.append(f"score {score:g} < {s['full_score']:g}: {s['reduced_size_pct']:g}%")
    if shill:
        size *= 0.5
        why.append("shill ring: half")
    pool_cap, ticket_cap = pool_liq * s["pool_cap_pct"] / 100.0, float(s["max_ticket"])
    cap_hit = "none"
    if size > pool_cap and pool_cap <= ticket_cap:
        size, cap_hit = pool_cap, "pool"
    elif size > ticket_cap:
        size, cap_hit = ticket_cap, "ticket"
    size = cents(size)
    if size < s["min_ticket"]:
        return Sizing(True, size, 0.0, cap_hit, f"under ${s['min_ticket']:g}")
    return Sizing(False, size, 100.0 * size / free_cash, cap_hit, ", ".join(why))


class Brain:
    def __init__(self, deps: Deps):
        self.d = deps
        self.q = subscribe_queue(deps.hub, "audit_pass")

    async def handle(self, a: dict) -> Sizing:
        d = self.d
        reserved = d.book.reserved()
        free = d.book.free_cash(reserved)
        eq = d.book.equity({})
        sz = size_trade(free, a["score"], a.get("pool_liq"), d.settings, shill=bool(a.get("shill")),
                        trades_tonight=d.session.trades if d.session else 0,
                        night_loss_pct=d.session.loss_pct(eq) if d.session else 0.0)
        if sz.skip:
            await decide(d, "brain", a["mint"], a.get("symbol"), "skip", sz.reason, value=f"${sz.size_usd:.2f}")
            return sz
        await decide(d, "brain", a["mint"], a.get("symbol"), "size", sz.reason,
                     value=f"${sz.size_usd:.2f} ({sz.pct_of_cash:.1f}% of cash, cap {sz.cap_hit})")
        d.hub.bus.publish("order_request", {**a, "size_usd": sz.size_usd, "pct_of_cash": sz.pct_of_cash, "cap_hit": sz.cap_hit})
        return sz

    async def run(self) -> None:
        from bus import wait_if_paused

        while True:
            await wait_if_paused(self.d.hub, "brain")
            try:
                a = await asyncio.wait_for(self.q.get(), timeout=20)
            except asyncio.TimeoutError:
                self.d.hub.heartbeats.beat("brain")
                continue
            self.d.hub.heartbeats.beat("brain")
            try:
                await self.handle(a)
            except Exception as e:  # noqa: BLE001
                log.exception("brain failed on %s: %s", a.get("mint"), e)


import asyncio  # noqa: E402  (kept at the bottom to keep the pure function above readable)
