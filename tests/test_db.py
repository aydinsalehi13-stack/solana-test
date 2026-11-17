import asyncio
import time

import pytest

from db import Database, DatabaseClosed

TABLES = {"coins", "scans", "audits", "scores", "orders", "positions", "activity", "prices", "balance", "settings",
          "rugs", "change_log", "heartbeats", "decisions", "nights", "new_tokens", "push_subs", "ticks", "sol_price",
          "strat_positions", "strat_balance", "strat_events"}


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db", checkpoint_interval=0.1)
    await d.start()
    yield d
    await d.stop()


async def test_wal_and_normal_sync(db):
    assert db.writer_pragmas["journal_mode"] == "wal"
    assert db.writer_pragmas["synchronous"] == 1  # NORMAL
    assert (await db.fetchone("PRAGMA journal_mode"))["journal_mode"] == "wal"


async def test_all_spec_tables_exist(db):
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    assert TABLES <= {r["name"] for r in rows}


async def test_write_then_read_is_visible(db):
    r = await db.execute("INSERT INTO new_tokens(mint, ts, name) VALUES (?,?,?)", ("M1", 1.0, "x"))
    assert r.rowcount == 1
    assert (await db.fetchone("SELECT name FROM new_tokens WHERE mint='M1'"))["name"] == "x"


async def test_concurrent_writes_all_land_in_order(db):
    await asyncio.gather(*[db.execute("INSERT INTO sol_price(ts, usd) VALUES (?,?)", (i, 100 + i)) for i in range(300)])
    assert (await db.fetchone("SELECT COUNT(*) n FROM sol_price"))["n"] == 300


async def test_failed_write_raises_but_others_succeed(db):
    ok = db.execute("INSERT INTO new_tokens(mint, ts) VALUES ('A', 1)")
    bad = db.execute("INSERT INTO nope VALUES (1)")
    res = await asyncio.gather(ok, bad, return_exceptions=True)
    assert res[0].rowcount == 1 and isinstance(res[1], Exception)
    assert (await db.fetchone("SELECT COUNT(*) n FROM new_tokens"))["n"] == 1


async def test_orders_status_check_constraint(db):
    with pytest.raises(Exception):
        await db.execute("INSERT INTO orders(ts, mint, side, status) VALUES (1,'m','buy','bogus')")


async def test_event_loop_is_not_blocked_by_heavy_writes(db):
    lags = []

    async def ticker():
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t - 0.01)

    task = asyncio.create_task(ticker())
    rows = [(i, "M", "trade", "buy", 1.0, 0.1, "w", None) for i in range(20000)]
    await db.executemany("INSERT INTO ticks(ts, mint, kind, side, sol, price, wallet, data) VALUES (?,?,?,?,?,?,?,?)", rows)
    task.cancel()
    assert max(lags) < 0.25, f"loop stalled {max(lags):.3f}s"


async def test_retention_deletes_old_in_batches(db):
    now = time.time()
    old, new = now - 8 * 86400, now - 3600
    await db.executemany("INSERT INTO ticks(ts, mint, kind) VALUES (?,?,?)", [(old, "m", "t")] * 2500 + [(new, "m", "t")] * 10)
    await db.executemany("INSERT INTO prices(mint, ts, price) VALUES (?,?,?)", [("m", now - 31 * 86400, 1)] * 5 + [("m", new, 1)])
    out = await db.retention(now=now, batch=1000)
    assert out == {"ticks": 2500, "prices": 5}
    assert (await db.fetchone("SELECT COUNT(*) n FROM ticks"))["n"] == 10
    assert (await db.fetchone("SELECT COUNT(*) n FROM prices"))["n"] == 1


async def test_heartbeat_upsert_and_settings(db):
    await db.write_heartbeats([("loop", 1.0, "")])
    await db.write_heartbeats([("loop", 2.0, "x")])
    assert (await db.fetchone("SELECT ts, info FROM heartbeats WHERE name='loop'")) == {"ts": 2.0, "info": "x"}
    assert await db.get_setting("mode", "PAPER") == "PAPER"


async def test_closed_db_rejects_writes(tmp_path):
    d = Database(tmp_path / "c.db")
    await d.start()
    await d.stop()
    with pytest.raises(DatabaseClosed):
        await d.execute("SELECT 1")
