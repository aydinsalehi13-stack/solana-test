"""The ONLY module that touches FOMO web. Drives the dedicated desk Chrome over CDP on 127.0.0.1.

Hard rules enforced here (CLAUDE.md section 0):
  2  in PAPER the primary Buy/Sell button and Confirm are never pressed: the flow stops at the quote.
  4  a deny-regex raises BEFORE any click on Withdraw, Deposit, Apple Pay, Orders, Perps, Send, settings,
     export/key screens, Discover, Thesis, links outside fomo.family, downloads, or any box to tick.
  7  page text is data. It is parsed with fixed regexes and never reaches an LLM or changes settings.
  8  every FOMO URL comes from links.py.

Selectors follow the texts in fomo_web.md. They have NOT yet been verified against the live page (that
needs the owner's login); anything unexpected stops, takes a screenshot to logs/fail/ and asks.
"""
from __future__ import annotations

import asyncio
import base64
import heapq
import itertools
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import config
import links

log = logging.getLogger("desk.browser")

BOT_COLORS = {"scanner": "#64d2ff", "analyst": "#ffd60a", "brain": "#bf5af2", "executor": "#00d35a",
              "risk": "#ff6b9a", "orion": "#06B6D4"}
LANE_PRIORITY = {"risk": 0, "executor": 5}  # Risk sells jump the queue; everyone else waits their turn

# ---- hard rule 4: the deny list ---------------------------------------------------------------------
DENY_RE = re.compile(
    r"withdraw|deposit|apple\s*pay|\borders?\b|\bperps?\b|\bsend\b|settings?|\bexport\b|private\s*key|"
    r"seed|recovery|\bconfirm\b|discover|thesis|download|\bgear\b|log\s*out|sign\s*out|disconnect|delete",
    re.I,
)
SAFE_HOST = "fomo.family"


class ClickDenied(Exception):
    """Raised before a click that the hard rules forbid."""


class LoggedOutError(Exception):
    pass


class CoinNotOpened(Exception):
    pass


class NeedsHuman(Exception):
    """Something not described in fomo_web.md: stopped, screenshot saved, ask the owner."""


def assert_click_allowed(info: dict, *, mode: str = "PAPER", primary: bool = False) -> None:
    """info: text, aria, title, href, download, checkbox of what is about to be clicked."""
    if primary and mode != "LIVE":
        raise ClickDenied("PAPER: the primary Buy/Sell button is never pressed")
    label = " | ".join(str(info.get(k) or "") for k in ("text", "aria", "title"))
    if info.get("checkbox"):
        raise ClickDenied("a box to tick is never ticked")
    if info.get("download"):
        raise ClickDenied("downloads are never clicked")
    href = info.get("href") or ""
    if href:
        host = (urlparse(href).hostname or "").lower()
        if host != SAFE_HOST and not host.endswith("." + SAFE_HOST):
            raise ClickDenied(f"link outside fomo.family: {host or href[:40]}")
    if not primary:  # the primary button's own label ("Buy SEND") is a token symbol, not a control name
        m = DENY_RE.search(label)
        if m:
            raise ClickDenied(f"denied control: {m.group(0)!r} in {label[:60]!r}")


# ---- lane: one click at a time, Risk jumps the queue -----------------------------------------------------
class Lane:
    def __init__(self) -> None:
        self.holder: str | None = None
        self._heap: list = []
        self._seq = itertools.count()

    @asynccontextmanager
    async def hold(self, bot: str):
        prio = LANE_PRIORITY.get(bot, 10)
        if self.holder is not None:
            fut = asyncio.get_running_loop().create_future()
            entry = (prio, next(self._seq), fut)
            heapq.heappush(self._heap, entry)
            try:
                await fut
            except asyncio.CancelledError:
                if fut.done() and not fut.cancelled():
                    self._release()  # we were handed the lane while being cancelled
                else:
                    self._heap = [e for e in self._heap if e is not entry]
                    heapq.heapify(self._heap)
                raise
        self.holder = bot
        try:
            yield
        finally:
            self._release()

    def _release(self) -> None:
        self.holder = None
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                self.holder = "?"  # reserved for the waiter until it sets its own name
                fut.set_result(True)
                return


# ---- page text parsing (fixed regexes; the text is data) ---------------------------------------------------
QUOTE_RE = re.compile(r"^\s*([\d][\d.,]*\s?[KMBkmb]?)\s+(\S+)\s+\(([-+]?\d+(?:\.\d+)?)%\)\s*$", re.M)
FEE_RE = re.compile(r"\$\s?([\d][\d.,]*)\s+fee", re.I)
SHORT_ADDR_RE = re.compile(r"([1-9A-HJ-NP-Za-km-z]{3,})(?:\.\.\.|…)([1-9A-HJ-NP-Za-km-z]{3,})")
MCAP_RE = re.compile(r"Market cap\s*:?\s*\$?\s*([\d][\d.,]*\s?[KMBTkmbt]?)", re.I)
CASH_RE = re.compile(r"^\$\s?[\d,]+(?:\.\d+)?$")


@dataclass
class QuoteResult:
    ok: bool
    action: str  # buy | sell
    mint: str
    usd: float | None = None
    pct: int | None = None
    quote_text: str | None = None
    fee_text: str | None = None
    tokens_text: str | None = None
    impact_pct: float | None = None
    fee_usd: float | None = None
    reason: str | None = None  # why it was skipped / aborted
    primary_label: str | None = None
    quote_seen_at: float | None = None
    positions_text: str | None = None
    shots: list[str] = field(default_factory=list)
    stopped_before_button: bool = True  # always true in PAPER


def parse_quote(text: str) -> dict | None:
    q = QUOTE_RE.search(text)
    f = FEE_RE.search(text)
    if not q or not f:
        return None
    return {"quote_text": q.group(0).strip(), "tokens_text": f"{q.group(1).strip()} {q.group(2)}",
            "impact_pct": float(q.group(3)), "fee_text": f.group(0).strip(),
            "fee_usd": float(f.group(1).replace(",", ""))}


def short_address_matches(text: str, mint: str) -> bool:
    return any(mint.startswith(a) and mint.endswith(b) for a, b in SHORT_ADDR_RE.findall(text))


JS_INFO = """
(el) => {
  const c = el.closest('button,a,[role=button],[role=tab],label') || el;
  const a = el.closest('a');
  return {text: (c.innerText || c.textContent || '').trim().slice(0, 200),
          aria: c.getAttribute('aria-label') || '', title: c.getAttribute('title') || '',
          href: a ? a.href : '', download: !!el.closest('a[download]'),
          checkbox: !!(el.matches('input[type=checkbox],input[type=radio],[role=checkbox],[role=switch]') ||
                       c.matches('input[type=checkbox],input[type=radio],[role=checkbox],[role=switch]') ||
                       c.querySelector('input[type=checkbox],input[type=radio],[role=checkbox],[role=switch]'))};
}
"""
JS_POINT_INFO = """
([x, y]) => { const el = document.elementFromPoint(x, y); if (!el) return null; return (%s)(el); }
""" % JS_INFO.strip()

JS_CURSOR = """
({name, color, x, y}) => {
  if (!document.getElementById('__desk_cursor_style')) {
    const st = document.createElement('style'); st.id = '__desk_cursor_style';
    st.textContent = '.__desk_cursor{position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;' +
      'user-select:none;transition:transform 300ms cubic-bezier(.22,.8,.3,1);will-change:transform}' +
      '.__desk_cursor::after{content:attr(data-name);position:absolute;left:16px;top:12px;font:600 10px sans-serif;' +
      'padding:1px 5px;border-radius:6px;background:#000c;color:var(--c);white-space:nowrap}';
    document.head.appendChild(st);
  }
  let el = document.getElementById('__desk_cursor_' + name);
  if (!el) {
    el = document.createElement('div'); el.id = '__desk_cursor_' + name; el.className = '__desk_cursor';
    el.dataset.name = name; el.style.setProperty('--c', color); el.setAttribute('aria-hidden', 'true');
    el.innerHTML = '<svg width="18" height="18" viewBox="0 0 18 18"><path d="M1 1L1 14L5 10.5L8 17L10.5 16L7.5 9.5L13 9.5Z" fill="' + color + '" stroke="#000" stroke-width="1"/></svg>';
    document.body.appendChild(el);
  }
  el.style.transform = 'translate(' + x + 'px,' + y + 'px)';
}
"""
JS_HEADER = """
() => {
  const out = {avatar: false, cash: null, wide: window.innerWidth};
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.top > 90 || r.height === 0 || r.width === 0) continue;
    if (el.tagName === 'IMG' && r.width >= 18 && r.width <= 72) out.avatar = true;
    if (el.children.length === 0) { const t = (el.textContent || '').trim(); if (t.length < 24 && /^\\$\\s?[\\d,]+(\\.\\d+)?$/.test(t)) out.cash = out.cash || t; }
  }
  return out;
}
"""
JS_BOX_IN_TRADE_PANEL = """
() => {
  const f = document.querySelector('input[placeholder="Enter amount"]');
  if (!f) return false;
  let n = f.parentElement;
  for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
    if ([...n.querySelectorAll('button')].some(b => /^(Buy|Sell)\\b/.test((b.innerText || '').trim()) && !/^(Buy|Sell)$/.test((b.innerText || '').trim()))) break;
  }
  if (!n) return false;
  return [...n.querySelectorAll('input[type=checkbox],input[type=radio],[role=checkbox],[role=switch]')]
    .some(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
}
"""
JS_PRIMARY = """
() => {
  const b = [...document.querySelectorAll('button,[role=button]')].filter(e => {
    const t = (e.innerText || '').trim(), r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && /^(Buy|Sell)\\b/.test(t) && !/^(Buy|Sell)$/.test(t);
  });
  if (!b.length) return null;
  const e = b[b.length - 1];
  return {text: (e.innerText || '').trim(), disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true')};
}
"""
JS_DIALOG = "() => [...document.querySelectorAll('[role=dialog],[aria-modal=true]')].some(e => {const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0;})"
JS_LOGIN_FORM = "() => !!document.querySelector('input[type=password],input[autocomplete=one-time-code],input[type=email]')"
JS_CHART = """
() => {
  const c = [...document.querySelectorAll('canvas,iframe,svg')].map(e => e.getBoundingClientRect()).filter(r => r.width >= 200 && r.height >= 100);
  return c.length ? {x: c[0].x, y: c[0].y, width: c[0].width, height: c[0].height} : null;
}
"""


class Browser:
    def __init__(self, hub=None, mode: str = "PAPER", cdp_port: int | None = None):
        if mode != "PAPER":
            raise NotImplementedError("LIVE clicking is not enabled: it needs every gate in CLAUDE.md section 0.1")
        self.mode, self.hub = mode, hub
        self.cdp_url = f"http://{config.CDP_HOST}:{cdp_port or config.cdp_port()}"
        self.lane = Lane()
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._last_relaunch = 0.0
        self.primary_clicks = 0  # must stay 0 in PAPER (asserted by tests)
        self.header_wait = 6.0  # seconds to wait for avatar + cash before calling it logged out
        self.quote_timeout = 10.0  # S5: a quote slower than this is retried once, then skipped

    # ---- connection ---------------------------------------------------------------------------------------
    async def connect(self) -> None:
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.page = None
        await self.ensure_page()

    async def close(self) -> None:
        try:
            if self._pw:
                await self._pw.stop()
        finally:
            self._pw = self.browser = self.context = self.page = None

    async def ensure_page(self):
        """One tab named 'desk'. Reattaches if the tab was closed (page-None crash from the first build)."""
        if self.browser is None or not self.browser.is_connected():
            await self._reconnect()
        if self.page is not None and not self.page.is_closed():
            return self.page
        pages = [p for p in self.context.pages if not p.is_closed()]
        for p in pages:
            try:
                if await p.evaluate("window.name") == "desk":
                    self.page = p
                    return p
            except Exception:  # noqa: BLE001
                continue
        self.page = pages[0] if pages else await self.context.new_page()
        await self.page.evaluate("window.name = 'desk'")
        return self.page

    async def _reconnect(self) -> None:
        """CDP lost: relaunch Chrome (at most once per 60 s), reattach. The session is re-proven by ensure_ready()."""
        import chrome_launcher

        if time.time() - self._last_relaunch < 60:
            raise ConnectionError("CDP lost; relaunch already tried in the last 60 s")
        self._last_relaunch = time.time()
        res = await asyncio.to_thread(chrome_launcher.ensure_chrome)
        if res["status"] == "failed":
            raise ConnectionError(res.get("error", "Chrome relaunch failed"))
        if self._pw is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.page = None

    async def get_page(self):
        return await self.ensure_page()

    # ---- reading ------------------------------------------------------------------------------------------
    async def page_text(self) -> str:
        page = await self.ensure_page()
        return await page.evaluate("document.body ? document.body.innerText : ''")

    async def read_header(self) -> dict:
        page = await self.ensure_page()
        h = await page.evaluate(JS_HEADER)
        h["url"] = page.url
        return h

    async def is_logged_out(self) -> bool:
        h = await self.read_header()
        host = (urlparse(h["url"]).hostname or "").lower()
        if host and host != SAFE_HOST and not host.endswith("." + SAFE_HOST) and h["url"] != "about:blank":
            return False  # not on FOMO at all: not "logged out", just elsewhere
        return not (h["avatar"] and h["cash"])

    async def ensure_wide(self) -> None:
        """FOMO web refuses narrow windows. Widen the window to 1440 and reload."""
        page = await self.ensure_page()
        if await page.evaluate("window.innerWidth") >= 1400:
            return
        try:
            s = await self.context.new_cdp_session(page)
            w = await s.send("Browser.getWindowForTarget")
            await s.send("Browser.setWindowBounds", {"windowId": w["windowId"],
                                                      "bounds": {"windowState": "normal", "width": 1440, "height": 900}})
            await s.detach()
            await page.reload()
        except Exception as e:  # noqa: BLE001
            log.warning("could not widen window: %s", e)

    # ---- S0 ---------------------------------------------------------------------------------------------------
    async def ensure_ready(self, fresh: bool = True) -> dict:
        """S0 before every order: desk tab on fomo.family, avatar + cash in the header, no pop-up.

        fresh=True loads the FOMO home page first, so a stale page can never pass for a live session.
        Urgent stop-sells use fresh=False (open_coin still detects a landing page): a stop never waits.
        """
        page = await self.ensure_page()
        host = (urlparse(page.url).hostname or "").lower()
        if fresh or (host != SAFE_HOST and not host.endswith("." + SAFE_HOST)):
            await page.goto(links.SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
        await self.ensure_wide()
        await self._clear_popup()
        deadline = time.time() + self.header_wait  # the page hydrates after domcontentloaded
        while True:
            h = await self.read_header()
            if h["avatar"] and h["cash"]:
                return h
            if time.time() >= deadline:
                break
            await asyncio.sleep(0.25)
        await self.fail_shot("logged_out_or_header_missing")
        raise LoggedOutError("no avatar + cash in the header: log in to FOMO in the desk Chrome")

    async def _clear_popup(self) -> None:
        page = await self.ensure_page()
        if await page.evaluate(JS_DIALOG):
            await page.keyboard.press("Escape")  # once
            await asyncio.sleep(0.4)
            if await page.evaluate(JS_DIALOG):
                await self.fail_shot("unknown_popup")
                raise NeedsHuman("unknown pop-up, pressed Escape once, still there (screenshot in logs/fail/)")

    # ---- clicking (guarded) -------------------------------------------------------------------------------
    async def _move_cursor(self, bot: str, x: float, y: float) -> None:
        page = await self.ensure_page()
        await page.evaluate(JS_CURSOR, {"name": bot, "color": BOT_COLORS.get(bot, "#ffffff"), "x": x, "y": y})
        await asyncio.sleep(0.3)  # the arrow glides for 300 ms before the click

    async def _click_locator(self, loc, bot: str, *, primary: bool = False) -> None:
        page = await self.ensure_page()
        if primary:
            assert_click_allowed({}, mode=self.mode, primary=True)  # PAPER: raises. LIVE is not built yet.
        await loc.scroll_into_view_if_needed(timeout=5000)
        box = await loc.bounding_box()
        if not box:
            raise NeedsHuman("target has no box on screen")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        assert_click_allowed(await loc.evaluate(JS_INFO), mode=self.mode)
        # also check what is REALLY under the pointer (an overlay could sit on top)
        under = await page.evaluate(JS_POINT_INFO, [x, y])
        if under is not None:
            assert_click_allowed(under, mode=self.mode)
        await self._move_cursor(bot, x, y)
        await page.mouse.move(x, y)
        await page.mouse.click(x, y)

    async def click_text(self, label: str, bot: str = "executor", exact: bool = True) -> None:
        page = await self.ensure_page()
        loc = page.get_by_text(label, exact=exact).locator("visible=true").first
        if await loc.count() == 0:
            raise NeedsHuman(f"{label!r} not found on the page")
        await self._click_locator(loc, bot)

    async def press_primary(self, bot: str = "executor") -> None:
        """The primary Buy/Sell button. PAPER: always refused. LIVE: not built until the gates pass."""
        assert_click_allowed({}, mode=self.mode, primary=True)
        raise NotImplementedError("LIVE clicking is not enabled")

    # ---- S1 ---------------------------------------------------------------------------------------------------
    async def open_coin(self, mint: str, timeout: float = 25.0) -> None:
        page = await self.ensure_page()
        await page.goto(links.coin_url(mint), wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(0.6)
        if await self.is_logged_out():
            await self.fail_shot("coin_landing_logged_out")
            raise LoggedOutError("landing/signup page instead of the coin page: logged out")
        if await self._wait_coin_ready(mint, timeout):
            return
        if await self._open_via_search(mint) and await self._wait_coin_ready(mint, 12):
            return
        await self.fail_shot("coin_not_opened")
        raise CoinNotOpened(f"coin page for {mint[:6]}...{mint[-6:]} did not finish loading")

    async def _reload_coin(self, mint: str) -> None:
        """S5: reload the page once and wait for the coin page to finish loading again."""
        await (await self.ensure_page()).reload(wait_until="domcontentloaded")
        if not await self._wait_coin_ready(mint, 20):
            await self.fail_shot("reload_not_ready")
            raise CoinNotOpened(f"coin page for {mint[:6]}...{mint[-6:]} not ready after reload")

    async def _wait_coin_ready(self, mint: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = await self.page_text()
            if short_address_matches(text, mint) and MCAP_RE.search(text) and await self._chart_drawn():
                return True
            await asyncio.sleep(0.25)
        return False

    async def _chart_drawn(self) -> bool:
        page = await self.ensure_page()
        box = await page.evaluate(JS_CHART)
        if not box:
            return False
        png = await page.screenshot(clip=box, type="png")
        return len(png) > 4000  # an empty chart compresses to almost nothing

    async def _open_via_search(self, mint: str) -> bool:
        """Fallback: search -> paste the mint -> the Tokens row whose address matches. Never Trending."""
        page = await self.ensure_page()
        try:
            await page.goto(links.SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
            box = page.get_by_placeholder(re.compile("search", re.I)).first
            await self._click_locator(box, "executor")
            await page.keyboard.type(mint, delay=15)
            await asyncio.sleep(1.2)
            rows = page.get_by_text(re.compile(re.escape(mint[:6]) + r".*" + re.escape(mint[-6:]))).locator("visible=true")
            if await rows.count() == 0:
                return False
            await self._click_locator(rows.first, "executor")
            return True
        except (ClickDenied, NeedsHuman):
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("search fallback failed: %s", e)
            return False

    # ---- amount / quote -------------------------------------------------------------------------------------
    async def _read_amount(self) -> float | None:
        page = await self.ensure_page()
        v = await page.get_by_placeholder("Enter amount").first.input_value()
        try:
            return float(v.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None

    async def _type_amount(self, usd: float, bot: str) -> bool:
        """Type the size with 2 decimals; read it back; retype once; else False ('amount mismatch')."""
        page = await self.ensure_page()
        field_ = page.get_by_placeholder("Enter amount").first
        for _ in range(2):
            await self._click_locator(field_, bot)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(f"{usd:.2f}", delay=25)
            got = await self._read_amount()
            if got is not None and abs(got - round(usd, 2)) < 0.005:
                return True
        return False

    async def _click_preset(self, label: str, bot: str) -> None:
        await self.click_text(label, bot)

    async def _wait_quote(self, timeout: float) -> dict | str | None:
        """Returns the parsed quote, 'impact' for 'Price impact too high', or None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = await self.page_text()
            if re.search(r"price impact too high", text, re.I):
                return "impact"
            if "Fetching quote" not in text:
                q = parse_quote(text)
                if q:
                    q["seen_at"] = time.time()
                    return q
            await asyncio.sleep(0.15)
        return None

    async def _read_primary(self) -> dict | None:
        page = await self.ensure_page()
        return await page.evaluate(JS_PRIMARY)

    async def _positions_text(self) -> str | None:
        text = await self.page_text()
        m = re.search(r"Your positions\s*\n(.{0,160})", text, re.S)
        return " ".join(m.group(1).split()) if m else None

    # ---- S2 buy (PAPER: stops at the quote) -------------------------------------------------------------------
    async def buy_to_quote(self, mint: str, usd: float, *, bot: str = "executor", order_id: int | None = None) -> QuoteResult:
        r = QuoteResult(ok=False, action="buy", mint=mint, usd=round(usd, 2))
        async with self.lane.hold(bot):
            await self.ensure_ready()
            await self.open_coin(mint)
            r.shots.append(await self.order_shot(order_id, "01_coin"))
            await self.click_text("Buy", bot)
            if await self._box_to_tick():
                return self._skip(r, "box to tick")
            preset = f"${int(usd)}" if usd in (10, 100, 500, 1000) else None
            if preset:
                await self._click_preset(preset, bot)
            if not (preset and await self._amount_ok(usd)) and not await self._type_amount(usd, bot):
                await self.fail_shot("amount_mismatch")
                return self._skip(r, "amount mismatch")
            q = await self._wait_quote(self.quote_timeout)
            if q is None:  # quote > 10 s: reload once, then skip
                await self._reload_coin(mint)
                await self.click_text("Buy", bot)
                if not await self._type_amount(usd, bot):
                    return self._skip(r, "amount mismatch")
                q = await self._wait_quote(self.quote_timeout)
            if q is None:
                await self.fail_shot("no_quote")
                return self._skip(r, "no quote in 10 s")
            if q == "impact":
                return self._skip(r, "price impact too high")
            r.shots.append(await self.order_shot(order_id, "02_quote"))
            prim = await self._read_primary()
            r.primary_label = prim["text"] if prim else None
            if prim is None:
                await self.fail_shot("no_primary_button")
                return self._skip(r, "no primary button")
            if re.search(r"apple\s*pay", prim["text"], re.I):
                return self._skip(r, "no cash (Buy with Apple Pay): never clicked")
            if prim["disabled"]:
                await self.fail_shot("button_greyed")
                return self._skip(r, "button greyed")
            self._fill(r, q)
            r.ok = True  # PAPER: fill = this quote. The button is never pressed.
            return r

    async def _amount_ok(self, usd: float) -> bool:
        got = await self._read_amount()
        return got is not None and abs(got - round(usd, 2)) < 0.005

    async def _box_to_tick(self) -> bool:
        return bool(await (await self.ensure_page()).evaluate(JS_BOX_IN_TRADE_PANEL))

    # ---- S3/S4 sells (PAPER: opens the Sell tab, reads the quote, stops) ------------------------------------------
    async def sell_to_quote(self, mint: str, pct: int = 100, *, bot: str = "executor", order_id: int | None = None,
                            urgent: bool = False) -> QuoteResult:
        if pct not in (10, 25, 50, 100):
            raise ValueError("pct must be 10, 25, 50 or 100")
        r = QuoteResult(ok=False, action="sell", mint=mint, pct=pct)
        async with self.lane.hold(bot):
            await self.ensure_ready(fresh=not urgent)
            await self.open_coin(mint)
            r.positions_text = await self._positions_text()
            r.shots.append(await self.order_shot(order_id, "01_coin"))
            await self.click_text("Sell", bot)
            await self._click_preset(f"{pct}%", bot)
            text = await self.page_text()
            if re.search(r"minimum amount", text, re.I):
                if pct < 100:  # whole position over $2 -> sell 100% instead; else it is dust
                    await self._click_preset("100%", bot)
                    r.pct = 100
                    text = await self.page_text()
                if re.search(r"minimum amount", text, re.I):
                    return self._skip(r, "dust: below the $2 minimum")
            attempts = 3 if urgent else 1  # a stop never waits: reload and repeat at once
            q = None
            for _ in range(attempts):
                q = await self._wait_quote(self.quote_timeout)
                if q is not None:
                    break
                await self._reload_coin(mint)
                await self.click_text("Sell", bot)
                await self._click_preset(f"{r.pct}%", bot)
            if q is None:
                await self.fail_shot("no_sell_quote")
                return self._skip(r, "no quote in 10 s")
            if q == "impact":  # sells try 50% then the rest
                return self._skip(r, "price impact too high")
            r.shots.append(await self.order_shot(order_id, "02_quote"))
            prim = await self._read_primary()
            r.primary_label = prim["text"] if prim else None
            if prim is None or prim["disabled"]:
                await self.fail_shot("sell_button_missing")
                return self._skip(r, "sell button missing or greyed")
            self._fill(r, q)
            r.ok = True
            return r

    @staticmethod
    def _fill(r: QuoteResult, q: dict) -> None:
        r.quote_text, r.fee_text, r.tokens_text = q["quote_text"], q["fee_text"], q["tokens_text"]
        r.impact_pct, r.fee_usd, r.quote_seen_at = q["impact_pct"], q["fee_usd"], q["seen_at"]

    @staticmethod
    def _skip(r: QuoteResult, reason: str) -> QuoteResult:
        r.ok, r.reason = False, reason
        return r

    # ---- frames and screenshots -------------------------------------------------------------------------------
    async def capture_jpeg(self, width: int = 1280, quality: int = 60) -> bytes:
        page = await self.ensure_page()
        vw, vh = await page.evaluate("[window.innerWidth, window.innerHeight]")
        s = await self.context.new_cdp_session(page)
        try:
            res = await s.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": quality,
                "clip": {"x": 0, "y": 0, "width": vw, "height": vh, "scale": min(1.0, width / vw)}})
        finally:
            await s.detach()
        return base64.b64decode(res["data"])

    async def capture_frame(self, keep: int = 300) -> bool:
        """data/screen.jpg every second, last 300 in data/frames/. Nothing while a login form is open."""
        page = await self.ensure_page()
        paused = config.DATA_DIR / "screen.paused"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if await page.evaluate(JS_LOGIN_FORM):
            paused.write_text("Paused · private")
            return False
        paused.unlink(missing_ok=True)
        jpg = await self.capture_jpeg()
        tmp = config.DATA_DIR / "screen.jpg.tmp"
        tmp.write_bytes(jpg)
        tmp.replace(config.DATA_DIR / "screen.jpg")
        fdir = config.DATA_DIR / "frames"
        fdir.mkdir(exist_ok=True)
        (fdir / f"{time.time():.3f}.jpg").write_bytes(jpg)
        old = sorted(fdir.glob("*.jpg"))[:-keep]
        for p in old:
            p.unlink(missing_ok=True)
        return True

    async def frame_loop(self, hub=None, every: float = 1.0) -> None:
        while True:
            try:
                await self.capture_frame()
            except Exception as e:  # noqa: BLE001
                log.debug("frame failed: %s", e)
            if hub:
                hub.heartbeats.beat("frames")
            await asyncio.sleep(every)

    async def order_shot(self, order_id: int | None, label: str) -> str:
        d = config.LOG_DIR / "orders" / str(order_id if order_id is not None else "adhoc")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{label}.jpg"
        try:
            p.write_bytes(await self.capture_jpeg())
        except Exception as e:  # noqa: BLE001
            log.warning("order shot failed: %s", e)
        return str(p.relative_to(config.LOG_DIR.parent)) if config.LOG_DIR.parent in p.parents else str(p)

    async def fail_shot(self, reason: str) -> str:
        d = config.LOG_DIR / "fail"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{int(time.time())}_{re.sub(r'[^a-z0-9_]+', '_', reason.lower())}.jpg"
        try:
            p.write_bytes(await self.capture_jpeg())
        except Exception as e:  # noqa: BLE001
            log.warning("fail shot failed: %s", e)
        return str(p)
