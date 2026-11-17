import asyncio
import json
import os

import httpx
import pytest

import fees
import links
import orion_core as oc
import replay as rp
from api import create_app
from botkit import FakeBrowser, FakeCoinbase, FakeJup, FakeRpc, FakeRug, T0, make_deps
from bots.orion import STRATEGY, Orion
from conftest import ROOT
from recorder import load_ticks

MINT = "OrionMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"


class FakeFeed:
    def __init__(self, key=None):
        self.api_key, self.watched = key, []

    async def set_trade_watch(self, mints):
        self.watched = list(mints)
        return bool(self.api_key)


class FakeFallbacks:
    def __init__(self, badge="PumpPortal live"):
        self.b, self.watch = badge, []

    def badge(self):
        return self.b

    def set_watch(self, mints):
        self.watch = list(mints)


async def make(tmp_path, badge="PumpPortal live", key="k", **kw):
    d = await make_deps(tmp_path)
    d.rpc.balances["DEV"] = 5e7
    fb, feed = FakeFallbacks(badge), FakeFeed(key)
    p = tmp_path / "orion_fast.json"
    p.write_text((ROOT / "strategies" / "orion_fast.json").read_text())
    o = Orion(d, fb, feed, params_path=p)
    return d, o, fb, feed


def new_token(t, mint=MINT, late=False):
    return {"mint": mint, "name": "Orion Coin", "symbol": "ORN", "creator": "DEV", "pool": "pump", "seen_at": t, "v_sol": 30.0,
            "v_tokens": 1e9, "initial_buy": 5e7, "late": late}


def trade_ev(t, side, sol, wallet, v, mint=MINT):
    return {"mint": mint, "txType": side, "solAmount": sol, "tokenAmount": sol * 1e7, "traderPublicKey": wallet, "vSolInBondingCurve": v,
            "vTokensInBondingCurve": 1e9, "seen_at": t}


def publish_burst(d, t0, mint=MINT, buys=9):
    seq = [("sell", "S1", 0.2, 70.0)] + [("buy", f"B{i % 7}", 0.5, 70.0 + 7.0 * (i + 1) / buys) for i in range(buys)]
    for i, (side, w, sol, v) in enumerate(seq):
        d.hub.bus.publish("trade", trade_ev(t0 + i * 2.0, side, sol, w, v, mint))


async def barrier(d):
    await d.db.execute("SELECT 1")


async def go_live(d, o, t0=T0):
    """Create the coin, give it a SOL price and a safety card, then a burst, through the bus like the real feed."""
    d.clock.t = t0
    d.hub.bus.publish("new_token", new_token(t0))
    await o.step(t0)
    await o.handle({"kind": "sol", "ts": t0, "usd": 150.0})
    d.clock.t = t0 + 60
    card = await o.prefetch_safety(MINT)
    assert card["ok"] and card["rt_loss_pct"] == 3.0
    await o.handle(card)
    d.clock.t = t0 + 100
    publish_burst(d, t0 + 100)
    await o.step(t0 + 118)


async def test_a_burst_through_the_bus_opens_a_paper_position_and_everything_is_recorded(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    await go_live(d, o)
    d.clock.t = T0 + 120
    await o.step(T0 + 120)
    assert MINT in o.engine.positions
    await barrier(d)
    row = await d.db.fetchone("SELECT strategy, symbol, size_usd, closed_at FROM strat_positions")
    assert row == {"strategy": STRATEGY, "symbol": "ORN", "size_usd": 6.0, "closed_at": None}
    bal = await d.db.fetchone("SELECT cash_usd FROM strat_balance ORDER BY id DESC LIMIT 1")
    assert bal["cash_usd"] == pytest.approx(144.0)
    opened = d.hub.bus.recent(topic="trade_open")[-1]["data"]
    assert opened["strategy"] == STRATEGY and opened["symbol"] == "ORN"
    push = d.hub.bus.recent(topic="push")[-1]["data"]
    assert push["text"].startswith("PAPER · Bought ORN") and push["strategy"] == STRATEGY and push["link"].startswith(links.SIGNUP_URL + "/coin?")
    dec = await d.db.fetchone("SELECT bot, decision, strategy FROM decisions WHERE decision='buy'")
    assert dec == {"bot": "orion", "decision": "buy", "strategy": STRATEGY}
    await d.db.stop()


async def test_replaying_what_was_recorded_reproduces_the_live_trades_exactly(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    await go_live(d, o)
    for i, v in enumerate((60.0, 55.0)):  # the coin dumps: stop-out
        d.hub.bus.publish("trade", trade_ev(T0 + 122 + i * 2, "sell", 1.0, "X", v))
    await o.step(T0 + 130)
    assert not o.engine.positions and len(o.engine.trades) == 1
    await barrier(d)
    ticks = await load_ticks(d.db)
    assert len(ticks) >= 15 and ticks[0]["kind"] == "create"
    r = rp.replay(ticks, o.params)
    assert json.dumps(r["trades"], sort_keys=True, default=str) == json.dumps(o.engine.trades, sort_keys=True, default=str)
    pos = await d.db.fetchone("SELECT closed_at, exit_rule, pnl_usd, fees_usd, data FROM strat_positions")
    tr = o.engine.trades[0]
    assert pos["closed_at"] is not None and pos["exit_rule"].startswith("stop") and pos["pnl_usd"] == pytest.approx(tr["pnl_usd"])
    full = json.loads(pos["data"])
    for k in ("entry_reason", "signal", "safety_card", "council", "round_trip_pct", "fees_buy", "fees_sell", "signal_lag_s", "fill_latency_s", "exit_rule", "pnl_usd"):
        assert k in full, k  # the stored trade row is complete
    assert d.hub.bus.recent(topic="push")[-1]["data"]["text"].startswith("PAPER · Stopped ORN")
    await d.db.stop()


async def test_entries_pause_on_a_slow_feed_and_say_why(tmp_path):
    d, o, fb, feed = await make(tmp_path, badge="GeckoTerminal (20-50 s late)")
    await go_live(d, o)
    assert o.engine.pause_reason == "GeckoTerminal (20-50 s late): too slow to trade on" and MINT not in o.engine.positions
    await barrier(d)
    skip = await d.db.fetchone("SELECT reason, strategy FROM decisions WHERE decision='skip'")
    assert skip["reason"].startswith("entries paused: GeckoTerminal") and skip["strategy"] == STRATEGY
    fb.b = "feed down"
    o.update_pause()
    assert "feed down" in o.engine.pause_reason
    for ok_badge in ("PumpPortal live", "DexScreener snapshots"):
        fb.b = ok_badge
        o.update_pause()
        assert o.engine.pause_reason is None
    await d.db.stop()


async def test_late_gecko_sightings_are_ignored_by_orion(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    d.hub.bus.publish("new_token", new_token(T0, late=True))
    await o.step(T0)
    assert MINT not in o.engine.mints and o.ticks == 0
    await d.db.stop()


async def test_safety_card_reasons_and_unavailable_sources(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    d.hub.bus.publish("new_token", new_token(T0))
    await o.step(T0)
    d.rpc.mint["mint_authority"] = "X"
    assert (await o.prefetch_safety(MINT))["reasons"] == ["mint authority"]
    d.rpc.mint.update(mint_authority=None, freeze_authority="F")
    assert (await o.prefetch_safety(MINT))["reasons"] == ["freeze authority"]
    d.rpc.mint["freeze_authority"] = None
    d.rugcheck.rep["risks"] = [{"name": "Mutable", "level": "danger"}]
    assert (await o.prefetch_safety(MINT))["reasons"] == ["RugCheck danger"]
    d.rugcheck.rep["risks"] = []
    d.jupiter.rt, d.jupiter.impact = 13.0, 16.0
    card = await o.prefetch_safety(MINT)
    assert card["ok"] is False and card["reasons"] == ["round trip 13.0%", "sell impact 16.0%"]
    d.jupiter.rt, d.jupiter.impact = 3.0, 2.0
    assert (await o.prefetch_safety(MINT))["ok"] is True
    for breaker in (lambda: d.rpc.fail.add("mint_info"), lambda: setattr(d.jupiter, "fail", True)):
        breaker()
        assert await o.prefetch_safety(MINT) is None  # a source that is down is 'no data', never a pass
        d.rpc.fail.clear()
        d.jupiter.fail = False
    d.rugcheck.fail = {"report"}
    assert await o.prefetch_safety(MINT) is None
    await d.db.stop()


async def test_safety_job_retries_later_when_no_data(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    d.hub.bus.publish("new_token", new_token(T0))
    await o.step(T0)
    d.jupiter.fail = True
    o._inflight.add(MINT)
    await o._safety_job(MINT)
    assert o._retry_at[MINT] == d.clock() + 30 and MINT not in o._inflight and o.engine.mints[MINT].safety is None
    await d.db.stop()


async def test_without_a_trade_stream_the_creator_check_decides_dev(tmp_path):
    d, o, fb, feed = await make(tmp_path, key=None)
    d.hub.bus.publish("new_token", new_token(T0))
    await o.step(T0)
    m = o.engine.mints[MINT]
    assert m.dev_sold is None  # no trade stream: we cannot see a creator sale, so it is unknown, not 'False'
    d.rpc.balances["DEV"] = 5e7
    assert (await o.dev_check(MINT))["dev_sold"] is False
    d.rpc.balances["DEV"] = 1e7
    assert (await o.dev_check(MINT))["dev_sold"] is True
    d.rpc.balances.pop("DEV")
    assert await o.dev_check(MINT) is None
    d.rpc.fail.add("token_balance")
    assert await o.dev_check(MINT) is None
    assert await o.dev_check("unknown") is None
    await d.db.stop()


async def test_snapshot_ticks_are_converted_and_recorded(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    d.hub.bus.publish("new_token", new_token(T0))
    d.hub.bus.publish("snapshot", {"mint": MINT, "ts": T0 + 5, "price_usd": 1.2, "liquidity_usd": 9000.0, "buys_m5": 9, "sells_m5": 2, "dex": "pumpfun"})
    d.hub.bus.publish("snapshot", {"mint": MINT, "ts": T0 + 6, "price_usd": None})  # unreadable: dropped
    await o.step(T0 + 6)
    await barrier(d)
    ticks = await load_ticks(d.db)
    assert [t["kind"] for t in ticks] == ["create", "snap"] and ticks[1]["buys_m5"] == 9 and ticks[0]["watching_trades"] is True
    await d.db.stop()


async def test_a_restart_closes_open_positions_at_the_last_real_price(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    await d.db.execute("INSERT INTO strat_balance(ts, strategy, cash_usd, equity_usd) VALUES (1, ?, 144.0, 150.0)", (STRATEGY,))
    data = {"last_price": 2.0, "venue": "curve", "jup_extra_pct": 1.5}
    await d.db.execute("INSERT INTO strat_positions(strategy, mint, symbol, opened_at, entry_price, size_usd, tokens, fees_usd, data) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (STRATEGY, MINT, "ORN", 1, 1.0, 6.0, 5.4, 0.6, json.dumps(data)))
    await o.start()
    gross = 5.4 * 2.0
    cost = fees.side_cost(gross, curve=True, jupiter_extra_pct=1.5).total
    row = await d.db.fetchone("SELECT closed_at, exit_price, exit_rule, pnl_usd FROM strat_positions")
    assert row["closed_at"] is not None and row["exit_price"] == 2.0 and row["exit_rule"] == "desk restart · marked at last real price"
    assert row["pnl_usd"] == pytest.approx(gross - cost - 6.0)
    assert o.engine.cash == pytest.approx(144.0 + gross - cost)
    assert (await d.db.fetchone("SELECT cash_usd FROM strat_balance ORDER BY id DESC LIMIT 1"))["cash_usd"] == pytest.approx(144.0 + gross - cost)
    await d.db.stop()


async def test_params_hot_reload_and_a_bad_edit_keeps_the_old_params(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    cfg = json.loads(o.params_path.read_text())
    cfg["exits"]["stop_pct"] = 12.0
    o.params_path.write_text(json.dumps(cfg))
    os.utime(o.params_path, (o._mtime + 5, o._mtime + 5))
    o._maybe_reload_params()
    assert o.engine.p["exits"]["stop_pct"] == 12.0
    o.params_path.write_text("{ not json")
    os.utime(o.params_path, (o._mtime + 10, o._mtime + 10))
    o._maybe_reload_params()
    assert o.engine.p["exits"]["stop_pct"] == 12.0  # kept
    o.params_path.write_text(json.dumps({"strategy": "x"}))
    os.utime(o.params_path, (o._mtime + 20, o._mtime + 20))
    o._maybe_reload_params()
    assert o.engine.p["exits"]["stop_pct"] == 12.0
    await d.db.stop()


async def test_watch_list_and_trade_subscription_are_capped_and_expire(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    for i in range(120):
        o.watch[f"M{i}"] = T0 + i
    o._refresh_watch(T0 + 119)
    await asyncio.sleep(0.05)
    assert len(fb.watch) == 90 and fb.watch[0] == "M119"  # newest first, at most 90 snapshot mints
    assert len(feed.watched) == 50  # the 50 hottest for the metered trade stream
    o._refresh_watch(T0 + 119 + 1300)
    assert o.watch == {}  # older than the age window: dropped
    await d.db.stop()


async def test_orion_is_paper_only_has_its_own_bank_and_never_touches_the_page(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    st = d.hub.status[STRATEGY]()
    assert st["mode"] == "PAPER" and st["cash"] == 150.0 and st["open"] == 0
    src = (ROOT / "bots" / "orion.py").read_text().lower()
    assert "browser" not in src and "press_primary" not in src and "buy_to_quote" not in src
    assert not hasattr(o, "browser") and o.engine.start_equity == 150.0
    d.book.cash = 10.0  # the prompt strategy's bank is separate
    assert o.engine.cash == 150.0
    await d.db.stop()


# ---- /api/live: both strategies side by side ----------------------------------------------------------------------------------------------
TOKEN = "live-api-token-1234567890"


async def live(db, hub=None):
    from bus import Hub

    app = create_app(db, hub or Hub(), TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/live")).status_code == 401
        r = await c.get("/api/live", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    return r.json()


async def test_api_live_with_no_data_never_invents_a_number(tmp_path):
    d = await make_deps(tmp_path)  # no balance rows yet: the cards say None, not a made-up balance
    j = await live(d.db)
    names = [s["name"] for s in j["strategies"]]
    assert names == ["prompt", "orion_fast"] and j["mode"] == "PAPER"
    for s in j["strategies"]:
        assert s["balance"] is None and s["pnl_usd"] is None and s["win_rate"] is None and s["trades"] == 0 and s["open"] == []
        assert s["start_cash"] == 150.0 and s["mode"] == "PAPER"
    await d.db.stop()


async def test_api_live_shows_both_strategies_side_by_side(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    await go_live(d, o)
    for i, v in enumerate((60.0, 55.0)):
        d.hub.bus.publish("trade", trade_ev(T0 + 122 + i * 2, "sell", 1.0, "X", v))
    await o.step(T0 + 130)
    await d.db.execute("INSERT INTO balance(ts, strategy, cash_usd, equity_usd, mode) VALUES (?,?,?,?,?)", (1, "prompt", 120.0, 150.0, "PAPER"))
    await d.db.execute("INSERT INTO positions(mint, symbol, opened_at, size_usd, entry_price, tokens, stop_price, closed_at, pnl_usd, fees_usd, strategy) "
                       "VALUES ('P1','P',1,18,1,18,0.75,2,3.5,0.6,'prompt')")
    j = await live(d.db, d.hub)
    prompt, orion = j["strategies"]
    assert prompt["balance"] == 150.0 and prompt["trades"] == 1 and prompt["win_rate"] == 100.0 and prompt["pnl_usd"] == 0.0
    tr = o.engine.trades[0]
    assert orion["trades"] == 1 and orion["win_rate"] == 0.0 and orion["pnl_usd"] == pytest.approx(o.engine.equity() - 150.0)
    assert orion["balance"] == pytest.approx(o.engine.equity()) and orion["pnl_usd"] < 0 and orion["fees_usd"] == pytest.approx(tr["fees_total"])
    assert j["engines"]["orion_fast"]["mode"] == "PAPER" and any(x["strategy"] == "orion_fast" for x in j["decisions"])
    await d.db.stop()
