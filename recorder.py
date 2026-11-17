"""Records every tick the orion engine sees, in the order it saw them, so any hour replays exactly (spec 6.3.1).

Order matters: rows are replayed by id (insertion order), not by timestamp. The full tick is kept as JSON in `data`;
ts/mint/kind/side/sol/price/wallet are copied out for queries. Retention (7 days) is db.retention().
"""
from __future__ import annotations

import json


class TickRecorder:
    def __init__(self, db):
        self.db = db
        self.count = 0

    def record(self, tick: dict) -> None:
        self.db.execute_nowait(
            "INSERT INTO ticks(ts, mint, kind, side, sol, price, wallet, data) VALUES (?,?,?,?,?,?,?,?)",
            (tick["ts"], tick.get("mint") or "", tick["kind"], tick.get("side"), tick.get("sol"),
             tick.get("price_usd", tick.get("price")), tick.get("wallet"), json.dumps(tick, sort_keys=True, separators=(",", ":"))))
        self.count += 1


async def load_ticks(db, since: float | None = None, until: float | None = None) -> list[dict]:
    """Recorded ticks in the order the engine saw them."""
    q, args = "SELECT data FROM ticks WHERE data IS NOT NULL", []
    if since is not None:
        q, args = q + " AND ts >= ?", args + [since]
    if until is not None:
        q, args = q + " AND ts < ?", args + [until]
    return [json.loads(r["data"]) for r in await db.fetchall(q + " ORDER BY id", tuple(args))]
