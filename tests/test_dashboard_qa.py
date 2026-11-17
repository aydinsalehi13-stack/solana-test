"""Dashboard QA in a real Chromium: 1440 and 390 screenshots, no console errors, every panel fed by /api/live, live updates without a
reload, reconnect after an API drop, empty/error/login states, no token visible, page CPU under 30% of one core.

The data is the seeded DEMO database from seed.py (fake coins, clearly named). The screenshots in proof/phase6/ are of that demo data."""
import asyncio
import time
from pathlib import Path

import pytest

import links
from bus import Hub
from conftest import ROOT, _chrome_path
from db import Database
from seed import DEMO_MINTS, seed_demo
from serverkit import LiveServer

TOKEN = "qa-token-ABCDEFGH-1234567890"
PROOF = ROOT / "proof" / "phase6"
pw_mod = pytest.importorskip("playwright.async_api")
CHROME = _chrome_path()
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome on this machine")


def make_hub():
    hub = Hub()
    for n in ("scanner", "analyst", "brain", "executor", "risk", "orion", "loop", "feed", "heartbeat_writer"):
        hub.heartbeats.beat(n)
    hub.status["feed"] = lambda: {"badge": "PumpPortal live", "pumpportal": {"state": "live"}, "watching": 12}
    hub.status["prompt"] = lambda: {"cash": 138.0, "open": 1, "waiting": 2, "room": 9, "trades_tonight": 3, "browser": True}
    hub.status["orion_fast"] = lambda: {"mode": "PAPER", "cash": 144.0, "equity": 150.0, "open": 1, "pending": 0, "trades": 4}
    hub.objects["marks"] = lambda: {DEMO_MINTS[3]: {"price": 0.62, "tokens": 36.0, "realized": 0.0}, DEMO_MINTS[8]: {"price": 2.3, "tokens": 3.0, "realized": 0.0}}
    for ms in (80, 95, 120, 400, 800, 1500):
        hub.metrics.record("analyst.layer8", ms)
    for ms in (4, 5, 7, 9):
        hub.metrics.record("feed.handle", ms)
    for ms in (0.4, 0.6, 0.9, 1.2):
        hub.lag.add(ms)
    return hub


class Env:
    def __init__(self, db, hub, srv, pw, browser):
        self.db, self.hub, self.srv, self.pw, self.browser = db, hub, srv, pw, browser

    async def page(self, width=1440, height=900, mobile=False, token=TOKEN, **ctx_kw):
        ctx = await self.browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, is_mobile=mobile, has_touch=mobile, **ctx_kw)
        if token:
            await ctx.add_init_script(f"localStorage.setItem('desk_token', '{token}')")
        page = await ctx.new_page()
        page.errors = []
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        await page.goto(self.srv.url)
        return page


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "qa.db")
    await db.start()
    await seed_demo(db)
    hub = make_hub()
    srv = await LiveServer(db, hub, TOKEN).start()
    async with pw_mod.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        yield Env(db, hub, srv, pw, browser)
        await browser.close()
    await srv.stop()
    await db.stop()


@pytest.fixture
async def empty_env(tmp_path):
    db = Database(tmp_path / "qa0.db")
    await db.start()
    hub = Hub()
    srv = await LiveServer(db, hub, TOKEN).start()
    async with pw_mod.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        yield Env(db, hub, srv, pw, browser)
        await browser.close()
    await srv.stop()
    await db.stop()


def unexpected(page):
    """Console errors other than the browser noting the 401 the desk correctly gives a wrong or missing token."""
    return [e for e in page.errors if "401" not in e]


async def ready(page):
    await page.wait_for_function("document.querySelector('#bal-prompt').getAttribute('aria-label') !== 'loading'", timeout=10000)
    await page.wait_for_function("document.querySelectorAll('#agents li').length === 6", timeout=10000)


async def scroll_all(page):
    """Scroll through the page so every canvas is on screen at least once (the radar only draws while visible)."""
    h = await page.evaluate("document.documentElement.scrollHeight")
    for y in range(0, h, 500):
        await page.evaluate(f"window.scrollTo(0, {y})")
        await page.wait_for_timeout(120)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)


# ---- login / token ---------------------------------------------------------------------------------------------------------------------
async def test_login_screen_bad_token_then_good_token_and_the_token_is_never_visible(env):
    page = await env.page(token=None)
    assert await page.is_visible("#login")
    assert await page.evaluate("document.elementFromPoint(720, 450).closest('#login') !== null")  # the login covers the desk
    await page.fill("#token-input", "definitely-wrong")
    await page.click("#login-form button[type=submit]")
    await page.wait_for_selector("#login-error:not([hidden])")
    assert "Token not accepted" in await page.inner_text("#login-error")
    assert await page.evaluate("localStorage.getItem('desk_token')") is None  # a wrong token is not kept
    await page.fill("#token-input", TOKEN)
    await page.click("#login-form button[type=submit]")
    await ready(page)
    assert not await page.is_visible("#login")
    assert TOKEN not in page.url and TOKEN not in await page.content() and TOKEN not in await page.inner_text("body")
    assert await page.evaluate("document.querySelector('#token-input').value") == ""  # the field is cleared
    assert await page.evaluate("localStorage.getItem('desk_token')") == TOKEN  # kept in this browser only
    assert unexpected(page) == [], page.errors


async def test_lock_button_clears_the_token_and_shows_login(env):
    page = await env.page()
    await ready(page)
    await page.click("#lock-btn")
    assert await page.is_visible("#login")
    assert await page.evaluate("localStorage.getItem('desk_token')") is None
    await page.fill("#token-input", TOKEN)  # and it can be opened again
    await page.click("#login-form button[type=submit]")
    await ready(page)
    assert unexpected(page) == [], page.errors


async def test_a_token_the_desk_stops_accepting_returns_to_login(env):
    page = await env.page(token="stale-token-from-last-week")
    await page.wait_for_selector("#login:not([hidden])", timeout=10000)
    assert "did not accept" in await page.inner_text("#login-error")


# ---- 1440: everything fed, screenshot -----------------------------------------------------------------------------------------------------
async def test_desktop_1440_every_panel_is_fed_by_the_desk(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.wait_for_function("document.querySelectorAll('#log li').length > 20")
    await page.wait_for_timeout(1500)
    panels = await page.evaluate("[...document.querySelectorAll('[data-panel]')].map(e => e.dataset.panel)")
    assert len(panels) == 15
    t = lambda sel: page.inner_text(sel)  # noqa: E731
    assert (await page.get_attribute("#bal-prompt", "aria-label")).startswith("$1") and "$" in await page.get_attribute("#bal-orion_fast", "aria-label")
    assert "+$" in await t("#pnl-prompt") and "−$" in await t("#pnl-orion_fast")
    assert "Agents · 6 running" == await t("#agents-title")
    assert await page.locator("#agents li").count() == 6 and await page.locator("#agents svg.spark").count() == 6 and await page.locator("#agents svg.ring").count() == 6
    assert await page.locator("#positions .row").count() == 2 and "BORK" in await t("#positions")
    assert await page.locator("#audit .disc").count() == 8 and "Revoked" in await t("#audit")
    assert await page.locator("#kills .bar").count() >= 5 and await page.locator("#health li").count() >= 6
    assert "analyst.layer8" in await t("#speed") and "loop lag p95" in await t("#lag-chip")
    assert "17" in await t("#recap") and "fluffcore3" in await t("#account") and await page.locator("#settings details").count() == 7
    assert await t("#feed-badge") == "PumpPortal live" and await t("#mode-pill") == "PAPER"
    assert not await page.is_visible("#tabbar")  # the tab bar is for phones only
    assert not await page.is_visible("#offline") and await page.locator(".skeleton").count() == 0
    # no horizontal scroll, no overlapping giants
    assert await page.evaluate("document.documentElement.scrollWidth") <= 1440
    await scroll_all(page)
    PROOF.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(PROOF / "desktop_1440.png"), full_page=True)
    assert page.errors == [], page.errors


async def test_the_masonry_leaves_no_overlap_between_panels(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.wait_for_timeout(800)
    boxes = await page.evaluate("[...document.querySelectorAll('.grid > *')].filter(e => e.offsetParent).map(e => { const r = e.getBoundingClientRect(); return [e.dataset.panel, r.left, r.top + scrollY, r.right, r.bottom + scrollY]; })")
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            overlap = a[1] < b[3] - 1 and b[1] < a[3] - 1 and a[2] < b[4] - 1 and b[2] < a[4] - 1
            assert not overlap, f"{a[0]} overlaps {b[0]}"


# ---- 390: phone ---------------------------------------------------------------------------------------------------------------------------
async def test_phone_390_tabs_tap_targets_and_no_sideways_scroll(env):
    page = await env.page(390, 844, mobile=True)
    await ready(page)
    assert await page.is_visible("#tabbar") and await page.evaluate("document.documentElement.scrollWidth") <= 390
    assert await page.is_visible("#lock-btn")  # the header fits: nothing is pushed off the edge
    box = await page.locator("#lock-btn").bounding_box()
    assert box["x"] + box["width"] <= 390
    assert not await page.is_visible("#theme-btn")
    shown = {"home": "ledger", "agents": "flow", "activity": "log", "account": "settings"}
    hidden = {"home": "flow", "agents": "log", "activity": "settings", "account": "ledger"}
    PROOF.mkdir(parents=True, exist_ok=True)
    for tab in ("home", "agents", "activity", "account"):
        await page.click(f'#tabbar [data-go="{tab}"]')
        await page.wait_for_timeout(700)
        assert await page.is_visible(f'[data-panel="{shown[tab]}"]'), tab
        assert not await page.is_visible(f'[data-panel="{hidden[tab]}"]'), tab
        assert await page.evaluate("document.documentElement.scrollWidth") <= 390, f"sideways scroll on {tab}"
        small = await page.evaluate("""() => [...document.querySelectorAll('button, a.link-btn, summary')].filter(e => e.offsetParent && !e.closest('dialog'))
            .map(e => [e.id || e.className || e.textContent.trim().slice(0, 20), e.getBoundingClientRect().height]).filter(([_, h]) => h > 0 && h < 43.5)""")
        assert small == [], f"tap targets under 44 px on {tab}: {small}"
        if tab == "agents":
            await scroll_all(page)
        await page.screenshot(path=str(PROOF / f"phone_390_{tab}.png"), full_page=(tab != "home"))
    assert page.errors == [], page.errors


async def test_phone_ledger_shows_both_strategies_side_by_side(env):
    page = await env.page(390, 844, mobile=True)
    await ready(page)
    a, b = await page.locator("#bal-prompt").bounding_box(), await page.locator("#bal-orion_fast").bounding_box()
    assert abs(a["y"] - b["y"]) < 4 and a["x"] + a["width"] <= b["x"]  # same row
    assert a["x"] + a["width"] <= 390 and b["x"] + b["width"] <= 390


# ---- live updates without a reload -------------------------------------------------------------------------------------------------------------
async def test_events_update_the_page_without_a_reload(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.evaluate("window.__marker = 'same-page'")
    hub = env.hub
    await page.wait_for_timeout(600)
    hub.bus.publish("decision", {"ts": time.time(), "bot": "scanner", "mint": "LiveMint1xxxxxxxxxx", "symbol": "FRESH", "decision": "kill", "reason": "liq $9.9K", "strategy": "prompt"})
    await page.wait_for_selector("#log li:first-child >> text=FRESH", timeout=5000)
    assert "liq $9.9K" in await page.inner_text("#log li:first-child")
    for layer in range(1, 4):
        hub.bus.publish("audit_layer", {"mint": "LiveMint2xxxxxxxxxx", "layer": layer, "name": "x", "value": f"live value {layer}", "pass": 1 if layer < 3 else 0})
    await page.wait_for_function("document.querySelector('#audit').textContent.includes('live value 3')", timeout=5000)
    states = await page.evaluate("[...document.querySelectorAll('#audit .disc')].map(d => d.className)")
    assert states[0].endswith("is-ok") and states[2].endswith("is-bad") and states[3].endswith("is-off")
    hub.bus.publish("trade_close", {"mint": "LiveMint3xxxxxxxxxx", "symbol": "WINNER", "pnl_usd": 7.25, "pnl_pct": 40.3, "reason": "take profit 2x", "strategy": "prompt"})
    await page.wait_for_selector(".banner--up", timeout=5000)
    text = await page.inner_text(".banner--up")
    assert "WINNER" in text and "+$7.25" in text and "paper" in text
    hub.bus.publish("trade_open", {"mint": "LiveMint4xxxxxxxxxx", "symbol": "NEWBUY", "usd": 18.0, "price": 1.0, "strategy": "prompt"})
    await page.wait_for_selector(".banner >> text=Bought NEWBUY", timeout=5000)
    for i in range(12):  # a burst of everything the pipeline draws
        for t, d in (("room_add", {"mint": f"R{i}", "symbol": f"C{i}", "room": i}), ("scan_pass", {"mint": f"R{i}", "symbol": f"C{i}"}),
                     ("audit_pass", {"mint": f"R{i}", "symbol": f"C{i}", "score": 80}), ("order_request", {"mint": f"R{i}", "symbol": f"C{i}"}),
                     ("new_token", {"mint": f"N{i}", "symbol": "N", "name": "N", "late": False})):
            hub.bus.publish(t, d)
    await page.wait_for_timeout(1500)
    assert await page.evaluate("window.__marker") == "same-page"  # never reloaded
    assert page.errors == [], page.errors


async def test_a_loss_has_no_confetti_and_a_profit_over_5_dollars_does(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.evaluate("window.__confetti = 0; const orig = D.confetti; D.confetti = () => { window.__confetti++; orig(); }; null")
    await page.wait_for_timeout(500)
    env.hub.bus.publish("trade_close", {"mint": "L1xxxxxxxxxx", "symbol": "LOSS", "pnl_usd": -4.0, "pnl_pct": -22, "reason": "stop", "strategy": "prompt"})
    env.hub.bus.publish("trade_close", {"mint": "L2xxxxxxxxxx", "symbol": "SMALL", "pnl_usd": 3.0, "pnl_pct": 17, "reason": "tp", "strategy": "prompt"})
    await page.wait_for_selector(".banner--down", timeout=5000)
    await page.wait_for_timeout(500)
    assert await page.evaluate("window.__confetti") == 0
    env.hub.bus.publish("trade_close", {"mint": "L3xxxxxxxxxx", "symbol": "BIG", "pnl_usd": 5.01, "pnl_pct": 28, "reason": "tp", "strategy": "prompt"})
    await page.wait_for_function("window.__confetti === 1", timeout=5000)


# ---- offline / reconnect / empty states ----------------------------------------------------------------------------------------------------------
async def test_api_drop_shows_offline_keeps_last_numbers_and_recovers(env):
    page = await env.page(1440, 900)
    await ready(page)
    before = await page.get_attribute("#bal-prompt", "aria-label")
    port = env.srv.port
    await env.srv.stop()
    await page.wait_for_selector("#offline:not([hidden])", timeout=15000)
    txt = await page.inner_text("#offline")
    assert "Desk offline" in txt and "retrying in" in txt and "last update" in txt and "--:--:--" not in txt
    assert await page.get_attribute("#bal-prompt", "aria-label") == before  # the last good numbers stay on screen
    env.srv = await LiveServer(env.db, env.hub, TOKEN, port=port).start()
    await page.wait_for_selector("#offline", state="hidden", timeout=30000)
    assert await page.get_attribute("#bal-prompt", "aria-label") == before
    await page.wait_for_function("true")
    dropped = ("ERR_CONNECTION_REFUSED", "Failed to fetch", "ERR_INCOMPLETE_CHUNKED_ENCODING", "ERR_CONNECTION_RESET")  # the drop itself
    real = [e for e in page.errors if not any(d in e for d in dropped)]
    assert real == [], real


async def test_empty_states_say_what_to_do_and_never_show_made_up_numbers(empty_env):
    page = await empty_env.page(1440, 900)
    await page.wait_for_function("document.querySelector('#bal-prompt').getAttribute('aria-label') !== 'loading'", timeout=10000)
    await page.wait_for_timeout(800)
    assert await page.get_attribute("#bal-prompt", "aria-label") == "no data" and "no data" in await page.inner_text("#pnl-prompt")
    for sel, text in (("#log-empty", "Waiting for the first decision"), ("#pos-empty", "No open positions"), ("#kills-empty", "Nothing has been rejected"),
                      ("#audit-empty", "Waiting for the first audited coin"), ("#radar-empty", "No coin has been judged"), ("#recap-empty", "No session"),
                      ("#chart-empty", "Waiting for the first balance")):
        assert await page.is_visible(sel) and text in await page.inner_text(sel), sel
    assert "no data" in await page.inner_text("#stats-prompt") or "no trades" in await page.inner_text("#stats-prompt")
    assert page.errors == [], page.errors


# ---- coin sheet, sell now, settings, pause ----------------------------------------------------------------------------------------------------------
async def test_coin_sheet_opens_on_fomo_with_a_links_py_url_and_sell_needs_two_presses(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.click("#positions .row >> text=BORK")
    await page.wait_for_selector("#coin[open]")
    await page.wait_for_function("document.querySelector('#coin-sub').textContent.includes('open in prompt')", timeout=5000)
    assert "BORK" in await page.inner_text("#coin-title")
    href = await page.get_attribute("#coin-actions a.link-btn", "href")
    assert href == links.coin_url(DEMO_MINTS[3]) and await page.get_attribute("#coin-actions a.link-btn", "rel") == "noopener noreferrer"
    assert await page.locator("#coin-audit .disc").count() == 8 and not await page.is_visible("#candles-empty")
    PROOF.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(PROOF / "desktop_coin_sheet.png"))
    await page.click("#coin-actions .seg button >> text=25%")
    await page.click("#coin-actions >> text=Sell now")
    assert "Confirm: sell 25% (paper)" in await page.inner_text("#coin-actions")  # first press only arms it
    assert env.hub.bus.recent(topic="manual_sell") == []
    await page.click("#coin-actions >> text=Confirm: sell 25% (paper)")
    await page.wait_for_selector("#coin-note.is-ok")
    assert env.hub.bus.recent(topic="manual_sell")[-1]["data"] == {"mint": DEMO_MINTS[3], "pct": 25}
    await page.keyboard.press("Escape")
    assert not await page.evaluate("document.querySelector('#coin').open")
    assert page.errors == [], page.errors


async def test_settings_save_validate_and_log(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.click("#settings summary >> text=Scanner")
    row = page.locator("#settings .set", has_text="Kill: liquidity below")
    num = row.locator("input[type=number]")
    await num.fill("25000")
    await num.dispatch_event("change")
    await page.wait_for_selector(".banner >> text=Saved Kill: liquidity below")
    log = await env.db.fetchone("SELECT key, old, new, who FROM change_log")
    assert log == {"key": "kill_liq_usd", "old": "20000", "new": "25000", "who": "owner"}
    assert "changed" in await row.evaluate("e => getComputedStyle(e.querySelector('.set__label'), '::after').content")
    await num.fill("-5")
    await num.dispatch_event("change")
    await page.wait_for_selector("#settings .set__err:not([hidden])")
    assert "outside" in await row.locator(".set__err").inner_text()
    assert await num.input_value() == "25000"  # reverted to the last good value
    assert (await env.db.fetchone("SELECT COUNT(*) n FROM change_log"))["n"] == 1


async def test_pause_and_resume_an_agent(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.click("#agents li >> nth=0 >> text=Pause")
    await page.wait_for_function("document.querySelector('#agents li .pause-btn').textContent === 'Resume'", timeout=6000)
    assert "scanner" in env.hub.paused and "paused by you" in await page.inner_text("#agents li >> nth=0")
    await page.click("#agents li >> nth=0 >> text=Resume")
    await page.wait_for_function("document.querySelector('#agents li .pause-btn').textContent === 'Pause'", timeout=6000)
    assert "scanner" not in env.hub.paused


async def test_risk_cannot_be_paused_with_a_position_open(env):
    page = await env.page(1440, 900)
    await ready(page)
    env.hub.objects["risk_pausable"] = lambda: False
    await page.click("#agents li >> nth=4 >> text=Pause")
    await page.wait_for_selector(".banner--down >> text=Risk cannot be paused", timeout=5000)
    assert "risk" not in env.hub.paused


# ---- theme, motion, performance ------------------------------------------------------------------------------------------------------------------------
async def test_light_theme_and_reduced_motion_render_without_errors(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.click("#theme-btn")
    assert await page.evaluate("document.documentElement.dataset.theme") == "light"
    await page.wait_for_timeout(600)
    await scroll_all(page)
    PROOF.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(PROOF / "desktop_1440_light.png"), full_page=True)
    assert await page.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(0, 0, 0)"
    assert page.errors == [], page.errors
    page2 = await env.page(390, 844, mobile=True, reduced_motion="reduce")
    await ready(page2)
    assert await page2.evaluate("getComputedStyle(document.querySelector('.od__d > span')).transitionDuration") in ("0s", "0.000001s", "1e-06s")
    assert page2.errors == [], page2.errors


async def test_page_cpu_stays_under_30_percent_of_one_core(env):
    page = await env.page(1440, 900)
    await ready(page)
    await page.wait_for_timeout(2500)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Performance.enable")

    async def busy():
        m = {x["name"]: x["value"] for x in (await cdp.send("Performance.getMetrics"))["metrics"]}
        return m["TaskDuration"]

    t0, b0 = time.time(), await busy()
    for i in range(6):  # real events while measuring: the animated paths run, not just an idle page
        env.hub.bus.publish("room_add", {"mint": f"P{i}", "symbol": "X", "room": i})
        env.hub.bus.publish("decision", {"ts": time.time(), "bot": "scanner", "mint": f"P{i}", "symbol": "X", "decision": "kill", "reason": "liq $9K", "strategy": "prompt"})
        await page.wait_for_timeout(800)
    load = (await busy() - b0) / (time.time() - t0)
    assert load < 0.30, f"page used {load:.0%} of one core"
