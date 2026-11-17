"""Realistic paper fees (spec section 2). Paper must never look better than live.

All percentages are the owner's settings (marked with a gear in the spec); defaults below.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeParams:
    fomo_high_pct: float = 0.45  # % above $190
    pump_fee_pct: float = 1.25  # bonding-curve fills
    slippage_pct: float = 0.5  # bonding-curve fills
    impact_pct: float = 0.5  # bonding-curve fills


@dataclass(frozen=True)
class FeeBreakdown:
    usd: float
    fomo: float
    pump: float
    slippage: float
    impact: float
    jupiter: float

    @property
    def total(self) -> float:
        return self.fomo + self.pump + self.slippage + self.impact + self.jupiter

    @property
    def pct(self) -> float:
        return 100.0 * self.total / self.usd if self.usd else 0.0


def fomo_fee(usd: float, params: FeeParams = FeeParams()) -> float:
    """FOMO fee for one side on Solana. Under $5: $0.10. $5-$47.50: 2%. $47.50-$190: $0.95. Above $190: 0.45%."""
    if usd <= 0:
        return 0.0
    if usd < 5:
        return 0.10
    if usd <= 47.50:
        return usd * 0.02
    if usd <= 190:
        return 0.95
    return usd * params.fomo_high_pct / 100.0


def side_cost(
    usd: float,
    curve: bool = False,
    jupiter_extra_pct: float = 0.0,
    params: FeeParams = FeeParams(),
) -> FeeBreakdown:
    """Total cost of one side. curve=True adds the pump fee, slippage and AMM impact.

    jupiter_extra_pct is how much worse the real Jupiter route is than our fill model; it is
    added on each leg and can never make the fill better (negative values are ignored).
    """
    pump = usd * params.pump_fee_pct / 100.0 if curve else 0.0
    slip = usd * params.slippage_pct / 100.0 if curve else 0.0
    imp = usd * params.impact_pct / 100.0 if curve else 0.0
    jup = usd * max(0.0, jupiter_extra_pct) / 100.0
    return FeeBreakdown(usd, fomo_fee(usd, params), pump, slip, imp, jup)


def round_trip(
    usd: float,
    curve: bool = False,
    jupiter_extra_pct: float = 0.0,
    params: FeeParams = FeeParams(),
) -> FeeBreakdown:
    """Buy then sell of the same notional, as one breakdown (usd is the ticket, not doubled)."""
    one = side_cost(usd, curve, jupiter_extra_pct, params)
    return FeeBreakdown(
        usd, one.fomo * 2, one.pump * 2, one.slippage * 2, one.impact * 2, one.jupiter * 2
    )
