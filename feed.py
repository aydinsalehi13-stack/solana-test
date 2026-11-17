"""Market feed: PumpPortal websocket (primary), DexScreener snapshots, GeckoTerminal (last resort), and the badge.

PumpPortal rules (spec section 2):
  * ONE connection for the whole desk (one per IP is allowed), free streams only:
    subscribeNewToken + subscribeMigration.
  * subscribeTokenTrade is metered: it is sent only if PUMPPORTAL_API_KEY is set, and only for the 50 hottest
    watched mints, unsubscribing when a mint is dropped. The key never appears in logs (logsetup redacts it).
  * silent 120 s -> reconnect; backoff 1, 2, 4 ... 60 s; on wake from sleep reconnect immediately.
No wallet, no trading API (hard rule 3).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

import config
import sources
from bus import Hub, beating_sleep

log = logging.getLogger("desk.feed")

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
SILENCE_S = 120.0
MAX_TRADE_WATCH = 50
_ACTIVE: set[int] = set()  # ids of running PumpPortalFeed objects: only one is allowed


def backoff_delay(attempt: int) -> int:
    """1, 2, 4, 8, 16, 32, 60, 60, ... seconds."""
    return min(60, 2 ** attempt)


def describe(e: BaseException) -> str:
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status:
        return f"HTTP {status}" + (" (blocked: cloud/VPN IP?)" if status == 403 else "")
    return f"{type(e).__name__}: {str(e)[:80]}"


class _Reconnect(Exception):
    pass


class PumpPortalFeed:
    def __init__(self, hub: Hub, db=None, url: str = PUMPPORTAL_URL, api_key: str | None = None,
                 silence_s: float = SILENCE_S, beat_s: float = 5.0, sleep=None, connect=None):
        self.hub, self.db, self.base_url, self.api_key = hub, db, url, api_key or None
        self.silence_s, self.beat_s = silence_s, beat_s
        self._sleep = sleep or (lambda d: beating_sleep(hub, "feed", d))
        self._connect = connect or websockets.connect
        self._wake = asyncio.Event()
        self._ws = None
        self._trade_subs: set[str] = set()
        self._trade_target: list[str] = []
        self._got_msg = False
        self.state = "down"  # live | reconnecting | down
        self.last_error: str | None = None
        self.last_msg: float | None = None
        self.connects = 0
        self.reconnects = 0
        self.counts = {"new_token": 0, "migration": 0, "trade": 0, "ack": 0, "other": 0, "malformed": 0}
        self.sent: list[str] = []  # methods sent on the current/last session (tests + audit)
        hub.bus.on("wake", lambda ev: self.request_reconnect())

    # ---- public --------------------------------------------------------------------------------
    def url(self) -> str:
        return f"{self.base_url}?api-key={self.api_key}" if self.api_key else self.base_url

    def request_reconnect(self) -> None:
        self._wake.set()

    def healthy(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.state == "live" and self.last_msg is not None and now - self.last_msg <= self.silence_s

    def info(self) -> dict:
        return {"state": self.state, "last_error": self.last_error, "connects": self.connects,
                "reconnects": self.reconnects, "counts": dict(self.counts), "trades": "keyed" if self.api_key else "none",
                "last_msg_age_s": None if self.last_msg is None else round(time.time() - self.last_msg, 1)}

    async def set_trade_watch(self, mints: list[str]) -> bool:
        """Hottest first. Keyed accounts only; at most 50; dropped mints are unsubscribed."""
        if not self.api_key:
            return False
        self._trade_target = list(dict.fromkeys(mints))[:MAX_TRADE_WATCH]
        await self._sync_trades()
        return True

    # ---- run -----------------------------------------------------------------------------------
    async def run(self) -> None:
        if _ACTIVE:
            raise RuntimeError("PumpPortal allows one connection per IP: a feed is already running")
        _ACTIVE.add(id(self))
        attempt = 0
        try:
            while True:
                self._got_msg = False
                reason = "closed"
                try:
                    async with self._connect(self.url(), open_timeout=15, max_size=2 ** 20) as ws:
                        self.connects += 1
                        self._ws = ws
                        self.sent = []
                        await self._session(ws)
                except asyncio.CancelledError:
                    raise
                except _Reconnect as e:
                    reason = str(e)
                except Exception as e:  # noqa: BLE001
                    reason = describe(e)
                    self.last_error = reason
                finally:
                    self._ws = None
                    self._trade_subs.clear()
                self.reconnects += 1
                # "reconnecting" = we had a working feed and are getting it back; "down" = never worked / refused
                self.state = "reconnecting" if self.last_msg is not None and not (self.last_error or "").startswith("HTTP") else "down"
                if reason == "wake":
                    delay = 0  # machine woke up: reconnect immediately
                else:
                    attempt = 0 if self._got_msg else attempt
                    delay = backoff_delay(attempt)
                    attempt += 1
                log.warning("PumpPortal %s, reconnecting in %ss", reason, delay)
                self._wake.clear()
                if delay:
                    await self._sleep(delay)
        finally:
            _ACTIVE.discard(id(self))
            self.state = "down"

    async def _send(self, ws, payload: dict) -> None:
        await ws.send(json.dumps(payload))
        self.sent.append(payload["method"])

    async def _session(self, ws) -> None:
        await self._send(ws, {"method": "subscribeNewToken"})
        await self._send(ws, {"method": "subscribeMigration"})
        self.state, self.last_error = "live", None
        if self.api_key and self._trade_target:
            await self._sync_trades()
        last = time.monotonic()
        recv = None
        try:
            while True:
                self.hub.heartbeats.beat("feed")
                if recv is None:
                    recv = asyncio.ensure_future(ws.recv())
                wake = asyncio.ensure_future(self._wake.wait())
                done, _ = await asyncio.wait({recv, wake}, timeout=self.beat_s, return_when=asyncio.FIRST_COMPLETED)
                wake.cancel()
                if self._wake.is_set():
                    raise _Reconnect("wake")
                if recv in done:
                    raw, recv = recv.result(), None  # raises ConnectionClosed if the server hung up
                    last, self._got_msg = time.monotonic(), True
                    self.last_msg = time.time()
                    await self._handle(raw)
                elif time.monotonic() - last > self.silence_s:
                    raise _Reconnect(f"silent {self.silence_s:.0f}s")
        finally:
            if recv is not None:
                recv.cancel()

    # ---- trade subscriptions (keyed only) ---------------------------------------------------------
    async def _sync_trades(self) -> None:
        ws = self._ws
        if ws is None or not self.api_key:
            return
        target = set(self._trade_target)
        add, drop = [m for m in self._trade_target if m not in self._trade_subs], list(self._trade_subs - target)
        if add:
            await self._send(ws, {"method": "subscribeTokenTrade", "keys": add})
        if drop:
            await self._send(ws, {"method": "unsubscribeTokenTrade", "keys": drop})
        self._trade_subs = target

    # ---- messages ---------------------------------------------------------------------------------
    async def _handle(self, raw) -> None:
        t0 = time.perf_counter()
        try:
            msg = await asyncio.to_thread(json.loads, raw) if len(raw) > 20000 else json.loads(raw)
        except (ValueError, TypeError):
            self.counts["malformed"] += 1
            return
        if not isinstance(msg, dict):
            self.counts["other"] += 1
            return
        kind = classify(msg)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if kind == "new_token":
            ev = {"mint": msg["mint"], "name": msg.get("name"), "symbol": msg.get("symbol"),
                  "creator": msg.get("traderPublicKey"), "pool": msg.get("pool"), "seen_at": time.time(),
                  "v_sol": msg.get("vSolInBondingCurve"), "v_tokens": msg.get("vTokensInBondingCurve"),
                  "mcap_sol": msg.get("marketCapSol"), "initial_buy": msg.get("initialBuy"),
                  "bonding_curve": msg.get("bondingCurveKey"), "late": False}
            if self.db is not None:
                self.db.execute_nowait("INSERT OR IGNORE INTO new_tokens(mint, ts, name, symbol, creator) VALUES (?,?,?,?,?)",
                                       (ev["mint"], ev["seen_at"], ev["name"], ev["symbol"], ev["creator"]))
            self.hub.bus.publish("new_token", ev)
        elif kind == "migration":
            self.hub.bus.publish("migration", {"mint": msg["mint"], "pool": msg.get("pool"), "seen_at": time.time(),
                                               "signature": msg.get("signature")})
        elif kind == "trade":
            self.hub.bus.publish("trade", {k: msg.get(k) for k in (
                "mint", "txType", "traderPublicKey", "solAmount", "tokenAmount", "vSolInBondingCurve",
                "vTokensInBondingCurve", "marketCapSol", "signature")} | {"seen_at": time.time()})
        self.hub.metrics.record("feed.handle", (time.perf_counter() - t0) * 1000.0)


def classify(msg: dict) -> str:
    t = msg.get("txType")
    mint = msg.get("mint")
    if t == "create" and mint:
        return "new_token"
    if t == "migrate" and mint:
        return "migration"
    if t in ("buy", "sell") and mint:
        return "trade"
    if "message" in msg and not mint:
        return "ack"
    return "other"


# ---- fallbacks -------------------------------------------------------------------------------------------
def compute_badge(pumpportal_ok: bool, gecko_age: float | None, snapshot_age: float | None) -> str:
    if pumpportal_ok:
        return "PumpPortal live"
    if gecko_age is not None and gecko_age < 60:
        return "GeckoTerminal (20-50 s late)"
    if snapshot_age is not None and snapshot_age < 30:
        return "DexScreener snapshots"
    return "feed down"


class Fallbacks:
    """DexScreener snapshots of watched mints (every 3 s) and, when PumpPortal has been down for a while,
    GeckoTerminal new pools (late, rate limited)."""

    def __init__(self, hub: Hub, feed: PumpPortalFeed, dex: sources.DexScreener, gecko: sources.GeckoTerminal, db=None,
                 snapshot_every: float = 3.0, snapshot_dexes: tuple = ("pumpfun", "pumpswap"), gecko_grace: float = 30.0, gecko_every: float = 8.0, gecko_idle: float = 2.0, clock=time.monotonic):
        self.hub, self.feed, self.dex, self.gecko, self.db = hub, feed, dex, gecko, db
        self.snapshot_every, self.gecko_grace, self.gecko_every, self._clock = snapshot_every, gecko_grace, gecko_every, clock
        self.gecko_idle = gecko_idle
        self.snapshot_dexes = snapshot_dexes
        self.watch: list[str] = []
        self._seen_gecko: set[str] = set()
        self._down_since: float | None = None
        self.snapshot_ok: float | None = None

    def set_watch(self, mints: list[str]) -> None:
        self.watch = list(dict.fromkeys(mints))

    def badge(self) -> str:
        now = self._clock()
        g = None if self.gecko.last_ok is None else now - self.gecko.last_ok
        s = None if self.snapshot_ok is None else now - self.snapshot_ok
        return compute_badge(self.feed.healthy(), g, s)

    def info(self) -> dict:
        return {"badge": self.badge(), "pumpportal": self.feed.info(), "watching": len(self.watch)}

    async def snapshot_loop(self) -> None:
        while True:
            if self.watch:
                try:
                    snaps = await self.dex.snapshots(self.watch, dexes=self.snapshot_dexes)
                    self.snapshot_ok = self._clock()
                    for s in snaps.values():
                        self.hub.bus.publish("snapshot", s)
                except sources.RateLimited:
                    pass
                except Exception as e:  # noqa: BLE001
                    log.warning("snapshots failed: %s", e)
            await beating_sleep(self.hub, "snapshots", self.snapshot_every, step=self.snapshot_every)

    async def gecko_loop(self) -> None:
        while True:
            now = self._clock()
            if self.feed.healthy():
                self._down_since = None
            elif self._down_since is None:
                self._down_since = now
            if self._down_since is not None and now - self._down_since >= self.gecko_grace:
                try:
                    for p in await self.gecko.new_pools():
                        if p["mint"] in self._seen_gecko:
                            continue
                        self._seen_gecko.add(p["mint"])
                        ev = {"mint": p["mint"], "name": p["name"], "symbol": None, "creator": None, "pool": p["pool"],
                              "seen_at": time.time(), "late": True}
                        if self.db is not None:
                            self.db.execute_nowait("INSERT OR IGNORE INTO new_tokens(mint, ts, name, symbol, creator) VALUES (?,?,?,?,?)",
                                                   (ev["mint"], ev["seen_at"], ev["name"], None, None))
                        self.hub.bus.publish("new_token", ev)
                except sources.RateLimited:
                    pass
                except Exception as e:  # noqa: BLE001
                    log.warning("GeckoTerminal fallback failed: %s", e)
                await beating_sleep(self.hub, "feed_fallback", self.gecko_every, step=self.gecko_every)
            else:
                await beating_sleep(self.hub, "feed_fallback", self.gecko_idle, step=self.gecko_idle)


def build(hub: Hub, db=None, clients: sources.Clients | None = None):
    """Wire the feed for the desk. Returns (feed, fallbacks, clients)."""
    clients = clients or sources.Clients()
    feed = PumpPortalFeed(hub, db, api_key=__import__("os").environ.get("PUMPPORTAL_API_KEY"))
    fb = Fallbacks(hub, feed, sources.DexScreener(clients), sources.GeckoTerminal(clients), db)
    hub.status["feed"] = fb.info
    return feed, fb, clients
