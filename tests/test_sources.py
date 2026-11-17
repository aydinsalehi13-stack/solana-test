import asyncio
import json
import time

import httpx
import pytest

import feed as fd
import sources as sc
from bus import Hub


class Clock:
    def __init__(self):
        self.t = 100.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.sleeps.append(d)
        self.t += d


def pair(mint, liq, dex="pumpswap", **kw):
    p = {"dexId": dex, "pairAddress": f"pair-{mint}-{liq}", "baseToken": {"address": mint}, "priceUsd": "0.0012",
         "liquidity": {"usd": liq}, "volume": {"m5": 100, "h1": 5000}, "txns": {"m5": {"buys": 9, "sells": 3}, "h1": {"buys": 80, "sells": 50}},
         "priceChange": {"m5": 1.5, "h1": 40}, "marketCap": 90000, "pairCreatedAt": 1_700_000_000_000}
    if liq is None:
        del p["liquidity"]
    p.update(kw)
    return p


def clients(handler):
    return sc.Clients(transport=httpx.MockTransport(handler))


def test_ttl_cache_expires():
    clk = Clock()
    c = sc.TTLCache(3, clk)
    c.put("a", 1)
    assert c.get("a") == 1
    clk.t += 3.1
    assert c.get("a") is None


async def test_rate_limiter_spaces_calls():
    clk = Clock()
    rl = sc.RateLimiter.per_minute(250, clock=clk, sleep=clk.sleep)
    for _ in range(3):
        await rl.wait()
    assert clk.sleeps == [pytest.approx(0.24), pytest.approx(0.24)]


async def test_get_json_retries_5xx_with_backoff_then_succeeds():
    seq = [500, 502, 200]
    clk = Clock()

    def h(req):
        s = seq.pop(0)
        return httpx.Response(s, json={"ok": 1}) if s == 200 else httpx.Response(s)

    assert await sc.get_json(clients(h), "https://x.test/a", sleep=clk.sleep) == {"ok": 1}
    assert clk.sleeps == [0.5, 1.0]


async def test_get_json_429_raises_rate_limited_with_retry_after():
    cl = clients(lambda r: httpx.Response(429, headers={"retry-after": "7"}))
    with pytest.raises(sc.RateLimited) as e:
        await sc.get_json(cl, "https://x.test/a")
    assert e.value.retry_after == 7.0


async def test_get_json_4xx_is_not_retried_and_network_errors_are():
    calls = []

    def h404(req):
        calls.append(1)
        return httpx.Response(404)

    with pytest.raises(sc.SourceError):
        await sc.get_json(clients(h404), "https://x.test/a", sleep=Clock().sleep)
    assert len(calls) == 1
    n = []

    def boom(req):
        n.append(1)
        raise httpx.ConnectTimeout("t")

    with pytest.raises(sc.SourceError, match="ConnectTimeout"):
        await sc.get_json(clients(boom), "https://x.test/a", sleep=Clock().sleep)
    assert len(n) == 3


async def test_one_client_per_host():
    c = sc.Clients(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert c.get("https://a.test/x") is c.get("https://a.test/y") and c.get("https://b.test/") is not c.get("https://a.test/")
    await c.aclose()


def test_parse_pairs_picks_pumpswap_with_most_liquidity_and_ignores_other_dexes():
    got = sc.parse_pairs([pair("M1", 1000), pair("M1", 25000), pair("M1", 99999, dex="raydium"), pair("M2", 5)])
    assert got["M1"]["liquidity_usd"] == 25000 and got["M1"]["pair"] == "pair-M1-25000"
    assert got["M1"]["price_usd"] == 0.0012 and got["M1"]["buys_m5"] == 9 and got["M1"]["pair_created_at"] == 1_700_000_000.0
    assert "M3" not in got  # unknown to DexScreener = absent, never a guess


def test_parse_pairs_separates_unindexed_liquidity_from_real_zero():
    got = sc.parse_pairs([pair("NOLIQ", None), pair("ZERO", 0)])
    assert got["NOLIQ"]["liquidity_known"] is False and got["NOLIQ"]["liquidity_usd"] is None
    assert got["ZERO"]["liquidity_known"] is True and got["ZERO"]["liquidity_usd"] == 0.0


async def test_dexscreener_batches_30_per_call_and_caches_3s():
    urls = []
    mints = [f"M{i}" for i in range(31)]

    def h(req):
        urls.append(str(req.url))
        asked = str(req.url).rsplit("/", 1)[1].split(",")
        return httpx.Response(200, json=[pair(m, 20000) for m in asked if m != "M5"])

    clk = Clock()
    dex = sc.DexScreener(clients(h), sleep=clk.sleep, clock=clk)
    out = await dex.snapshots(mints)
    assert len(urls) == 2 and len(urls[0].rsplit("/", 1)[1].split(",")) == 30
    assert len(out) == 30 and "M5" not in out
    await dex.snapshots(mints)
    assert len(urls) == 2  # 3 s cache, including the 'unknown' answer for M5
    clk.t += 3.5
    await dex.snapshots(["M0"])
    assert len(urls) == 3


async def test_dexscreener_429_waits_30s_before_next_call():
    seq = [429, 200]

    def h(req):
        s = seq.pop(0)
        return httpx.Response(429) if s == 429 else httpx.Response(200, json=[pair("M1", 1)])

    clk = Clock()
    dex = sc.DexScreener(clients(h), sleep=clk.sleep, clock=clk)
    with pytest.raises(sc.RateLimited):
        await dex.snapshots(["M1"])
    out = await dex.snapshots(["M1"])
    assert 30.0 in [round(x, 1) for x in clk.sleeps] and "M1" in out


GECKO = {"data": [
    {"attributes": {"address": "PoolX", "name": "PAWS / SOL", "pool_created_at": "2026-11-17T10:00:00Z"},
     "relationships": {"base_token": {"data": {"id": "solana_MintAAA"}}, "dex": {"data": {"id": "pumpswap"}}}},
    {"attributes": {"address": "PoolY"}, "relationships": {}}]}


def test_parse_gecko_new_pools_skips_incomplete_items():
    assert sc.parse_gecko_new_pools(GECKO) == [{"mint": "MintAAA", "pool": "PoolX", "name": "PAWS", "dex": "pumpswap",
                                                "created_at": "2026-11-17T10:00:00Z"}]
    assert sc.parse_gecko_new_pools(None) == [] and sc.parse_gecko_new_pools({"data": "x"}) == []


# ---- badge and fallbacks ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("pp,g,s,badge", [(True, None, None, "PumpPortal live"), (True, 1, 1, "PumpPortal live"),
                                          (False, 10, 10, "GeckoTerminal (20-50 s late)"), (False, 90, 10, "DexScreener snapshots"),
                                          (False, None, 5, "DexScreener snapshots"), (False, None, None, "feed down"),
                                          (False, 90, 90, "feed down")])
def test_badge_priority(pp, g, s, badge):
    assert fd.compute_badge(pp, g, s) == badge


def make_fallbacks(handler, hub=None, **kw):
    hub = hub or Hub()
    cl = clients(handler)
    feed = fd.PumpPortalFeed(hub)
    return hub, feed, fd.Fallbacks(hub, feed, sc.DexScreener(cl), sc.GeckoTerminal(cl, sleep=lambda d: asyncio.sleep(0)), **kw)


async def test_snapshot_loop_publishes_watched_mints_and_sets_badge():
    hub, feed, fb = make_fallbacks(lambda r: httpx.Response(200, json=[pair("M1", 30000)]), snapshot_every=0.05)
    fb.set_watch(["M1"])
    t = asyncio.create_task(fb.snapshot_loop())
    await asyncio.sleep(0.3)
    await stop(t)
    snap = hub.bus.recent(topic="snapshot")[0]["data"]
    assert snap["mint"] == "M1" and snap["liquidity_usd"] == 30000
    assert fb.badge() == "DexScreener snapshots" and hub.heartbeats.age("snapshots") is not None


async def test_gecko_runs_only_after_pumpportal_is_down_past_grace_and_marks_late():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(200, json=GECKO)

    hub, feed, fb = make_fallbacks(h, gecko_grace=0.15, gecko_every=0.05, gecko_idle=0.03)
    t = asyncio.create_task(fb.gecko_loop())
    await asyncio.sleep(0.08)
    assert calls == []  # still inside the grace period
    await asyncio.sleep(0.5)
    await stop(t)
    assert calls
    evs = hub.bus.recent(topic="new_token")
    assert len(evs) == 1 and evs[0]["data"]["late"] is True and evs[0]["data"]["mint"] == "MintAAA"  # deduplicated
    assert fb.badge() == "GeckoTerminal (20-50 s late)"


async def test_gecko_is_silent_while_pumpportal_is_healthy():
    calls = []
    hub, feed, fb = make_fallbacks(lambda r: calls.append(1) or httpx.Response(200, json=GECKO), gecko_grace=0.0, gecko_every=0.05, gecko_idle=0.03)
    feed.state, feed.last_msg = "live", time.time()
    t = asyncio.create_task(fb.gecko_loop())
    await asyncio.sleep(0.4)
    await stop(t)
    assert calls == [] and fb.badge() == "PumpPortal live"


async def test_health_endpoint_includes_feed_status():
    import httpx as hx
    from api import create_app
    from db import Database

    import tempfile, pathlib

    d = Database(pathlib.Path(tempfile.mkdtemp()) / "x.db")
    await d.start()
    hub = Hub()
    hub.status["feed"] = lambda: {"badge": "feed down"}
    app = create_app(d, hub, "tok-1234567890")
    async with hx.AsyncClient(transport=hx.ASGITransport(app=app), base_url="http://t") as c:
        j = (await c.get("/api/health", headers={"Authorization": "Bearer tok-1234567890"})).json()
    assert j["feed"] == {"badge": "feed down"}
    await d.stop()


async def stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
