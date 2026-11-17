"""Phase 9: the night check (coverage, loop lag, restarts) and what counts as a full PAPER night."""
import json
import sqlite3
import time

import config
import gates
import night_check as nc
from db import Database
from test_process import TOKEN, desk, http  # noqa: F401  (desk is a fixture)

NIGHT = 8 * 3600.0
T0 = 1_700_000_000.0


async def make_night(db, covered_h=7.8, lag_p95=40.0, lag_minutes=480, closed=True, restarts=True, night_id=1):
    await db.execute("INSERT INTO nights(id, strategy, started_at, ended_at, start_cash, end_cash) VALUES (?,?,?,?,150,151)", (night_id, "prompt", T0, T0 + NIGHT if closed else None))
    t = T0
    while t < T0 + covered_h * 3600:  # beats every 30 s keep one uptime segment going
        await db.record_uptime(t)
        t += 30
    for i in range(lag_minutes):
        await db.execute("INSERT INTO metrics_log(ts, lag_p95_ms, lag_max_ms, restarts) VALUES (?,?,?,0)", (T0 + 60 * i, lag_p95, lag_p95 * 3))
    if restarts:
        await db.execute("INSERT INTO restart_log(ts, kind, bot, reason, down_s) VALUES (?,?,?,?,?)", (T0 - 100, "desk_start", None, "desk process started", None))


async def test_a_good_night_is_full(tmp_path):
    db = Database(tmp_path / "n.db")
    await db.start()
    await make_night(db)
    await db.execute("INSERT INTO restart_log(ts, kind, bot, reason, down_s) VALUES (?,?,?,?,?)", (T0 + 3600, "component", "scanner", "stale 120s", 120.0))
    await db.execute("INSERT INTO restart_log(ts, kind, bot, reason, down_s) VALUES (?,?,?,?,?)", (T0 + 7200, "wake", None, "laptop woke from sleep", 600.0))
    res = await nc.evaluate(db, now=T0 + NIGHT + 5)
    assert res["full"] and res["reasons"] == [] and 96 < res["coverage_pct"] < 98
    assert res["lag"]["status"] == "pass" and res["lag"]["p95_ms"] == 40.0 and res["lag"]["minutes"] == 480
    assert res["restarts"]["components"] == 1 and res["restarts"]["wakes"] == 1 and res["restarts"]["by_bot"] == {"scanner": 1}
    text = nc.render(res)
    assert "FULL PAPER NIGHT" in text and "scanner" in text and "stale 120s" in text
    assert await nc.full_nights(db) == 1
    await db.stop()


async def test_a_night_the_laptop_slept_through_is_not_full_and_says_how_much(tmp_path):
    db = Database(tmp_path / "n.db")
    await db.start()
    await make_night(db, covered_h=4.0)
    res = await nc.evaluate(db)
    assert not res["full"] and 49.8 < res["coverage_pct"] < 50.0 and any("coverage 50% is under 90%" in r for r in res["reasons"])
    await db.stop()


async def test_slow_loop_fails_the_night_and_missing_data_is_never_a_pass(tmp_path):
    db = Database(tmp_path / "n.db")
    await db.start()
    await make_night(db, lag_p95=300.0)
    res = await nc.evaluate(db)
    assert res["lag"]["status"] == "fail" and not res["full"] and any("300.0 ms is not under 250 ms" in r for r in res["reasons"])
    await db.stop()
    db = Database(tmp_path / "m.db")
    await db.start()
    await make_night(db, lag_minutes=0, restarts=False)
    res = await nc.evaluate(db)
    assert res["lag"] == {"status": "no data", "minutes": 0, "p95_ms": None, "worst_minute_p95_ms": None, "max_ms": None, "limit_ms": 250.0}
    assert res["restarts"]["status"] == "no data" and not res["full"] and "loop lag: no data" in res["reasons"] and "restarts: no restart log" in res["reasons"]
    await db.stop()


async def test_lag_is_the_p95_of_the_minutes_so_one_stall_cannot_fail_a_night_and_a_bad_stretch_cannot_hide(tmp_path):
    db = Database(tmp_path / "n.db")
    await db.start()
    await make_night(db, lag_minutes=0)
    for i in range(100):
        await db.execute("INSERT INTO metrics_log(ts, lag_p95_ms, lag_max_ms, restarts) VALUES (?,?,?,0)", (T0 + 60 * i, 900.0 if i == 7 else 30.0, 950.0))  # one bad minute in 100
    one = await nc.evaluate(db)
    assert one["lag"]["status"] == "pass" and one["lag"]["worst_minute_p95_ms"] == 900.0 and one["lag"]["max_ms"] == 950.0
    for i in range(100, 110):  # ten more bad minutes: now 11 of 110 are bad, more than 5%
        await db.execute("INSERT INTO metrics_log(ts, lag_p95_ms, lag_max_ms, restarts) VALUES (?,?,?,0)", (T0 + 60 * i, 900.0, 950.0))
    assert (await nc.evaluate(db))["lag"]["status"] == "fail"
    await db.stop()


async def test_an_open_night_or_no_night_is_not_full(tmp_path):
    db = Database(tmp_path / "n.db")
    await db.start()
    assert (await nc.evaluate(db)) == {"night": None, "full": False, "reasons": ["no night in the database yet"]}
    assert nc.render(await nc.evaluate(db)) == "night check: no night in the database yet"
    await make_night(db, closed=False)
    res = await nc.evaluate(db, now=T0 + NIGHT)
    assert res["night"]["open"] and "the night is still open" in res["reasons"] and await nc.full_nights(db) == 0
    await db.stop()


async def test_the_live_gate_counts_only_full_nights(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    db = Database(tmp_path / "n.db")
    await db.start()
    await make_night(db, covered_h=1.0)  # a closed night, but the desk barely ran
    g = (await gates.live_gates(db))[0]
    assert g["gate"].startswith("one full PAPER night") and g["met"] is False
    await db.execute("DELETE FROM nights")
    await db.execute("DELETE FROM uptime_segments")
    await db.execute("DELETE FROM metrics_log")
    await db.execute("DELETE FROM restart_log")
    await make_night(db)
    assert (await gates.live_gates(db))[0]["met"] is True
    await db.stop()


# ---- the restart log is written by the real desk ----------------------------------------------------------------------------------
def test_the_desk_logs_its_own_start_for_the_night_check(desk):  # noqa: F811
    p, port, tmp = desk
    time.sleep(0.5)
    p.terminate()
    assert p.wait(15) == 0
    con = sqlite3.connect(tmp / "data" / "desk.db")
    rows = con.execute("SELECT kind, reason FROM restart_log").fetchall()
    con.close()
    assert rows == [("desk_start", "desk process started")]


def test_cli_exit_codes_and_json(tmp_path, monkeypatch, capsys):
    import asyncio

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")
    assert nc.main([]) == 1 and "no database yet" in capsys.readouterr().out
    path = tmp_path / "d.db"
    monkeypatch.setattr(config, "DB_PATH", path)

    async def seed():
        db = Database(path)
        await db.start()
        await make_night(db)
        await db.stop()

    asyncio.run(seed())
    assert nc.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["full"] is True and out["night"]["id"] == 1
