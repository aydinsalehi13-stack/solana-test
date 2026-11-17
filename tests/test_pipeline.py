"""Scanner -> Analyst -> Brain -> Executor -> Risk in PAPER, with fakes for every outside service."""
import asyncio

import pytest

import fees
import links
from botkit import MINT, POOL, T0, FakeBrowser, make_deps, patch_fields, snap
from bots.analyst import Analyst
from bots.brain import Brain
from bots.executor import Executor
from bots.risk import Risk
from bots.scanner import Scanner
from book import PaperBook


def audit_payload(**kw):
    p = {"mint": MINT, "symbol": "PAWS", "name": "Paws", "score": 85, "social_pts": 0, "shill": False, "pool_liq": 50000.0,
         "price": 1.0, "scanner_liq": 40000.0, "creator": "DEV", "creator_balance": 100.0, "pool": POOL, "curve_min": 30.0}
    p.update(kw)
    return p


async def desk(tmp_path, monkeypatch, **kw):
    patch_fields(monkeypatch)
    d = await make_deps(tmp_path, **kw)
    d.rpc.balances["DEV"] = 100.0
    ex = Executor(d)
    return d, ex, Risk(d, ex)


async def barrier(d):
    await d.db.execute("SELECT 1")


async def place_and_dip(d, ex, brain_cash=None, buy_price=0.84, **kw):
    if d.browser is not None:
        d.browser.price = buy_price
    assert await ex.place_dip_order({**audit_payload(**kw), "size_usd": 18.0})
    await ex.on_prices({MINT: snap(price=1.0)})  # at the high: waits
    assert MINT in d.book.waiting and MINT not in d.book.positions
    d.clock.t += 60
    d.dex.snaps[MINT] = snap(price=buy_price, buys5=12, sells5=5)
    await ex.on_prices({MINT: d.dex.snaps[MINT]})


async def test_the_bots_talk_only_through_the_bus_scan_to_order(tmp_path, monkeypatch):
    patch_fields(monkeypatch)
    d = await make_deps(tmp_path)
    sc, an, br, ex = Scanner(d), Analyst(d), Brain(d), Executor(d)
    an.fresh_spread_s = 0
    d.rpc.balances["DEV"] = 1000.0
    await d.db.execute("INSERT INTO new_tokens(mint, ts, name, symbol, creator) VALUES (?,?,?,?,?)", (MINT, T0 - 11 * 60 - 1800, "Paws", "PAWS", "DEV"))
    d.dex.snaps[MINT] = snap(price=1.0)
    d.fomo.holders[MINT] = 104
    e = await sc.add({"mint": MINT, "seen_at": T0 - 11 * 60})
    e.holders_hist.append((T0 - 600, 100))
    assert (await sc.poll())["pass"] == 1
    coin = an.q.get_nowait()  # Scanner -> Analyst, via the bus
    assert coin["mint"] == MINT and coin["curve_min"] == pytest.approx(30.0)
    res = await an.audit(coin)
    assert res["passed"]
    sized = await br.handle(br.q.get_nowait())  # Analyst -> Brain
    assert not sized.skip and sized.size_usd == 18.0 and sized.cap_hit == "none"  # 150 x 3% / 25%
    req = ex.q.get_nowait()  # Brain -> Executor
    assert req["size_usd"] == 18.0 and req["pct_of_cash"] == pytest.approx(12.0)
    assert await ex.place_dip_order(req)
    assert d.book.reserved() == 18.0 and d.book.free_cash() == 132.0  # a waiting order's money is not free cash
    await d.db.stop()


async def test_dip_buy_fills_the_quote_minus_fees_and_never_presses_the_button(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    assert MINT in d.book.positions and MINT not in d.book.waiting
    pos = d.book.positions[MINT]
    assert d.browser.buys == [(MINT, 18.0, "executor")]
    assert not hasattr(d.browser, "press_primary")  # the fake has no primary button: a paper desk never asks for one
    fee_model = fees.side_cost(18.0, curve=False, jupiter_extra_pct=1.5).total  # Jupiter round trip 3% -> 1.5% a leg
    assert fee_model == pytest.approx(0.36 + 0.27)
    assert pos.fees_usd == pytest.approx(0.63) and pos.tokens == pytest.approx((18.0 - 0.63) / 0.84)
    assert pos.entry_price == 0.84 and pos.stop_price == pytest.approx(0.63)  # stop 25% below entry
    assert d.book.cash == pytest.approx(150 - 18.0) and d.session.trades == 1
    await barrier(d)
    order = await d.db.fetchone("SELECT side, status, usd, fee_text, strategy FROM orders")
    assert order == {"side": "buy", "status": "done", "usd": 18.0, "fee_text": "$0.36 fee", "strategy": "prompt"}
    assert (await d.db.fetchone("SELECT mode, strategy FROM positions"))["mode"] == "PAPER"
    push = d.hub.bus.recent(topic="push")[-1]["data"]
    assert push["text"].startswith("PAPER · Bought PAWS") and push["link"].startswith(links.SIGNUP_URL + "/coin?address=")
    await d.db.stop()


async def test_take_profit_half_then_trail_closes_the_rest_and_the_books_balance(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    pos = d.book.positions[MINT]
    tokens0, entry = pos.tokens, pos.entry_price
    events = []
    d.hub.bus.on("trade_partial", lambda e: events.append(e["data"]))
    d.hub.bus.on("trade_close", lambda e: events.append(e["data"]))
    d.clock.t += 60
    await ex.on_prices({MINT: snap(price=1.7)})  # 2.02x entry: take profit
    assert pos.tp_done and pos.tokens == pytest.approx(tokens0 / 2) and len(events) == 1
    assert events[0]["reason"].startswith("take profit") and not events[0]["closed"]
    assert d.browser.sells[-1][:2] == (MINT, 50)
    await ex.on_prices({MINT: snap(price=3.0)})  # peak rises
    assert pos.peak_price == 3.0 and MINT in d.book.positions
    await ex.on_prices({MINT: snap(price=1.75)})  # 41.7% below the peak: trail
    assert MINT not in d.book.positions and events[-1]["closed"] and events[-1]["reason"].startswith("trail")
    proceeds = sum(e["proceeds"] for e in events)
    assert d.book.cash == pytest.approx(150 - 18.0 + proceeds)
    assert events[-1]["pnl_usd"] == pytest.approx(proceeds - 18.0)
    leg1 = tokens0 / 2 * 1.7 - fees.side_cost(tokens0 / 2 * 1.7, jupiter_extra_pct=1.5).total
    assert events[0]["proceeds"] == pytest.approx(leg1)  # filled at the DexScreener price that fired it, minus fees
    await barrier(d)
    row = await d.db.fetchone("SELECT closed_at, pnl_usd, exit_reason, exit_price FROM positions")
    assert row["closed_at"] is not None and row["pnl_usd"] == pytest.approx(proceeds - 18.0) and row["exit_price"] == 1.75
    assert [r["side"] for r in await d.db.fetchall("SELECT side FROM orders ORDER BY id")] == ["buy", "sell", "sell"]
    await d.db.stop()


async def test_risk_stop_sells_at_the_real_price_with_fees_and_jumps_the_lane(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    entry = d.book.positions[MINT].entry_price
    d.clock.t += 5
    d.dex.snaps[MINT] = snap(price=entry * 0.74)
    sold = await risk.poll()
    assert sold and sold[0][0] == MINT and sold[0][1].startswith("stop -26")
    assert MINT not in d.book.positions
    assert d.browser.sells[-1] == (MINT, 100, "risk", True)  # Risk bot, urgent: a stop never waits
    assert d.book.cash < 150  # a loss, after fees
    await barrier(d)
    row = await d.db.fetchone("SELECT pnl_usd, fees_usd, exit_reason FROM positions")
    assert row["pnl_usd"] < -4.5 and row["fees_usd"] > 0.63 and row["exit_reason"].startswith("stop")
    assert d.hub.bus.recent(topic="push")[-1]["data"]["text"].startswith("PAPER · Stopped PAWS")
    await d.db.stop()


async def test_a_stop_is_not_delayed_by_a_dead_browser(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    d.browser = None
    d.clock.t += 5
    d.dex.snaps[MINT] = snap(price=0.1)
    assert (await risk.poll())[0][0] == MINT and MINT not in d.book.positions
    await d.db.stop()


async def test_never_a_second_buy_on_an_open_position(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    assert not await ex.place_dip_order({**audit_payload(), "size_usd": 18.0})
    with pytest.raises(ValueError):
        await d.book.open_position(MINT, "PAWS", 5, 5, 1, 0, 25, {})
    await d.db.stop()


@pytest.mark.parametrize("mutate,snap_kw,expected", [
    (lambda d: None, {"liq": 28000}, "liquidity down 30% since scan"),
    (lambda d: d.rpc.balances.update({"DEV": 50.0}), {}, "dev sold while we waited"),
    (lambda d: setattr(d.jupiter, "rt", 13.0), {}, "Jupiter round trip 13.0% > 12%"),
    (lambda d: setattr(d.jupiter, "impact", 16.0), {}, "Jupiter sell impact 16.0% > 15%"),
    (lambda d: setattr(d.jupiter, "fail", True), {}, "no data: Jupiter round trip"),
    (lambda d: setattr(d.fomo, "fail", True), {}, "no data: FOMO warnings"),
    (lambda d: d.fomo.warn.update({"disableSelling": True}), {}, "FOMO: selling/buying off"),
    (lambda d: d.fomo.warn.update({"warnings": ["Freeze authority"]}), {}, "FOMO warns"),
    (lambda d: d.rpc.fail.add("token_balance"), {}, "no data: dev balance"),
])
async def test_recheck_skips_before_the_click(tmp_path, monkeypatch, mutate, snap_kw, expected):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    assert await ex.place_dip_order({**audit_payload(), "size_usd": 18.0})
    await ex.on_prices({MINT: snap(price=1.0)})  # at the high: waits
    mutate(d)
    await ex.on_prices({MINT: snap(**{"price": 0.84, "buys5": 12, "sells5": 5, **snap_kw})})  # a valid dip triggers the re-check
    assert MINT not in d.book.positions and MINT not in d.book.waiting and d.browser.buys == []
    await barrier(d)
    reasons = [r["reason"] for r in await d.db.fetchall("SELECT reason FROM decisions WHERE decision='skip'")]
    assert expected in reasons
    await d.db.stop()


async def test_recheck_ran_before_the_click(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    assert await ex.place_dip_order({**audit_payload(), "size_usd": 18.0})  # analyst price 1.0
    await ex.on_prices({MINT: snap(price=2.0)})  # spiked after the audit
    await ex.on_prices({MINT: snap(price=1.7, buys5=12, sells5=5)})  # a 15% dip from 2.0, but +70% since the audit
    assert MINT not in d.book.positions and d.browser.buys == []
    await barrier(d)
    assert "ran before the click" in [r["reason"] for r in await d.db.fetchall("SELECT reason FROM decisions")]
    await d.db.stop()


async def test_browser_unavailable_or_logged_out_is_a_skip_never_a_phantom_fill(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    d.browser = None
    await place_and_dip(d, ex)
    assert MINT not in d.book.positions and d.book.cash == 150
    await barrier(d)
    assert "FOMO browser unavailable" in (await d.db.fetchone("SELECT reason FROM decisions WHERE decision='skip'"))["reason"]
    await d.db.stop()


async def test_logged_out_browser_skips_and_pushes(tmp_path, monkeypatch):
    from browser import LoggedOutError

    d, ex, risk = await desk(tmp_path, monkeypatch)
    d.browser = FakeBrowser()
    d.browser.error = LoggedOutError("no avatar")
    await place_and_dip(d, ex)
    assert MINT not in d.book.positions
    assert "LoggedOutError" in d.hub.bus.recent(topic="push")[-1]["data"]["text"]
    await d.db.stop()


@pytest.mark.parametrize("reason", ["box to tick", "no cash (Buy with Apple Pay): never clicked", "price impact too high"])
async def test_fomo_quote_skips_are_honoured(tmp_path, monkeypatch, reason):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    d.browser.reason = reason
    await place_and_dip(d, ex)
    assert MINT not in d.book.positions and d.book.cash == 150
    await barrier(d)
    assert f"FOMO: {reason}" in [r["reason"] for r in await d.db.fetchall("SELECT reason FROM decisions WHERE decision='skip'")]
    await d.db.stop()


async def test_page_price_far_from_dexscreener_skips_the_buy(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    d.browser.price = 0.50  # the page quotes a price 40% away from DexScreener
    assert await ex.place_dip_order({**audit_payload(), "size_usd": 18.0})
    await ex.on_prices({MINT: snap(price=1.0)})
    await ex.on_prices({MINT: snap(price=0.84, buys5=12, sells5=5)})
    assert MINT not in d.book.positions
    await barrier(d)
    assert "apart (> 10%)" in (await d.db.fetchone("SELECT reason FROM decisions WHERE decision='skip'"))["reason"]
    await d.db.stop()


async def test_worse_jupiter_route_costs_more_never_less(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    d.jupiter.rt = 8.0
    await place_and_dip(d, ex)
    assert d.book.positions[MINT].fees_usd == pytest.approx(0.36 + 18 * 0.04)  # 4% extra per leg
    await d.db.stop()


async def test_dip_order_times_out_with_no_dip_and_crash_skips(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    assert await ex.place_dip_order({**audit_payload(), "size_usd": 18.0})
    d.clock.t += 31 * 60
    await ex.on_prices({MINT: snap(price=1.0)})
    assert MINT not in d.book.waiting and d.book.cash == 150
    await barrier(d)
    assert "no dip" in [r["reason"] for r in await d.db.fetchall("SELECT reason FROM decisions")]
    await d.db.stop()


async def test_crash_skips_the_order(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    assert await ex.place_dip_order({**audit_payload(), "size_usd": 18.0})
    await ex.on_prices({MINT: snap(price=0.59, buys5=12, sells5=5)})  # -41% from the 1.0 high
    assert MINT not in d.book.waiting and MINT not in d.book.positions
    await barrier(d)
    assert any(r["reason"].startswith("crash") for r in await d.db.fetchall("SELECT reason FROM decisions"))
    await d.db.stop()


async def test_free_cash_excludes_other_waiting_orders(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch, overrides={"paper_start_cash": 30})
    assert await ex.place_dip_order({**audit_payload(), "size_usd": 18.0})
    assert not await ex.place_dip_order({**audit_payload(mint="OTHERMINT"), "size_usd": 18.0})  # only $12 is free
    await d.db.stop()


async def test_risk_cannot_be_paused_while_a_position_is_open(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    assert risk.pausable()
    await place_and_dip(d, ex)
    assert not risk.pausable()
    await d.db.stop()


async def test_lock_time_sells_everything_cancels_waiting_and_rolls_the_session(tmp_path, monkeypatch):
    import datetime as dt

    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    assert await ex.place_dip_order({**audit_payload(mint="WAITING"), "size_usd": 10.0})
    d.book.last_prices[MINT] = 0.9
    now = dt.datetime.fromtimestamp(d.clock()).replace(hour=6, minute=31)
    assert d.session.lock_due(now) and not d.session.lock_due(now)  # once per day
    n = await ex.sell_all("lock time")
    assert n == 1 and not d.book.positions and not d.book.waiting
    old_id = d.session.id
    await d.session.close(d.book.equity({}))
    await d.session.open()
    assert d.session.id != old_id and d.session.trades == 0
    await barrier(d)
    ended = await d.db.fetchone("SELECT ended_at, trades FROM nights WHERE id=?", (old_id,))
    assert ended["ended_at"] is not None and ended["trades"] == 1
    assert (await d.db.fetchone("SELECT exit_reason FROM positions"))["exit_reason"] == "lock time"
    await d.db.stop()


async def test_night_loss_stops_the_brain(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    br = Brain(d)
    d.book.cash = 100.0  # started the night with 150: a 33% loss
    d.session.start_equity = 150.0
    sized = await br.handle(audit_payload())
    assert sized.skip and sized.reason.startswith("night loss")
    assert d.hub.bus.recent(topic="order_request") == []
    await d.db.stop()


async def test_book_survives_a_restart(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    cash, tokens = d.book.cash, d.book.positions[MINT].tokens
    await barrier(d)
    book2 = PaperBook(d.db, d.hub, d.settings, d.clock)
    await book2.load()
    assert book2.cash == pytest.approx(cash)
    p = book2.positions[MINT]
    assert p.tokens == pytest.approx(tokens) and p.meta["creator"] == "DEV" and p.stop_price == pytest.approx(0.63)
    await d.db.stop()


async def test_decisions_carry_reason_and_value_for_the_dashboard(tmp_path, monkeypatch):
    d, ex, risk = await desk(tmp_path, monkeypatch)
    await place_and_dip(d, ex)
    await barrier(d)
    rows = await d.db.fetchall("SELECT bot, decision, reason, value FROM decisions ORDER BY id")
    buy = next(r for r in rows if r["decision"] == "buy")
    assert buy["bot"] == "executor" and "dip" in buy["reason"] and buy["value"].startswith("$18.00 @ 0.84")
    assert any(e["data"]["decision"] == "buy" for e in d.hub.bus.recent(topic="decision"))
    await d.db.stop()
