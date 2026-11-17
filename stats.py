"""Per-strategy numbers for the dashboard (/api/live). Everything comes from the database: no data is None, never invented."""
from __future__ import annotations

import time

import orion_core as oc


async def _card(db, name: str, start_cash: float, bal_sql: str, closed_sql: str, open_sql: str, now: float) -> dict:
    bal = await db.fetchone(bal_sql, (name,))
    closed = await db.fetchall(closed_sql, (name,))
    pnls = [r["pnl_usd"] for r in closed if r["pnl_usd"] is not None]
    wins = [p for p in pnls if p > 0]
    open_rows = await db.fetchall(open_sql, (name,))
    equity = bal["equity_usd"] if bal else None
    return {
        "name": name, "mode": "PAPER", "start_cash": start_cash,
        "cash": bal["cash_usd"] if bal else None, "balance": equity,
        "pnl_usd": None if equity is None else equity - start_cash,
        "pnl_pct": None if equity is None or not start_cash else 100.0 * (equity - start_cash) / start_cash,
        "trades": len(pnls), "wins": len(wins), "win_rate": 100.0 * len(wins) / len(pnls) if pnls else None,
        "trades_last_hour": sum(1 for r in closed if r["closed_at"] and now - r["closed_at"] <= 3600),
        "fees_usd": sum(r["fees_usd"] or 0.0 for r in closed),
        "open": [{"mint": r["mint"], "symbol": r["symbol"], "size_usd": r["size_usd"], "entry_price": r["entry_price"],
                  "opened_at": r["opened_at"]} for r in open_rows],
    }


async def strategy_cards(db, prompt_start_cash: float, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    prompt = await _card(
        db, "prompt", prompt_start_cash,
        "SELECT cash_usd, equity_usd FROM balance WHERE strategy=? ORDER BY id DESC LIMIT 1",
        "SELECT pnl_usd, closed_at, fees_usd FROM positions WHERE strategy=? AND closed_at IS NOT NULL",
        "SELECT mint, symbol, size_usd, entry_price, opened_at FROM positions WHERE strategy=? AND closed_at IS NULL", now)
    orion = await _card(
        db, "orion_fast", float(oc.load_params().get("start_cash", 150.0)),
        "SELECT cash_usd, equity_usd FROM strat_balance WHERE strategy=? ORDER BY id DESC LIMIT 1",
        "SELECT pnl_usd, closed_at, fees_usd FROM strat_positions WHERE strategy=? AND closed_at IS NOT NULL",
        "SELECT mint, symbol, size_usd, entry_price, opened_at FROM strat_positions WHERE strategy=? AND closed_at IS NULL", now)
    return [prompt, orion]
