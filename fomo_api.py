"""FOMO read API (unofficial, read/verify only, can break any day).

FOMO's web app authenticates with a Privy session COOKIE, not a Bearer token. So every call runs as
fetch(url, {credentials:'include'}) inside the desk tab over CDP on 127.0.0.1. This module never reads,
copies or stores the cookie. One priority queue, at most 1 call per 1.2 s in total.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import time
from pathlib import Path

import config

log = logging.getLogger("desk.fomo_api")

BASE = "https://prod-api.fomo.family"
SOLANA_CHAIN_ID = 1399811149
# VERIFY and Risk first, then Executor, Analyst, Scanner.
PRIORITY = {"verify": 0, "risk": 1, "executor": 2, "analyst": 3, "scanner": 4}

# Runs inside the page. credentials:'include' sends the session cookie without our code ever seeing it.
FETCH_JS = """
async ({url, method, body}) => {
  const init = {method, credentials: 'include', headers: {}};
  if (body !== null && body !== undefined) { init.headers['content-type'] = 'application/json'; init.body = JSON.stringify(body); }
  const r = await fetch(url, init);
  return {status: r.status, text: await r.text()};
}
"""


class FomoApiError(Exception):
    pass


class QueueBusy(FomoApiError):
    """Queue over the limit: a droppable (scanner) read skips this round."""


class RateLimited(FomoApiError):
    pass


class LoggedOut(FomoApiError):
    pass


class NeedsCapturedBody(FomoApiError):
    """Request bodies are copied from the tab's own network requests, never guessed."""


class FomoApi:
    def __init__(self, fetcher, hub=None, gap: float = 1.2, max_gap: float = 20.0, queue_limit: int = 40,
                 raw_dir: Path | None = None, sleep=asyncio.sleep, clock=time.monotonic):
        self._fetcher = fetcher  # async (url, method, body) -> (status, text)
        self.hub, self.base_gap, self.gap, self.max_gap = hub, gap, gap, max_gap
        self.queue_limit = queue_limit
        self.raw_dir = raw_dir
        self._sleep, self._clock = sleep, clock
        self._heap: list = []
        self._seq = itertools.count()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._last_call = -1e9
        self._raw_saved: set[str] = set()
        self.consecutive_401 = 0
        self.logged_out = False
        self.calls = 0

    # ---- state ------------------------------------------------------------------------------
    def depth(self) -> int:
        return len(self._heap)

    def scanner_should_skip(self) -> bool:
        return self.depth() > self.queue_limit

    def session_ok(self) -> bool:
        return not self.logged_out

    # ---- queue ------------------------------------------------------------------------------
    async def request(self, path: str, *, method: str = "GET", body=None, bot: str = "scanner",
                      name: str | None = None, droppable: bool = False):
        if droppable and self.scanner_should_skip():
            raise QueueBusy(f"queue {self.depth()} > {self.queue_limit}")
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (PRIORITY[bot], next(self._seq), path, method, body, name or path, fut))
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="fomo-api")
        return await fut

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        for item in self._heap:
            if not item[-1].done():
                item[-1].cancel()
        self._heap.clear()

    async def _run(self) -> None:
        while True:
            if not self._heap:
                self._wake.clear()
                await self._wake.wait()
                continue
            prio, seq, path, method, body, name, fut = heapq.heappop(self._heap)
            if fut.done():
                continue
            wait = self._last_call + self.gap - self._clock()
            if wait > 0:
                await self._sleep(wait)
            try:
                fut.set_result(await self._execute(path, method, body, name))
            except asyncio.CancelledError:
                fut.cancel()
                raise
            except Exception as e:  # noqa: BLE001
                if not fut.done():
                    fut.set_exception(e)

    async def _execute(self, path, method, body, name):
        url = path if path.startswith("http") else BASE + path
        for attempt in range(3):
            self._last_call = self._clock()
            self.calls += 1
            status, text = await self._fetcher(url, method, body)
            if status == 429:
                self.gap = min(self.gap * 2, self.max_gap)
                log.warning("FOMO 429, gap now %.1fs", self.gap)
                await self._sleep(self.gap)
                continue
            return await self._finish(status, text, name)
        raise RateLimited(f"429 after retries on {name}")

    async def _finish(self, status: int, text: str, name: str):
        if status == 401:
            self.consecutive_401 += 1
            if self.consecutive_401 >= 2 and not self.logged_out:
                self.logged_out = True
                log.error("FOMO logged out (401 twice)")
                if self.hub:
                    self.hub.bus.publish("fomo_logged_out", {})
            raise LoggedOut("401 from FOMO")
        if 200 <= status < 300:
            self.gap = max(self.base_gap, self.gap * 0.9)  # ease back after a 429
            self.consecutive_401 = 0
            if self.logged_out:
                self.logged_out = False
                log.info("FOMO session proven again (API 200)")
                if self.hub:
                    self.hub.bus.publish("fomo_resumed", {})
            self._save_raw(name, text)
            return await asyncio.to_thread(json.loads, text) if len(text) > 20000 else json.loads(text)
        raise FomoApiError(f"HTTP {status} on {name}")

    def _save_raw(self, name: str, text: str) -> None:
        """One raw response per source, for fields.py to check the keys against."""
        key = "".join(c for c in name if c.isalnum() or c in "-_") or "response"
        if key in self._raw_saved:
            return
        self._raw_saved.add(key)
        d = self.raw_dir or config.LOG_DIR / "raw"
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{key}.json"
            if not p.exists():
                p.write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("could not save raw sample: %s", e)

    # ---- endpoints (spec 3.2) ----------------------------------------------------------------
    async def user_by_handle(self, handle: str, bot: str = "verify"):
        return await self.request(f"/v2/users/userHandle/{handle.lstrip('@')}", bot=bot, name="userHandle")

    async def swaps(self, user_id: str, page: int = 1, bot: str = "verify"):
        return await self.request(f"/v2/users/{user_id}/swaps?page={page}", bot=bot, name="swaps")

    async def balances(self, user_id: str, bot: str = "executor"):
        return await self.request(f"/v2/users/{user_id}/balances", bot=bot, name="balances")

    async def leaderboard(self, period: str, bot: str = "scanner"):
        if period not in ("24h", "7d", "30d"):
            raise ValueError("period must be 24h, 7d or 30d")
        return await self.request(f"/v2/leaderboard/{period}", bot=bot, name="leaderboard")

    async def filter_tokens(self, mints: list[str], bot: str = "scanner", droppable: bool = True):
        if not 0 < len(mints) <= 20:
            raise ValueError("filterTokens takes 1 to 20 mints")
        return await self.request("/proxy/filterTokens", method="POST", bot=bot, name="filterTokens",
                                  body=[f"{m}:{SOLANA_CHAIN_ID}" for m in mints], droppable=droppable)

    async def token_details(self, body, bot: str = "analyst"):
        return await self.request("/proxy/tokenDetails", method="POST", body=self._need(body, "tokenDetails"),
                                  bot=bot, name="tokenDetails")

    async def token_warnings(self, body, bot: str = "risk"):
        return await self.request("/proxy/tokenWarnings", method="POST", body=self._need(body, "tokenWarnings"),
                                  bot=bot, name="tokenWarnings")

    async def hodlers_top(self, query: str, bot: str = "analyst"):
        return await self.request(f"/hodlers/top?tokens={query}", bot=bot, name="hodlers")

    @staticmethod
    def _need(body, name):
        if body is None:
            raise NeedsCapturedBody(f"{name}: copy the request body from the tab's own network requests "
                                    f"(see logs/raw/requests/)")
        return body


def page_fetcher(get_page):
    """Fetcher bound to the desk tab. get_page() returns the live Playwright page (re-attached if needed)."""

    async def fetch(url: str, method: str, body):
        page = await get_page()
        res = await page.evaluate(FETCH_JS, {"url": url, "method": method, "body": body})
        return res["status"], res["text"]

    return fetch


def attach_request_recorder(page, out_dir: Path | None = None) -> None:
    """Record the tab's OWN calls to the FOMO API (method, url, body) so request bodies can be copied
    instead of guessed. Headers and cookies are never recorded."""
    d = out_dir or config.LOG_DIR / "raw" / "requests"
    seen: set[str] = set()

    def on_request(req):
        if not req.url.startswith(BASE):
            return
        path = req.url[len(BASE):].split("?")[0]
        if path in seen:
            return
        seen.add(path)
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / (path.strip("/").replace("/", "_") + ".json")).write_text(
                json.dumps({"method": req.method, "path": path, "body": req.post_data}), encoding="utf-8")
        except OSError:
            pass

    page.on("request", on_request)
