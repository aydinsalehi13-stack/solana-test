"""SQLite (WAL) store.

The event loop never touches the disk: all writes go through ONE writer thread fed by a queue
(group-committed, synchronous=NORMAL), reads run on a small thread pool with read-only
connections, and a background thread runs wal_checkpoint(PASSIVE) every 60 s.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("desk.db")

Result = namedtuple("Result", "lastrowid rowcount")
_STOP = object()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS coins (
  mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, pool TEXT, created_at REAL, migrated_at REAL,
  curve_min REAL, creator TEXT, creator_balance_at_migration REAL, holders INTEGER);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL, price REAL, mcap REAL,
  liquidity_usd REAL, volume_m5 REAL, volume_h1 REAL, top10_pct REAL, h1_change_pct REAL,
  h24_change_pct REAL, age_min REAL, liq_mcap REAL, accel REAL, holders INTEGER, holders_growth REAL,
  verdict TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_scans_mint ON scans(mint, ts);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL, layer INTEGER NOT NULL,
  name TEXT, value_text TEXT, "pass" INTEGER, source TEXT);
CREATE INDEX IF NOT EXISTS ix_audits_mint ON audits(mint, ts);
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL, alive REAL, organic REAL,
  flush REAL, chase REAL, social REAL, social_pts REAL, fresh_pct REAL, score REAL, "pass" INTEGER,
  reason TEXT);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mint TEXT NOT NULL, symbol TEXT, side TEXT NOT NULL,
  usd REAL, pct REAL, bot TEXT, reason TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','clicking','verifying','done','failed','unverified')),
  quote_text TEXT, fee_text TEXT, screen_ok INTEGER, api_ok INTEGER, chain_ok INTEGER, fill_usd REAL,
  error TEXT, shot_path TEXT, strategy TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, symbol TEXT, opened_at REAL NOT NULL,
  entry_price REAL, size_usd REAL, tokens REAL, peak_price REAL, stop_price REAL, tp_done INTEGER DEFAULT 0,
  trail_active INTEGER DEFAULT 0, closed_at REAL, exit_price REAL, exit_reason TEXT, pnl_usd REAL,
  fees_usd REAL, mode TEXT NOT NULL DEFAULT 'PAPER', strategy TEXT);
CREATE INDEX IF NOT EXISTS ix_positions_open ON positions(strategy, closed_at);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT, bot TEXT, mint TEXT, text TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS prices (mint TEXT NOT NULL, ts REAL NOT NULL, price REAL, volume_m5 REAL, holders INTEGER);
CREATE INDEX IF NOT EXISTS ix_prices ON prices(mint, ts);
CREATE INDEX IF NOT EXISTS ix_prices_ts ON prices(ts);
CREATE TABLE IF NOT EXISTS balance (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, strategy TEXT, cash_usd REAL, equity_usd REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS rugs (
  creator TEXT NOT NULL, mint TEXT NOT NULL, ts REAL, source TEXT, PRIMARY KEY (creator, mint));
CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key TEXT NOT NULL, old TEXT, new TEXT, who TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeats (name TEXT PRIMARY KEY, ts REAL NOT NULL, info TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, bot TEXT, mint TEXT, symbol TEXT, decision TEXT,
  reason TEXT, value TEXT, strategy TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_decisions ON decisions(ts);
CREATE TABLE IF NOT EXISTS nights (
  id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, started_at REAL NOT NULL, ended_at REAL,
  start_cash REAL, end_cash REAL, trades INTEGER, coverage_s REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS new_tokens (mint TEXT PRIMARY KEY, ts REAL NOT NULL, name TEXT, symbol TEXT, creator TEXT);
CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, keys TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS ticks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mint TEXT NOT NULL, kind TEXT NOT NULL,
  side TEXT, sol REAL, price REAL, wallet TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_ticks_ts ON ticks(ts);
CREATE INDEX IF NOT EXISTS ix_ticks_mint ON ticks(mint, ts);
CREATE TABLE IF NOT EXISTS sol_price (ts REAL PRIMARY KEY, usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS strat_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT, opened_at REAL,
  closed_at REAL, entry_price REAL, exit_price REAL, size_usd REAL, tokens REAL, pnl_usd REAL, fees_usd REAL,
  exit_rule TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS strat_balance (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, strategy TEXT NOT NULL, cash_usd REAL, equity_usd REAL);
CREATE TABLE IF NOT EXISTS strat_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, strategy TEXT NOT NULL, mint TEXT, kind TEXT, data TEXT);
"""


class DatabaseClosed(RuntimeError):
    pass


class Database:
    def __init__(self, path, checkpoint_interval: float = 60.0):
        self.path = Path(path)
        self.checkpoint_interval = checkpoint_interval
        self.writer_pragmas: dict[str, object] = {}
        self._q: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        self._ckpt: threading.Thread | None = None
        self._ckpt_stop = threading.Event()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._closed = False
        self._readers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="db-read")
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._read_lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = threading.Thread(target=self._writer_main, name="db-writer", daemon=True)
        self._writer.start()
        await asyncio.to_thread(self._ready.wait)
        if self._start_error:
            raise self._start_error
        self._ckpt = threading.Thread(target=self._ckpt_main, name="db-checkpoint", daemon=True)
        self._ckpt.start()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(_STOP)
        self._ckpt_stop.set()
        if self._writer:
            await asyncio.to_thread(self._writer.join)
        if self._ckpt:
            await asyncio.to_thread(self._ckpt.join)
        await asyncio.to_thread(self._readers.shutdown, True)
        with self._read_lock:
            for c in self._read_conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()

    # ---- connections -----------------------------------------------------------------------
    def _uri(self, mode: str) -> str:
        return f"{self.path.resolve().as_uri()}?mode={mode}"

    def _writer_main(self) -> None:
        try:
            conn = sqlite3.connect(self._uri("rwc"), uri=True, timeout=10, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self.writer_pragmas = {
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],  # 1 = NORMAL
            }
            conn.executescript(SCHEMA)
        except BaseException as e:  # noqa: BLE001 - reported to start()
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        stop = False
        while not stop:
            batch = [self._q.get()]
            while len(batch) < 500:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            outcomes = []
            try:
                conn.execute("BEGIN")
                for it in batch:
                    if it is _STOP:
                        stop = True
                        continue
                    sql, params, many, fut, loop = it
                    try:
                        cur = conn.executemany(sql, params) if many else conn.execute(sql, params)
                        outcomes.append((fut, loop, Result(cur.lastrowid, cur.rowcount), None))
                    except Exception as e:  # noqa: BLE001
                        outcomes.append((fut, loop, None, e))
                        if fut is None:
                            log.error("db write failed: %s", e)
                conn.execute("COMMIT")
            except Exception as e:  # noqa: BLE001 - commit failed: fail every pending write
                log.error("db commit failed: %s", e)
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                outcomes = [(f, lp, None, e) for f, lp, _, _ in outcomes]
            for fut, loop, res, err in outcomes:
                if fut is not None:
                    self._resolve(fut, loop, res, err)
        conn.close()

    @staticmethod
    def _resolve(fut, loop, res, err) -> None:
        def _set():
            if fut.done():
                return
            fut.set_exception(err) if err else fut.set_result(res)

        try:
            loop.call_soon_threadsafe(_set)
        except RuntimeError:  # loop already closed
            pass

    def _ckpt_main(self) -> None:
        conn = sqlite3.connect(self._uri("rw"), uri=True, timeout=10, isolation_level=None)
        try:
            while not self._ckpt_stop.wait(self.checkpoint_interval):
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error as e:
                    log.warning("wal checkpoint failed: %s", e)
        finally:
            conn.close()

    def _rconn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self._uri("ro"), uri=True, timeout=5, check_same_thread=False)
            c.row_factory = sqlite3.Row
            with self._read_lock:
                self._read_conns.append(c)
            self._local.c = c
        return c

    # ---- writes (queued, never on the loop) -------------------------------------------------
    async def execute(self, sql: str, params=()) -> Result:
        return await self._submit(sql, params, False)

    async def executemany(self, sql: str, rows) -> Result:
        return await self._submit(sql, list(rows), True)

    def execute_nowait(self, sql: str, params=()) -> None:
        """Fire and forget. Failures are logged by the writer."""
        if self._closed:
            raise DatabaseClosed("database is closed")
        self._q.put((sql, params, False, None, None))

    async def _submit(self, sql, params, many) -> Result:
        if self._closed:
            raise DatabaseClosed("database is closed")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._q.put((sql, params, many, fut, loop))
        return await fut

    # ---- reads (thread pool, read-only connections) -----------------------------------------
    async def fetchall(self, sql: str, params=()) -> list[dict]:
        def run():
            return [dict(r) for r in self._rconn().execute(sql, params).fetchall()]

        return await asyncio.get_running_loop().run_in_executor(self._readers, run)

    async def fetchone(self, sql: str, params=()) -> dict | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    # ---- helpers ----------------------------------------------------------------------------
    async def get_setting(self, key: str, default=None):
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    async def write_heartbeats(self, rows: list[tuple[str, float, str]]) -> None:
        if rows:
            await self.executemany(
                "INSERT INTO heartbeats(name, ts, info) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET ts=excluded.ts, info=excluded.info",
                rows,
            )

    async def retention(self, now: float | None = None, batch: int = 1000,
                        ticks_days: float = 7, prices_days: float = 30) -> dict[str, int]:
        """Delete old rows in small batches through the write queue, yielding between batches."""
        now = time.time() if now is None else now
        deleted: dict[str, int] = {}
        for table, days in (("ticks", ticks_days), ("prices", prices_days)):
            cutoff, total = now - days * 86400, 0
            while True:
                r = await self.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE ts < ? LIMIT ?)",
                    (cutoff, batch),
                )
                total += r.rowcount
                if r.rowcount < batch:
                    break
                await asyncio.sleep(0)
            deleted[table] = total
        return deleted
