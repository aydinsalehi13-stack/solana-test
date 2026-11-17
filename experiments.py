"""The 6.3 experiment grid: what to change, tested out-of-sample and nothing else.

Every candidate is a full param set derived from strategies/orion_fast.json. Tuning by feel is how the first version
lost 31.7%: here a candidate only matters if it survives walk-forward (pick on day N, report on day N+1).
"""
from __future__ import annotations

import orion_core as oc
import replay as rp

GRID: dict[str, dict] = {
    "baseline": {},
    # fee vs stop math: bigger tickets (fewer, better trades)
    "big_tickets": {"size_pct": 10.0, "max_open": 2},
    # wider stops with the size cut so $ risk per trade <= 1% of equity
    "stop_12_risk1": {"exits": {"stop_pct": 12.0}, "experiments": {"risk_size_pct": 1.0}},
    "stop_15_risk1": {"exits": {"stop_pct": 15.0}, "experiments": {"risk_size_pct": 1.0}},
    "stop_20_risk1": {"exits": {"stop_pct": 20.0}, "experiments": {"risk_size_pct": 1.0}},
    # break-even stop after +6%: at the entry price, and at entry + the round-trip fee (~9%, above the arming level)
    "breakeven_entry": {"experiments": {"breakeven_after_pct": 6.0, "breakeven_buffer_pct": 0.0}},
    "breakeven_plus_fees": {"experiments": {"breakeven_after_pct": 6.0, "breakeven_buffer_pct": 9.0}},
    "vol_stop": {"experiments": {"vol_stop_k": 2.0}},
    "sell_pressure_exit": {"experiments": {"sell_pressure_exit": {"window_s": 20, "min_sellers": 3}}},
    "liq_drop_exit": {"experiments": {"liq_drop_exit_pct": 25.0}},
    # entry quality
    "two_bursts": {"experiments": {"consecutive_bursts": 2, "burst_gap_s": 10}},
    "pullback_entry": {"experiments": {"pullback_entry_pct": 3.0, "pullback_timeout_s": 45}},
    "skip_first_60s": {"min_age_s": 60},
    "max_one_open": {"max_open": 1},
    "quality_combo": {"min_age_s": 60, "max_open": 1, "experiments": {"consecutive_bursts": 2, "burst_gap_s": 10}},
}


def candidates(base: dict | None = None) -> dict[str, dict]:
    base = base or oc.load_params()
    return {name: oc.merge(base, over) for name, over in GRID.items()}


def report(res: dict) -> str:
    cols = ["trades", "net_usd", "win_rate", "avg_win", "avg_loss", "profit_factor", "max_drawdown_pct", "trades_per_day", "top3_share_pct", "fees_usd"]
    lines = [f"walk-forward over {res['days_tested']} out-of-sample day(s): params picked on day N, reported on day N+1", "",
             "walk-forward choice (out of sample):", rp.table([res["oos"]], cols), "",
             "baseline params (out of sample):", rp.table([res["baseline_oos"]], cols), "",
             "picks: " + ", ".join(f"d{p['day']}:{p['train_best']}" for p in res["picks"]), "",
             rp.verdict_text(res["oos"])]
    if res["days_tested"] < 1:
        lines = ["not enough data: walk-forward needs at least 2 recorded days"]
    return "\n".join(lines)
