"""Dashboard screenshots to Telegram (spec section 8): every N minutes (your setting), on every order, and on /live.

Runs in its OWN THREAD with its own event loop and its own database connection, so a slow screenshot or a slow Telegram can never
stall the desk. The 390 px screenshot is taken in a separate headless browser: the desk token is injected into localStorage and is
never rendered. The schedule is saved in the database (live_next_at), so it survives a restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import osutil
from telegram import OWNER_KEY, TelegramClient, TelegramError, paper, status_line

log = logging.getLogger("desk.live")
DEBOUNCE_S = 10.0  # several orders in a burst make one screenshot
NEXT_KEY, INTERVAL_KEY = "live_next_at", "live_interval_min"


def capture_screenshot(url: str, token: str, out_path: str | Path, chrome: str | None = None, timeout_ms: int = 20000) -> str:
    """One 390 px phone-sized screenshot of the dashboard, as a signed-in viewer. The token goes into localStorage, never onto the page."""
    from playwright.sync_api import sync_playwright

    chrome = chrome or osutil.find_chrome()
    with sync_playwright() as p:
        kw: dict = {"args": ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []}
        if chrome:
            kw["executable_path"] = chrome
        browser = p.chromium.launch(**kw)
        try:
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
            ctx.add_init_script("localStorage.setItem('desk_token', " + json.dumps(token) + ")")
            page = ctx.new_page()
            page.goto(url)
            page.wait_for_function("document.querySelector('#bal-prompt') && document.querySelector('#bal-prompt').getAttribute('aria-label') !== 'loading'", timeout=timeout_ms)
            page.wait_for_timeout(1200)  # let the odometers settle
            page.screenshot(path=str(out_path))
        finally:
            browser.close()
    return str(out_path)


def fetch_status(api_url: str, token: str) -> str:
    """The one-line status, built from the desk's own /api/live."""
    req = urllib.request.Request(api_url.rstrip("/") + "/api/live", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - the local desk
        live = json.load(r)
    running = sum(1 for a in live["agents"] if a["running"])
    return status_line(live["strategies"], (live.get("feed") or {}).get("badge"), running, len(live["agents"]))


class LiveShots:
    def __init__(self, *, api_url: str, token: str, bot_token: str, db_path, chrome: str | None = None, clock=time.time,
                 capture_fn=None, status_fn=None, transport=None, tg_base: str | None = None):
        self.api_url, self.token, self.bot_token, self.db_path, self.chrome, self.clock = api_url, token, bot_token, Path(db_path), chrome, clock
        self.capture_fn = capture_fn or (lambda out: capture_screenshot(self.api_url, self.token, out, self.chrome))
        self.status_fn = status_fn or (lambda: fetch_status(self.api_url, self.token))
        self.transport, self.tg_base = transport, tg_base
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None
        self._last_sent = 0.0

    # ---- control (thread-safe) -------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._main, name="live-shots", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=15)

    def request_now(self, reason: str = "request") -> None:
        self._q.put(reason)

    # ---- own database connection ---------------------------------------------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def _get(self, conn, key: str, default=None):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, conn, key: str, value) -> None:
        conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, str(value), self.clock()))
        conn.commit()

    # ---- the thread ----------------------------------------------------------------------------------------------------------------------
    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        kw = {"transport": self.transport} if self.transport else {}
        if self.tg_base:
            kw["base"] = self.tg_base
        client = TelegramClient(self.bot_token, **kw)
        conn = self._conn()
        try:
            while not self._stop.is_set():
                self.tick(conn, loop, client)
        finally:
            conn.close()
            loop.run_until_complete(client.aclose())
            loop.close()

    def tick(self, conn, loop, client, wait: float = 5.0) -> bool:
        """One pass of the thread loop. Returns True when a screenshot was sent. (Separate so tests can step it.)"""
        now = self.clock()
        interval = max(60, int(float(self._get(conn, INTERVAL_KEY, 5) or 5) * 60))
        nxt = self._get(conn, NEXT_KEY)
        if nxt is None:  # first start: the first screenshot comes one interval from now, not a flood at install time
            nxt = now + interval
            self._set(conn, NEXT_KEY, nxt)
        nxt = float(nxt)
        reason = None
        try:
            item = self._q.get(timeout=max(0.05, min(wait, nxt - now))) if nxt > now else None
            if item is None and self._stop.is_set():
                return False
            reason = item
        except queue.Empty:
            pass
        now = self.clock()
        if reason is None and float(self._get(conn, NEXT_KEY)) > now:
            return False
        if reason is not None and reason != "schedule" and now - self._last_sent < DEBOUNCE_S:
            return False
        sent = self._send_one(conn, loop, client, reason or "schedule")
        self._set(conn, NEXT_KEY, self.clock() + interval)  # saved, so a restart keeps the rhythm
        return sent

    def _send_one(self, conn, loop, client, reason: str) -> bool:
        owner = self._get(conn, OWNER_KEY)
        if not owner:
            return False  # nobody has said /start yet
        try:
            status = self.status_fn()
        except Exception as e:  # noqa: BLE001
            status = paper("status unavailable")
            self.last_error = f"status: {type(e).__name__}"
        out = Path(tempfile.gettempdir()) / f"desk_live_{int(self.clock())}.png"
        try:
            path = self.capture_fn(out)
            loop.run_until_complete(client.send_photo(int(owner), path, paper(status)))
            self.sent += 1
            self._last_sent = self.clock()
            return True
        except TelegramError as e:
            self.failed += 1
            self.last_error = str(e)
            log.warning("screenshot not sent: %s", e)
        except Exception as e:  # noqa: BLE001 - a failed screenshot still gets its status line
            self.failed += 1
            self.last_error = f"screenshot: {type(e).__name__}"
            log.warning("screenshot failed (%s): %s", reason, type(e).__name__)
            try:
                loop.run_until_complete(client.send_text(int(owner), paper(f"{status}\n(the dashboard screenshot failed: {type(e).__name__})")))
            except TelegramError:
                pass
        finally:
            try:
                Path(out).unlink(missing_ok=True)
            except OSError:
                pass
        return False
