import asyncio

import pytest

import links

from botkit import MINT, POOL, T0, make_deps, patch_fields, snap
from bots import analyst as an
from bots.analyst import Analyst
from settings import Settings

S = Settings()
ADDR = "9fQx" + "a" * 37 + "T2a"  # 44 chars like a real address


# ---- the 8 layers, exact texts ---------------------------------------------------------------------------------------------------
def test_layers_1_2_authorities():
    ok = {"mint_authority": None, "freeze_authority": None}
    assert (an.layer1_mint(ok).ok, an.layer1_mint(ok).value) == (True, "Revoked")
    assert an.layer1_mint({"mint_authority": "X", "freeze_authority": None}).value == "mint not revoked"
    assert an.layer2_freeze({"mint_authority": None, "freeze_authority": "Y"}).value == "freeze not revoked"
    r = an.layer1_mint(None)
    assert not r.ok and r.value == "no data: mint authority" and r.nodata


def test_layer3_dev_texts():
    f = lambda **kw: an.layer3_dev(**{"creator": "D", "balance_now": 9e6, "balance_at_migration": 9e6, "supply": 1e9,  # noqa: E731
                                      "rugged_before": 0, **kw}, s=S)
    assert (f().ok, f().value) == (True, "0.9% · sold 0")
    assert f(balance_now=3.1e8, balance_at_migration=3.1e8).value == "dev holds 31%"
    assert f(balance_now=1e6, balance_at_migration=9e6).value == "dev sold"
    assert f(balance_now=None).value == "no data: dev"
    assert f(balance_at_migration=None).value == "no data: dev"  # cannot prove 'never sold'
    assert f(creator=None).value == "no data: dev"
    assert f(rugged_before=2).value == "dev rugged 2 before"
    assert f(rugged_before=0).ok  # a fresh dev with no history is NOT red
    assert f(balance_now=5e7, balance_at_migration=5e7).ok is False  # exactly 5% is not 'less than 5%'


def test_layer4_top10():
    assert an.layer4_top10(58.0, S).value == "top 10 hold 58%" and not an.layer4_top10(25.0, S).ok
    assert an.layer4_top10(24.9, S).ok and an.layer4_top10(None, S).value == "no data: top 10"


def rep(locked=100, pool=POOL, ins=4e7):
    return {"markets": [{"pubkey": pool, "lp": {"lpLockedPct": locked}}], "insiderNetworks": [{"tokenAmount": ins}], "token": {"supply": 1e9}}


def test_layer5_liquidity():
    assert an.layer5_liquidity(rep(), POOL, 40000, 40000, S).value == "100% burned"
    assert an.layer5_liquidity(rep(0), POOL, 40000, 40000, S).value == "liquidity unlocked"
    assert an.layer5_liquidity(rep(98.9), POOL, 40000, 40000, S).ok is False
    assert an.layer5_liquidity(rep(pool="OTHER"), POOL, 40000, 40000, S).value == "no data: liquidity"
    assert an.layer5_liquidity(None, POOL, 40000, 40000, S).value == "no data: liquidity"
    assert an.layer5_liquidity(rep(), POOL, 40000, 19000, S).value == "liquidity down 52% since scan"
    assert an.layer5_liquidity(rep(), POOL, 40000, 20000, S).ok  # exactly 50% is allowed
    assert an.layer5_liquidity(rep(), POOL, 40000, None, S).nodata


def test_layer6_pool_age():
    assert an.layer6_age(T0 - 9 * 60, T0, S).value == "pool too young"
    assert an.layer6_age(T0 - 10 * 60, T0, S).ok and an.layer6_age(None, T0, S).nodata


def chain(n):
    return {"nodes": [{"id": str(i)} for i in range(n)], "links": [{"source": str(i), "target": str(i + 1)} for i in range(n - 1)]}


def test_layer7_bundles_and_metric_insiders_38_percent_is_red():
    assert an.layer7_bundles(chain(14), rep(), S).value == "14 bundled wallets"
    assert an.layer7_bundles(chain(2), rep(ins=3.8e8), S).value == "insiders hold 38%"
    r = an.layer7_bundles(chain(2), rep(), S)
    assert r.ok and r.value == "2 bundled · insiders 4%"
    assert an.layer7_bundles({"nodes": []}, rep(), S).value == "no data: bundles"  # no edges key
    assert an.layer7_bundles(None, rep(), S).nodata
    assert an.layer7_bundles(chain(2), {"markets": []}, S).nodata  # report cannot say


def test_layer8_delegations():
    d = {"accounts": 100, "by_delegate": {ADDR: 41}, "top_holders": []}
    assert an.layer8_delegations(d, S).value == "41 wallets → 9fQx…T2a"
    assert an.layer8_delegations({"accounts": 5, "by_delegate": {ADDR: 10}, "top_holders": []}, S).ok  # exactly 10: fine
    assert an.layer8_delegations({"accounts": 5, "by_delegate": {}, "top_holders": []}, S).value == "None"
    assert an.layer8_delegations(None, S).value == "no data: delegations"


# ---- the audit ---------------------------------------------------------------------------------------------------------------------
def coin(**kw):
    c = {"mint": MINT, "symbol": "PAWS", "name": "Paws", "pool": POOL, "creator": "DEV", "curve_min": 30.0,
         "creator_balance_at_migration": 1000.0, "liq_at_scan": 40000.0, "migrated_at": T0 - 11 * 60}
    c.update(kw)
    return c


async def setup(tmp_path, monkeypatch, **kw):
    patch_fields(monkeypatch)
    d = await make_deps(tmp_path, **kw)
    d.rpc.balances["DEV"] = 1000.0
    d.dex.snaps[MINT] = snap()
    a = Analyst(d)
    a.fresh_spread_s = 0.0
    return d, a


async def rows(d, sql):
    await d.db.execute("SELECT 1")
    return await d.db.fetchall(sql)


async def test_all_green_passes_publishes_audit_pass_and_stores_everything(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    res = await a.audit(coin())
    assert res["passed"] and res["score"] == 90
    assert [l.value for l in res["layers"]][:2] == ["Revoked", "Revoked"] and res["layers"][7].value == "None"
    p = d.hub.bus.recent(topic="audit_pass")[-1]["data"]
    assert p["mint"] == MINT and p["pool_liq"] == 40000 and p["price"] == 1.0 and p["creator_balance"] == 1000.0
    assert p["scanner_liq"] == 40000 and p["social_pts"] == 0 and p["shill"] is False
    audits = await rows(d, 'SELECT layer, "pass" FROM audits ORDER BY layer')
    assert [(r["layer"], r["pass"]) for r in audits] == [(i, 1) for i in range(1, 9)]
    sc = (await rows(d, 'SELECT score, "pass", reason FROM scores'))[0]
    assert sc["score"] == 90 and sc["pass"] == 1 and "social: off" in sc["reason"]
    dec = (await rows(d, "SELECT decision, bot FROM decisions"))[-1]
    assert dec == {"decision": "pass", "bot": "analyst"}
    layers_live = [e["data"]["layer"] for e in d.hub.bus.recent(100, "audit_layer")]
    assert layers_live == list(range(1, 9))  # emitted live, one by one
    await d.db.stop()


async def test_first_red_stops_the_rest_show_not_checked_and_slow_sources_are_cancelled(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.rpc.mint["mint_authority"] = "SomeAuthority"
    d.rpc.block = {"delegations", "top10_pct"}  # would hang forever if they were awaited
    res = await asyncio.wait_for(a.audit(coin()), 3)
    assert not res["passed"] and res["reason"] == "layer 1 Mint authority: mint not revoked"
    audits = await rows(d, 'SELECT layer, value_text, "pass" FROM audits ORDER BY layer')
    assert audits[0]["pass"] == 0 and all(r["pass"] is None and r["value_text"] == "not checked" for r in audits[1:])
    assert len(audits) == 8
    assert (await rows(d, "SELECT decision FROM decisions"))[-1]["decision"] == "reject"
    await d.db.stop()


async def test_source_errors_are_retried_3_times_2s_apart_then_are_red_no_data(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.rpc.fail = {"mint_info"}
    res = await a.audit(coin())
    assert res["reason"] == "layer 1 Mint authority: no data: mint authority"
    assert d.clock.slept == [2, 2] and d.rpc.calls.count("mint_info") == 3
    await d.db.stop()


async def test_a_flaky_source_that_recovers_within_retries_passes(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.rpc.fail_times = {"mint_info": 2}
    assert (await a.audit(coin()))["passed"]
    await d.db.stop()


async def test_metric_creator_in_rugs_table_is_red_dev_rugged_2_before(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    await d.db.executemany("INSERT INTO rugs(creator, mint, ts, source) VALUES (?,?,?,?)", [("DEV", "OLD1", 1, "t"), ("DEV", "OLD2", 1, "t")])
    res = await a.audit(coin())
    assert res["reason"] == "layer 3 Dev wallet: dev rugged 2 before"
    await d.db.stop()


async def test_rugcheck_down_means_no_data_red_never_a_pass(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.rugcheck.fail = {"report"}
    res = await a.audit(coin())
    assert res["reason"] == "layer 5 Liquidity: no data: liquidity"
    await d.db.stop()


async def test_layer8_refusal_is_red_and_pushes(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.rpc.fail = {"delegations"}
    res = await a.audit(coin())
    assert res["reason"] == "layer 8 Delegations: no data: delegations"
    push = d.hub.bus.recent(topic="push")[-1]["data"]
    assert "Layer 8" in push["text"] and push["link"].startswith(links.SIGNUP_URL + "/coin?address=")
    await d.db.stop()


async def test_green_layers_but_low_score_is_rejected_with_a_score_row(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.dex.snaps[MINT] = snap(vol1=0.0, buys1=0, sells1=0, h1=200.0, buys5=1, sells5=10)
    res = await a.audit(coin())
    assert not res["passed"] and res["reason"] == "score 0 < 70"
    sc = (await rows(d, 'SELECT score, "pass" FROM scores'))[0]
    assert sc == {"score": 0, "pass": 0}
    assert d.hub.bus.recent(topic="audit_pass") == []
    await d.db.stop()


async def test_fresh_wallets_penalty_flows_into_the_score(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.rpc.sigs = 3  # every top holder is a brand new wallet
    res = await a.audit(coin(curve_min=4.0))
    assert res["scoring"]["fresh_pct"] == 100.0 and len(res["scoring"]["penalties"]) == 2
    assert res["scoring"]["organic"] == pytest.approx(100 / 150 * 0.25)
    await d.db.stop()


async def test_run_loop_never_re_asks_a_rejected_mint_the_same_night(tmp_path, monkeypatch):
    d, a = await setup(tmp_path, monkeypatch)
    d.rpc.mint["freeze_authority"] = "F"
    t = asyncio.create_task(a.run())
    d.hub.bus.publish("scan_pass", coin())
    await asyncio.sleep(0.3)
    d.hub.bus.publish("scan_pass", coin())
    await asyncio.sleep(0.3)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    decs = [r["decision"] for r in await rows(d, "SELECT decision FROM decisions ORDER BY id")]
    assert decs == ["reject", "skip"] and d.rpc.calls.count("mint_info") == 1
    a.new_night()
    assert MINT not in a.rejected
    await d.db.stop()
