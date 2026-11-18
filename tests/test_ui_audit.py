"""UI audit in a real Chromium: nothing is cut off, at five screen sizes, with extreme numbers and very long text (made-up rows in a throwaway
database, used only to stress the layout), and every control does something."""
import time

import pytest

from conftest import ROOT
from seed import DEMO_MINTS
from test_dashboard_qa import TOKEN, empty_env, env, ready, unexpected  # noqa: F401  (env and empty_env are fixtures)

pytestmark = pytest.mark.skipif(not __import__("conftest")._chrome_path(), reason="no Chrome on this machine")

SIZES = [(360, 740, True), (390, 844, True), (430, 932, True), (768, 1024, False), (1440, 900, False)]
LONG = "layer 3 Dev wallet: dev holds 31% of supply and sold 4 times in the first 90 seconds after the pool opened (no data: delegations)"

# Everything that could be cut off: horizontal or vertical clipping of text, ellipsis, and anything pushed past the screen edge or its card.
FIND_CUTS = """() => {
  const out = [], vw = document.documentElement.clientWidth;
  const skip = 'canvas, .ticker, .od, .od__d, .splash, .banners, .confetti, .tabbar, .skeleton, svg, dialog:not([open]), [hidden], .chart__stage, .radar, .flow, .candles, .frame, .range';
  const name = (e) => (e.id ? '#' + e.id : '') + '.' + String(e.className || '').split(' ').filter(Boolean).slice(0, 2).join('.') + ' "' + (e.textContent || '').trim().slice(0, 28) + '"';
  for (const e of document.querySelectorAll('body *')) {
    if (e.closest(skip) || !e.offsetParent && getComputedStyle(e).position !== 'fixed') continue;
    const cs = getComputedStyle(e), r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const hasText = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    const clipsX = cs.overflowX !== 'visible', clipsY = cs.overflowY !== 'visible';
    if (hasText && cs.textOverflow === 'ellipsis' && e.scrollWidth > e.clientWidth + 1) out.push(['ellipsis', name(e)]);
    if (clipsX && cs.overflowX !== 'auto' && cs.overflowX !== 'scroll' && e.scrollWidth > e.clientWidth + 1 && !e.matches('.bar__track, .speed__track, .score__bar, .sheet, .login, .connect, .setup, .log, .logclip')) out.push(['clipped sideways', name(e)]);
    if (clipsY && cs.overflowY !== 'auto' && cs.overflowY !== 'scroll' && e.scrollHeight > e.clientHeight + 1 && hasText && !e.matches('.sheet, .login, .connect, .setup, .log, .logclip, .bar__track, .score__bar')) out.push(['clipped vertically', name(e)]);
    if (hasText && (r.left < -1 || r.right > vw + 1)) out.push(['off screen', name(e), Math.round(r.left), Math.round(r.right)]);
    const card = e.closest('.panel, .card, .sheet__body');
    if (hasText && card && e !== card) { const c = card.getBoundingClientRect(); if (r.right > c.right + 1 || r.left < c.left - 1) out.push(['outside its card', name(e), Math.round(r.right - c.right)]); }
  }
  // two things that sit side by side must not overlap (the header's name, mode chip and live pill; every row's cells)
  const rows = [...document.querySelectorAll('.top, .top > *, .brand, .top__right, .hero__row, .hero__chips, .strat, .agent__row, .row, .panel__head, .chart__axis')];
  for (const parent of rows) {
    if (!parent.offsetParent && getComputedStyle(parent).position !== 'sticky') continue;
    const kids = [...parent.children].filter((k) => k.offsetParent && !k.matches('canvas, .tip, [hidden]') && k.getBoundingClientRect().width > 0 && getComputedStyle(k).position !== 'absolute');
    for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
      const a = kids[i].getBoundingClientRect(), b = kids[j].getBoundingClientRect();
      const overlapX = Math.min(a.right, b.right) - Math.max(a.left, b.left), overlapY = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (overlapX > 1.5 && overlapY > 1.5) out.push(['overlap', name(kids[i]), name(kids[j]), Math.round(overlapX)]);
    }
  }
  return out;
}"""


async def stress(env_):
    """The extreme case: a big balance, a long coin name, a huge profit, and decisions with very long reasons."""
    now = time.time()
    await env_.db.execute("INSERT INTO balance(ts, strategy, cash_usd, equity_usd, mode) VALUES (?,?,?,?,?)", (now - 1, "prompt", 100.0, 12345.67, "PAPER"))
    await env_.db.execute("INSERT INTO strat_balance(ts, strategy, cash_usd, equity_usd) VALUES (?,?,?,?)", (now - 1, "orion_fast", 99.0, 98765.43))
    await env_.db.execute("UPDATE positions SET symbol='SLINGFOLIOXL' WHERE closed_at IS NULL")
    for bot in ("scanner", "analyst", "brain", "executor", "risk", "orion"):
        await env_.db.execute("INSERT INTO decisions(ts, bot, mint, symbol, decision, reason, value, strategy) VALUES (?,?,?,?,?,?,?,?)",
                              (now - 2, bot, DEMO_MINTS[3], "SLINGFOLIOXL", "kill", LONG, "$14,200.55 / $20,000.00", "prompt"))
    env_.hub.objects["marks"] = lambda: {DEMO_MINTS[3]: {"price": 400.0, "tokens": 36.0, "realized": 0.0}, DEMO_MINTS[8]: {"price": 2.3, "tokens": 3.0, "realized": 0.0}}


@pytest.mark.parametrize("w,h,mobile", SIZES)
async def test_nothing_is_cut_off_with_extreme_numbers_and_long_text(env, w, h, mobile):
    await stress(env)
    page = await env.page(w, h, mobile=mobile)
    await ready(page)
    await page.wait_for_timeout(1500)
    tabs = ["home", "agents", "activity", "account"] if mobile else [None]
    for view in ("prompt", "orion_fast", "both"):  # every choice of the big number
        if mobile:
            await page.click('#tabbar [data-go="home"]')
        await page.click(f'#strats [data-view="{view}"]')
        await page.wait_for_timeout(500)
        for tab in tabs:
            if tab:
                await page.click(f'#tabbar [data-go="{tab}"]')
                await page.wait_for_timeout(500)
            if tab in (None, "account"):
                for s in await page.locator("#settings summary").all():
                    await s.click()
                await page.wait_for_timeout(300)
            if tab in (None, "agents", "home"):
                for r in await page.locator("#agents .agent__row").all():
                    await r.click()   # open every agent: its long line is shown in full
                await page.wait_for_timeout(300)
            cuts = await page.evaluate(FIND_CUTS)
            assert cuts == [], f"{w}px, view {view}, tab {tab}: {cuts[:6]}"
    big = await page.evaluate("document.querySelector('#bal-total').getBoundingClientRect().right")
    assert big <= w and unexpected(page) == [], page.errors  # even $98,765.43 sits inside the screen


@pytest.mark.parametrize("w,h,mobile", SIZES[:2])
async def test_every_dialog_fits_its_screen(env, w, h, mobile):
    await stress(env)
    page = await env.page(w, h, mobile=mobile)
    await ready(page)
    await page.click("#hero-in-chip")
    await page.wait_for_function("document.querySelector('#rc-lines').textContent.includes('win rate')", timeout=5000)
    await page.wait_for_timeout(900)
    cuts = await page.evaluate(FIND_CUTS)
    assert cuts == [], cuts[:8]
    box = await page.locator("#recap-sheet").bounding_box()
    assert box["x"] >= -1 and box["x"] + box["width"] <= w + 1 and box["y"] >= 0
    await page.click("#recap-close")
    await page.wait_for_function("!document.querySelector('#recap-sheet').open")
    await page.locator("#positions .row__coin").first.scroll_into_view_if_needed()
    await page.evaluate("window.scrollBy(0, -140)")  # clear of the tab bar
    await page.locator("#positions .row__coin").first.click()
    await page.wait_for_selector("#coin[open]", timeout=8000)
    await page.wait_for_function("document.querySelector('#coin-sub').textContent.includes('open in')", timeout=5000)
    await page.wait_for_timeout(700)
    cuts = await page.evaluate(FIND_CUTS)
    assert cuts == [], cuts[:8]


# ---- the big number starts on prompt ($150) and every choice works ----------------------------------------------------------------------
async def test_the_big_number_starts_on_prompt_and_every_choice_switches_it_and_is_remembered(env):
    page = await env.page(390, 844, mobile=True)
    await ready(page)
    t = lambda sel: page.inner_text(sel)  # noqa: E731
    assert await t("#hero-view") == "prompt" and "in $150" in await t("#hero-in-text")
    prompt = await page.get_attribute("#bal-total", "aria-label")
    await page.click('#strats [data-view="orion_fast"]')
    await page.wait_for_function("document.querySelector('#hero-view').textContent === 'orion_fast'")
    assert "in $150" in await t("#hero-in-text") and await page.get_attribute("#bal-total", "aria-label") == await page.get_attribute("#bal-orion_fast", "aria-label") != prompt
    assert await page.get_attribute('#strats [data-view="orion_fast"]', "aria-checked") == "true" and await page.get_attribute('#strats [data-view="prompt"]', "aria-checked") == "false"
    await page.click('#strats [data-view="both"]')
    await page.wait_for_function("document.querySelector('#hero-view').textContent === 'both strategies'")
    assert "in $300" in await t("#hero-in-text") and await page.get_attribute("#bal-total", "aria-label") == (await t("#bal-both"))
    assert await page.evaluate("D.equity.model.lines.length") == 2 and await page.evaluate("D.equity.model.putIn") == 300
    await page.click('#strats [data-view="prompt"]')
    await page.wait_for_function("D.equity.model.lines.length === 1 && D.equity.model.putIn === 150")  # the line is the prompt strategy's own
    await page.focus('#strats [data-view="orion_fast"]')
    await page.keyboard.press("Enter")  # keyboard works too
    await page.wait_for_function("document.querySelector('#hero-view').textContent === 'orion_fast'")
    await page.reload()
    await ready(page)
    assert await t("#hero-view") == "orion_fast"  # remembered in this browser
    assert unexpected(page) == [], page.errors


async def test_a_view_with_no_balance_says_no_data_never_a_partial_sum(empty_env):
    page = await empty_env.page(390, 844, mobile=True)
    await page.wait_for_function("document.querySelector('#bal-prompt').getAttribute('aria-label') !== 'loading'", timeout=10000)
    await page.click('#strats [data-view="both"]')
    assert await page.get_attribute("#bal-total", "aria-label") == "no data" and await page.inner_text("#bal-both") == "no data"


# ---- every control does what it says ----------------------------------------------------------------------------------------------
async def test_every_control_works(env):
    page = await env.page(1440, 900)
    await ready(page)
    # chart ranges: each one asks the desk and the selected one is marked
    for rng in ("live", "1h", "4h", "1d", "1w", "all", "night"):
        await page.click(f'#range-tabs [data-range="{rng}"]')
        await page.wait_for_function(f"document.querySelector('#range-tabs [data-range=\"{rng}\"]').getAttribute('aria-selected') === 'true'")
        await page.wait_for_function(f"D.equity.data.range === '{rng}'", timeout=5000)
    # positions: open / closed
    await page.click('#pos-tabs [data-pos="closed"]')
    await page.wait_for_function("document.querySelectorAll('#positions .row').length >= 7", timeout=5000)
    assert "take profit 2x" in await page.inner_text("#positions")
    await page.click('#pos-tabs [data-pos="open"]')
    assert await page.locator("#positions .row").count() == 2
    # log filters and the coin sheet from a log row and from a tape chip
    await page.click("#log-filters .chip >> text=orion_fast")
    assert await page.locator("#log li").count() > 0 and all(t == "ORION" for t in await page.locator("#log li .tag").all_inner_texts())
    await page.click("#log-filters .chip >> text=Both strategies")
    await page.click("#log li >> nth=0")
    await page.wait_for_selector("#coin[open]")
    await page.keyboard.press("Escape")
    await page.hover("#ticker")  # the tape pauses under a finger or a pointer
    await page.click("#ticker .tk >> nth=1")
    await page.wait_for_selector("#coin[open]")
    await page.keyboard.press("Escape")
    # theme and lock
    await page.click("#theme-btn")
    assert await page.evaluate("document.documentElement.dataset.theme") == "dark"
    await page.click("#theme-btn")
    # the night report: recap -> full report opens, behind the token, as a page without scripts
    await page.click("#recap-open")
    await page.wait_for_selector("#recap-sheet[open]")
    await page.click("#rc-report")
    await page.wait_for_selector("#report[open]", timeout=10000)
    frame = page.frame_locator("#report-frame")
    await frame.locator("h1").wait_for(timeout=5000)
    assert "Night report" in await frame.locator("h1").inner_text() and await page.evaluate("document.querySelector('#report-frame').getAttribute('sandbox')") == ""
    await page.click("#report-close")
    # lock: the token is forgotten and the connect sheet comes back
    await page.click("#lock-btn")
    assert await page.is_visible("#login") and await page.evaluate("localStorage.getItem('desk_token')") is None
    assert unexpected(page) == [], page.errors


async def test_the_audit_itself_catches_a_cut_off(env):
    page = await env.page(390, 844, mobile=True)
    await ready(page)
    await page.evaluate("""() => { const d = document.createElement('div'); d.id = 'probe';
      d.innerHTML = '<p class="strat__pnl" style="width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">+$12,345.67 (+8,230.4%)</p><p style="position:absolute;left:380px">pushed past the edge of the screen</p>';
      document.querySelector('.grid').append(d); }""")
    kinds = {c[0] for c in await page.evaluate(FIND_CUTS)}
    assert {"ellipsis", "off screen"} <= kinds


async def test_no_text_anywhere_says_null_or_undefined_or_nan(env):
    page = await env.page(1440, 900)
    await ready(page)
    for tab in ('#range-tabs [data-range="1h"]', '#strats [data-view="both"]', '#strats [data-view="orion_fast"]', '#pos-tabs [data-pos="closed"]'):
        await page.click(tab)
        await page.wait_for_timeout(400)
    await page.click("#hero-in-chip")
    await page.wait_for_function("document.querySelector('#rc-lines').textContent.includes('win rate')", timeout=5000)
    text = await page.evaluate("document.body.innerText")
    bad = [w for w in ("null", "undefined", "NaN", "[object") if w in text]
    assert bad == [], bad
