"""The owner's settings (every number marked with a gear in the spec), their defaults and valid ranges.

Hard rule 10: these are the owner's. Code never changes one; only the owner does, in the app (validated range,
change_log row with who = 'owner'). strategies/prompt.json freezes the defaults and a test pins its sha256.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

OWNER = "owner"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    key: str
    default: object
    lo: float | None
    hi: float | None
    group: str
    label: str


def _s(key, default, lo, hi, group, label):
    return Spec(key, default, lo, hi, group, label)


REGISTRY: list[Spec] = [
    # session
    _s("hours_24_7", True, None, None, "session", "Trade 24/7"),
    _s("night_start", "22:00", None, None, "session", "Night start (local HH:MM)"),
    _s("lock_time", "06:30", None, None, "session", "Lock time: session rolls over, open positions sold"),
    _s("report_time", "08:00", None, None, "session", "Night report time"),
    _s("paper_start_cash", 150.0, 10, 100000, "session", "PAPER start cash ($)"),
    # scanner
    _s("scan_poll_s", 60, 10, 600, "scanner", "Waiting-room poll (s)"),
    _s("kill_liq_usd", 20000, 0, 10_000_000, "scanner", "Kill: liquidity below ($)"),
    _s("kill_top10_pct", 25, 1, 100, "scanner", "Kill: top 10 hold more than (%)"),
    _s("kill_h1_pct", 200, 10, 100000, "scanner", "Kill: 1h price change above (%)"),
    _s("runner_h24_pct", 300, 50, 1_000_000, "scanner", "Copycat check: today's runners ran at least (%)"),
    _s("holders_growth_min_pct", 0, -100, 100, "scanner", "Kill: holders growth in 10 min below (%)"),
    _s("pass_age_min", 10, 1, 120, "scanner", "Pass at pool age (min)"),
    _s("drop_after_min", 60, 10, 600, "scanner", "Drop coin not passed after (min)"),
    # analyst
    _s("source_retries", 3, 1, 10, "analyst", "Source error retries"),
    _s("dev_hold_max_pct", 5, 0, 100, "analyst", "Layer 3: dev holds less than (%)"),
    _s("top10_max_pct", 25, 1, 100, "analyst", "Layer 4: top 10 hold less than (%)"),
    _s("lp_locked_min_pct", 99, 0, 100, "analyst", "Layer 5: LP burned/locked at least (%)"),
    _s("liq_drop_max_pct", 50, 1, 100, "analyst", "Layer 5: liquidity not down more than (%) since Scanner"),
    _s("pool_age_min", 10, 1, 120, "analyst", "Layer 6: pool age at least (min)"),
    _s("bundle_wallets_max", 5, 1, 100, "analyst", "Layer 7: bundled wallets fewer than"),
    _s("insiders_max_pct", 15, 1, 100, "analyst", "Layer 7: insider networks hold less than (%)"),
    _s("delegates_max", 10, 1, 1000, "analyst", "Layer 8: accounts delegating to one address, at most"),
    _s("alive_volume_h1", 25000, 1000, 10_000_000, "analyst", "Score: 1h volume for full 'alive' ($)"),
    _s("organic_unique_buys", 80, 1, 10000, "analyst", "Score: unique buyers (1h) for full 'organic'"),
    _s("organic_txns_buys", 150, 1, 100000, "analyst", "Score: buys (1h) for full 'organic' when no unique data"),
    _s("organic_penalty", 0.5, 0.05, 1.0, "analyst", "Score: organic multiplier per red flag"),
    _s("sniped_curve_min", 10, 1, 600, "analyst", "Score: curve under (min) = sniped"),
    _s("fresh_pct_max", 50, 1, 100, "analyst", "Score: fresh-wallet holders at least (%) = flag"),
    _s("flush_ratio", 1.5, 1.0, 10, "analyst", "Score: sells within x buys (5m) = half flush"),
    _s("chase_h1_pct", 200, 10, 100000, "analyst", "Score: 1h change that counts as full chase (%)"),
    _s("social_weight", 0.5, 0, 2, "analyst", "Score: social weight"),
    _s("pass_score", 70, 1, 100, "analyst", "Pass at score"),
    _s("shill_threshold", 0.5, 0, 1, "analyst", "Shill ring: Brain sizes half at"),
    # brain
    _s("risk_pct", 3, 0.1, 20, "brain", "Risk per trade (% of free cash)"),
    _s("stop_pct", 25, 1, 90, "brain", "Stop (% below entry)"),
    _s("full_score", 80, 1, 100, "brain", "Full size at score"),
    _s("reduced_size_pct", 50, 1, 100, "brain", "Size below full score (% of full)"),
    _s("pool_cap_pct", 2, 0.1, 50, "brain", "Cap at (% of pool liquidity)"),
    _s("max_ticket", 50, 2, 100000, "brain", "Max ticket ($)"),
    _s("min_ticket", 2, 1, 1000, "brain", "Min ticket ($)"),
    _s("max_trades_night", 30, 1, 1000, "brain", "Max trades per night"),
    _s("night_loss_pct", 30, 1, 100, "brain", "Stop buying at night loss (%)"),
    # executor
    _s("dip_wait_min", 30, 1, 240, "executor", "Dip order waits up to (min)"),
    _s("dip_window_min", 10, 1, 120, "executor", "High is the top of the last (min)"),
    _s("dip_pct", 15, 1, 90, "executor", "Buy when price is below the high by (%)"),
    _s("sellers_ratio", 1.5, 1.0, 10, "executor", "Wait while 5m sellers exceed x buyers"),
    _s("crash_pct", 35, 5, 99, "executor", "Crash: skip when deeper than (%)"),
    _s("recheck_liq_drop_pct", 20, 1, 100, "executor", "Re-check: skip if liquidity down more than (%)"),
    _s("recheck_run_pct", 15, 1, 1000, "executor", "Re-check: skip if price up more than (%) since Analyst"),
    _s("jupiter_roundtrip_max_pct", 12, 1, 100, "executor", "Jupiter round-trip loss at most (%)"),
    _s("jupiter_sell_impact_max_pct", 15, 1, 100, "executor", "Jupiter sell impact at most (%)"),
    _s("tp_mult", 2.0, 1.1, 100, "executor", "Take profit at (x entry)"),
    _s("tp_sell_pct", 50, 1, 100, "executor", "Take-profit sells (% of position)"),
    _s("trail_pct", 40, 1, 90, "executor", "Trail below the peak (%)"),
    # risk
    _s("risk_poll_s", 5, 1, 60, "risk", "Risk poll (s)"),
    _s("dead_hours", 3, 0.5, 48, "risk", "Dead money after (h)"),
    _s("dead_gain_pct", 5, 0, 100, "risk", "Dead money if gain below (%)"),
    # fees
    _s("fomo_high_fee_pct", 0.45, 0, 10, "fees", "FOMO fee above $190 (%)"),
    _s("pump_fee_pct", 1.25, 0, 10, "fees", "Pump fee, bonding-curve fills (%)"),
    _s("slippage_pct", 0.5, 0, 10, "fees", "Slippage, bonding-curve fills (%)"),
    _s("impact_pct", 0.5, 0, 10, "fees", "AMM impact, bonding-curve fills (%)"),
]
# Owner settings that are NOT part of the frozen "prompt" strategy (so strategies/prompt.json does not contain them).
EXTRA: list[Spec] = [
    _s("live_interval_min", 5, 1, 120, "telegram", "Dashboard screenshot to Telegram every (min)"),
]
ALL: list[Spec] = REGISTRY + EXTRA
BY_KEY = {s.key: s for s in ALL}


def defaults() -> dict:
    return {s.key: s.default for s in REGISTRY}


def validate(key: str, value):
    spec = BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"unknown setting {key!r}")
    if isinstance(spec.default, bool):
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(spec.default, str):
        if not isinstance(value, str) or not _HHMM.match(value):
            raise SettingError(f"{key}: expected HH:MM, got {value!r}")
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise SettingError(f"{key}: not a number: {value!r}") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise SettingError(f"{key}: not a finite number")
    if spec.lo is not None and v < spec.lo or spec.hi is not None and v > spec.hi:
        raise SettingError(f"{key}: {v:g} is outside {spec.lo:g}..{spec.hi:g}")
    return int(v) if isinstance(spec.default, int) and float(v).is_integer() else v


class Settings:
    """Defaults plus the owner's validated overrides (from the settings table)."""

    def __init__(self, overrides: dict | None = None):
        self._v = {s.key: s.default for s in ALL}
        for k, v in (overrides or {}).items():
            if k in BY_KEY:
                self._v[k] = validate(k, v)

    def __getitem__(self, key: str):
        return self._v[key]

    def get(self, key: str):
        return self._v[key]

    def as_dict(self) -> dict:
        return dict(self._v)

    @classmethod
    async def load(cls, db) -> "Settings":
        rows = await db.fetchall("SELECT key, value FROM settings")
        over = {}
        for r in rows:
            if r["key"] in BY_KEY:
                try:
                    over[r["key"]] = validate(r["key"], r["value"])
                except SettingError:
                    pass  # an invalid stored value never loosens anything: the default stays
        return cls(over)

    async def set(self, db, key: str, value, who: str):
        """Only the owner may change a setting. Validated, written to settings and change_log."""
        if who != OWNER:
            raise SettingError(f"only the owner changes settings (who={who!r})")
        new = validate(key, value)
        old = self._v[key]
        now = time.time()
        await db.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (key, str(new), now))
        await db.execute("INSERT INTO change_log(ts, key, old, new, who) VALUES (?,?,?,?,?)", (now, key, str(old), str(new), who))
        self._v[key] = new
        return old, new

    def fee_params(self):
        from fees import FeeParams

        return FeeParams(self["fomo_high_fee_pct"], self["pump_fee_pct"], self["slippage_pct"], self["impact_pct"])
