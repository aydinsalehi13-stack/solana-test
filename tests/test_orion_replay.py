import json
import math

import pytest

import experiments as exp
import orion_core as oc
import replay as rp
from db import Database
from orionkit import SOL, T0, base, burst, kinds, params, px, trade
from recorder import TickRecorder, load_ticks
from synth import DAY0, market


def blob(r):
    return json.dumps([r["events"], r["trades"], r["equity_curve"], r["final_equity"]], sort_keys=True, default=str)


def test_replay_is_deterministic_two_runs_identical():
    ticks = market(days=2, per_day=10)
    p = oc.load_params()
    a, b = rp.replay(ticks, p), rp.replay(ticks, p)
    assert blob(a) == blob(b) and a["stats"]["trades"] > 0, "the synthetic market should produce some trades"
    assert blob(rp.replay(list(ticks), json.loads(json.dumps(p)))) == blob(a)  # fresh copies, same answer


def test_live_style_tick_by_tick_equals_replay():
    ticks = market(days=1, per_day=8)
    eng = oc.Engine(oc.load_params())
    ev = []
    for t in ticks:
        ev += eng.on_tick(t)
    r = rp.replay(ticks, oc.load_params())
    assert json.dumps(ev, default=str, sort_keys=True) == json.dumps(r["events"], default=str, sort_keys=True)


async def test_recorded_ticks_replay_exactly_in_the_order_the_engine_saw_them(tmp_path):
    ticks = market(days=1, per_day=6)
    ticks.insert(40, dict(ticks[10], ts=ticks[10]["ts"] - 500))  # an out-of-order tick (late arrival): order must be kept
    db = Database(tmp_path / "r.db")
    await db.start()
    rec = TickRecorder(db)
    for t in ticks:
        rec.record(t)
    await db.execute("SELECT 1")
    loaded = await load_ticks(db)
    assert loaded == ticks and rec.count == len(ticks)
    assert blob(rp.replay(loaded, oc.load_params())) == blob(rp.replay(ticks, oc.load_params()))
    row = await db.fetchone("SELECT ts, mint, kind, side, wallet FROM ticks WHERE kind='trade' ORDER BY id LIMIT 1")
    assert row["kind"] == "trade" and row["side"] in ("buy", "sell") and row["mint"].startswith("SYN")
    assert len(await load_ticks(db, since=DAY0 + 1e9)) == 0  # time filter
    await db.stop()


def test_stats_math():
    trades = [{"pnl_usd": x, "fees_total": 1.0} for x in (10.0, -4.0, -6.0, 2.0)]
    s = rp.stats(trades, 100.0, days=2.0)
    assert s["trades"] == 4 and s["net_usd"] == 2.0 and s["net_pct"] == 2.0 and s["win_rate"] == 50.0
    assert s["avg_win"] == 6.0 and s["avg_loss"] == -5.0 and s["profit_factor"] == pytest.approx(1.2)
    assert s["max_drawdown_pct"] == pytest.approx(100 * 10 / 110)  # 110 -> 100
    assert s["trades_per_day"] == 2.0 and s["top3_share_pct"] == pytest.approx(100 * 8 / 2) and s["fees_usd"] == 4.0


def test_stats_edge_cases():
    assert rp.stats([], 100.0)["profit_factor"] is None and rp.stats([], 100.0)["win_rate"] is None
    assert rp.stats([{"pnl_usd": 5.0, "fees_total": 0}], 100.0)["profit_factor"] == math.inf
    s = rp.stats([{"pnl_usd": -3.0, "fees_total": 0}], 100.0)
    assert s["profit_factor"] == 0.0 and s["top3_share_pct"] is None  # no profit to concentrate


def tail(start, n=30, step=2.0, creep=0.01, v0=77.0):
    return [trade(start + i * step, "buy", 0.5, f"T{i % 5}", v0 * (1 + creep) ** (i + 1)) for i in range(n)]


def test_latency_sweep_shows_the_edge_disappearing_when_you_are_late():
    ticks = base() + burst(buys=8) + tail(T0 + 117, n=60, step=1.0, creep=0.005)  # signal on the last burst tick; then +0.5% every second
    rows = rp.latency_sweep(ticks, oc.load_params())
    assert [r["latency_s"] for r in rows] == [0, 2, 5, 20, 40]
    entry_prices = []
    for lat in (0, 2, 5, 20, 40):
        r = rp.replay(ticks, oc.load_params(), latency_s=float(lat))
        fills = kinds(r["events"], "fill_buy")
        entry_prices.append(fills[0]["price"] if fills else None)
    assert entry_prices[0] < entry_prices[1] < entry_prices[2] < entry_prices[3]  # later = a worse entry price
    assert entry_prices[4] is None  # 40 s late the coin has run > 15%: the entry is cancelled
    assert rows[4]["cancelled"] == 1 and rows[0]["cancelled"] == 0


def test_walk_forward_picks_on_day_n_and_reports_day_n_plus_1():
    ticks = market(days=4, per_day=10)
    good = oc.load_params()
    never = oc.merge(good, {"min_age_s": 10_000})  # never trades
    res = rp.walk_forward(ticks, {"baseline": good, "never": never})
    assert res["days_tested"] == 3 and len(res["picks"]) == 3
    assert all(p["train_best"] == "baseline" for p in res["picks"])  # the one that actually traded on the training day
    days = rp.split_days(ticks)
    expected = sum(len(rp.replay(d, good)["engine"].trades) for d in days[1:])
    assert res["oos"]["trades"] == expected  # reported on days 1..3 only, never on the day it was picked on


def test_split_days_keeps_order_inside_each_day():
    ticks = market(days=3, per_day=3)
    days = rp.split_days(ticks)
    assert len(days) == 3 and all(int(t["ts"] // 86400) == int(d[0]["ts"] // 86400) for d in days for t in d)
    assert sum(len(d) for d in days) == len(ticks)


@pytest.mark.parametrize("pf,n,dd,ok", [(1.31, 50, 14.9, True), (1.30, 50, 14.9, False), (1.31, 49, 14.9, False), (1.31, 50, 15.0, False),
                                        (None, 80, 5.0, False), (math.inf, 60, 3.0, True)])
def test_promotion_rule_thresholds(pf, n, dd, ok):
    res, why = rp.evaluate_promotion({"profit_factor": pf, "trades": n, "max_drawdown_pct": dd})
    assert res is ok and (not why) == ok


def test_verdict_says_no_edge_found_plainly_and_promotion_needs_the_owner():
    assert rp.verdict_text({"profit_factor": 0.8, "trades": 12, "max_drawdown_pct": 20.0}).startswith("no edge found: ")
    assert "Needs the owner's ok" in rp.verdict_text({"profit_factor": 2.0, "trades": 80, "max_drawdown_pct": 5.0})


def test_experiment_grid_covers_every_idea_in_the_spec_and_runs():
    cands = exp.candidates()
    assert set(cands) == set(exp.GRID) and cands["baseline"] == oc.load_params()
    for needed in ("big_tickets", "stop_12_risk1", "stop_15_risk1", "stop_20_risk1", "breakeven_entry", "breakeven_plus_fees", "vol_stop",
                   "sell_pressure_exit", "liq_drop_exit", "two_bursts", "pullback_entry", "skip_first_60s", "max_one_open"):
        assert needed in cands
    assert cands["stop_20_risk1"]["exits"]["stop_pct"] == 20.0 and cands["stop_20_risk1"]["experiments"]["risk_size_pct"] == 1.0
    assert cands["skip_first_60s"]["min_age_s"] == 60 and cands["max_one_open"]["max_open"] == 1
    ticks = market(days=1, per_day=6)
    for name, p in cands.items():
        rp.replay(ticks, p)  # every candidate must run without error


def test_experiment_report_text():
    ticks = market(days=3, per_day=8)
    res = rp.walk_forward(ticks, exp.candidates())
    text = exp.report(res)
    assert "out-of-sample" in text and "baseline params" in text and ("no edge found" in text or "PASSES" in text)
    one = rp.walk_forward(market(days=1, per_day=4), exp.candidates())
    assert "not enough data" in exp.report(one)
