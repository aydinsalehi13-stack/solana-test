import pytest

from botkit import MINT, POOL, T0, FakeDex, make_deps, patch_fields, snap
from bots.common import norm_name
from bots.scanner import Entry, Scanner, evaluate_scan, holders_growth_pct
from settings import Settings

S = Settings()
OLD = T0 - 11 * 60  # migrated 11 minutes before "now"


def ev(snap_=None, name="Paws", migrated=OLD, now=T0, runners=None, **kw):
    e = Entry(MINT, migrated, name=name, symbol="PAWS")
    return evaluate_scan(now=now, entry=e, snap=snap_, runners=runners or {}, s=S, **kw)


def full(**kw):
    d = dict(top10_pct=15.0, warnings={"disable_selling": False, "disable_buying": False, "list": []}, holders_growth=5.0,
             have_pass_data=True)
    d.update(kw)
    return d


def test_liquidity_kill_text_and_threshold():
    v = ev(snap(liq=14200))
    assert v.action == "kill" and v.reason == "liq $14.2K"
    assert ev(snap(liq=19999)).action == "kill" and ev(snap(liq=20000)).action != "kill"


def test_liq_zero_or_missing_is_a_recheck_never_a_kill():
    for s_ in (snap(liq=None, known=False), snap(liq=0.0)):
        v = ev(s_)
        assert v.action == "wait" and "re-check" in v.reason
    assert ev(None).action == "wait"  # DexScreener does not know the pair: never passes, never kills


def test_h1_kill_boundary():
    assert ev(snap(h1=201)).reason == "1h +201%" and ev(snap(h1=200)).action != "kill"


def test_copycat_rules():
    runners = {"paws": ("OTHER", T0, 450.0)}
    v = ev(snap(), name="PAWS", runners=runners)
    assert v.action == "kill" and v.reason == "copycat of paws (ran +450% today)"
    assert ev(snap(), name="Paws 2.0 \U0001F43E", runners=runners).action == "kill"  # digits/emoji/spaces stripped
    assert ev(snap(), name="Super Paws Coin", runners=runners).action == "kill"  # one name inside the other
    assert ev(snap(), name="Paw", runners=runners).action == "kill"  # 'paw' is inside 'paws'
    assert ev(snap(), name="Pa", runners=runners).action != "kill"  # under 3 letters: too short for an 'inside' match
    assert ev(snap(), name="Zebra", runners=runners).action != "kill"
    assert ev(snap(), name="Paws", runners={"paws": (MINT, T0, 450.0)}).action != "kill"  # a coin is not its own copycat
    assert ev(snap(), name="12 \U0001F600", runners=runners).action != "kill"  # nothing left after stripping


def test_norm_name():
    assert norm_name("Pa ws 123 \U0001F43E!") == "paws" and norm_name(None) == ""


def test_metric_tokenwarnings_disable_selling_is_a_scanner_kill():
    v = ev(snap(), **full(warnings={"disable_selling": True, "disable_buying": False, "list": []}))
    assert v.action == "kill" and v.reason == "FOMO: selling off"
    v = ev(snap(), **full(warnings={"disable_selling": False, "disable_buying": False, "list": ["Mutable metadata"]}))
    assert v.action == "kill" and v.reason.startswith("FOMO warning")


def test_metric_holders_minus_4_percent_is_a_kill_and_zero_passes():
    v = ev(snap(), **full(holders_growth=-4.0))
    assert v.action == "kill" and v.reason == "holders leaving -4% in 10m"
    assert ev(snap(), **full(holders_growth=0.0)).action == "pass"


def test_top10_kill():
    v = ev(snap(), **full(top10_pct=38.0))
    assert v.action == "kill" and v.reason == "top 10 hold 38%"


def test_pass_needs_age_and_every_datum():
    assert ev(snap(), migrated=T0 - 9 * 60).action == "wait"
    assert ev(snap()).action == "need_data"
    assert ev(snap(), **full()).action == "pass"
    assert ev(snap(), **full(top10_pct=None)).reason == "no data: top 10"
    assert ev(snap(), **full(warnings=None)).reason == "no data: FOMO tokenWarnings"
    assert ev(snap(), **full(holders_growth=None)).reason == "no data: holders"
    assert ev(snap(h1=None), **full()).reason == "no data: 1h change"
    assert ev(snap(), migrated=T0 - 61 * 60).action == "drop"


def test_holders_growth_helper():
    h = [(T0 - 600, 100), (T0 - 300, 102), (T0, 96)]
    assert holders_growth_pct(h, T0) == pytest.approx(-4.0)
    assert holders_growth_pct([(T0, 100)], T0) is None
    assert holders_growth_pct([(T0 - 100, 100), (T0, 110)], T0) is None  # no reading from ~10 min ago
    assert holders_growth_pct([(T0 - 600, None), (T0, 50)], T0) is None


# ---- the waiting room --------------------------------------------------------------------------------------------------------
async def room(tmp_path, monkeypatch, **kw):
    patch_fields(monkeypatch)
    d = await make_deps(tmp_path, **kw)
    sc = Scanner(d)
    return d, sc


async def seed_history(d, hold=100):
    """Holders readings from ~10 min ago so growth can be computed."""
    return [(T0 - 600, hold)]


async def test_add_computes_curve_minutes_and_creator_balance(tmp_path, monkeypatch):
    d, sc = await room(tmp_path, monkeypatch)
    await d.db.execute("INSERT INTO new_tokens(mint, ts, name, symbol, creator) VALUES (?,?,?,?,?)", (MINT, OLD - 4 * 60, "Paws", "PAWS", "DEV"))
    d.rpc.balances["DEV"] = 1234.0
    e = await sc.add({"mint": MINT, "seen_at": OLD})
    assert e.curve_min == pytest.approx(4.0) and e.creator_balance0 == 1234.0 and e.symbol == "PAWS"
    assert await sc.add({"mint": MINT, "seen_at": OLD}) is None  # once
    await d.db.stop()


async def test_unknown_creator_balance_stays_null(tmp_path, monkeypatch):
    d, sc = await room(tmp_path, monkeypatch)
    d.rpc.fail.add("token_balance")
    await d.db.execute("INSERT INTO new_tokens(mint, ts, name, symbol, creator) VALUES (?,?,?,?,?)", (MINT, OLD - 60, "Paws", "PAWS", "DEV"))
    e = await sc.add({"mint": MINT, "seen_at": OLD})
    assert e.creator_balance0 is None
    await d.db.stop()


async def test_poll_passes_once_publishes_scan_pass_and_stores_prices(tmp_path, monkeypatch):
    d, sc = await room(tmp_path, monkeypatch)
    d.fomo.holders[MINT] = 96
    e = await sc.add({"mint": MINT, "seen_at": OLD})
    e.name = "Paws"
    e.holders_hist.append((T0 - 600, 100))
    d.dex.snaps[MINT] = snap(price=2.5, vol5=1000, vol1=6000, liq=40000, mcap=200000)
    d.rpc.top10 = 15.0
    counts = await sc.poll()
    assert counts["pass"] == 0 and counts["kill"] == 1  # holders 100 -> 96 = -4%: kill
    d.hub.bus.recent(topic="scan_pass") == []

    sc2 = Scanner(d)
    d.fomo.holders[MINT] = 104
    e2 = await sc2.add({"mint": MINT, "seen_at": OLD})
    e2.holders_hist.append((T0 - 600, 100))
    counts = await sc2.poll()
    assert counts["pass"] == 1 and MINT not in sc2.room and MINT in sc2.passed_tonight
    payload = d.hub.bus.recent(topic="scan_pass")[-1]["data"]
    assert payload["mint"] == MINT and payload["pool"] == POOL and payload["liq_at_scan"] == 40000
    assert await sc2.add({"mint": MINT, "seen_at": OLD}) is None  # never passes a coin twice a night
    import asyncio
    await asyncio.sleep(0.1)
    scans = await d.db.fetchall("SELECT verdict, liq_mcap, accel, holders FROM scans ORDER BY id")
    assert scans[-1]["verdict"] == "pass" and scans[-1]["liq_mcap"] == pytest.approx(0.2) and scans[-1]["accel"] == pytest.approx(2.0)
    assert (await d.db.fetchone("SELECT price FROM prices ORDER BY ts DESC LIMIT 1"))["price"] == 2.5
    await d.db.stop()


async def test_nothing_passes_without_holders_or_warnings_data(tmp_path, monkeypatch):
    d, sc = await room(tmp_path, monkeypatch)
    d.fomo.fail = True  # logged out
    e = await sc.add({"mint": MINT, "seen_at": OLD})
    e.name = "Paws"
    d.dex.snaps[MINT] = snap()
    counts = await sc.poll()
    assert counts["pass"] == 0 and MINT in sc.room
    assert d.hub.bus.recent(topic="scan_pass") == []
    await d.db.stop()


async def test_unindexed_pair_is_rechecked_then_dropped_at_60_min_never_killed(tmp_path, monkeypatch):
    d, sc = await room(tmp_path, monkeypatch)
    e = await sc.add({"mint": MINT, "seen_at": OLD})
    d.dex.snaps[MINT] = snap(liq=None, known=False)
    for _ in range(3):
        assert (await sc.poll())["wait"] == 1
    d.dex.snaps[MINT] = snap(liq=45000)  # DexScreener caught up: it is judged normally from now on
    d.clock.t += 0
    assert MINT in sc.room
    d.clock.t = OLD + 61 * 60
    assert (await sc.poll())["drop"] == 1 and MINT not in sc.room and MINT in sc.dead
    await d.db.execute("SELECT 1")  # barrier: queued writes land first
    rows = await d.db.fetchall("SELECT reason FROM scans WHERE reason LIKE 'liq unindexed%'")
    assert rows and "[liquidity key absent]" in rows[0]["reason"]  # recorded for the liq-$0 investigation
    await d.db.stop()


async def test_runners_register_and_expire_after_a_day(tmp_path, monkeypatch):
    d, sc = await room(tmp_path, monkeypatch)
    e = await sc.add({"mint": MINT, "seen_at": OLD})
    e.name = "Paws"
    d.dex.snaps[MINT] = snap(h24=450.0, liq=45000)
    await sc.poll()
    assert "paws" in sc.runners
    assert "paws" in sc._runners_today(T0) and "paws" not in sc._runners_today(T0 + 86401)
    await d.db.stop()


async def test_dex_failure_changes_nothing(tmp_path, monkeypatch):
    d, sc = await room(tmp_path, monkeypatch)
    await sc.add({"mint": MINT, "seen_at": OLD})
    d.dex.fail = True
    assert await sc.poll() == {"pass": 0, "kill": 0, "wait": 0, "drop": 0} and MINT in sc.room
    await d.db.stop()
