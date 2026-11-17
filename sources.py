"""Data sources: one shared HTTP/2 keep-alive client per host, TTL caches, rate limiters, retries with backoff.

Phase 3 holds DexScreener and GeckoTerminal. RPC, RugCheck and Jupiter join in Phase 4. GET only, no wallets.
Field names below follow the public docs of each API and are not yet checked against live responses from
the owner's machine; a missing field is None, never a guess (hard rule 9).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("desk.sources")

DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
DEX_BATCH = 30  # mints per DexScreener call


class RateLimited(Exception):
    def __init__(self, msg: str = "429", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class SourceError(Exception):
    pass


class TTLCache:
    def __init__(self, ttl: float, clock=time.monotonic, maxsize: int = 5000):
        self.ttl, self._clock, self.maxsize = ttl, clock, maxsize
        self._d: dict = {}

    def get(self, key):
        item = self._d.get(key)
        if item is None:
            return None
        if self._clock() - item[0] > self.ttl:
            del self._d[key]
            return None
        return item[1]

    def put(self, key, value) -> None:
        if len(self._d) >= self.maxsize:
            self._d.pop(next(iter(self._d)))
        self._d[key] = (self._clock(), value)


class RateLimiter:
    """At most one call per `min_gap` seconds (e.g. 250/min -> 0.24 s). Waiters are served in order."""

    def __init__(self, min_gap: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.min_gap, self._clock, self._sleep = min_gap, clock, sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def per_minute(cls, n: int, **kw) -> "RateLimiter":
        return cls(60.0 / n, **kw)

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            delay = self._next - now
            if delay > 0:
                await self._sleep(delay)
            self._next = max(now, self._next) + self.min_gap


class Clients:
    """One shared keep-alive client per host (HTTP/2 when talking to the real internet)."""

    def __init__(self, transport=None, timeout: float = 10.0):
        self._transport, self._timeout = transport, timeout
        self._c: dict[str, httpx.AsyncClient] = {}

    def get(self, url: str) -> httpx.AsyncClient:
        host = urlparse(url).netloc
        if host not in self._c:
            kw = {"transport": self._transport} if self._transport else {"http2": True}
            self._c[host] = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False,
                                              limits=httpx.Limits(max_keepalive_connections=8), **kw)
        return self._c[host]

    async def aclose(self) -> None:
        await asyncio.gather(*(c.aclose() for c in self._c.values()), return_exceptions=True)
        self._c.clear()


async def get_json(clients: Clients, url: str, *, params=None, headers=None, retries: int = 3,
                   sleep=asyncio.sleep, limiter: RateLimiter | None = None, sem: asyncio.Semaphore | None = None):
    """Timeout + retry with backoff on network errors and 5xx. 429 raises RateLimited (the caller owns that wait)."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            if limiter:
                await limiter.wait()
            async with sem if sem else _null():
                r = await clients.get(url).get(url, params=params, headers=headers)
            if r.status_code == 429:
                ra = r.headers.get("retry-after")
                raise RateLimited("429", float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
            if r.status_code >= 500:
                raise SourceError(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise SourceError(f"HTTP {r.status_code} (not retried)")
            if len(r.content) > 20000:
                return await asyncio.to_thread(json.loads, r.content)
            return r.json()
        except RateLimited:
            raise
        except (httpx.TransportError, SourceError) as e:
            last = e
            if isinstance(e, SourceError) and "not retried" in str(e):
                raise
            if attempt < retries - 1:
                await sleep(0.5 * 2 ** attempt)
    raise SourceError(f"{urlparse(url).netloc}: {type(last).__name__}: {last}")


class _null:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def num(v):
    """Float or None. DexScreener sends some numbers as strings."""
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def parse_pairs(pairs, now: float | None = None) -> dict[str, dict]:
    """mint -> snapshot of the PUMPSWAP pair with the most liquidity. A mint DexScreener does not know is absent.

    liquidity_known tells 'DexScreener sent no liquidity at all' (a pair not indexed yet) apart from a real $0,
    which is the open 'liq $0.0K' question for Phase 4.
    """
    now = time.time() if now is None else now
    best: dict[str, tuple[float, dict]] = {}
    for p in pairs or []:
        if not isinstance(p, dict) or p.get("dexId") != "pumpswap":
            continue
        mint = dig(p, "baseToken", "address")
        if not mint:
            continue
        liq = num(dig(p, "liquidity", "usd"))
        rank = -1.0 if liq is None else liq
        snap = {
            "mint": mint, "pair": p.get("pairAddress"), "dex": p.get("dexId"), "ts": now,
            "price_usd": num(p.get("priceUsd")), "liquidity_usd": liq, "liquidity_known": liq is not None,
            "mcap": num(p.get("marketCap")) if p.get("marketCap") is not None else num(p.get("fdv")),
            "volume_m5": num(dig(p, "volume", "m5")), "volume_h1": num(dig(p, "volume", "h1")),
            "buys_m5": dig(p, "txns", "m5", "buys"), "sells_m5": dig(p, "txns", "m5", "sells"),
            "buys_h1": dig(p, "txns", "h1", "buys"), "sells_h1": dig(p, "txns", "h1", "sells"),
            "change_m5": num(dig(p, "priceChange", "m5")), "change_h1": num(dig(p, "priceChange", "h1")),
            "change_h24": num(dig(p, "priceChange", "h24")),
            "pair_created_at": (dig(p, "pairCreatedAt") / 1000.0) if isinstance(p.get("pairCreatedAt"), (int, float)) else None,
        }
        if mint not in best or rank > best[mint][0]:
            best[mint] = (rank, snap)
    return {m: s for m, (_, s) in best.items()}


class DexScreener:
    """Batched token snapshots (30 mints per call), 3 s cache, at most 250 calls/min, 429 -> wait 30 s."""

    def __init__(self, clients: Clients, sleep=asyncio.sleep, clock=time.monotonic, cache_ttl: float = 3.0,
                 per_minute: int = 250, concurrency: int = 4):
        self.clients, self._sleep, self._clock = clients, sleep, clock
        self.cache = TTLCache(cache_ttl, clock)
        self.limiter = RateLimiter.per_minute(per_minute, clock=clock, sleep=sleep)
        self.sem = asyncio.Semaphore(concurrency)
        self.blocked_until = 0.0
        self.calls = 0
        self.last_ok: float | None = None

    async def snapshots(self, mints: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        todo = []
        for m in dict.fromkeys(mints):
            hit = self.cache.get(m)
            if hit is not None:
                if hit != "unknown":
                    out[m] = hit
            else:
                todo.append(m)
        for i in range(0, len(todo), DEX_BATCH):
            batch = todo[i:i + DEX_BATCH]
            wait = self.blocked_until - self._clock()
            if wait > 0:
                await self._sleep(wait)
            try:
                self.calls += 1
                data = await get_json(self.clients, DEXSCREENER_TOKENS.format(mints=",".join(batch)),
                                      sleep=self._sleep, limiter=self.limiter, sem=self.sem)
            except RateLimited:
                self.blocked_until = self._clock() + 30.0
                log.warning("DexScreener 429: waiting 30 s")
                raise
            self.last_ok = self._clock()
            found = parse_pairs(data if isinstance(data, list) else data.get("pairs") if isinstance(data, dict) else [])
            for m in batch:
                self.cache.put(m, found.get(m, "unknown"))
                if m in found:
                    out[m] = found[m]
        return out


def parse_gecko_new_pools(payload) -> list[dict]:
    """[{mint, pool, name, dex, created_at}] from GeckoTerminal new_pools (JSON:API). Missing parts are None."""
    out = []
    for item in (payload or {}).get("data", []) if isinstance(payload, dict) else []:
        base = dig(item, "relationships", "base_token", "data", "id")
        if not isinstance(base, str) or "_" not in base:
            continue
        a = item.get("attributes") or {}
        out.append({"mint": base.split("_", 1)[1], "pool": a.get("address"),
                    "name": (a.get("name") or "").split(" / ")[0] or None,
                    "dex": dig(item, "relationships", "dex", "data", "id"), "created_at": a.get("pool_created_at")})
    return out


class GeckoTerminal:
    """Last resort: new pools, 20-50 s late, about 10 calls/min."""

    def __init__(self, clients: Clients, sleep=asyncio.sleep, clock=time.monotonic):
        self.clients, self._sleep, self._clock = clients, sleep, clock
        self.limiter = RateLimiter.per_minute(10, clock=clock, sleep=sleep)
        self.blocked_until = 0.0
        self.last_ok: float | None = None

    async def new_pools(self) -> list[dict]:
        wait = self.blocked_until - self._clock()
        if wait > 0:
            await self._sleep(wait)
        try:
            data = await get_json(self.clients, GECKO_NEW_POOLS, params={"page": 1},
                                  headers={"Accept": "application/json;version=20230302"},
                                  sleep=self._sleep, limiter=self.limiter)
        except RateLimited as e:
            self.blocked_until = self._clock() + (e.retry_after or 30.0)
            raise
        self.last_ok = self._clock()
        return parse_gecko_new_pools(data)
