"""Starts the real desk as a subprocess: /api/ping 200, token needed from the LAN IP, pid file, clean stop."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

import chrome_launcher
import osutil
from conftest import ROOT

TOKEN = "process-test-token-0123456789"


def http(url, token=None, timeout=3):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError):
        return None, None


@pytest.fixture
def desk(tmp_path):
    port = osutil.free_port()
    env = {**os.environ, "DESK_DATA_DIR": str(tmp_path / "data"), "DESK_LOG_DIR": str(tmp_path / "logs"),
           "DESK_PORT": str(port), "DESK_HOST": "0.0.0.0", "APP_TOKEN": TOKEN, "DESK_FEED": "0", "DESK_BOTS": "0"}
    p = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        s, _ = http(f"http://127.0.0.1:{port}/api/ping", TOKEN, 1) if p.poll() is None else (None, None)
        if s == 200:
            break
        time.sleep(0.2)
    else:
        p.kill()
        pytest.fail("desk did not come up: " + (p.stdout.read() if p.stdout else ""))
    yield p, port, tmp_path
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def test_ping_200_and_401(desk):
    p, port, _ = desk
    assert http(f"http://127.0.0.1:{port}/api/ping", TOKEN)[0] == 200
    assert http(f"http://127.0.0.1:{port}/api/ping")[0] == 401


def test_lan_ip_needs_the_token(desk):
    _, port, _ = desk
    ip = osutil.lan_ip()
    if not ip:
        pytest.skip("no LAN address on this machine")
    assert http(f"http://{ip}:{port}/api/state")[0] == 401
    assert http(f"http://{ip}:{port}/api/state", TOKEN)[0] == 200


def test_pid_file_heartbeats_and_clean_stop(desk):
    p, port, tmp = desk
    assert (tmp / "data" / "desk.pid").read_text() == str(p.pid)
    time.sleep(0.5)
    s, h = http(f"http://127.0.0.1:{port}/api/health", TOKEN)
    assert s == 200 and "loop" in h["heartbeat_age_s"] and h["loop_lag_ms"]["n"] > 0
    p.terminate()
    assert p.wait(15) == 0
    assert not (tmp / "data" / "desk.pid").exists()
    import sqlite3
    con = sqlite3.connect(tmp / "data" / "desk.db")
    names = {r[0] for r in con.execute("SELECT name FROM heartbeats")}
    assert {"loop", "heartbeat_writer"} <= names  # final heartbeat flush on shutdown


def test_port_in_use_exits_nonzero(desk, tmp_path):
    _, port, _ = desk
    env = {**os.environ, "DESK_DATA_DIR": str(tmp_path / "d2"), "DESK_LOG_DIR": str(tmp_path / "l2"),
           "DESK_PORT": str(port), "APP_TOKEN": TOKEN, "DESK_FEED": "0", "DESK_BOTS": "0"}
    r = subprocess.run([sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=env, capture_output=True, timeout=30)
    assert r.returncode != 0


CHROME = osutil.find_chrome() or next(iter(sorted(__import__("glob").glob("/opt/pw-browsers/chromium-*/chrome-linux64/chrome"))), None)


@pytest.mark.skipif(not CHROME, reason="no Chrome on this machine")
def test_cdp_listens_on_loopback_only_and_relaunch_is_fast(tmp_path, monkeypatch):
    port = osutil.free_port()
    monkeypatch.setenv("CHROME_PATH", CHROME)
    monkeypatch.setenv("DESK_CDP_PORT", str(port))
    monkeypatch.setenv("DESK_CHROME_HEADLESS", "1")
    monkeypatch.setenv("DESK_CHROME_URL", "about:blank")
    import config
    monkeypatch.setattr(config, "PROFILE_DIR", tmp_path / "chrome-profile")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    try:
        r = chrome_launcher.ensure_chrome(wait=40)
        assert r["status"] == "started", r
        assert chrome_launcher.cdp_alive(port)
        ip = osutil.lan_ip()
        if ip:
            import socket
            s = socket.socket(); s.settimeout(2)
            assert s.connect_ex((ip, port)) != 0, "CDP must not be reachable on the LAN address"
            s.close()
        assert chrome_launcher.ensure_chrome()["status"] == "running"  # idempotent
        # kill Chrome, launcher brings it back in < 60 s
        _pids = chrome_launcher.profile_pids(config.PROFILE_DIR)
        for pid in _pids:
            osutil.terminate(pid, grace=2)
        for _ in range(30):
            if chrome_launcher.cdp_alive(port) is None:
                break
            time.sleep(0.2)
        assert chrome_launcher.cdp_alive(port) is None
        t = time.time()
        r2 = chrome_launcher.ensure_chrome(wait=40)
        assert r2["status"] == "started" and time.time() - t < 60
    finally:
        chrome_launcher.kill_profile_chrome(config.PROFILE_DIR)
