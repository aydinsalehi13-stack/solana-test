import asyncio

import pytest

from bus import Bus, Heartbeats, Hub, Metrics, Samples, beating_sleep, lag_monitor, percentile


def test_percentile_nearest_rank_and_no_data():
    assert percentile([], 95) is None
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([5], 50) == 5


def test_metrics_stage_snapshot():
    m = Metrics()
    for v in (10, 20, 30, 40, 50):
        m.record("score", v)
    s = m.snapshot()["score"]
    assert s["n"] == 5 and s["p50"] == 30 and s["p95"] == 50
    with m.timer("brain"):
        pass
    assert m.snapshot()["brain"]["n"] == 1


def test_bus_ring_and_drop_oldest():
    b = Bus(ring=3)
    for i in range(5):
        b.publish("t", {"i": i})
    assert [e["data"]["i"] for e in b.recent()] == [2, 3, 4]
    with b.subscribe(maxsize=2) as q:
        for i in range(5):
            b.publish("t", {"i": i})
        assert [q.get_nowait()["data"]["i"] for _ in range(2)] == [3, 4]


def test_heartbeats_stale_and_touch_all():
    t = [1000.0]
    hb = Heartbeats(clock=lambda: t[0])
    hb.beat("a"); hb.beat("b")
    t[0] += 100
    hb.beat("b")
    assert hb.stale(95) == ["a"]
    assert hb.stale(95, exclude=("a",)) == []
    hb.touch_all()
    assert hb.stale(95) == []
    assert hb.age("ghost") is None


async def test_lag_monitor_records_and_beats():
    hub = Hub()
    task = asyncio.create_task(lag_monitor(hub, interval=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    snap = hub.lag.snapshot()
    assert snap["n"] >= 5 and snap["p95"] is not None
    assert hub.heartbeats.age("loop") is not None


async def test_beating_sleep_keeps_heartbeat_fresh():
    hub = Hub()
    await beating_sleep(hub, "x", 0.12, step=0.03)
    assert hub.heartbeats.age("x") < 0.1
