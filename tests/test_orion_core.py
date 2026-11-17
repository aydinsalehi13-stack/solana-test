import json
import re

import pytest

import fees
import orion_core as oc
from conftest import ROOT
from orionkit import SOL, T0, base, burst, entered, kinds, params, px, run, trade


def test_mode_is_hard_coded_paper_and_params_cannot_change_it():
    assert oc.MODE == "PAPER"
    eng = oc.Engine(params(mode="LIVE"))
    assert oc.MODE == "PAPER" and not hasattr(eng, "mode")
    for f in ROOT.rglob("*.py"):
        if ".venv" in f.parts or "tests" in f.parts:
            continue
        assert not re.search(r"""MODE\s*=\s*['"]LIVE['"]""", f.read_text()), f.name


# ---- the entry ------------------------------------------------------------------------------------------------------------
def test_burst_entry_happy_path_and_fill_model():
    eng, fill_ts, ev = entered()
    sig = kinds(ev, "entry_signal")[0]
    fill = kinds(ev, "fill_buy")[0]
    assert sig["signal"]["mode"] == "burst" and sig["signal"]["buys"] == 8 and sig["signal"]["buyers"] == 7
    assert sig["size"] == pytest.approx(150 * 0.04)  # 4% of equity
    assert fill["ts"] - sig["ts"] >= 1.5 and fill["latency_s"] == pytest.approx(2.0)  # first real trade after the 1.5 s latency
    cost = fees.side_cost(6.0, curve=True, jupiter_extra_pct=1.5)  # Jupiter round trip 3% -> 1.5% extra a leg
    assert fill["fees"] == pytest.approx(cost.total)
    assert fill["price"] == pytest.approx(px(77.0)) and fill["tokens"] == pytest.approx((6.0 - cost.total) / px(77.0))
    assert eng.cash == pytest.approx(144.0) and fill["council"].startswith("council ")


def test_the_fill_waits_for_the_latency():
    eng = oc.Engine(params())
    ticks = base() + burst()
    ev = run(eng, ticks)
    sig_ts = kinds(ev, "entry_signal")[0]["ts"]
    eng2 = oc.Engine(params())
    run(eng2, base() + burst()[:9])  # up to the signal tick
    assert not eng2.positions
    early = eng2.on_tick(trade(sig_ts + 1.0, "buy", 0.5, "B1", 76.5))
    assert not kinds(early, "fill_buy")  # 1.0 s < 1.5 s latency
    late = eng2.on_tick(trade(sig_ts + 1.6, "buy", 0.5, "B2", 77.0))
    assert kinds(late, "fill_buy")


def test_no_real_price_within_20s_cancels_and_a_run_over_15_percent_cancels():
    eng = oc.Engine(params())
    run(eng, base() + burst()[:9])
    sig_ts = T0 + 100 + 8 * 2.0
    ev = eng.on_tick(trade(sig_ts + 25, "buy", 0.5, "B1", 77.0))
    assert kinds(ev, "entry_cancel")[0]["reason"] == "no real price within the wait" and not eng.positions
    eng = oc.Engine(params())
    run(eng, base() + burst()[:9])
    ev = eng.on_tick(trade(sig_ts + 2, "buy", 0.5, "B1", 95.0))  # +23% since the signal
    assert kinds(ev, "entry_cancel")[0]["reason"].startswith("ran +") and not eng.positions


def test_one_entry_per_mint_and_cooldown():
    eng, fill_ts, _ = entered()
    # the position is stopped out, then another burst on the SAME mint must not re-enter
    run(eng, [trade(fill_ts + 2, "sell", 1.0, "X", 60.0), trade(fill_ts + 6, "sell", 1.0, "X", 58.0)])
    assert not eng.positions and len(eng.trades) == 1
    ev = run(eng, burst(start=fill_ts + 700, v0=70, v1=77))
    assert not kinds(ev, "entry_signal")


GATES = [
    ("few buys", dict(buys=7), None),
    ("few buyers", dict(buyers=5), None),
    ("net sol", dict(sol_each=0.1), None),
    ("buy/sell ratio", dict(extra_sells=6), None),
    ("top wallet", dict(heavy=True), None),
    ("rise too small", dict(v1=70.5), None),
    ("rise too big", dict(v0=60.0, v1=120.0), None),
]


@pytest.mark.parametrize("name,kw,_", GATES, ids=[g[0] for g in GATES])
def test_signal_conditions_each_block_the_entry(name, kw, _):
    ev = run(oc.Engine(params()), base() + burst(**kw))
    assert not kinds(ev, "entry_signal") and not kinds(ev, "fill_buy"), name


BLOCKED = [
    ("age", dict(start=T0 + 2), "< 20s"),
    ("too old", dict(start=T0 + 1300), "age"),
    ("liquidity", dict(v0=40.0, v1=45.0), "liquidity"),
    ("curve vsol", dict(v0=105.0, v1=115.0), "curve vSOL"),
    ("honeypot", dict(sell_first=False), "honeypot check"),
    ("lag", dict(event_ts=None), "signal lag"),
]


@pytest.mark.parametrize("name,kw,reason", BLOCKED, ids=[b[0] for b in BLOCKED])
def test_gates_block_with_a_reason(name, kw, reason):
    kw = dict(kw)
    if "event_ts" in kw:
        kw.pop("event_ts")
        ticks = [dict(t, event_ts=t["ts"] - 90) for t in burst()]
    else:
        ticks = burst(**kw)
    eng = oc.Engine(params())
    ev = run(eng, base() + ticks)
    skips = [e["reason"] for e in kinds(ev, "skip")]
    assert any(reason in s for s in skips) and not kinds(ev, "fill_buy"), (name, skips)


def test_dev_sold_blocks_entry_and_unknown_dev_is_no_data():
    t = base() + [trade(T0 + 90, "sell", 1.0, "DEV", 68.0)] + burst()
    ev = run(oc.Engine(params()), t)
    assert any(e["reason"] == "dev sold" for e in kinds(ev, "skip")) and not kinds(ev, "fill_buy")
    ev = run(oc.Engine(params()), base(create=False) + burst())  # joined late: creation never seen
    assert any("no data" in e["reason"] for e in kinds(ev, "skip")) and not kinds(ev, "fill_buy")


def test_safety_card_is_required_and_must_be_ok():
    ev = run(oc.Engine(params()), base(safety=False) + burst())
    assert any(e["reason"] == "no data: safety card" for e in kinds(ev, "skip"))
    ev = run(oc.Engine(params()), base(safety_ok=False) + burst())
    assert any(e["reason"] == "unsafe: mint authority" for e in kinds(ev, "skip")) and not kinds(ev, "fill_buy")


def test_pause_max_open_cash_and_daily_loss():
    eng = oc.Engine(params())
    eng.pause_reason = "GeckoTerminal feed is 20-50 s late"
    ev = run(eng, base() + burst())
    assert any(e["reason"] == "entries paused: GeckoTerminal feed is 20-50 s late" for e in kinds(ev, "skip"))
    eng = oc.Engine(params(max_open=1))
    ev = run(eng, base("A") + burst("A") + base("B", t0=T0 + 5) + burst("B", start=T0 + 105))
    assert len(eng.positions) == 1 and any(e["reason"] == "max open" for e in kinds(ev, "skip"))
    ev = run(oc.Engine(params(min_ticket=500.0)), base() + burst())
    assert any(e["reason"] == "cash" for e in kinds(ev, "skip"))


def test_no_sol_price_means_no_trade():
    eng = oc.Engine(params())
    run(eng, [t for t in base() if t["kind"] != "sol"] + burst())
    assert not eng.positions and not eng.trades


def test_daily_loss_stop_halts_entries_until_the_next_day():
    p = params(daily_loss_pct=1.0)
    eng, fill_ts, _ = entered(p, "A")
    ev = run(eng, [trade(fill_ts + 2, "sell", 1.0, "X", 40.0, mint="A"), trade(fill_ts + 6, "sell", 1.0, "X", 38.0, mint="A")])
    assert any(e["type"] == "halt" for e in ev) and eng.halted
    ev = run(eng, base("B", t0=fill_ts + 10) + burst("B", start=fill_ts + 110))
    assert any(e["reason"] == "daily loss stop" for e in kinds(ev, "skip")) and "B" not in eng.positions
    nxt = run(eng, [{"kind": "sol", "ts": T0 + 86400 + 10, "usd": SOL}])
    assert not eng.halted and any(e["type"] == "day_start" for e in nxt)


# ---- the exits ------------------------------------------------------------------------------------------------------------------
def test_a_gapped_stop_fills_at_the_first_real_price_never_the_stop_line():
    eng, t, _ = entered()
    entry = eng.positions["M"]["entry_price"]
    stop_line = entry * 0.92
    ev = run(eng, [trade(t + 2, "sell", 1.0, "X", 70.0), trade(t + 4, "sell", 1.0, "X", 60.0)])  # trigger, then a gap down
    sig = kinds(ev, "exit_signal")[0]
    assert sig["rule"] == "stop -8%"
    fill = kinds(ev, "fill_sell")[0]
    assert fill["price"] == pytest.approx(px(60.0)) and fill["price"] < stop_line * 0.9  # filled far below the line
    tr = eng.trades[0]
    assert tr["exit_rule"] == "stop -8%" and tr["pnl_usd"] < 0


def test_take_profit_ladder_then_trail():
    eng, t, _ = entered()
    pos = eng.positions["M"]
    tokens0 = pos["tokens0"]
    ev = run(eng, [trade(t + 2, "buy", 1, "X", 89.0), trade(t + 4, "buy", 1, "X", 90.0)])  # +15.6%: TP1 triggers, fills
    assert [e["rule"] for e in kinds(ev, "fill_sell")] == ["tp1 +15%"] and pos["tokens"] == pytest.approx(tokens0 * 0.6)
    ev = run(eng, [trade(t + 6, "buy", 1, "X", 101.0), trade(t + 8, "buy", 1, "X", 102.0)])  # +31%: TP2
    assert [e["rule"] for e in kinds(ev, "fill_sell")] == ["tp2 +30%"] and pos["tokens"] == pytest.approx(tokens0 * 0.3)
    ev = run(eng, [trade(t + 10, "buy", 1, "X", 110.0), trade(t + 12, "sell", 1, "X", 98.0), trade(t + 14, "sell", 1, "X", 97.0)])
    assert kinds(ev, "exit_signal")[0]["rule"] == "trail -10%"  # 110 -> 98 is -10.9% off the peak
    assert not eng.positions and len(eng.trades) == 1
    tr = eng.trades[0]
    assert [l["rule"] for l in tr["legs"]] == ["tp1 +15%", "tp2 +30%", "trail -10%"] and tr["pnl_usd"] > 0
    assert tr["pnl_usd"] == pytest.approx(sum(l["proceeds"] for l in tr["legs"]) - tr["size_usd"])


def test_time_stop_max_hold_stale_and_dev_sold():
    eng, t, _ = entered()
    ev = eng.on_tick({"kind": "clock", "ts": t + 181})  # 180 s, not up
    assert kinds(ev, "exit_signal")[0]["rule"] == "time stop"
    ev = eng.on_tick(trade(t + 184, "buy", 1, "X", 76.0))
    assert kinds(ev, "fill_sell")[0]["price"] == pytest.approx(px(76.0)) and not eng.positions

    eng, t, _ = entered()  # a rising coin that keeps trading is not time-stopped, but max hold still ends it at 600 s
    ev = run(eng, [trade(t + 30 * i, "buy", 1, "X", 80.0) for i in range(1, 22)])  # +3.9%: below TP1, above entry
    sig = kinds(ev, "exit_signal")
    assert len(sig) == 1 and sig[0]["rule"] == "max hold" and sig[0]["ts"] >= t + 600

    eng, t, _ = entered()  # another coin keeps ticking while ours goes quiet
    run(eng, [trade(t + 2, "buy", 1, "X", 78.0)])
    ev = run(eng, base("Q", t0=t + 3) + [trade(t + 60, "buy", 1, "Z", 50.0, mint="Q")])
    assert any(e["type"] == "exit_signal" and e["rule"] == "stale" for e in ev)

    eng, t, _ = entered()
    ev = run(eng, [trade(t + 2, "sell", 1.0, "DEV", 76.0), trade(t + 4, "buy", 1, "X", 75.0)])
    assert kinds(ev, "exit_signal")[0]["rule"] == "dev sold" and not eng.positions


def test_an_exit_with_no_tick_is_forced_at_the_last_real_price():
    eng, t, _ = entered()
    eng.on_tick({"kind": "clock", "ts": t + 181})
    ev = eng.on_tick({"kind": "clock", "ts": t + 181 + 1.5 + 30})
    f = kinds(ev, "fill_sell")[0]
    assert f["forced"] and f["price"] == pytest.approx(px(77.0)) and not eng.positions  # never an invented price


def test_close_all_at_last_price_for_restarts():
    eng, t, _ = entered()
    ev = eng.close_all_at_last_price(t + 5)
    assert kinds(ev, "trade_closed")[0]["trade"]["exit_rule"] == "desk restart · marked at last real price" and not eng.positions


# ---- trade rows, snapshots, determinism -----------------------------------------------------------------------------------------
def test_trade_rows_are_complete():
    eng, t, _ = entered()
    run(eng, [trade(t + 2, "sell", 1.0, "X", 60.0), trade(t + 4, "sell", 1.0, "X", 59.0)])
    tr = eng.trades[0]
    for key in ("entry_reason", "signal", "safety_card", "council", "round_trip_pct", "fees_buy", "fees_sell", "fees_total",
                "signal_lag_s", "fill_latency_s", "exit_rule", "pnl_usd", "pnl_pct", "legs", "hold_s"):
        assert key in tr, key
    assert tr["entry_reason"] == "burst" and tr["round_trip_pct"] == 3.0 and tr["safety_card"]["ok"] is True
    assert tr["fees_total"] == pytest.approx(tr["fees_buy"] + tr["fees_sell"]) and tr["fees_sell"] > 0
    assert tr["signal_lag_s"] == 0.0 and tr["fill_latency_s"] == pytest.approx(2.0)


def snaps(mint="S", t0=T0 + 100, dex="pumpfun", sells=5, liq=20000.0, buys=(8, 12, 17), prices=(1.0, 1.03, 1.05), gap=5.0):
    out = []
    for i, (b, pr) in enumerate(zip(buys, prices)):
        out.append({"kind": "snap", "ts": t0 + i * gap, "mint": mint, "price_usd": pr, "liquidity_usd": liq, "buys_m5": b,
                    "sells_m5": sells, "dex": dex})
    return out


def test_snapshot_entry_when_there_are_no_live_trades():
    eng = oc.Engine(params())
    ev = run(eng, base("S") + snaps() + [{"kind": "snap", "ts": T0 + 115, "mint": "S", "price_usd": 1.06, "liquidity_usd": 20000.0,
                                         "buys_m5": 19, "sells_m5": 5, "dex": "pumpfun"}])
    sig = kinds(ev, "entry_signal")[0]["signal"]
    assert sig["mode"] == "snapshot" and sig["delta_buys"] == 9 and sig["m5_buys"] == 17 and sig["span_s"] == 10
    assert kinds(ev, "fill_buy")[0]["price"] == 1.06 and eng.positions["S"]["venue"] == "curve"
    eng = oc.Engine(params())
    run(eng, base("S") + snaps(dex="pumpswap") + [{"kind": "snap", "ts": T0 + 115, "mint": "S", "price_usd": 1.06, "liquidity_usd": 20000.0,
                                                 "buys_m5": 19, "sells_m5": 5, "dex": "pumpswap"}])
    assert eng.positions["S"]["venue"] == "amm"  # a migrated coin pays no pump fee / slippage / impact


@pytest.mark.parametrize("kw", [dict(buys=(8, 9, 10)), dict(buys=(10, 12, 14), prices=(1.0, 1.0, 1.0)), dict(buys=(8, 12, 14)),
                                dict(sells=20), dict(sells=0), dict(liq=2000.0)])
def test_snapshot_conditions_block(kw):
    eng = oc.Engine(params())
    ev = run(eng, base("S") + snaps(**kw))
    assert not kinds(ev, "fill_buy") and "S" not in eng.positions


def test_determinism_two_runs_are_identical():
    def go():
        eng = oc.Engine(params())
        ev = run(eng, base("A") + burst("A") + [trade(T0 + 130, "sell", 1, "X", 60.0), trade(T0 + 134, "sell", 1, "X", 55.0)]
                 + base("S") + snaps())
        return json.dumps([ev, eng.trades, eng.equity_curve, eng.cash], sort_keys=True, default=str)

    assert go() == go()


# ---- experiment switches (all off by default) -----------------------------------------------------------------------------------------
def test_experiments_are_off_by_default():
    ex = oc.load_params()["experiments"]
    assert ex["risk_size_pct"] is None and ex["breakeven_after_pct"] is None and ex["vol_stop_k"] is None
    assert ex["sell_pressure_exit"] is None and ex["liq_drop_exit_pct"] is None and ex["pullback_entry_pct"] is None
    assert ex["consecutive_bursts"] == 1


def test_risk_sizing_cuts_the_size_for_wider_stops():
    eng, _, ev = entered(params(size_pct=10.0, exits={"stop_pct": 20.0}, experiments={"risk_size_pct": 1.0}))
    assert kinds(ev, "entry_signal")[0]["size"] == pytest.approx(150 * 0.01 / 0.20)  # 10% = $15 cut to $7.50 so a stop risks 1%
    eng, _, ev = entered(params(exits={"stop_pct": 40.0}, experiments={"risk_size_pct": 1.0}))
    assert kinds(ev, "entry_signal")[0]["size"] == pytest.approx(5.0)  # floor at the min ticket


def test_breakeven_stop_after_a_gain():
    eng, t, _ = entered(params(experiments={"breakeven_after_pct": 6.0}, exits={"tp1_pct": 50.0, "tp2_pct": 80.0}))
    run(eng, [trade(t + 2, "buy", 1, "X", 82.0)])  # +6.5%: armed
    ev = run(eng, [trade(t + 4, "sell", 1, "X", 76.0), trade(t + 6, "sell", 1, "X", 76.0)])  # back below the entry price
    assert kinds(ev, "exit_signal")[0]["rule"] == "break-even stop"


def test_volatility_scaled_stop_is_wider_for_a_wild_coin():
    calm, _, _ = entered(params(experiments={"vol_stop_k": 3.0}))  # a smooth ramp: volatility ~0, the stop stays at 8%
    assert calm.positions["M"]["stop_pct"] == 8.0
    zig = [74, 70, 76, 71, 78, 73, 80, 75, 82, 78, 84]  # a jumpy coin that still rises overall
    ticks = [trade(T0 + 100, "sell", 0.2, "S1", 70.0)] + [trade(T0 + 102 + i * 2, "buy", 0.5, f"B{i % 7}", float(v)) for i, v in enumerate(zig)]
    wild = oc.Engine(params(experiments={"vol_stop_k": 2.0}, burst={"rise_max_pct": 80.0}))
    run(wild, base() + ticks)
    assert wild.positions["M"]["stop_pct"] > 8.0  # wider for a wild coin


def test_sell_pressure_and_liquidity_drop_exits():
    eng, t, _ = entered(params(experiments={"sell_pressure_exit": {"window_s": 20, "min_sellers": 3}}, exits={"stop_pct": 30.0}))
    seq = [trade(t + 2 + i, "sell", 2.0, f"W{i}", 76.0 - 0.1 * i) for i in range(4)]
    ev = run(eng, seq)
    assert any(e["type"] == "exit_signal" and e["rule"] == "sell pressure" for e in ev)
    eng, t, _ = entered(params(experiments={"liq_drop_exit_pct": 20.0}, exits={"stop_pct": 40.0}))
    ev = run(eng, [trade(t + 2, "sell", 1.0, "X", 66.0)])  # liquidity (66-30)*150 vs (77-30)*150 = -23%
    assert any(e["type"] == "exit_signal" and e["rule"] == "liquidity drop" for e in ev)


def test_two_consecutive_bursts_required():
    p = params(experiments={"consecutive_bursts": 2, "burst_gap_s": 10})
    eng = oc.Engine(p)
    ev = run(eng, base() + burst(buys=9))
    assert not kinds(ev, "entry_signal")  # one burst is not enough
    ev = run(eng, [trade(T0 + 100 + 18 + 12 + i * 2, "buy", 0.5, f"B{i % 7}", 77.5 + i * 0.1) for i in range(3)])
    assert kinds(ev, "entry_signal")  # a second qualifying evaluation >= 10 s later


def test_pullback_entry_waits_for_a_pullback():
    p = params(experiments={"pullback_entry_pct": 3.0, "pullback_timeout_s": 60})
    eng = oc.Engine(p)
    ev = run(eng, base() + burst())
    assert not kinds(ev, "entry_signal")  # armed, not entered: no chasing
    run(eng, [trade(T0 + 120 + i * 2, "buy", 0.5, f"B{i}", 78.0 + i) for i in range(3)])  # keeps rising
    ev = run(eng, [trade(T0 + 130, "sell", 0.2, "X", 77.0), trade(T0 + 132, "buy", 0.5, "B1", 77.0)])
    assert kinds(ev, "entry_signal") and kinds(ev, "entry_signal")[0]["signal"]["mode"] == "burst"
