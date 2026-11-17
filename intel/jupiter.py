"""Jupiter round-trip check (keyless lite API, GET only). Spec section 2.

Before any paper entry: quote the real ticket SOL -> token, then that token amount back -> SOL. A round-trip loss
over the owner's limit, or a sell impact over the limit, means skip. The loss also becomes extra cost on each paper
leg when the real route is worse than our fill model (never better). No wallet, taker or account field is ever sent.
"""
from __future__ import annotations

import asyncio
import time

import sources

BASE = "https://lite-api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"
FORBIDDEN_PARAMS = {"taker", "userpublickey", "wallet", "payer", "destinationtokenaccount", "feeaccount", "owner", "signer"}


def clean_params(params: dict) -> dict:
    bad = [k for k in params if k.lower() in FORBIDDEN_PARAMS]
    if bad:
        raise ValueError(f"Jupiter calls never carry wallet/taker fields: {bad}")
    return params


class Jupiter:
    def __init__(self, clients: sources.Clients, sleep=asyncio.sleep, clock=time.monotonic):
        self.clients, self._sleep, self._clock = clients, sleep, clock
        self.gap = 1.0  # about 1 call/s
        self._next = 0.0
        self.calls = 0

    async def _get(self, path: str, params: dict):
        clean_params(params)
        for attempt in range(3):
            wait = self._next - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next = self._clock() + self.gap
            self.calls += 1
            try:
                return await sources.get_json(self.clients, BASE + path, params=params, retries=1, sleep=self._sleep)
            except sources.RateLimited:
                self.gap = min(max(self.gap * 1.5, 1.0), 6.0)  # 429 -> backoff min(max(gap*1.5,1),6)
                self._next = self._clock() + self.gap
        raise sources.RateLimited("jupiter 429 after retries")

    async def quote(self, in_mint: str, out_mint: str, amount: int) -> dict:
        params = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(int(amount)), "slippageBps": "300"}
        try:
            return await self._get("/swap/v1/quote", params)
        except sources.SourceError:
            return await self._get("/swap/v2/order", params)  # fallback, still no taker

    async def round_trip(self, mint: str, usd: float, sol_usd: float) -> dict:
        """{'rt_loss_pct', 'sell_impact_pct', 'tokens_raw'}. Raises SourceError when the route cannot be quoted."""
        lamports = int(usd / sol_usd * 1e9)
        buy = await self.quote(SOL_MINT, mint, lamports)
        out_tokens = int(buy.get("outAmount") or 0)
        if out_tokens <= 0:
            raise sources.SourceError("jupiter: no buy route")
        sell = await self.quote(mint, SOL_MINT, out_tokens)
        back = int(sell.get("outAmount") or 0)
        if back <= 0:
            raise sources.SourceError("jupiter: no sell route")
        impact = sources.num(sell.get("priceImpactPct"))
        return {"rt_loss_pct": 100.0 * (1 - back / lamports),
                # priceImpactPct is read as a fraction (x100). If the API already sends percent this is stricter, not looser.
                "sell_impact_pct": None if impact is None else abs(impact) * 100.0, "tokens_raw": out_tokens}
