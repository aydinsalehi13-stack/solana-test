"""Layer-8 proof: does this RPC allow getProgramAccounts on a graduated mint? (spec Phase 2)

  python rpc_probe.py <mint>

Counts the token accounts of the mint on the Token and Token-2022 programs, trying (in order) Helius if
HELIUS_API_KEY is set, then the public endpoints. RPC URLs that contain a key are never printed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

import config

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
REFUSED_MSG = "Analyst layer 8 turns every coin red until rpc_url allows getProgramAccounts"


def endpoints(environ: dict | None = None) -> list[tuple[str, str]]:
    env = os.environ if environ is None else environ
    out = []
    if env.get("HELIUS_API_KEY"):
        out.append(("helius", f"https://mainnet.helius-rpc.com/?api-key={env['HELIUS_API_KEY']}"))
    out += [("solana-mainnet-beta", "https://api.mainnet-beta.solana.com"),
            ("publicnode", "https://solana-rpc.publicnode.com")]
    return out


def gpa_payload(program: str, mint: str, with_size: bool) -> dict:
    filters = [{"memcmp": {"offset": 0, "bytes": mint}}]
    if with_size:
        filters.insert(0, {"dataSize": 165})
    return {"jsonrpc": "2.0", "id": 1, "method": "getProgramAccounts",
            "params": [program, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}, "filters": filters}]}


async def count_accounts(client: httpx.AsyncClient, url: str, program: str, mint: str, with_size: bool):
    r = await client.post(url, json=gpa_payload(program, mint, with_size), timeout=30)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    j = r.json()
    if "error" in j:
        return None, f"RPC error {j['error'].get('code')}: {str(j['error'].get('message'))[:80]}"
    return len(j.get("result") or []), None


async def probe(mint: str, eps=None, client: httpx.AsyncClient | None = None) -> dict:
    own = client is None
    client = client or httpx.AsyncClient(http2=True)
    results = []
    try:
        for label, url in eps or endpoints():
            t0 = time.time()
            try:
                tok, e1 = await count_accounts(client, url, TOKEN_PROGRAM, mint, True)
                t22, e2 = await count_accounts(client, url, TOKEN_2022_PROGRAM, mint, False)
            except Exception as e:  # noqa: BLE001
                tok, t22, e1, e2 = None, None, type(e).__name__, None
            ok = tok is not None
            results.append({"endpoint": label, "ok": ok, "token_accounts": tok, "token2022_accounts": t22,
                            "error": e1 or e2, "seconds": round(time.time() - t0, 2)})
            if ok:
                break
    finally:
        if own:
            await client.aclose()
    allowed = any(r["ok"] for r in results)
    return {"allowed": allowed, "results": results, "verdict": "getProgramAccounts allowed" if allowed else REFUSED_MSG}


async def amain(argv: list[str]) -> int:
    config.load_env()
    if not argv:
        print("usage: python rpc_probe.py <graduated mint>")
        return 2
    res = await probe(argv[0])
    for r in res["results"]:
        print(r)
    print(res["verdict"])
    if not res["allowed"]:
        print("Ask the owner for a free Helius key (HELIUS_API_KEY in .env).")
    return 0 if res["allowed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:])))
