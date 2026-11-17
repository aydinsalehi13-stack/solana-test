import hashlib
import json

import pytest

import settings as st
from conftest import ROOT
from db import Database

SPEC_NUMBERS = {  # the numbers written in the owner's spec (section 5)
    "paper_start_cash": 150, "scan_poll_s": 60, "kill_liq_usd": 20000, "kill_top10_pct": 25, "kill_h1_pct": 200,
    "runner_h24_pct": 300, "holders_growth_min_pct": 0, "pass_age_min": 10, "drop_after_min": 60, "source_retries": 3,
    "dev_hold_max_pct": 5, "top10_max_pct": 25, "lp_locked_min_pct": 99, "liq_drop_max_pct": 50, "pool_age_min": 10,
    "bundle_wallets_max": 5, "insiders_max_pct": 15, "delegates_max": 10, "alive_volume_h1": 25000, "organic_unique_buys": 80,
    "organic_txns_buys": 150, "organic_penalty": 0.5, "sniped_curve_min": 10, "fresh_pct_max": 50, "flush_ratio": 1.5,
    "chase_h1_pct": 200, "social_weight": 0.5, "pass_score": 70, "shill_threshold": 0.5, "risk_pct": 3, "stop_pct": 25,
    "full_score": 80, "reduced_size_pct": 50, "pool_cap_pct": 2, "max_ticket": 50, "min_ticket": 2, "max_trades_night": 30,
    "night_loss_pct": 30, "dip_wait_min": 30, "dip_window_min": 10, "dip_pct": 15, "sellers_ratio": 1.5, "crash_pct": 35,
    "recheck_liq_drop_pct": 20, "recheck_run_pct": 15, "jupiter_roundtrip_max_pct": 12, "jupiter_sell_impact_max_pct": 15,
    "tp_mult": 2.0, "tp_sell_pct": 50, "trail_pct": 40, "risk_poll_s": 5, "dead_hours": 3, "dead_gain_pct": 5,
    "fomo_high_fee_pct": 0.45, "pump_fee_pct": 1.25, "slippage_pct": 0.5, "impact_pct": 0.5,
}


def test_defaults_match_the_spec_numbers():
    d = st.defaults()
    for k, v in SPEC_NUMBERS.items():
        assert d[k] == v, k
    assert d["lock_time"] == "06:30" and d["night_start"] == "22:00" and d["report_time"] == "08:00" and d["hours_24_7"] is True


def test_registry_is_consistent():
    keys = [s.key for s in st.REGISTRY]
    assert len(keys) == len(set(keys))
    for s in st.REGISTRY:
        if s.lo is not None:
            assert s.lo <= float(s.default) <= s.hi, s.key


def test_prompt_json_is_frozen_to_the_registry_and_its_hash():
    p = ROOT / "strategies" / "prompt.json"
    doc = json.loads(p.read_text())
    assert doc["settings"] == st.defaults() and doc["frozen"] is True
    assert hashlib.sha256(p.read_bytes()).hexdigest() == (ROOT / "strategies" / "prompt.json.sha256").read_text().strip(), \
        "strategies/prompt.json changed without the owner's change_log entry"


@pytest.mark.parametrize("key,val", [("kill_liq_usd", -1), ("kill_liq_usd", "abc"), ("pass_score", 101), ("stop_pct", 0),
                                     ("lock_time", "25:00"), ("lock_time", 630), ("nope", 1), ("pass_score", float("nan")),
                                     ("tp_mult", 1.0)])
def test_invalid_values_are_rejected(key, val):
    with pytest.raises(st.SettingError):
        st.validate(key, val)


def test_valid_values_convert():
    assert st.validate("pass_score", "75") == 75 and st.validate("lock_time", "07:15") == "07:15"
    assert st.validate("hours_24_7", "false") is False and st.validate("organic_penalty", "0.4") == 0.4


async def test_only_the_owner_changes_settings_and_it_is_logged(tmp_path):
    db = Database(tmp_path / "s.db")
    await db.start()
    s = st.Settings()
    for who in ("claude", "bot", "", "system", "Owner"):
        with pytest.raises(st.SettingError):
            await s.set(db, "pass_score", 75, who)
    assert s["pass_score"] == 70
    assert await s.set(db, "pass_score", 75, st.OWNER) == (70, 75)
    row = await db.fetchone("SELECT key, old, new, who FROM change_log")
    assert row == {"key": "pass_score", "old": "70", "new": "75", "who": "owner"}
    assert (await st.Settings.load(db))["pass_score"] == 75
    with pytest.raises(st.SettingError):
        await s.set(db, "pass_score", 500, st.OWNER)
    assert (await db.fetchone("SELECT COUNT(*) n FROM change_log"))["n"] == 1
    await db.stop()


async def test_corrupt_stored_value_never_loosens_anything(tmp_path):
    db = Database(tmp_path / "s2.db")
    await db.start()
    await db.execute("INSERT INTO settings(key, value, updated_at) VALUES ('kill_liq_usd', '-5', 1)")
    assert (await st.Settings.load(db))["kill_liq_usd"] == 20000
    await db.stop()


def test_no_code_path_changes_settings_except_settings_set():
    import re

    allowed = {"settings.py"}
    for f in ROOT.rglob("*.py"):
        if ".venv" in f.parts or "tests" in f.parts or f.name in allowed:
            continue
        t = f.read_text()
        assert not re.search(r"settings\._v\[|\.set\(db,", t) or f.name in ("api.py", "api_dash.py"), f.name  # the dashboard's owner-authenticated POST /api/settings
