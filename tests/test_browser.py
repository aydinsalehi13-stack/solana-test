"""Browser lane against a MOCK of the FOMO page built from the texts in the spec (fomo_web.md).
This proves the lane's logic and the hard rules. It does NOT prove the selectors on the live page."""
import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

import browser as br
import config
import links
from browser import Browser, ClickDenied, LoggedOutError, assert_click_allowed

FIX = Path(__file__).parent / "fixtures"
MINTS = ["4CxjZyEFGHJKLMNPQRSTUVWXYZabcdefghijkmn8spump",
         "9QrStUvWxYzabcdefghijkmnpqrstuvwxyzABCDEFGHJpump",
         "7NpQrStUvWxYzabcdefghijkmnopqrstuvwxyzABCDEFGpump"]
M = MINTS[0]


class Site:
    """Fake fomo.family served through request interception. Records every request."""

    def __init__(self):
        self.flags = ""
        self.logged_out = False
        self.requests: list[str] = []

    async def handler(self, route):
        url = route.request.url
        self.requests.append(url)
        path = urlparse(url).path
        if path.startswith("/__"):
            return await route.fulfill(status=200, body="{}", content_type="application/json")
        if self.logged_out or "LOGGEDOUT" in url:
            return await route.fulfill(status=200, body=(FIX / "fomo_landing.html").read_text(), content_type="text/html")
        html = (FIX / "fomo_mock.html").read_text().replace("<head>", f"<head><script>window.__FLAGS='{self.flags}'</script>")
        await route.fulfill(status=200, body=html, content_type="text/html")

    def hits(self, name):
        return [u for u in self.requests if f"/__{name}" in u]


@pytest.fixture
async def desk(chrome_port, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    b = Browser(cdp_port=chrome_port)
    b.quote_timeout = 1.5
    b.header_wait = 1.5
    await b.connect()
    site = Site()
    await b.context.route(links.SIGNUP_URL + "/**", site.handler)
    yield b, site
    await b.context.unroute(links.SIGNUP_URL + "/**", site.handler)
    await b.close()


# ---- the deny list (hard rule 4), unit level --------------------------------------------------------------
@pytest.mark.parametrize("text", ["Withdraw", "Deposit", "Deposit more", "Buy with Apple Pay", "Orders", "Perps", "Send",
                                  "Settings", "Export private key", "Confirm", "Discover", "Thesis", "Download", "Log out"])
def test_denied_controls_raise(text):
    with pytest.raises(ClickDenied):
        assert_click_allowed({"text": text})


@pytest.mark.parametrize("info", [{"aria": "Withdraw funds"}, {"title": "Deposit"}, {"text": "ok", "checkbox": True},
                                  {"text": "get", "download": True}, {"text": "go", "href": "https://evil.example.com/x"},
                                  {"text": "go", "href": links.SIGNUP_URL + ".evil.com/x"}])
def test_denied_by_attribute(info):
    with pytest.raises(ClickDenied):
        assert_click_allowed(info)


@pytest.mark.parametrize("text", ["Buy", "Sell", "50%", "$10", "Max", "Tokens", "Leaderboard", "Paste"])
def test_safe_controls_pass(text):
    assert_click_allowed({"text": text, "href": links.SIGNUP_URL + "/leaderboard" if text == "Leaderboard" else ""})


def test_primary_is_refused_in_paper():
    with pytest.raises(ClickDenied):
        assert_click_allowed({"text": "Buy PAWS"}, mode="PAPER", primary=True)


def test_live_mode_cannot_be_constructed():
    with pytest.raises(NotImplementedError):
        Browser(mode="LIVE")


def test_no_cookie_access_in_code():
    for f in ("browser.py", "fomo_api.py"):
        t = (config.ROOT / f).read_text()
        assert "document.cookie" not in t and ".cookies(" not in t, f


# ---- lane -------------------------------------------------------------------------------------------------
async def test_lane_risk_jumps_the_queue():
    lane, order = br.Lane(), []

    async def use(bot, hold=0.0):
        async with lane.hold(bot):
            order.append(bot)
            await asyncio.sleep(hold)

    first = asyncio.create_task(use("scanner", 0.1))
    await asyncio.sleep(0.02)
    rest = [asyncio.create_task(use(b)) for b in ("analyst", "executor", "risk")]
    await asyncio.gather(first, *rest)
    assert order == ["scanner", "risk", "executor", "analyst"]
    assert lane.holder is None


async def test_lane_cancelled_waiter_does_not_leak():
    lane = br.Lane()
    async with lane.hold("executor"):
        t = asyncio.create_task(lane.hold("scanner").__aenter__())
        await asyncio.sleep(0.01)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    assert lane.holder is None
    async with lane.hold("risk"):
        assert lane.holder == "risk"


# ---- S0..S2 on the mock page ------------------------------------------------------------------------------
async def test_three_coins_type_2_read_quote_stop_before_button(desk):
    b, site = desk
    for m in MINTS:
        r = await b.buy_to_quote(m, 2.0, order_id=900)
        assert r.ok, r.reason
        assert r.usd == 2.0 and r.quote_text == "44K PAWS (-0.61%)" and r.fee_text == "$0.36 fee" and r.fee_usd == 0.36
        assert r.impact_pct == -0.61 and r.primary_label == "Buy PAWS" and r.stopped_before_button
        assert (await (await b.ensure_page()).evaluate("window.__primaryClicks")) == 0
        assert (await (await b.ensure_page()).evaluate("document.getElementById('amt').value")) == "2.00"
    assert b.primary_clicks == 0 and not site.hits("primary")
    opened = [u for u in site.requests if "/coin" in u]
    assert opened == [links.coin_url(m) for m in MINTS]  # exact links.py URLs
    shots = list((config.LOG_DIR / "orders" / "900").glob("*.jpg"))
    assert len(shots) >= 2 and all(p.stat().st_size > 1000 for p in shots)


async def test_preset_used_only_for_exact_amounts(desk):
    b, site = desk
    r = await b.buy_to_quote(M, 10.0)
    assert r.ok and (await (await b.ensure_page()).evaluate("document.getElementById('amt').value")) == "10"


async def test_withdraw_deposit_and_foreign_link_refused_on_real_dom(desk):
    b, site = desk
    await b.ensure_ready()
    before = len(site.requests)
    for label in ("Withdraw", "Deposit more", "Discover"):
        with pytest.raises(ClickDenied):
            await b.click_text(label)
    assert not site.hits("withdraw") and not site.hits("deposit")
    assert len(site.requests) == before  # nothing was even navigated


async def test_primary_button_refused_even_if_asked_directly(desk):
    b, site = desk
    await b.ensure_ready()
    with pytest.raises(ClickDenied):
        await b.press_primary()
    assert not site.hits("primary")


async def test_amount_mismatch_aborts(desk):
    b, site = desk
    site.flags = "mismatch=1"
    r = await b.buy_to_quote(M, 2.0)
    assert not r.ok and r.reason == "amount mismatch"
    assert list((config.LOG_DIR / "fail").glob("*amount_mismatch*"))


async def test_box_to_tick_is_never_ticked_and_buy_skips(desk):
    b, site = desk
    site.flags = "box=1"
    r = await b.buy_to_quote(M, 2.0)
    assert not r.ok and r.reason == "box to tick"
    assert await (await b.ensure_page()).evaluate("document.querySelector('input[type=checkbox]').checked") is False


async def test_apple_pay_means_no_cash_never_clicked(desk):
    b, site = desk
    site.flags = "cash=0"
    r = await b.buy_to_quote(M, 2.0)
    assert not r.ok and "Apple Pay" in r.reason and not site.hits("primary")


async def test_price_impact_too_high_skips_buy(desk):
    b, site = desk
    site.flags = "impact=1"
    r = await b.buy_to_quote(M, 2.0)
    assert not r.ok and r.reason == "price impact too high"


async def test_slow_quote_reloads_once_then_skips(desk):
    b, site = desk
    site.flags = "slow=1"
    r = await b.buy_to_quote(M, 2.0)
    assert not r.ok and r.reason == "no quote in 10 s"
    assert len([u for u in site.requests if "/coin" in u]) == 2  # opened, reloaded once


# ---- sells ---------------------------------------------------------------------------------------------------
async def test_partial_sell_quote_and_full_sell(desk):
    b, site = desk
    site.flags = "pos=1"
    r = await b.sell_to_quote(M, 50)
    assert r.ok and r.pct == 50 and r.primary_label == "Sell PAWS" and "PAWS $20.00" in (r.positions_text or "")
    r = await b.sell_to_quote(M, 100)
    assert r.ok and r.pct == 100 and not site.hits("primary")


async def test_dust_position_is_marked_dust(desk):
    b, site = desk
    site.flags = "pos=small"
    r = await b.sell_to_quote(M, 50)
    assert not r.ok and r.reason.startswith("dust")


async def test_sell_rejects_odd_percent(desk):
    b, _ = desk
    with pytest.raises(ValueError):
        await b.sell_to_quote(M, 33)


# ---- login, reattach, cursor, frames ------------------------------------------------------------------------
async def test_landing_page_means_logged_out(desk):
    b, site = desk
    site.logged_out = True
    with pytest.raises(LoggedOutError):
        await b.ensure_ready()
    assert list((config.LOG_DIR / "fail").glob("*.jpg"))


async def test_coin_landing_page_is_logged_out(desk):
    b, site = desk
    await b.ensure_ready()
    with pytest.raises(LoggedOutError):
        await b.open_coin("LOGGEDOUT" + M)


async def test_closed_tab_is_reattached_not_a_crash(desk):
    b, site = desk
    await b.ensure_ready()
    await b.page.close()
    r = await b.buy_to_quote(M, 2.0)
    assert r.ok


async def test_cursor_is_drawn_by_page_and_never_steals_the_click(desk):
    b, site = desk
    r = await b.buy_to_quote(M, 2.0, bot="executor")
    page = await b.ensure_page()
    info = await page.evaluate("""() => { const e = document.getElementById('__desk_cursor_executor');
        return {exists: !!e, pe: e && getComputedStyle(e).pointerEvents, z: e && getComputedStyle(e).zIndex}; }""")
    assert info == {"exists": True, "pe": "none", "z": "2147483647"}
    assert "executor" not in (await b.page_text()).lower()  # label is CSS-only, never pollutes page text
    assert r.ok


async def test_frame_is_1280_wide_jpeg_and_pauses_on_login_form(desk):
    b, site = desk
    await b.ensure_ready()
    jpg = await b.capture_jpeg()
    assert jpg[:2] == b"\xff\xd8"
    i = 2
    while i < len(jpg):  # find SOF marker for the real width
        if jpg[i] == 0xFF and jpg[i + 1] in (0xC0, 0xC2):
            width = int.from_bytes(jpg[i + 7:i + 9], "big")
            break
        i += 2 + int.from_bytes(jpg[i + 2:i + 4], "big")
    assert width <= 1280
    assert await b.capture_frame() is True
    assert (config.DATA_DIR / "screen.jpg").exists()
    mtime = (config.DATA_DIR / "screen.jpg").stat().st_mtime_ns
    await (await b.ensure_page()).evaluate("document.body.insertAdjacentHTML('beforeend','<input type=password>')")
    assert await b.capture_frame() is False
    assert (config.DATA_DIR / "screen.paused").read_text().startswith("Paused")
    assert (config.DATA_DIR / "screen.jpg").stat().st_mtime_ns == mtime


async def test_frames_keep_only_last_n(desk):
    b, _ = desk
    await b.ensure_ready()
    for _ in range(5):
        await b.capture_frame(keep=3)
    assert len(list((config.DATA_DIR / "frames").glob("*.jpg"))) == 3


def test_parse_quote_and_address():
    q = br.parse_quote("Fetching\n44K paws (-0.61%)\n$0.36 fee")
    assert q["impact_pct"] == -0.61 and q["fee_usd"] == 0.36 and q["tokens_text"] == "44K paws"
    assert br.parse_quote("44K paws (-0.61%)") is None
    assert br.short_address_matches("Contract address 4CxjZy...8spump", "4CxjZyEFGH8spump")
    assert not br.short_address_matches("Contract address 4CxjZy...8spump", "9CxjZyEFGH8spump")
