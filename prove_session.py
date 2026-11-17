"""Prove the FOMO session after the owner has logged in by hand in the desk Chrome.

  python prove_session.py [--handle fluffcore3] [--mint <coin mint>]

Checks the header shows the avatar and cash, reads userId + Solana address through the FOMO API (in-tab fetch,
the cookie is never touched), saves them to the settings table, and takes screenshots into proof/session/.
The address is never printed in full.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import config
import fields


def mask(addr: str) -> str:
    return f"{addr[:4]}...{addr[-4:]}" if addr and len(addr) > 10 else "(short)"


async def prove(browser, api, db, handle: str = "fluffcore3", mint: str | None = None,
                out_dir: Path | None = None, fmap: dict | None = None) -> dict:
    out = out_dir or config.ROOT / "proof" / "session"
    out.mkdir(parents=True, exist_ok=True)
    header = await browser.ensure_ready()  # raises LoggedOutError unless avatar + cash are in the header
    page = await browser.ensure_page()
    hdr_png = out / "header.png"
    await page.screenshot(path=str(hdr_png), clip={"x": 0, "y": 0, "width": max(header.get("wide", 1440), 1), "height": 90})
    raw = await api.user_by_handle(handle)
    uid, addr = fields.get("user.id", raw, fmap), fields.get("user.address", raw, fmap)
    res = {"ok": False, "cash": header["cash"], "shots": [str(hdr_png)]}
    if uid is None or addr is None:
        res["needs"] = ("userId/address keys are not mapped in fields_map.json yet. A raw sample was saved to "
                        "logs/raw/userHandle.json: run `python fields.py --suggest`, then map user.id and user.address.")
        return res
    now = time.time()
    await db.executemany("INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         [("fomo_user_id", str(uid), now), ("fomo_address", str(addr), now), ("fomo_handle", handle, now)])
    if mint:
        await browser.open_coin(mint)
        coin_png = out / "coin_page.png"
        await page.screenshot(path=str(coin_png))
        res["shots"].append(str(coin_png))
    res.update(ok=True, user_id=str(uid), address=mask(str(addr)))
    return res


async def amain(argv: list[str]) -> int:
    from browser import Browser
    from db import Database
    from fomo_api import FomoApi, attach_request_recorder, page_fetcher

    handle = argv[argv.index("--handle") + 1] if "--handle" in argv else "fluffcore3"
    mint = argv[argv.index("--mint") + 1] if "--mint" in argv else None
    config.load_env()
    config.ensure_dirs()
    db = Database(config.DB_PATH)
    await db.start()
    b = Browser()
    try:
        await b.connect()
        attach_request_recorder(await b.ensure_page())
        api = FomoApi(page_fetcher(b.get_page))
        try:
            res = await prove(b, api, db, handle, mint)
        finally:
            await api.close()
    finally:
        await b.close()
        await db.stop()
    for k, v in res.items():
        print(f"{k}: {v}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:])))
