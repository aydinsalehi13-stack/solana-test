"""In-process event bus, per-stage latency metrics, heartbeats and the event-loop lag monitor."""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from contextlib import contextmanager


def percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile. None when there is no data (never invent a number)."""
    if not sorted_vals:
        return None
    idx = max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1)
    return sorted_vals[idx]


class Samples:
    def __init__(self, maxlen: int = 2000):
        self._d: deque[float] = deque(maxlen=maxlen)

    def add(self, v: float) -> None:
        self._d.append(v)

    def snapshot(self) -> dict:
        s = sorted(self._d)
        return {"n": len(s), "p50": percentile(s, 50), "p95": percentile(s, 95), "max": s[-1] if s else None}


class Metrics:
    """p50/p95 per stage (feed, scanner, each layer, score, brain, executor)."""

    def __init__(self) -> None:
        self.stages: dict[str, Samples] = {}

    def record(self, stage: str, ms: float) -> None:
        self.stages.setdefault(stage, Samples()).add(ms)

    @contextmanager
    def timer(self, stage: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record(stage, (time.perf_counter() - t0) * 1000.0)

    def snapshot(self) -> dict:
        return {k: v.snapshot() for k, v in self.stages.items()}


class Heartbeats:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._t: dict[str, float] = {}
        self._info: dict[str, str] = {}

    def beat(self, name: str, info: str = "") -> None:
        self._t[name] = self._clock()
        if info:
            self._info[name] = info

    def touch_all(self) -> None:
        """After a machine sleep every bot gets a fresh window instead of a false alarm."""
        now = self._clock()
        for k in self._t:
            self._t[k] = now

    def age(self, name: str) -> float | None:
        t = self._t.get(name)
        return None if t is None else self._clock() - t

    def ages(self) -> dict[str, float]:
        now = self._clock()
        return {k: now - t for k, t in self._t.items()}

    def stale(self, threshold: float, exclude: tuple[str, ...] = ()) -> list[str]:
        return [k for k, a in self.ages().items() if a > threshold and k not in exclude]

    def rows(self) -> list[tuple[str, float, str]]:
        return [(k, t, self._info.get(k, "")) for k, t in self._t.items()]

    def forget(self, name: str) -> None:
        self._t.pop(name, None)


class Bus:
    """Pub/sub with a ring buffer. Slow subscribers lose their oldest events, never block publishers."""

    def __init__(self, ring: int = 500) -> None:
        self._seq = 0
        self._ring: deque[dict] = deque(maxlen=ring)
        self._subs: set[asyncio.Queue] = set()
        self._callbacks: dict[str, list] = {}

    def on(self, topic: str, fn) -> None:
        """Synchronous callback run inside publish(). Keep it tiny (e.g. set an Event)."""
        self._callbacks.setdefault(topic, []).append(fn)

    def publish(self, topic: str, data: dict | None = None) -> int:
        self._seq += 1
        ev = {"seq": self._seq, "ts": time.time(), "topic": topic, "data": data or {}}
        self._ring.append(ev)
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(ev)
        for fn in self._callbacks.get(topic, ()):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - a bad listener must never break the publisher
                pass
        return self._seq

    def recent(self, n: int = 100, topic: str | None = None) -> list[dict]:
        evs = [e for e in self._ring if topic is None or e["topic"] == topic]
        return evs[-n:]

    @contextmanager
    def subscribe(self, maxsize: int = 256):
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        try:
            yield q
        finally:
            self._subs.discard(q)


class Hub:
    def __init__(self, clock=time.time) -> None:
        self.bus = Bus()
        self.metrics = Metrics()
        self.heartbeats = Heartbeats(clock)
        self.lag = Samples()
        self.status: dict = {}  # name -> callable returning a JSON-able dict, shown in /api/health (feed badge...)
        self.objects: dict = {}  # shared handles the API needs (settings, book, orion...): set by desk.build
        self.paused: set[str] = set()  # bots the owner paused from the dashboard


async def beating_sleep(hub: Hub, name: str, seconds: float, step: float = 20.0) -> None:
    """Sleep without looking dead to the watchdog."""
    end = time.monotonic() + seconds
    while True:
        hub.heartbeats.beat(name)
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(step, left))


async def wait_if_paused(hub: Hub, name: str) -> None:
    """A paused bot idles (still beating) until the owner resumes it."""
    while name in hub.paused:
        hub.heartbeats.beat(name)
        await asyncio.sleep(1.0)


async def lag_monitor(hub: Hub, interval: float = 0.1, name: str = "loop") -> None:
    """100 ms ticker. Lag = how late the loop woke us up."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        hub.lag.add(max(0.0, (time.perf_counter() - t0 - interval) * 1000.0))
        hub.heartbeats.beat(name)
