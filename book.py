"""The paper bank for the 'prompt' strategy: cash, open positions, dip orders waiting, and persistence.

PAPER only. Money in open positions or held by a waiting dip order is never free cash (spec 5.3).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

STRATEGY = "prompt"


@dataclass
class Position:
    id: int
    mint: str
    symbol: str | None
    opened_at: float
    entry_price: float  # effective price paid per token, fees included
    size_usd: float
    tokens: float  # tokens still held
    peak_price: float
    stop_price: float
    tp_done: bool = False
    trail_active: bool = False
    fees_usd: float = 0.0
    realized_usd: float = 0.0  # proceeds already taken (take-profit leg)
    meta: dict = field(default_factory=dict)  # pool, creator, creator_balance_buy, analyst_price, entry_liq ...

    @property
    def cost_remaining(self) -> float:
        return self.size_usd * (self.tokens / self.initial_tokens) if self.initial_tokens else 0.0

    @property
    def initial_tokens(self) -> float:
        return float(self.meta.get("initial_tokens") or self.tokens)


@dataclass
class DipOrder:
    mint: str
    symbol: str | None
    size_usd: float
    created: float
    analyst_price: float
    scanner_liq: float | None
    creator: str | None
    creator_balance_analyst: float | None
    pool: str | None
    pool_liq: float | None
    score: float
    meta: dict = field(default_factory=dict)


class PaperBook:
    def __init__(self, db, hub, settings, clock, strategy: str = STRATEGY):
        self.db, self.hub, self.s, self.clock, self.strategy = db, hub, settings, clock, strategy
        self.cash: float = float(settings["paper_start_cash"])
        self.positions: dict[str, Position] = {}
        self.waiting: dict[str, DipOrder] = {}
        self.last_prices: dict[str, float] = {}
        self.closed_today: list[dict] = []

    async def load(self) -> None:
        row = await self.db.fetchone("SELECT cash_usd FROM balance WHERE strategy=? ORDER BY id DESC LIMIT 1", (self.strategy,))
        if row and row["cash_usd"] is not None:
            self.cash = float(row["cash_usd"])
        for r in await self.db.fetchall("SELECT * FROM positions WHERE strategy=? AND closed_at IS NULL", (self.strategy,)):
            self.positions[r["mint"]] = Position(
                id=r["id"], mint=r["mint"], symbol=r["symbol"], opened_at=r["opened_at"], entry_price=r["entry_price"],
                size_usd=r["size_usd"], tokens=r["tokens"], peak_price=r["peak_price"] or r["entry_price"],
                stop_price=r["stop_price"], tp_done=bool(r["tp_done"]), trail_active=bool(r["trail_active"]),
                fees_usd=r["fees_usd"] or 0.0, realized_usd=r["realized_usd"] or 0.0, meta=json.loads(r["meta"] or "{}"))

    # ---- cash ---------------------------------------------------------------------------------------------
    def reserved(self) -> float:
        """Held by waiting dip orders."""
        return sum(o.size_usd for o in self.waiting.values())

    def free_cash(self, reserved: float | None = None) -> float:
        return self.cash - (self.reserved() if reserved is None else reserved)

    def positions_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for p in self.positions.values():
            px = prices.get(p.mint) or self.last_prices.get(p.mint) or p.entry_price
            total += p.tokens * px
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.reserved() + self.positions_value(prices)

    async def snapshot(self, prices: dict[str, float] | None = None) -> None:
        eq = self.equity(prices or {})
        await self.db.execute("INSERT INTO balance(ts, strategy, cash_usd, equity_usd, mode) VALUES (?,?,?,?,?)",
                              (self.clock(), self.strategy, self.cash, eq, "PAPER"))

    # ---- positions ----------------------------------------------------------------------------------------
    async def open_position(self, mint: str, symbol: str | None, size_usd: float, tokens: float, entry_price: float,
                            fee_usd: float, stop_pct: float, meta: dict) -> Position:
        if mint in self.positions:
            raise ValueError("a second buy on an open position is never allowed")
        stop = entry_price * (1 - stop_pct / 100.0)
        meta = {**meta, "initial_tokens": tokens}
        now = self.clock()
        r = await self.db.execute(
            "INSERT INTO positions(mint, symbol, opened_at, entry_price, size_usd, tokens, peak_price, stop_price, fees_usd, "
            "fees_buy_usd, realized_usd, mode, strategy, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mint, symbol, now, entry_price, size_usd, tokens, entry_price, stop, fee_usd, fee_usd, 0.0, "PAPER", self.strategy,
             json.dumps(meta)))
        pos = Position(r.lastrowid, mint, symbol, now, entry_price, size_usd, tokens, entry_price, stop, False, False,
                       fee_usd, 0.0, meta)
        self.positions[mint] = pos
        self.cash -= size_usd
        await self.snapshot()
        return pos

    async def reduce(self, mint: str, fraction: float, price: float, fee_usd: float, reason: str) -> dict:
        """Sell `fraction` (0..1] of the tokens still held at `price`. fraction 1 closes the position."""
        pos = self.positions[mint]
        fraction = min(1.0, max(0.0, fraction))
        sold = pos.tokens * fraction
        proceeds = sold * price - fee_usd
        pos.tokens -= sold
        pos.realized_usd += proceeds
        pos.fees_usd += fee_usd
        self.cash += proceeds
        self.last_prices[mint] = price
        out = {"mint": mint, "symbol": pos.symbol, "price": price, "proceeds": proceeds, "fee": fee_usd, "reason": reason,
               "closed": pos.tokens <= 1e-12 or fraction >= 0.999999}
        if out["closed"]:
            pnl = pos.realized_usd - pos.size_usd
            out["pnl_usd"], out["pnl_pct"] = pnl, 100.0 * pnl / pos.size_usd if pos.size_usd else 0.0
            await self.db.execute(
                "UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, pnl_usd=?, fees_usd=?, realized_usd=?, tokens=0 WHERE id=?",
                (self.clock(), price, reason, pnl, pos.fees_usd, pos.realized_usd, pos.id))
            del self.positions[mint]
            self.closed_today.append(out)
        else:
            await self.persist(pos)
        await self.snapshot({mint: price})
        return out

    async def persist(self, pos: Position) -> None:
        await self.db.execute(
            "UPDATE positions SET tokens=?, peak_price=?, stop_price=?, tp_done=?, trail_active=?, fees_usd=?, realized_usd=? WHERE id=?",
            (pos.tokens, pos.peak_price, pos.stop_price, int(pos.tp_done), int(pos.trail_active), pos.fees_usd, pos.realized_usd, pos.id))


def tighten_stop(current_stop: float, entry_price: float, stop_pct: float) -> float:
    """The stop is set at the buy and only ever tightens: a looser setting never moves it down."""
    return max(current_stop, entry_price * (1 - stop_pct / 100.0))
