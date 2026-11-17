"""Phase 10: VERIFY (3 checks, never a second click), reconcile (LIVE only), and the desk surviving a kill with no duplicate orders."""
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

import osutil
import reconcile as rc
import verify as vf
from conftest import ROOT
from db import Database
from test_process import TOKEN, http

MINT = "VerifyMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"
T = 1_700_000_000.0


class Clock:
    def __init__(self):
        self.t = T

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


async def new_order(db, side="buy", usd=2.0, status="queued"):
    r = await db.execute("INSERT INTO orders(ts, mint, symbol, side, usd, bot, status, strategy) VALUES (?,?,?,?,?,?,?,?)", (T, MINT, "VFY", side, usd, "executor", status, "prompt"))
    return r.lastrowid


def swap(side="buy", usd=2.0, ts=T + 5, mint=MINT):
    return {"side": side, "mint": mint, "usd": usd, "ts": ts}


async def run_verify(db, oid, *, text="Bought VFY", swaps=None, before=1.0, after=2.0, side="buy", usd=2.0, wait_s=6.0, notify=None):
    c = Clock()

    async def page():
        return text

    async def sw():
        return swaps

    async def chain():
        return after

    return await vf.verify_order(db, oid, side=side, mint=MINT, usd=usd, page_text_fn=page, swaps_fn=sw, chain_fn=chain, since=T, chain_before=before,
                                 wait_s=wait_s, poll_s=2.0, clock=c, sleep=c.sleep, notify=notify)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "v.db")
    await d.start()
    yield d
    await d.stop()


# ---- the three checks -----------------------------------------------------------------------------------------------------------
def test_screen_api_chain_checks_are_each_true_false_or_no_data():
    assert vf.screen_check("buy", "Bought $2 VFY") is True and vf.screen_check("buy", "Buying $2 VFY") is False and vf.screen_check("sell", "Sold VFY") is True
    assert vf.screen_check("buy", None) is None and vf.screen_check("buy", "") is None
    assert vf.api_check("buy", MINT, 2.0, [swap()], T) is True
    assert vf.api_check("buy", MINT, 2.0, [swap(usd=2.06)], T) is True  # 3% of $2 is $0.06
    assert vf.api_check("buy", MINT, 2.0, [swap(usd=2.07)], T) is False
    assert vf.api_check("buy", MINT, 2.0, [swap(side="sell")], T) is False and vf.api_check("buy", MINT, 2.0, [swap(mint="Other")], T) is False
    assert vf.api_check("buy", MINT, 2.0, [swap(ts=T - 600)], T) is False  # an OLD swap of the same coin is not this order
    assert vf.api_check("buy", MINT, 2.0, None, T) is None and vf.api_check("buy", MINT, 2.0, [], T) is False
    assert vf.chain_check("buy", 1.0, 2.0) is True and vf.chain_check("buy", 2.0, 2.0) is False and vf.chain_check("sell", 2.0, 0.0) is True
    assert vf.chain_check("buy", None, 2.0) is None and vf.chain_check("buy", 1.0, None) is None


def test_decide_needs_all_three_and_chain_may_be_na_but_never_wrong():
    assert vf.decide(True, True, True)[0] == "done"
    status, why = vf.decide(True, True, None)
    assert status == "done" and "n/a" in why  # chain could not be proven, and the record says so
    assert vf.decide(True, True, False)[0] == "unverified"
    assert vf.decide(True, False, True)[0] == "unverified" and vf.decide(True, None, True)[0] == "unverified"
    assert vf.decide(False, True, True)[0] == "unverified" and vf.decide(None, None, None)[0] == "unverified"
    assert vf.decide(False, False, None)[0] == "failed"


async def test_a_fake_bought_with_no_api_swap_is_unverified_and_never_clicked_again(db):
    oid = await new_order(db)
    assert await vf.claim_click(db, oid) is True  # the one click
    told = []
    res = await run_verify(db, oid, text="Bought VFY", swaps=[], before=1.0, after=1.0, notify=told.append)
    assert res["status"] == "unverified" and res["screen"] is True and res["api"] is False and res["chain"] is False
    row = await db.fetchone("SELECT status, screen_ok, api_ok, chain_ok, error FROM orders WHERE id=?", (oid,))
    assert row["status"] == "unverified" and (row["screen_ok"], row["api_ok"], row["chain_ok"]) == (1, 0, 0) and "check FOMO, not clicked again" in row["error"]
    assert told == ["Unverified: Verify buy, check FOMO"]
    assert await vf.claim_click(db, oid) is False  # no second click, ever, for this order
    assert (await db.fetchone("SELECT COUNT(*) n FROM orders"))["n"] == 1


async def test_all_three_agreeing_is_done_and_waits_for_a_late_swap(db):
    oid = await new_order(db)
    await vf.claim_click(db, oid)
    c, calls = Clock(), {"n": 0}

    async def swaps():
        calls["n"] += 1
        return [swap()] if calls["n"] >= 3 else []  # the API lags the screen by two polls

    async def page():
        return "Bought VFY"

    async def chain():
        return 2.0

    res = await vf.verify_order(db, oid, side="buy", mint=MINT, usd=2.0, page_text_fn=page, swaps_fn=swaps, chain_fn=chain, since=T, chain_before=1.0, wait_s=30, poll_s=2.0, clock=c, sleep=c.sleep)
    assert res["status"] == "done" and calls["n"] == 3 and c.t - T == 4.0  # stopped as soon as all three agreed
    assert (await db.fetchone("SELECT status, error FROM orders WHERE id=?", (oid,))) == {"status": "done", "error": None}


async def test_chain_na_is_recorded_as_null_not_as_a_pass_or_a_fail(db):
    oid = await new_order(db)
    await vf.claim_click(db, oid)
    res = await run_verify(db, oid, swaps=[swap()], before=None, after=None, wait_s=0)
    row = await db.fetchone("SELECT status, chain_ok FROM orders WHERE id=?", (oid,))
    assert res["status"] == "done" and row["chain_ok"] is None


async def test_no_data_anywhere_is_never_a_pass(db):
    oid = await new_order(db)
    await vf.claim_click(db, oid)
    res = await run_verify(db, oid, text=None, swaps=None, before=None, after=None, wait_s=0)
    assert res["status"] == "unverified" and "no data" in res["why"]


async def test_a_sell_is_verified_by_the_right_side_only(db):
    oid = await new_order(db, side="sell")
    await vf.claim_click(db, oid)
    wrong = await run_verify(db, oid, side="sell", text="Sold VFY", swaps=[swap(side="buy")], before=5.0, after=0.0, wait_s=0)
    assert wrong["status"] == "unverified" and wrong["api"] is False


# ---- one click per order, across a crash ------------------------------------------------------------------------------------------
async def test_claim_click_is_atomic_two_claimers_one_click(db):
    import asyncio

    oid = await new_order(db)
    got = await asyncio.gather(*(vf.claim_click(db, oid) for _ in range(5)))
    assert got.count(True) == 1


async def test_recover_marks_in_flight_orders_unverified_and_leaves_the_rest(db):
    a, b, c, d = await new_order(db, status="clicking"), await new_order(db, status="verifying"), await new_order(db, status="queued"), await new_order(db, status="done")
    rows = await vf.recover_orders(db)
    assert {r["id"] for r in rows} == {a, b}
    st = {r["id"]: r["status"] for r in await db.fetchall("SELECT id, status FROM orders")}
    assert st == {a: "unverified", b: "unverified", c: "queued", d: "done"}  # queued was never clicked: untouched
    assert await vf.recover_orders(db) == []  # idempotent


def test_swaps_from_raw_reads_only_mapped_fields_and_never_guesses():
    raw = {"data": {"swaps": [{"s": "BUY", "m": MINT, "u": "2.01", "t": T + 1}]}}
    unmapped = {"swaps.rows": {"path": None}}
    assert vf.swaps_from_raw(raw, unmapped) is None  # unmapped: no data, even though the keys look obvious
    fmap = {"swaps.rows": {"path": "data.swaps"}, "swap.side": {"path": "s"}, "swap.in_symbol": {"path": None}, "swap.out_symbol": {"path": None},
            "swap.mint": {"path": "m"}, "swap.usd": {"path": "u"}, "swap.ts": {"path": "t"}}
    assert vf.swaps_from_raw(raw, fmap) == [{"side": "buy", "mint": MINT, "usd": 2.01, "ts": T + 1}]


# ---- reconcile (LIVE only) ---------------------------------------------------------------------------------------------------------
async def live_pos(db, mint, symbol="LVP"):
    await db.execute("INSERT INTO positions(mint, symbol, opened_at, entry_price, size_usd, tokens, mode, strategy) VALUES (?,?,?,?,?,?,?,?)", (mint, symbol, T, 1.0, 2.0, 2.0, "LIVE", "prompt"))


async def test_reconcile_does_nothing_in_paper(db):
    await db.execute("INSERT INTO positions(mint, symbol, opened_at, entry_price, size_usd, tokens, mode, strategy) VALUES ('P1','PPR',?,1,6,6,'PAPER','prompt')", (T,))
    res = await rc.reconcile(db, {}, mode="PAPER")
    assert res["status"].startswith("skipped") and (await db.fetchone("SELECT closed_at FROM positions WHERE mint='P1'"))["closed_at"] is None


async def test_reconcile_closes_what_fomo_does_not_hold_without_inventing_pnl_and_adopts_with_a_price(db):
    await live_pos(db, "GONE")
    await live_pos(db, "KEPT", "KPT")
    told = []
    res = await rc.reconcile(db, {"KEPT": 5.0, "NEW": 9.0}, mode="LIVE", prices={"NEW": 0.5}, push=told.append, now=T + 9)
    assert res["closed"] == ["GONE"] and res["adopted"] == ["NEW"]
    gone = await db.fetchone("SELECT closed_at, exit_reason, pnl_usd FROM positions WHERE mint='GONE'")
    assert gone == {"closed_at": T + 9, "exit_reason": "not found on FOMO", "pnl_usd": None}
    new = await db.fetchone("SELECT entry_price, tokens, mode FROM positions WHERE mint='NEW'")
    assert new == {"entry_price": 0.5, "tokens": 9.0, "mode": "LIVE"}
    assert (await db.fetchone("SELECT closed_at FROM positions WHERE mint='KEPT'"))["closed_at"] is None
    assert len(told) == 2 and any("not found on FOMO" in t for t in told) and any("Adopted" in t for t in told)


async def test_reconcile_with_no_data_changes_nothing_and_adopt_needs_a_real_price(db):
    await live_pos(db, "SAFE")
    told = []
    res = await rc.reconcile(db, None, mode="LIVE", push=told.append)
    assert res["status"] == "no data" and (await db.fetchone("SELECT closed_at FROM positions WHERE mint='SAFE'"))["closed_at"] is None and told
    res = await rc.reconcile(db, {"SAFE": 1.0, "MYSTERY": 3.0}, mode="LIVE", prices={}, push=told.append)
    assert res["adopted"] == [] and await db.fetchone("SELECT 1 FROM positions WHERE mint='MYSTERY'") is None and "no price is known" in told[-1]


def test_before_a_buy_already_held_or_unreadable_means_no_buy():
    assert rc.buy_allowed({"X": 1.0}, "X") == (False, "already held on FOMO")
    assert rc.buy_allowed({"X": 1.0}, "Y")[0] is True
    assert rc.buy_allowed(None, "Y")[0] is False  # cannot prove we do not hold it


def test_held_from_balances_needs_every_row_readable():
    fmap = {"balances.positions": {"path": "p"}, "balances.position_mint": {"path": "m"}, "balances.position_amount": {"path": "a"}}
    assert rc.held_from_balances({"p": [{"m": "A", "a": "2"}, {"m": "B", "a": 0}]}, fmap) == {"A": 2.0}
    assert rc.held_from_balances({"p": []}, fmap) == {}  # holds nothing is data
    assert rc.held_from_balances({"p": [{"m": "A", "a": "x"}]}, fmap) is None and rc.held_from_balances({}, fmap) is None
    assert rc.held_from_balances({"p": []}, {"balances.positions": {"path": None}}) is None


# ---- the desk dies hard and comes back: no duplicate order, in-flight order not re-clicked -----------------------------------
def start_desk(tmp_path, port):
    env = {**os.environ, "DESK_DATA_DIR": str(tmp_path / "data"), "DESK_LOG_DIR": str(tmp_path / "logs"), "DESK_PORT": str(port), "DESK_HOST": "127.0.0.1",
           "APP_TOKEN": TOKEN, "DESK_FEED": "0", "DESK_BOTS": "0", "DESK_KEEP_AWAKE": "0"}
    p = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if p.poll() is None and http(f"http://127.0.0.1:{port}/api/ping", TOKEN, 1)[0] == 200:
            return p
        time.sleep(0.2)
    p.kill()
    pytest.fail("desk did not come up")


def test_kill_the_desk_and_it_comes_back_without_running_an_order_twice(tmp_path):
    port = osutil.free_port()
    p1 = start_desk(tmp_path, port)
    pid1 = p1.pid
    time.sleep(0.5)
    os.kill(p1.pid, signal.SIGKILL)  # no cleanup, no goodbye
    p1.wait(10)
    db = tmp_path / "data" / "desk.db"
    con = sqlite3.connect(db)
    con.execute("INSERT INTO orders(ts, mint, symbol, side, usd, bot, status, strategy) VALUES (?,?,?,?,?,?,?,?)", (T, MINT, "MID", "buy", 2.0, "executor", "clicking", "prompt"))
    con.execute("INSERT INTO orders(ts, mint, symbol, side, usd, bot, status, strategy) VALUES (?,?,?,?,?,?,?,?)", (T, MINT, "NEXT", "buy", 2.0, "executor", "queued", "prompt"))
    con.commit()
    con.close()
    (tmp_path / "data" / "desk.pid").write_text(str(pid1))  # the kill left a stale pid file, like a real crash
    p2 = start_desk(tmp_path, port)
    try:
        assert p2.pid != pid1 and (tmp_path / "data" / "desk.pid").read_text() == str(p2.pid)
        time.sleep(0.5)
        con = sqlite3.connect(db)
        orders = con.execute("SELECT symbol, status FROM orders ORDER BY id").fetchall()
        starts = con.execute("SELECT COUNT(*) FROM restart_log WHERE kind='desk_start'").fetchone()[0]
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        assert orders == [("MID", "unverified"), ("NEXT", "queued")]  # the in-flight one is never clicked again; the never-clicked one is untouched
        assert starts == 2 and ok == "ok"
    finally:
        p2.terminate()
        p2.wait(15)
