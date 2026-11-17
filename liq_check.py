"""Phase 4 investigation, for YOUR laptop: why did 136 of 322 kills say 'liq $0.0K'?

  python liq_check.py --spot 10       raw DexScreener answers for the 10 most recent coins the Scanner saw
  python liq_check.py <mint> [...]    the same for specific mints
  python liq_check.py --watch <mint>  poll every 15 s for 10 min: when does liquidity appear?

For each coin it saves the full raw DexScreener JSON (ALL pairs, every dex) to logs/raw/liq_spot/ and prints what
our parser made of it: which pairs exist, which dex, the liquidity key (absent vs a real 0), the pair age.
That separates the three suspects: DexScreener lag on new pairs, the wrong pair picked, or a mapping bug.
Nothing here changes any threshold.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time

import config
import sources


def recent_mints(n: int) -> list[str]:
    if not config.DB_PATH.exists():
        return []
    con = sqlite3.connect(f"{config.DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    rows = con.execute("SELECT DISTINCT mint FROM scans WHERE reason LIKE 'liq%' ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if len(rows) < n:
        rows += con.execute("SELECT DISTINCT mint FROM scans ORDER BY id DESC LIMIT ?", (n - len(rows),)).fetchall()
    con.close()
    return [r[0] for r in rows]


def describe(mint: str, raw) -> dict:
    pairs = raw if isinstance(raw, list) else (raw or {}).get("pairs") or []
    mine = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
    parsed = sources.parse_pairs(pairs).get(mint)
    return {"mint": mint[:6] + "..." + mint[-4:], "pairs": len(mine),
            "dexes": sorted({p.get("dexId") for p in mine if p.get("dexId")}),
            "liquidity_keys": [("absent" if "liquidity" not in p else p["liquidity"].get("usd")) for p in mine],
            "pair_age_min": [round((time.time() * 1000 - p["pairCreatedAt"]) / 60000, 1) for p in mine if p.get("pairCreatedAt")],
            "parsed_liquidity": None if parsed is None else parsed["liquidity_usd"],
            "parsed_known": None if parsed is None else parsed["liquidity_known"]}


async def fetch(clients, mint):
    return await sources.get_json(clients, sources.DEXSCREENER_TOKENS.format(mints=mint))


async def amain(argv: list[str]) -> int:
    config.load_env()
    clients = sources.Clients()
    out_dir = config.LOG_DIR / "raw" / "liq_spot"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if "--watch" in argv:
            mint = argv[argv.index("--watch") + 1]
            for i in range(40):
                d = describe(mint, await fetch(clients, mint))
                print(f"[{i * 15:4d}s] pairs={d['pairs']} liquidity={d['liquidity_keys']} parsed={d['parsed_liquidity']}")
                await asyncio.sleep(15)
            return 0
        mints = recent_mints(int(argv[argv.index("--spot") + 1])) if "--spot" in argv else [a for a in argv if not a.startswith("--")]
        if not mints:
            print("no mints: run the desk for a while first, or pass mints")
            return 1
        for m in mints:
            try:
                raw = await fetch(clients, m)
            except Exception as e:  # noqa: BLE001
                print(f"{m[:6]}...: fetch failed: {e}")
                continue
            (out_dir / f"{m}.json").write_text(json.dumps(raw, indent=1))
            print(describe(m, raw))
    finally:
        await clients.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:])))
