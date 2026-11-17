"""Data sources: one shared HTTP/2 keep-alive client per host, TTL caches, rate limiters, retries with backoff.

Phase 3 holds DexScreener and GeckoTerminal. RPC, RugCheck and Jupiter join in Phase 4. GET only, no wallets.
Field names below follow the public docs of each API and are not yet checked against live responses from
the owner's machine; a missing field is None, never a guess (hard rule 9).
"""
from __future__ import annotations

import asyncio
import base64
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


def parse_pairs(pairs, now: float | None = None, dexes: tuple = ("pumpswap",)) -> dict[str, dict]:
    """mint -> snapshot of the PUMPSWAP pair with the most liquidity. A mint DexScreener does not know is absent.

    liquidity_known tells 'DexScreener sent no liquidity at all' (a pair not indexed yet) apart from a real $0,
    which is the open 'liq $0.0K' question for Phase 4.
    """
    now = time.time() if now is None else now
    best: dict[str, tuple[float, dict]] = {}
    for p in pairs or []:
        if not isinstance(p, dict) or p.get("dexId") not in dexes:
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

    async def snapshots(self, mints: list[str], dexes: tuple = ("pumpswap",)) -> dict[str, dict]:
        out: dict[str, dict] = {}
        todo = []
        for m in dict.fromkeys(mints):
            hit = self.cache.get((m, dexes))
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
            found = parse_pairs(data if isinstance(data, list) else data.get("pairs") if isinstance(data, dict) else [], dexes=dexes)
            for m in batch:
                self.cache.put((m, dexes), found.get(m, "unknown"))
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


# ======================================================================================================
# Phase 4 adapters: Solana RPC, RugCheck, Coinbase. Any failure raises SourceError / RateLimited, which the
# Analyst turns into "no data: ..." (red), never into a pass.
# ======================================================================================================
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
RUGCHECK = "https://api.rugcheck.xyz"
COINBASE_SOL = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
# Helius free plan credit cost; everything else counts 1
RPC_COST = {"getProgramAccounts": 10}


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


class Rpc:
    """Solana JSON-RPC. Uses HELIUS_API_KEY when given (the URL is never logged), else the public endpoint."""

    def __init__(self, clients: Clients, url: str | None = None, helius_key: str | None = None, sleep=asyncio.sleep,
                 clock=time.monotonic):
        self.clients, self._sleep, self._clock = clients, sleep, clock
        self._url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}" if helius_key else (url or PUBLIC_RPC)
        self.label = "helius" if helius_key else "public"
        self.sem = asyncio.Semaphore(8)
        self.authority_cache = TTLCache(600, clock)
        self.holders_cache = TTLCache(60, clock)
        self.history_cache = TTLCache(3600, clock)
        self.credits: dict[str, int] = {}
        self.calls = 0
        self._id = 0

    def credits_today(self) -> int:
        return self.credits.get(time.strftime("%Y-%m-%d"), 0)

    async def call(self, method: str, params: list, retries: int = 1):
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        day = time.strftime("%Y-%m-%d")
        self.credits[day] = self.credits.get(day, 0) + RPC_COST.get(method, 1)
        self.calls += 1
        last = None
        for attempt in range(retries):
            try:
                async with self.sem:
                    r = await self.clients.get(self._url).post(self._url, json=body)
                if r.status_code == 429:
                    raise RateLimited("rpc 429")
                if r.status_code != 200:
                    raise SourceError(f"rpc HTTP {r.status_code}")
                j = r.json()
                if "error" in j:
                    raise SourceError(f"rpc error {j['error'].get('code')}: {str(j['error'].get('message'))[:80]}")
                return j.get("result")
            except (httpx.TransportError, ValueError) as e:
                last = SourceError(f"rpc {type(e).__name__}")
                if attempt < retries - 1:
                    await self._sleep(0.5 * 2 ** attempt)
        raise last or SourceError("rpc failed")

    async def mint_info(self, mint: str) -> dict:
        """mint/freeze authority (None = revoked), supply (UI units) and decimals. 10 min cache."""
        hit = self.authority_cache.get(mint)
        if hit:
            return hit
        res = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        info = dig(res, "value", "data", "parsed", "info")
        if not isinstance(info, dict) or "mintAuthority" not in info and "freezeAuthority" not in info:
            raise SourceError("no mint data")
        dec = int(info.get("decimals", 0))
        out = {"mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"),
               "supply": float(info.get("supply", 0)) / 10 ** dec if info.get("supply") is not None else None, "decimals": dec}
        self.authority_cache.put(mint, out)
        return out

    async def largest_accounts(self, mint: str) -> list[tuple[str, float]]:
        res = await self.call("getTokenLargestAccounts", [mint])
        return [(v["address"], float(v.get("uiAmount") or 0)) for v in (dig(res, "value") or [])]

    async def owners_of(self, token_accounts: list[str]) -> dict[str, str | None]:
        res = await self.call("getMultipleAccounts", [token_accounts, {"encoding": "jsonParsed"}])
        out = {}
        for addr, v in zip(token_accounts, dig(res, "value") or []):
            out[addr] = dig(v, "data", "parsed", "info", "owner") if v else None
        return out

    async def top10_pct(self, mint: str, exclude_owners: set[str]) -> float | None:
        """Share of supply held by the 10 biggest holders, with the pool and the bonding curve excluded."""
        hit = self.holders_cache.get(("top10", mint, tuple(sorted(exclude_owners))))
        if hit is not None:
            return hit
        info = await self.mint_info(mint)
        if not info["supply"]:
            return None
        accts = await self.largest_accounts(mint)
        owners = await self.owners_of([a for a, _ in accts]) if accts else {}
        kept = sorted((amt for a, amt in accts if owners.get(a) not in exclude_owners), reverse=True)[:10]
        pct = 100.0 * sum(kept) / info["supply"]
        self.holders_cache.put(("top10", mint, tuple(sorted(exclude_owners))), pct)
        return pct

    async def token_balance(self, owner: str, mint: str) -> float | None:
        res = await self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        vals = dig(res, "value")
        if vals is None:
            return None
        return sum(float(dig(v, "account", "data", "parsed", "info", "tokenAmount", "uiAmount") or 0) for v in vals)

    async def delegations(self, mint: str, decimals: int) -> dict:
        """Token accounts of the mint on both token programs: owner, amount, delegate (via dataSlice 32..108)."""
        hit = self.holders_cache.get(("deleg", mint))
        if hit is not None:
            return hit
        accounts = []
        for program, with_size in ((TOKEN_PROGRAM, True), (TOKEN_2022_PROGRAM, False)):
            filters = [{"memcmp": {"offset": 0, "bytes": mint}}] + ([{"dataSize": 165}] if with_size else [])
            res = await self.call("getProgramAccounts", [program, {"encoding": "base64", "filters": filters,
                                                                  "dataSlice": {"offset": 32, "length": 76}}])
            for item in res or []:
                raw = base64.b64decode(dig(item, "account", "data")[0])
                if len(raw) < 76:
                    continue
                amount = int.from_bytes(raw[32:40], "little") / 10 ** decimals
                has_delegate = int.from_bytes(raw[40:44], "little") == 1
                accounts.append({"owner": b58encode(raw[0:32]), "amount": amount,
                                 "delegate": b58encode(raw[44:76]) if has_delegate else None})
        by_delegate: dict[str, int] = {}
        for a in accounts:
            if a["delegate"]:
                by_delegate[a["delegate"]] = by_delegate.get(a["delegate"], 0) + 1
        out = {"accounts": len(accounts), "by_delegate": by_delegate,
               "top_holders": sorted(((a["owner"], a["amount"]) for a in accounts), key=lambda x: -x[1])[:20]}
        self.holders_cache.put(("deleg", mint), out)
        return out

    async def signature_count(self, owner: str, limit: int = 20) -> int:
        res = await self.call("getSignaturesForAddress", [owner, {"limit": limit}])
        return len(res or [])


def lp_for_pool(report: dict | None, pool: str | None) -> dict | None:
    """RugCheck market whose pubkey is the pool Scanner saw: {locked_pct} or None when there is no such market."""
    for m in (report or {}).get("markets") or []:
        if pool and m.get("pubkey") == pool:
            return {"locked_pct": num((m.get("lp") or {}).get("lpLockedPct"))}
    return None


def insiders_pct(report: dict | None) -> float | None:
    """Share of supply held by RugCheck's insider networks, or None when the report cannot say."""
    if not isinstance(report, dict) or "insiderNetworks" not in report:
        return None
    nets = report.get("insiderNetworks")
    if nets is None:
        return 0.0
    supply = num(dig(report, "token", "supply"))
    if not supply:
        return None
    return 100.0 * sum(num(n.get("tokenAmount")) or 0 for n in nets) / supply


def biggest_bundle(graph: dict | None) -> int | None:
    """Size of the biggest group of holders joined by funding edges. None when the graph has no edges key."""
    if not isinstance(graph, dict):
        return None
    key = "links" if "links" in graph else "edges" if "edges" in graph else None
    if key is None:
        return None
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in graph[key] or []:
        a, b = e.get("source"), e.get("target")
        if a is not None and b is not None:
            parent[find(str(a))] = find(str(b))
    sizes: dict[str, int] = {}
    for n in list(parent):
        sizes[find(n)] = sizes.get(find(n), 0) + 1
    return max(sizes.values(), default=1 if graph.get("nodes") else 0)


class RugCheck:
    """RugCheck report (2 min cache), insiders graph, recent rugs. Concurrency 3."""

    def __init__(self, clients: Clients, sleep=asyncio.sleep, clock=time.monotonic):
        self.clients, self._sleep = clients, sleep
        self.sem = asyncio.Semaphore(3)
        self.cache = TTLCache(120, clock)

    async def report(self, mint: str) -> dict:
        hit = self.cache.get(("r", mint))
        if hit:
            return hit
        data = await get_json(self.clients, f"{RUGCHECK}/v1/tokens/{mint}/report", sleep=self._sleep, sem=self.sem)
        if not isinstance(data, dict):
            raise SourceError("rugcheck: bad report")
        self.cache.put(("r", mint), data)
        return data

    async def insiders_graph(self, mint: str) -> dict:
        hit = self.cache.get(("g", mint))
        if hit:
            return hit
        data = await get_json(self.clients, f"{RUGCHECK}/v1/tokens/{mint}/insiders/graph", sleep=self._sleep, sem=self.sem)
        self.cache.put(("g", mint), data)
        return data

    async def recent_rugs(self, limit: int = 50) -> list:
        data = await get_json(self.clients, f"{RUGCHECK}/v1/stats/rugs/ticker", params={"limit": limit},
                              sleep=self._sleep, sem=self.sem)
        return data if isinstance(data, list) else []


class Coinbase:
    """SOL-USD spot every 30 s."""

    def __init__(self, clients: Clients, clock=time.monotonic):
        self.clients = clients
        self.cache = TTLCache(30, clock)

    async def spot(self) -> float:
        hit = self.cache.get("sol")
        if hit:
            return hit
        data = await get_json(self.clients, COINBASE_SOL)
        v = num(dig(data, "data", "amount"))
        if not v or v <= 0:
            raise SourceError("coinbase: no SOL price")
        self.cache.put("sol", v)
        return v
