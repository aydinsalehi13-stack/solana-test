import asyncio

from bus import Hub
from main import Supervisor


async def test_crashing_component_is_restarted():
    hub, runs = Hub(), []

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("boom")
        hub.heartbeats.beat("flaky")
        await asyncio.sleep(10)

    sup = Supervisor(hub, stale_s=5, tick_s=0.05)
    sup.add("flaky", flaky)
    sup.start()
    await asyncio.sleep(2.5)
    await sup.stop()
    assert len(runs) >= 3
    assert any("crashed: RuntimeError" in r["reason"] for r in sup.restarts)


async def test_stale_component_is_restarted_with_reason():
    hub, starts = Hub(), []

    async def silent():
        starts.append(1)
        await asyncio.sleep(10)  # never beats

    sup = Supervisor(hub, stale_s=0.3, tick_s=0.05)
    sup.add("silent", silent)
    sup.start()
    await asyncio.sleep(1.2)
    await sup.stop()
    assert len(starts) >= 2
    assert any(r["bot"] == "silent" and r["reason"].startswith("stale") for r in sup.restarts)


async def test_healthy_component_is_left_alone():
    hub, starts = Hub(), []

    async def good():
        starts.append(1)
        while True:
            hub.heartbeats.beat("good")
            await asyncio.sleep(0.02)

    sup = Supervisor(hub, stale_s=0.3, tick_s=0.05)
    sup.add("good", good)
    sup.start()
    await asyncio.sleep(1.0)
    await sup.stop()
    assert len(starts) == 1 and not sup.restarts


async def test_wake_gives_everyone_a_fresh_window():
    t = [1000.0]
    hub = Hub(clock=lambda: t[0])
    sup = Supervisor(hub, stale_s=95, clock=lambda: t[0])
    hub.heartbeats.beat("a")
    t[0] += 600  # laptop slept 10 minutes
    assert hub.heartbeats.stale(95) == ["a"]
    sup.on_wake()
    assert hub.heartbeats.stale(95) == []
    assert hub.bus.recent(topic="wake")


async def test_stop_cancels_everything():
    hub = Hub()

    async def forever():
        while True:
            hub.heartbeats.beat("f")
            await asyncio.sleep(0.01)

    sup = Supervisor(hub)
    sup.add("f", forever)
    sup.start()
    await asyncio.sleep(0.05)
    await sup.stop()
    assert all(t.done() for t in sup.runners.values())
    assert all(t.done() for t in sup.inner.values())
