"""Shared helpers for the five bots: dependencies, decision log, pushes, small pure functions."""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field

import links


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def cents(x: float) -> float:
    return math.floor(x * 100 + 0.5) / 100


def norm_name(name: str | None) -> str:
    """Lowercase letters only: spaces, digits and emoji are stripped (copycat check)."""
    return "".join(c for c in (name or "").lower() if c.isalpha())


def kfmt(v: float) -> str:
    return f"${v / 1000:.1f}K" if abs(v) >= 1000 else f"${v:.0f}"


def short(addr: str | None) -> str:
    return "?" if not addr else addr if len(addr) <= 8 else f"{addr[:4]}…{addr[-3:]}"


@dataclass
class Deps:
    """Everything the bots share. Bots talk through the bus and the database; this is just their toolbox."""
    db: object
    hub: object
    settings: object
    book: object = None
    session: object = None
    dex: object = None
    fomo: object = None  # FomoApi-like: filter_tokens, token_details, token_warnings, balances
    rpc: object = None
    rugcheck: object = None
    jupiter: object = None
    coinbase: object = None
    browser: object = None  # Browser-like: buy_to_quote, sell_to_quote
    body_for: object = None  # (endpoint, mint) -> request body copied from the tab; None until captured
    clock: object = time.time
    sleep: object = asyncio.sleep
    extras: dict = field(default_factory=dict)


async def decide(deps: Deps, bot: str, mint: str | None, symbol: str | None, decision: str, reason: str,
                 value=None, strategy: str = "prompt", data: dict | None = None) -> dict:
    """Every pass/kill/skip with its reason and value goes to the decisions table and the dashboard log."""
    row = {"ts": deps.clock(), "bot": bot, "mint": mint, "symbol": symbol, "decision": decision, "reason": reason,
           "value": None if value is None else str(value), "strategy": strategy}
    deps.db.execute_nowait(
        "INSERT INTO decisions(ts, bot, mint, symbol, decision, reason, value, strategy, data) VALUES (?,?,?,?,?,?,?,?,?)",
        (row["ts"], bot, mint, symbol, decision, reason, row["value"], strategy, json.dumps(data) if data else None))
    deps.hub.bus.publish("decision", row)
    return row


def push(deps: Deps, text: str, mint: str | None = None, strategy: str = "prompt") -> None:
    """A message for the owner. Telegram delivery arrives in Phase 7; the link always comes from links.py."""
    deps.hub.bus.publish("push", {"text": text, "link": links.coin_url(mint) if mint else None, "strategy": strategy,
                                  "ts": deps.clock()})


def activity(deps: Deps, kind: str, bot: str, mint: str | None, text: str, data: dict | None = None) -> None:
    deps.db.execute_nowait("INSERT INTO activity(ts, kind, bot, mint, text, data) VALUES (?,?,?,?,?,?)",
                           (deps.clock(), kind, bot, mint, text, json.dumps(data) if data else None))
    deps.hub.bus.publish("activity", {"kind": kind, "bot": bot, "mint": mint, "text": text})


def subscribe_queue(hub, topic: str) -> asyncio.Queue:
    """Bots receive bus events through their own queue (the bus callback only enqueues)."""
    q: asyncio.Queue = asyncio.Queue()
    hub.bus.on(topic, lambda ev: q.put_nowait(ev["data"]))
    return q
