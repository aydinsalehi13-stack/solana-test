"""The desk: one asyncio process. Event loop, heartbeats, watchdog, API server."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time

import config
import logsetup
from bus import Hub, beating_sleep, lag_monitor

log = logging.getLogger("desk.main")

STALE_S = 95.0  # a bot silent this long gets restarted
WAKE_SLEEP_S = 15.0  # watchdog tick; if it takes > 2x this, the machine slept
HEARTBEAT_DB_S = 30.0


class Supervisor:
    """Runs named components, restarts crashed ones, and restarts stale ones (no heartbeat > 95 s)."""

    def __init__(self, hub: Hub, stale_s: float = STALE_S, tick_s: float = WAKE_SLEEP_S, clock=time.time):
        self.hub, self.stale_s, self.tick_s, self.clock = hub, stale_s, tick_s, clock
        self.specs: dict[str, callable] = {}
        self.runners: dict[str, asyncio.Task] = {}
        self.inner: dict[str, asyncio.Task] = {}
        self.restarts: list[dict] = []
        self._watchdog: asyncio.Task | None = None

    def add(self, name: str, factory) -> None:
        self.specs[name] = factory
        self.hub.heartbeats.beat(name)
        if self._watchdog is not None:  # added after start
            self.runners[name] = asyncio.create_task(self._runner(name), name=f"run:{name}")

    def start(self) -> None:
        for name in self.specs:
            self.runners[name] = asyncio.create_task(self._runner(name), name=f"run:{name}")
        self._watchdog = asyncio.create_task(self._watch(), name="watchdog")

    async def stop(self) -> None:
        tasks = list(self.runners.values()) + ([self._watchdog] if self._watchdog else [])
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _note(self, name: str, reason: str, down_s: float = 1.0) -> None:
        self.restarts.append({"ts": self.clock(), "bot": name, "reason": reason, "down_s": down_s})
        self.hub.bus.publish("restart", {"bot": name, "reason": reason, "down_s": down_s})
        log.warning("restarting %s: %s", name, reason)

    async def _runner(self, name: str) -> None:
        me = asyncio.current_task()
        while True:
            inner = asyncio.create_task(self.specs[name](), name=name)
            self.inner[name] = inner
            self.hub.heartbeats.beat(name)
            try:
                await inner
                self._note(name, "exited")
            except asyncio.CancelledError:
                if me.cancelling():  # we are shutting down
                    inner.cancel()
                    raise
                # the watchdog cancelled the inner task: loop and start it again
            except Exception as e:  # noqa: BLE001
                self._note(name, f"crashed: {type(e).__name__}: {e}")
                await asyncio.sleep(1.0)

    def on_wake(self, gap_s: float | None = None) -> None:
        """Laptop slept or froze: give every bot a fresh window instead of a false alarm.
        gap_s is how long the loop was frozen (None = unknown); subscribers decide what a long sleep means for them."""
        self.hub.heartbeats.touch_all()
        self.hub.bus.publish("wake", {"gap_s": gap_s})
        log.info("wake from sleep detected (frozen %s): heartbeats reset", "?" if gap_s is None else f"{gap_s:.0f}s")

    def check_stale(self) -> list[str]:
        stale = self.hub.heartbeats.stale(self.stale_s, exclude=("watchdog",))
        for name in stale:
            inner = self.inner.get(name)
            if inner is None or inner.done():
                continue
            self._note(name, f"stale {self.hub.heartbeats.age(name):.0f}s", self.hub.heartbeats.age(name))
            self.hub.heartbeats.beat(name)
            inner.cancel()
        return stale

    async def _watch(self) -> None:
        while True:
            t0 = time.time()
            await asyncio.sleep(self.tick_s)
            self.hub.heartbeats.beat("watchdog")
            if time.time() - t0 > 2 * self.tick_s:
                self.on_wake(time.time() - t0 - self.tick_s)
                continue
            self.check_stale()


async def heartbeat_writer(hub: Hub, db, interval: float = HEARTBEAT_DB_S, supervisor=None) -> None:
    last_metric = 0.0
    while True:
        now = time.time()
        await db.write_heartbeats(hub.heartbeats.rows())
        await db.record_uptime(now)  # coverage: only time the desk really ran counts
        if now - last_metric >= 60:  # one line a minute for the night check (loop lag, restarts)
            last_metric = now
            snap = hub.lag.snapshot()
            if snap["n"]:
                await db.execute("INSERT OR REPLACE INTO metrics_log(ts, lag_p95_ms, lag_max_ms, restarts) VALUES (?,?,?,?)",
                                 (now, snap["p95"], snap["max"], len(getattr(supervisor, "restarts", []))))
        await beating_sleep(hub, "heartbeat_writer", interval)


async def retention_task(hub: Hub, db) -> None:
    await beating_sleep(hub, "retention", 60)
    while True:
        deleted = await db.retention()
        if any(deleted.values()):
            log.info("retention deleted %s", deleted)
        await beating_sleep(hub, "retention", 6 * 3600)


async def lag_alert(hub: Hub) -> None:
    while True:
        await beating_sleep(hub, "lag_alert", 60)
        p95 = hub.lag.snapshot()["p95"]
        if p95 is not None and p95 > 250:
            log.warning("event-loop lag p95 %.0f ms (> 250 ms)", p95)


def write_pid() -> None:
    config.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.PID_FILE.write_text(str(os.getpid()))


def clear_pid() -> None:
    with contextlib.suppress(FileNotFoundError):
        if config.PID_FILE.read_text().strip() == str(os.getpid()):
            config.PID_FILE.unlink()


def _make_server_class():
    import uvicorn

    class _DeskServer(uvicorn.Server):
        """uvicorn re-raises SIGTERM after shutting down, which would kill us before our cleanup
        (pid file, final heartbeat flush, db close). We own the signals instead."""

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    return _DeskServer


def _install_stop_signals(server) -> None:
    import signal

    def stop(*_):
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError, ValueError):  # Windows event loops
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, stop)


async def amain() -> int:
    import uvicorn

    import osutil
    from api import create_app
    from db import Database

    config.ensure_dirs()
    config.load_env()
    logsetup.setup_logging()

    hub = Hub()
    db = Database(config.DB_PATH)
    await db.start()
    write_pid()

    # every restart is logged for good: the night check and the report read this, a process restart cannot lose it
    db.execute_nowait("INSERT INTO restart_log(ts, kind, bot, reason, down_s) VALUES (?,?,?,?,?)", (time.time(), "desk_start", None, "desk process started", None))
    hub.bus.on("restart", lambda ev: db.execute_nowait("INSERT INTO restart_log(ts, kind, bot, reason, down_s) VALUES (?,?,?,?,?)",
                                                         (ev["ts"], "component", ev["data"].get("bot"), str(ev["data"].get("reason"))[:200], ev["data"].get("down_s"))))
    hub.bus.on("wake", lambda ev: db.execute_nowait("INSERT INTO restart_log(ts, kind, bot, reason, down_s) VALUES (?,?,?,?,?)",
                                                      (ev["ts"], "wake", None, "laptop woke from sleep", ev["data"].get("gap_s"))))

    token = os.environ.get("APP_TOKEN", "")
    if not token:
        log.error("APP_TOKEN is empty: the API will refuse every request. Run setup.py or edit .env")

    awake = None
    if os.environ.get("DESK_KEEP_AWAKE") != "0":  # DESK_KEEP_AWAKE=0: tests and dev boxes
        from keepawake import KeepAwake

        awake = KeepAwake()
        hub.status["keep_awake"] = lambda a=awake: {"method": a.method}
        awake.start()

    sup = Supervisor(hub)
    sup.add("loop", lambda: lag_monitor(hub))
    sup.add("heartbeat_writer", lambda: heartbeat_writer(hub, db, supervisor=sup))
    sup.add("retention", lambda: retention_task(hub, db))
    sup.add("lag_alert", lambda: lag_alert(hub))
    clients = None
    pd = None
    if os.environ.get("DESK_FEED") != "0":  # DESK_FEED=0: no market feed (tests, offline development)
        import feed as feed_mod

        pump, fallbacks, clients = feed_mod.build(hub, db)
        sup.add("feed", pump.run)
        sup.add("snapshots", fallbacks.snapshot_loop)
        sup.add("feed_fallback", fallbacks.gecko_loop)
        if os.environ.get("DESK_BOTS") != "0":  # DESK_BOTS=0: market feed only, no strategy bots
            import desk as desk_mod

            pd = await desk_mod.build(hub, db, clients, fallbacks.dex, fallbacks=fallbacks, feed=pump)
            for name, bot in (("scanner", pd.scanner), ("analyst", pd.analyst), ("brain", pd.brain),
                              ("executor", pd.executor), ("risk", pd.risk)):
                sup.add(name, bot.run)
            sup.add("browser_link", pd.browser_link)
            if pd.orion is not None:
                sup.add("orion", pd.orion.run)
            for name, fn in pd.extra:
                sup.add(name, fn)
    import links
    import verify

    for o in await verify.recover_orders(db):  # an order caught mid-click by a crash is never clicked again; the owner is told
        hub.bus.publish("push", {"text": f"Unverified: {o['symbol'] or o['mint'][:6]} {o['side']}, check FOMO (the desk stopped mid-order)",
                                 "link": links.coin_url(o["mint"]), "strategy": "prompt", "ts": time.time()})
    sup.start()

    host, port = config.desk_host(), config.desk_port()
    server = _make_server_class()(
        uvicorn.Config(create_app(db, hub, token, sup), host=host, port=port, log_level="warning", access_log=False,
                       timeout_graceful_shutdown=3)  # an open dashboard stream must never keep the desk from stopping
    )
    _install_stop_signals(server)
    shown = osutil.lan_ip() or host if host in ("0.0.0.0", "::") else host
    log.info("desk up, mode=%s, dashboard on http://%s:%s", config.DEFAULT_MODE, shown, port)

    code = 0
    try:
        await server.serve()
    except SystemExit as e:  # uvicorn exits this way when the port is taken
        code = int(e.code or 1)
        log.error("API server could not start on %s:%s", host, port)
    finally:
        await sup.stop()
        if awake is not None:
            awake.stop()
        for hook in (pd.stop_hooks if pd is not None else []):
            with contextlib.suppress(Exception):
                res = hook()
                if asyncio.iscoroutine(res):
                    await res
        if clients is not None:
            await clients.aclose()
        with contextlib.suppress(Exception):
            await db.write_heartbeats(hub.heartbeats.rows())
        await db.stop()
        clear_pid()
    return code


def run() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(run())
