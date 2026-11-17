"""The gates for LIVE (CLAUDE.md section 0, rule 1). Shared by the dashboard API and Telegram.

LIVE needs all of: one full PAPER night in the database, the acceptance suite passed, and a $2 LIVE test trade the owner approved
(bought, verified on every check, sold 100%, verified). Even with all of them met, this build cannot go live: LIVE clicking is not
implemented. Nothing in the code ever writes mode=LIVE.
"""
from __future__ import annotations

import json

import config


def _flag(name: str) -> bool:
    try:
        return bool(json.loads((config.DATA_DIR / name).read_text()).get("passed"))
    except (OSError, ValueError):
        return False


async def live_gates(db) -> list[dict]:
    nights = await db.fetchone("SELECT COUNT(*) n FROM nights WHERE ended_at IS NOT NULL")
    return [{"gate": "one full PAPER night in the database", "met": (nights["n"] or 0) >= 1},
            {"gate": "the acceptance suite passed", "met": _flag("acceptance.json")},
            {"gate": "a $2 LIVE test trade approved by the owner, verified both ways", "met": _flag("live_test.json")}]


LIVE_NOT_BUILT = "LIVE is not implemented in this build"
