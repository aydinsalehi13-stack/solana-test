"""The spec's named test cases (section 11): gate, brain, risk, dip, metrics."""
import pytest

from bots.analyst import LayerResult, compute_score, passes_gate
from bots.brain import size_trade
from bots.executor import dip_decision, parse_amount, tp_decision
from bots.risk import MintState, check_position
from book import Position, tighten_stop
from settings import Settings

S = Settings()


def green(n=8):
    return [LayerResult(i + 1, "x", True, "ok") for i in range(n)]


def good(**kw):
    d = {"volume_h1": 25000, "unique_buys_1h": 80, "unique_sells_1h": 40, "unique_buys_5m": 10, "unique_sells_5m": 5,
         "change_h1": 0.0, "bundled": 0, "curve_min": 30, "fresh_pct": 10, "social": None}
    d.update(kw)
    return d


# ---- gate -----------------------------------------------------------------------------------------------------------------
def test_score_69_rejects_70_passes():
    assert not passes_gate(green(), 69, S) and passes_gate(green(), 70, S)


def test_one_red_layer_rejects_whatever_social_says():
    layers = green()
    layers[4] = LayerResult(5, "Liquidity", False, "liquidity unlocked")
    score = compute_score(good(social=1.0), S)["score"]
    assert score == 100 and not passes_gate(layers, score, S)
    assert not passes_gate(green(7), 100, S)  # 7 of 8 is a no


def test_social_off_gives_4_part_score_and_says_so():
    r = compute_score(good(), S)
    assert r["score"] == 100 and r["social_pts"] == 0 and "social: off" in r["notes"]
    core3 = good(unique_buys_5m=10, unique_sells_5m=14, volume_h1=25000)  # flush 0.5
    r = compute_score(core3, S)
    assert r["core"] == pytest.approx(3.5) and r["score"] == 88  # round(100*3.5/4)


def test_social_adds_weighted_points_and_social_pts_is_the_difference():
    base = compute_score(good(unique_buys_5m=10, unique_sells_5m=30), S)  # flush 0 -> core 3.0
    assert base["score"] == 75
    r = compute_score(good(unique_buys_5m=10, unique_sells_5m=30, social=1.0), S)
    assert r["score"] == round(100 * (3.0 + 0.5) / 4.5) == 78 and r["social_pts"] == 3 and "social: off" not in r["notes"]


def test_missing_data_can_never_raise_a_score():
    full = compute_score(good(), S)["score"]
    assert compute_score(good(volume_h1=None), S)["score"] < full
    assert compute_score(good(change_h1=None), S)["score"] < full  # missing chase = worst case
    assert compute_score(good(unique_buys_5m=None, unique_sells_5m=None, buys_m5=None, sells_m5=None), S)["score"] < full


def test_organic_falls_back_to_txns_and_says_so():
    r = compute_score(good(unique_buys_1h=None, unique_sells_1h=None, buys_h1=150, sells_h1=60), S)
    assert r["organic"] == 1.0 and "organic: txns" in r["notes"]


# ---- metrics (spec: curve 4 min + fresh 60% -> organic x0.25) ---------------------------------------------------------------
def test_sniped_curve_and_fresh_wallets_multiply_organic_by_quarter():
    r = compute_score(good(curve_min=4, fresh_pct=60), S)
    assert r["organic"] == pytest.approx(0.25) and len(r["penalties"]) == 2


def test_each_organic_penalty_halves():
    assert compute_score(good(unique_sells_1h=90), S)["organic"] == pytest.approx(0.5)
    assert compute_score(good(bundled=3), S)["organic"] == pytest.approx(0.5)
    assert compute_score(good(bundled=2), S)["organic"] == 1.0


def test_flush_levels():
    f = lambda b, s_: compute_score(good(unique_buys_5m=b, unique_sells_5m=s_), S)["flush"]  # noqa: E731
    assert f(10, 10) == 1.0 and f(10, 15) == 0.5 and f(10, 16) == 0.0


# ---- brain ----------------------------------------------------------------------------------------------------------------
def test_brain_spec_cases():
    big_pool = 1_000_000
    assert size_trade(100, 80, big_pool, S).size_usd == 12.00
    assert size_trade(100, 79, big_pool, S).size_usd == 6.00
    s = size_trade(10, 79, big_pool, S)
    assert s.skip and s.reason == "under $2"
    owner = Settings({"max_ticket": 100000})
    r = size_trade(10000, 90, 20000, owner)
    assert r.size_usd == 400.00 and r.cap_hit == "pool"


def test_brain_ticket_cap_shill_and_rounding():
    r = size_trade(1000, 90, 1_000_000, S)
    assert r.size_usd == 50.0 and r.cap_hit == "ticket"
    assert size_trade(100, 80, 1_000_000, S, shill=True).size_usd == 6.0
    assert size_trade(111.11, 80, 1_000_000, S).size_usd == 13.33  # 13.3332 rounded to cents
    assert size_trade(100, 80, 1_000_000, S).pct_of_cash == pytest.approx(12.0)


def test_brain_limits_and_no_data():
    assert size_trade(100, 90, 1e6, S, trades_tonight=30).skip
    assert size_trade(100, 90, 1e6, S, night_loss_pct=30.0).skip
    assert not size_trade(100, 90, 1e6, S, night_loss_pct=29.9).skip
    assert size_trade(100, 90, None, S).reason == "no data: pool liquidity"
    assert size_trade(0, 90, 1e6, S).skip


# ---- risk -----------------------------------------------------------------------------------------------------------------
def pos(entry=1.0, opened=0.0, **meta):
    return Position(1, "M", "PAWS", opened, entry, 20.0, 20.0 / entry, entry, entry * 0.75, meta={"initial_tokens": 20.0 / entry, **meta})


def risk(p, price, liq=40000.0, now=100.0, st=None, **kw):
    return check_position(p, price, liq, now, st or MintState(), S, **kw)


def test_stop_at_minus_25_1_percent_but_not_minus_24_9():
    assert risk(pos(), 0.749).sell.startswith("stop")
    assert risk(pos(), 0.75).sell.startswith("stop")
    assert risk(pos(), 0.751).sell is None


def test_dead_money_after_3h_at_plus_4_percent():
    h3 = 3 * 3600
    assert risk(pos(), 1.04, now=h3).sell == "dead money"
    assert risk(pos(), 1.06, now=h3).sell is None
    assert risk(pos(), 1.04, now=h3 - 60).sell is None


def test_pulled_liquidity():
    st = MintState(prev_liq=40000.0)
    assert risk(pos(), 1.0, liq=0.0, st=st).sell == "pulled"
    assert risk(pos(), 1.0, liq=8000.0, st=MintState(prev_liq=40000.0)).sell == "pulled"  # -80%
    assert risk(pos(), 1.0, liq=8400.0, st=MintState(prev_liq=40000.0)).sell is None  # -79%


def test_dev_dumping_and_fomo_warnings():
    p = pos(creator_balance_buy=1000.0)
    assert risk(p, 1.0, creator_balance=500.0).sell == "dev dumping"
    assert risk(p, 1.0, creator_balance=501.0).sell is None
    o = risk(pos(), 1.0, warnings={"disable_selling": True, "list": []})
    assert o.sell is None and "selling off" in o.pushes[0]
    st = MintState(selling_off_pushed=True)
    assert risk(pos(), 1.0, st=st, warnings={"disable_selling": True}).pushes == []  # one push only
    assert risk(pos(), 1.0, warnings={"disable_selling": False, "list": ["mintable"]}).sell == "FOMO warns"


def test_no_price_for_12_polls_sells_without_inventing_a_price():
    st, p = MintState(), pos()
    for i in range(11):
        assert risk(p, None, st=st).sell is None
    assert risk(p, None, st=st).sell == "no price"
    assert risk(p, 1.0, st=MintState(no_price_polls=5)).sell is None  # a price resets the count


def test_a_looser_stop_setting_never_moves_an_open_stop_down():
    p = pos()
    assert p.stop_price == 0.75
    risk(p, 1.0)
    assert p.stop_price == 0.75
    assert tighten_stop(0.75, 1.0, 40) == 0.75  # looser (40%) ignored
    assert tighten_stop(0.75, 1.0, 10) == pytest.approx(0.9)  # tighter applies


def test_order_of_rules_stop_first():
    assert risk(pos(), 0.5, liq=0.0, now=4 * 3600).sell.startswith("stop")


# ---- take profit / trail ----------------------------------------------------------------------------------------------------
def test_take_profit_then_trail():
    p = pos()
    assert tp_decision(p, 1.99, S) is None
    assert tp_decision(p, 2.0, S) == (50, "take profit 2x")
    p.tp_done, p.peak_price = True, 3.0
    assert tp_decision(p, 1.81, S) is None
    assert tp_decision(p, 1.80, S)[0] == 100 and "trail" in tp_decision(p, 1.7, S)[1]


# ---- dip ------------------------------------------------------------------------------------------------------------------
def test_dip_cases():
    assert dip_decision(1.0, 1.0, 10, 5, 1, S)[0] == "wait"  # wait at the high
    v = dip_decision(0.85, 1.0, 10, 20, 1, S)
    assert v[0] == "wait" and "sellers" in v[1]  # -15% with sellers running
    assert dip_decision(0.85, 1.0, 10, 15, 1, S)[0] == "buy"  # exactly 1.5x is allowed
    assert dip_decision(0.85, 1.0, 12, 5, 1, S)[0] == "buy"  # -15% with buyers back
    assert dip_decision(0.86, 1.0, 12, 5, 1, S)[0] == "wait"  # only -14%
    assert dip_decision(1.0, 1.0, 10, 5, 30, S) == ("skip", "no dip")
    c = dip_decision(0.59, 1.0, 12, 5, 1, S)
    assert c[0] == "skip" and c[1].startswith("crash") and "41" in c[1]  # a 41% dip
    assert dip_decision(0.65, 1.0, 12, 5, 1, S)[0] == "buy"  # -35% is still a dip, not a crash
    assert dip_decision(0.85, 1.0, None, None, 1, S)[1] == "no data: 5m buyers/sellers"


def test_parse_amount():
    assert parse_amount("44K paws") == 44000 and parse_amount("1.2M x") == 1_200_000 and parse_amount("17.5 PAWS") == 17.5
    assert parse_amount("nope") is None and parse_amount(None) is None
