import pytest

from fees import FeeParams, fomo_fee, round_trip, side_cost


@pytest.mark.parametrize("usd,fee", [
    (2, 0.10), (4.99, 0.10), (5, 0.10), (10, 0.20), (47.50, 0.95), (47.51, 0.95),
    (100, 0.95), (190, 0.95), (200, 0.90), (1000, 4.50),
])
def test_fomo_tiers(usd, fee):
    assert fomo_fee(usd) == pytest.approx(fee)


def test_zero_and_negative():
    assert fomo_fee(0) == 0 and fomo_fee(-5) == 0


def test_curve_adds_pump_slippage_impact():
    c = side_cost(100, curve=True)
    assert c.pump == pytest.approx(1.25) and c.slippage == pytest.approx(0.5) and c.impact == pytest.approx(0.5)
    assert c.total == pytest.approx(0.95 + 2.25)
    assert side_cost(100, curve=False).total == pytest.approx(0.95)


def test_jupiter_extra_never_improves_fill():
    base = side_cost(50, curve=True).total
    assert side_cost(50, curve=True, jupiter_extra_pct=-3).total == base
    assert side_cost(50, curve=True, jupiter_extra_pct=2).total == pytest.approx(base + 1.0)


def test_round_trip_is_two_sides_and_pct():
    rt = round_trip(5, curve=True)
    assert rt.total == pytest.approx(2 * side_cost(5, curve=True).total)
    assert rt.pct == pytest.approx(8.5)  # 2% x2 + pump 1.25% x2 + slip 0.5% x2 + impact 0.5% x2


def test_params_are_settings():
    assert fomo_fee(1000, FeeParams(fomo_high_pct=1.0)) == pytest.approx(10.0)
