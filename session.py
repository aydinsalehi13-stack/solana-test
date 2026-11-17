"""Night sessions (spec 5): the desk trades 24/7; night_start and lock_time are session boundaries for the stats.

At lock_time the session rolls over: the Executor sells every open prompt position ('lock time'), the session
closes and a new one opens at once. Night stats feed the Brain (max trades, loss limit).
"""
from __future__ import annotations

import datetime as dt


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


class Session:
    def __init__(self, db, settings, book, clock):
        self.db, self.s, self.book, self.clock = db, settings, book, clock
        self.id: int | None = None
        self.started_at = 0.0
        self.start_equity = float(settings["paper_start_cash"])
        self.trades = 0
        self._last_lock_day: dt.date | None = None

    async def open(self, equity: float | None = None) -> None:
        self.started_at = self.clock()
        self.start_equity = self.book.equity({}) if equity is None else equity
        self.trades = 0
        r = await self.db.execute("INSERT INTO nights(strategy, started_at, start_cash) VALUES (?,?,?)",
                                  (self.book.strategy, self.started_at, self.start_equity))
        self.id = r.lastrowid

    async def close(self, equity: float) -> None:
        if self.id is not None:
            await self.db.execute("UPDATE nights SET ended_at=?, end_cash=?, trades=? WHERE id=?",
                                  (self.clock(), equity, self.trades, self.id))

    def loss_pct(self, equity: float) -> float:
        if self.start_equity <= 0:
            return 0.0
        return max(0.0, 100.0 * (self.start_equity - equity) / self.start_equity)

    def lock_due(self, now: dt.datetime | None = None) -> bool:
        """True once per day, the first time local time is at or past lock_time."""
        now = now or dt.datetime.fromtimestamp(self.clock())
        if now.time() >= _hm(self.s["lock_time"]) and self._last_lock_day != now.date():
            self._last_lock_day = now.date()
            return True
        return False

    def mark_lock_checked_today(self, now: dt.datetime | None = None) -> None:
        """At startup after lock_time we do not roll over immediately."""
        now = now or dt.datetime.fromtimestamp(self.clock())
        if now.time() >= _hm(self.s["lock_time"]):
            self._last_lock_day = now.date()
