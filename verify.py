"""VERIFY: did a LIVE click really trade? (spec section 3.3). Also the guard that keeps one order from ever being clicked twice.

No LIVE click exists in this build (browser.py refuses it). This module is the part that will judge a click when LIVE is unlocked,
and it is tested now with fakes so the rules are proven before any money is involved.

A click is verified only if ALL THREE agree, each read from a different place:
  screen  the page shows "Bought" / "Sold" for that side
  api     /swaps page 1 has a NEW swap of that mint, the right side, $ within `tol_pct` (3%) of the order
  chain   the RPC token balance of the owner's FOMO address moved the right way (None = "n/a": it could not be proven, and we say so)
Screen done but api or chain not: the order is `unverified`, with a screenshot and a push, and it is NEVER clicked again.
Positions then follow the API's numbers, not the screen.

Each check returns True / False / None (None = no data). No data is never a pass.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import fields

log = logging.getLogger("desk.verify")

TOL_PCT = 3.0  # spec: "$ within 3% (setting)"
WAIT_S = 30.0  # spec: "up to 30 s (setting)"
POLL_S = 2.0
SCREEN_WORD = {"buy": r"\bBought\b", "sell": r"\bSold\b"}


def screen_check(side: str, page_text: str | None) -> bool | None:
    if not page_text:
        return None
    return bool(re.search(SCREEN_WORD[side], page_text))


def api_check(side: str, mint: str, usd: float, swaps: list[dict] | None, since: float, tol_pct: float = TOL_PCT) -> bool | None:
    """A NEW swap (after the click) of this mint, the right side, $ within tol_pct. `swaps` is normalised rows or None for no data."""
    if swaps is None:
        return None
    for s in swaps:
        if s.get("mint") != mint or s.get("side") != side or s.get("usd") is None or s.get("ts") is None:
            continue
        if s["ts"] >= since - 1.0 and usd > 0 and abs(s["usd"] - usd) / usd * 100.0 <= tol_pct + 1e-9:
            return True
    return False


def chain_check(side: str, before: float | None, after: float | None) -> bool | None:
    if before is None or after is None:
        return None
    return after > before if side == "buy" else after < before


def swaps_from_raw(raw, fmap: dict | None = None) -> list[dict] | None:
    """Normalise a /swaps page through fields.py. Unmapped or missing keys give None (no data), never a guess."""
    rows = fields.get("swaps.rows", raw, fmap)
    if not isinstance(rows, list):
        return None
    out = []
    for r in rows:
        side = fields.classify_side(fields.get("swap.side", r, fmap), fields.get("swap.in_symbol", r, fmap), fields.get("swap.out_symbol", r, fmap))
        out.append({"side": side, "mint": fields.get("swap.mint", r, fmap), "usd": _num(fields.get("swap.usd", r, fmap)), "ts": _num(fields.get("swap.ts", r, fmap))})
    return out


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def decide(screen: bool | None, api: bool | None, chain: bool | None) -> tuple[str, str]:
    """(status, why). Needs all three; chain may be 'n/a' (None) but never False."""
    word = lambda v: "no data" if v is None else ("ok" if v else "disagrees")  # noqa: E731
    detail = f"screen {word(screen)}, api {word(api)}, chain {'n/a (could not be proven)' if chain is None else word(chain)}"
    if screen and api and chain is not False:
        return "done", f"verified ({detail})"
    if screen:
        return "unverified", f"the screen says done but the rest does not confirm it ({detail}): check FOMO, not clicked again"
    if api and chain is not False:
        return "unverified", f"the API shows the swap but the screen never said Bought/Sold ({detail}): check FOMO, not clicked again"
    if screen is None and api is None:
        return "unverified", f"nothing could be read back ({detail}): check FOMO, not clicked again"
    return "failed", f"no sign the trade happened ({detail}); not clicked again"


async def verify_order(db, order_id: int, *, side: str, mint: str, usd: float, page_text_fn, swaps_fn, chain_fn, since: float,
                       tol_pct: float = TOL_PCT, wait_s: float = WAIT_S, poll_s: float = POLL_S, clock=time.time, sleep=asyncio.sleep,
                       chain_before: float | None = None, notify=None) -> dict:
    """Poll the three checks until all agree or `wait_s` runs out, then write the verdict on the order row. Never clicks."""
    await db.execute("UPDATE orders SET status='verifying' WHERE id=? AND status IN ('clicking','verifying')", (order_id,))
    deadline = clock() + wait_s
    screen = api = chain = None
    while True:
        screen = screen_check(side, await page_text_fn())
        api = api_check(side, mint, usd, await swaps_fn(), since, tol_pct)
        chain = chain_check(side, chain_before, await chain_fn())
        if (screen and api and chain is not None) or clock() >= deadline:
            break
        await sleep(poll_s)
    status, why = decide(screen, api, chain)
    flag = lambda v: None if v is None else int(v)  # noqa: E731
    await db.execute("UPDATE orders SET status=?, screen_ok=?, api_ok=?, chain_ok=?, error=? WHERE id=?",
                     (status, flag(screen), flag(api), flag(chain), None if status == "done" else why, order_id))
    res = {"order_id": order_id, "status": status, "why": why, "screen": screen, "api": api, "chain": chain}
    if status != "done" and notify is not None:
        notify(f"Unverified: {mint[:6]} {side}, check FOMO" if status == "unverified" else f"Failed: {mint[:6]} {side}, check FOMO")
    return res


# ---- one order is clicked at most once, even across a crash -----------------------------------------------------------------------
async def claim_click(db, order_id: int) -> bool:
    """Atomically queued -> clicking. False means this order was already claimed (or finished): the caller must NOT click."""
    r = await db.execute("UPDATE orders SET status='clicking' WHERE id=? AND status='queued'", (order_id,))
    return r.rowcount == 1


async def recover_orders(db, now: float | None = None) -> list[dict]:
    """On every start: an order left 'clicking' or 'verifying' may or may not have traded. Mark it unverified (never re-click)
    and return what to tell the owner. Orders still 'queued' were never clicked and are left alone."""
    rows = await db.fetchall("SELECT id, mint, symbol, side, usd FROM orders WHERE status IN ('clicking','verifying')")
    for r in rows:
        await db.execute("UPDATE orders SET status='unverified', error=? WHERE id=?",
                         ("the desk stopped while this order was being clicked: check FOMO, not clicked again", r["id"]))
    if rows:
        log.warning("%d order(s) were in flight when the desk stopped: marked unverified, never clicked again", len(rows))
    return rows
