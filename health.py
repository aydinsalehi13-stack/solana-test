"""External health check (run by the OS scheduler every 5 minutes).

pid alive? heartbeats fresh (< 120 s)? If not: wait 45 s, ask /api/ping for a second opinion, and only
then restart via run_desk.py. CDP down -> chrome_launcher. One line per run goes to logs/health.log.
Per-bot staleness is handled inside the desk by its watchdog; this script catches a dead or hung process.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

import config
import osutil

HEARTBEAT_MAX_AGE_S = 120.0
RECHECK_WAIT_S = 45.0


def evaluate(pid_alive: bool, newest_heartbeat_age: float | None, max_age: float = HEARTBEAT_MAX_AGE_S) -> str:
    """'ok', 'dead' (no process) or 'stale' (process but no recent heartbeat)."""
    if not pid_alive:
        return "dead"
    if newest_heartbeat_age is None or newest_heartbeat_age > max_age:
        return "stale"
    return "ok"


def newest_heartbeat_age() -> float | None:
    if not config.DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"{config.DB_PATH.resolve().as_uri()}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute("SELECT MAX(ts) FROM heartbeats").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return None if not row or row[0] is None else time.time() - row[0]


def ping() -> bool:
    token = os.environ.get("APP_TOKEN", "")
    if not token:
        return False
    host = config.desk_host()
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    req = urllib.request.Request(f"http://{host}:{config.desk_port()}/api/ping", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return json.load(r).get("ok") is True
    except Exception:  # noqa: BLE001
        return False


def log_line(msg: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOG_DIR / "health.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")


def snapshot() -> tuple[str, int | None, float | None]:
    import run_desk

    pid = run_desk.running_pid()
    age = newest_heartbeat_age()
    return evaluate(pid is not None, age), pid, age


def run_once(wait: float = RECHECK_WAIT_S, restart=None, chrome=None) -> str:
    """Returns the log line it wrote. `restart` / `chrome` are injectable for tests."""
    import chrome_launcher
    import run_desk

    status, pid, age = snapshot()
    action = "none"
    if status != "ok":
        time.sleep(wait)
        status, pid, age = snapshot()
        if status != "ok" and ping() and pid is not None:
            status, action = "ok", "ping-ok"  # slow heartbeat but the API answers: leave it alone
        elif status != "ok":
            action = "restart"
            if restart is not None:
                restart()
            else:
                run_desk.main(["--restart", "--no-chrome"])
    cdp = "skipped"
    if os.environ.get("DESK_NO_CHROME") != "1":
        cdp = "up"
        if chrome_launcher.cdp_alive() is None:
            res = (chrome or chrome_launcher.ensure_chrome)()
            cdp = f"relaunch-{res['status']}"
    line = f"{status} pid={pid} hb_age={'n/a' if age is None else f'{age:.0f}s'} action={action} cdp={cdp}"
    log_line(line)
    return line


def main() -> int:
    config.load_env()
    osutil.say(run_once())
    return 0


if __name__ == "__main__":
    sys.exit(main())
