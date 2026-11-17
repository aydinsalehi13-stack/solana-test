import asyncio
import json
from http import HTTPStatus

import pytest
from websockets.asyncio.server import serve

import feed as fd
import logsetup
import osutil
from bus import Hub
from db import Database

CREATE = {"txType": "create", "mint": "MintAAA", "name": "Paws", "symbol": "PAWS", "traderPublicKey": "Dev111",
          "pool": "pump", "vSolInBondingCurve": 30.1, "marketCapSol": 28.0}
MIGRATE = {"txType": "migrate", "mint": "MintAAA", "pool": "pump-amm", "signature": "sig1"}


class Server:
    def __init__(self, on_connect=None, process_request=None):
        self.conns, self.received, self.paths = 0, [], []
        self._on, self._pr, self.srv = on_connect, process_request, None

    async def _handler(self, ws):
        self.conns += 1
        self.paths.append(ws.request.path)
        if self._on:
            await self._on(ws)
        try:
            async for m in ws:
                self.received.append(json.loads(m))
        except Exception:
            pass

    async def __aenter__(self):
        self.srv = await serve(self._handler, "127.0.0.1", 0, process_request=self._pr)
        self.url = f"ws://127.0.0.1:{self.srv.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *a):
        self.srv.close()
        await self.srv.wait_closed()

    def methods(self):
        return [m["method"] for m in self.received]


async def until(cond, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


class Delays:
    def __init__(self):
        self.d = []

    async def __call__(self, delay):
        self.d.append(delay)
        await asyncio.sleep(0.005)


async def stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_backoff_delays_are_1_2_4_to_60():
    assert [fd.backoff_delay(i) for i in range(9)] == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def test_classify():
    assert fd.classify(CREATE) == "new_token" and fd.classify(MIGRATE) == "migration"
    assert fd.classify({"txType": "buy", "mint": "m"}) == "trade"
    assert fd.classify({"message": "Successfully subscribed to keys."}) == "ack"
    assert fd.classify({"x": 1}) == "other"


async def test_subscribes_to_the_two_free_streams_only_and_emits_events(tmp_path):
    async def on(ws):
        for m in (CREATE, MIGRATE, "not json", {"message": "ok"}, [1, 2]):
            await ws.send(m if isinstance(m, str) else json.dumps(m))

    db = Database(tmp_path / "f.db")
    await db.start()
    async with Server(on) as srv:
        hub = Hub()
        f = fd.PumpPortalFeed(hub, db, url=srv.url, beat_s=0.05)
        t = asyncio.create_task(f.run())
        await until(lambda: f.counts["migration"] == 1 and f.counts["malformed"] == 1 and f.counts["other"] == 1)
        assert f.healthy() and f.info()["state"] == "live"
        await stop(t)
        assert srv.methods() == ["subscribeNewToken", "subscribeMigration"]
        assert f.counts["new_token"] == 1 and f.counts["ack"] == 1
        ev = hub.bus.recent(topic="new_token")[0]["data"]
        assert ev["mint"] == "MintAAA" and ev["creator"] == "Dev111" and ev["late"] is False
        assert hub.bus.recent(topic="migration")[0]["data"]["pool"] == "pump-amm"
        assert f.state == "down"  # stopped
        await asyncio.sleep(0.1)
        row = await db.fetchone("SELECT symbol, creator FROM new_tokens WHERE mint='MintAAA'")
        assert row == {"symbol": "PAWS", "creator": "Dev111"}
        assert hub.metrics.snapshot()["feed.handle"]["n"] >= 1
    await db.stop()


async def test_only_one_connection_per_desk():
    async with Server() as srv:
        a = fd.PumpPortalFeed(Hub(), url=srv.url, beat_s=0.05)
        ta = asyncio.create_task(a.run())
        await until(lambda: srv.conns == 1)
        with pytest.raises(RuntimeError, match="one connection"):
            await fd.PumpPortalFeed(Hub(), url=srv.url).run()
        await asyncio.sleep(0.2)
        assert srv.conns == 1
        await stop(ta)
        assert not fd._ACTIVE  # released, so a restart by the watchdog is allowed


async def test_reconnect_backoff_sequence_when_unreachable():
    delays = Delays()
    f = fd.PumpPortalFeed(Hub(), url=f"ws://127.0.0.1:{osutil.free_port()}", sleep=delays, beat_s=0.05)
    t = asyncio.create_task(f.run())
    await until(lambda: len(delays.d) >= 8)
    await stop(t)
    assert delays.d[:8] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert f.state == "down"


async def test_backoff_resets_after_a_working_session():
    async def on(ws):
        await ws.send(json.dumps(CREATE))
        await asyncio.sleep(0.1)  # a real feed streams for a while before it drops
        await ws.close()

    delays = Delays()
    async with Server(on) as srv:
        f = fd.PumpPortalFeed(Hub(), url=srv.url, sleep=delays, beat_s=0.05)
        t = asyncio.create_task(f.run())
        await until(lambda: len(delays.d) >= 4, 10)
        await stop(t)
    assert delays.d[:4] == [1, 1, 1, 1]
    assert f.state in ("reconnecting", "down")


async def test_silence_forces_a_reconnect():
    delays = Delays()
    async with Server() as srv:
        f = fd.PumpPortalFeed(Hub(), url=srv.url, silence_s=0.3, beat_s=0.05, sleep=delays)
        t = asyncio.create_task(f.run())
        await until(lambda: srv.conns >= 2, 5)
        await stop(t)
    assert f.reconnects >= 1


async def test_wake_from_sleep_reconnects_immediately_without_backoff():
    delays = Delays()
    async with Server() as srv:
        hub = Hub()
        f = fd.PumpPortalFeed(hub, url=srv.url, silence_s=1000, beat_s=0.05, sleep=delays)
        t = asyncio.create_task(f.run())
        await until(lambda: srv.conns == 1)
        hub.bus.publish("wake", {})
        await until(lambda: srv.conns == 2, 2)
        await stop(t)
    assert delays.d == []  # delay 0: no sleep at all


async def test_http_403_is_reported_as_blocked_and_backs_off():
    def refuse(conn, request):
        return conn.respond(HTTPStatus.FORBIDDEN, "blocked\n")

    delays = Delays()
    async with Server(process_request=refuse) as srv:
        f = fd.PumpPortalFeed(Hub(), url=srv.url, sleep=delays, beat_s=0.05)
        t = asyncio.create_task(f.run())
        await until(lambda: len(delays.d) >= 2)
        await stop(t)
    assert f.state == "down" and f.last_error.startswith("HTTP 403") and "blocked" in f.last_error
    assert delays.d[:2] == [1, 2]


async def test_no_trade_subscription_without_a_key():
    async with Server() as srv:
        f = fd.PumpPortalFeed(Hub(), url=srv.url, beat_s=0.05)
        t = asyncio.create_task(f.run())
        await until(lambda: f.state == "live" and len(srv.received) == 2)
        assert await f.set_trade_watch(["m1", "m2"]) is False
        await asyncio.sleep(0.2)
        await stop(t)
    assert "subscribeTokenTrade" not in srv.methods() and "api-key" not in srv.paths[0]
    assert f.info()["trades"] == "none"


async def test_keyed_trades_capped_at_50_unsubscribe_dropped_and_key_never_shown():
    async with Server() as srv:
        f = fd.PumpPortalFeed(Hub(), url=srv.url, api_key="PPKEY-123456", beat_s=0.05)
        t = asyncio.create_task(f.run())
        await until(lambda: f.state == "live")
        assert await f.set_trade_watch([f"m{i}" for i in range(80)]) is True
        await until(lambda: "subscribeTokenTrade" in srv.methods())
        sub = next(m for m in srv.received if m["method"] == "subscribeTokenTrade")
        assert len(sub["keys"]) == 50 and sub["keys"][0] == "m0"
        await f.set_trade_watch([f"m{i}" for i in range(10)])
        await until(lambda: "unsubscribeTokenTrade" in srv.methods())
        unsub = next(m for m in srv.received if m["method"] == "unsubscribeTokenTrade")
        assert set(unsub["keys"]) == {f"m{i}" for i in range(10, 50)}
        await stop(t)
    assert "PPKEY-123456" in srv.paths[0]  # needed on the wire...
    assert "PPKEY-123456" not in json.dumps(f.info())  # ...never shown


def test_feed_logs_never_contain_the_key(monkeypatch):
    import logging

    monkeypatch.setenv("PUMPPORTAL_API_KEY", "PPKEY-123456")
    rec = logging.LogRecord("desk.feed", logging.WARNING, "f", 1, "connect to %s failed", ("wss://x/api/data?api-key=PPKEY-123456",), None)
    logsetup.RedactFilter().filter(rec)
    assert "PPKEY-123456" not in rec.getMessage()


async def test_wake_listener_is_a_sync_bus_callback():
    hub = Hub()
    f = fd.PumpPortalFeed(hub)
    hub.bus.publish("wake", {})
    assert f._wake.is_set()


def test_no_trading_or_wallet_calls_in_feed_code():
    from conftest import ROOT

    t = (ROOT / "feed.py").read_text() + (ROOT / "sources.py").read_text()
    for bad in ("api/trade", "trade-local", "privateKey", "Keypair", "walletApiKey", "create-wallet"):
        assert bad not in t
