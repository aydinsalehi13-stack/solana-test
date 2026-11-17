"""FOMO is the truth; the DB is a copy (spec section 3.3). LIVE only.

On every start, and before every buy, compare what FOMO says we hold (/balances) with what the DB says is open:
  held on FOMO, not open in the DB  -> adopt it (only if a real price is known) and push
  open in the DB, gone on FOMO      -> close it "not found on FOMO" (no invented P&L) and push
  before a buy: already held        -> no buy
In PAPER nothing is on FOMO, so reconcile does nothing: a paper position must never be closed because FOMO does not hold it.
"No data" (an unmapped field, a failed read) changes nothing and says so: it never closes or adopts anything.
"""
from __future__ import annotations

import logging
import time

import fields

log = logging.getLogger("desk.reconcile")
NOT_FOUND = "not found on FOMO"


def held_from_balances(raw, fmap: dict | None = None) -> dict[str, float] | None:
    """mint -> amount held, read through fields.py. None = no data (unmapped path, bad response); {} = we hold nothing."""
    items = fields.get("balances.positions", raw, fmap)
    if not isinstance(items, list):
        return None
    held: dict[str, float] = {}
    for it in items:
        mint, amount = fields.get("balances.position_mint", it, fmap), fields.get("balances.position_amount", it, fmap)
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return None  # one unreadable row: we cannot say what is held
        if not isinstance(mint, str) or not mint:
            return None
        if amount > 0:
            held[mint] = amount
    return held


def plan(open_in_db: set[str], held: dict[str, float] | None) -> dict:
    if held is None:
        return {"status": "no data", "adopt": [], "close": []}
    return {"status": "ok", "adopt": sorted(set(held) - open_in_db), "close": sorted(open_in_db - set(held))}


def buy_allowed(held: dict[str, float] | None, mint: str) -> tuple[bool, str]:
    """Before every buy: already held means no buy. No data also means no buy (we cannot prove we do not hold it)."""
    if held is None:
        return False, "FOMO balances unreadable (no data): not buying"
    if mint in held:
        return False, "already held on FOMO"
    return True, "not held"


async def reconcile(db, held: dict[str, float] | None, *, mode: str, prices: dict[str, float] | None = None, push=None, now: float | None = None,
                    strategy: str = "prompt") -> dict:
    now = time.time() if now is None else now
    if mode != "LIVE":
        return {"status": "skipped (PAPER: paper positions are not on FOMO)", "adopt": [], "close": [], "adopted": [], "closed": []}
    rows = await db.fetchall("SELECT id, mint, symbol FROM positions WHERE strategy=? AND closed_at IS NULL AND mode='LIVE'", (strategy,))
    res = plan({r["mint"] for r in rows}, held)
    res["adopted"], res["closed"] = [], []
    if res["status"] == "no data":
        if push:
            push("FOMO balances could not be read: positions were not reconciled")
        return res
    for r in rows:
        if r["mint"] in res["close"]:
            await db.execute("UPDATE positions SET closed_at=?, exit_reason=? WHERE id=?", (now, NOT_FOUND, r["id"]))  # pnl stays NULL: never invented
            res["closed"].append(r["mint"])
            if push:
                push(f"Closed {r['symbol'] or r['mint'][:6]} in the desk: {NOT_FOUND}")
    for mint in res["adopt"]:
        price = (prices or {}).get(mint)
        if price is None:  # no real price: do not invent an entry. Say so and let the owner look.
            if push:
                push(f"{mint[:6]} is held on FOMO but not in the desk, and no price is known: check FOMO")
            continue
        await db.execute("INSERT INTO positions(mint, opened_at, entry_price, tokens, mode, strategy, meta) VALUES (?,?,?,?,?,?,?)",
                         (mint, now, price, held[mint], "LIVE", strategy, '{"adopted_from_fomo": true}'))
        res["adopted"].append(mint)
        if push:
            push(f"Adopted {mint[:6]}: held on FOMO but not in the desk")
    return res
