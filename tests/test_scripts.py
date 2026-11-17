import os
import subprocess
import sys

import config
import health
import links
import osutil
import setup as desk_setup
from conftest import ROOT


def test_health_evaluate():
    assert health.evaluate(True, 10) == "ok"
    assert health.evaluate(True, 500) == "stale"
    assert health.evaluate(True, None) == "stale"
    assert health.evaluate(False, 10) == "dead"


def test_pid_alive():
    assert osutil.pid_alive(os.getpid())
    assert not osutil.pid_alive(0) and not osutil.pid_alive(-1)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not osutil.pid_alive(p.pid)


def test_terminate_stops_a_process():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert osutil.pid_alive(p.pid)
    assert osutil.terminate(p.pid, grace=3)
    p.wait()
    assert not osutil.pid_alive(p.pid)


def test_chrome_args_loopback_only_and_dedicated_profile(tmp_path):
    import chrome_launcher as cl

    a = cl.chrome_args("chrome", tmp_path / "prof", 9222, start_url=links.SIGNUP_URL)
    assert "--remote-debugging-address=127.0.0.1" in a
    assert "--remote-debugging-port=9222" in a
    assert f"--user-data-dir={tmp_path / 'prof'}" in a
    assert "--window-size=1440,900" in a
    assert "--disable-renderer-backgrounding" in a and "--disable-background-timer-throttling" in a
    assert not any("0.0.0.0" in x for x in a)
    assert config.CDP_HOST == "127.0.0.1"


def test_profile_pids_matches_only_exact_profile(monkeypatch, tmp_path):
    import chrome_launcher as cl

    prof = tmp_path / "chrome-profile"
    monkeypatch.setattr(osutil, "process_lines", lambda: [
        (11, f"/x/chrome --user-data-dir={prof} --remote-debugging-port=9222 about:blank"),
        (12, f"/x/chrome --user-data-dir={prof}2 --foo"),
        (13, f"/x/chrome --user-data-dir={prof}"),
        (14, "/x/chrome --user-data-dir=/somewhere/else"),
    ])
    assert cl.profile_pids(prof) == [11, 13]


def test_clear_singletons(tmp_path):
    import chrome_launcher as cl

    for n in cl.SINGLETONS:
        (tmp_path / n).write_text("x")
    (tmp_path / "Default").mkdir()
    cl.clear_singletons(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["Default"]


def test_setup_creates_locked_env_with_token_and_does_not_print_it(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(desk_setup.config, "ENV_FILE", env)
    msg = desk_setup.ensure_env_file()
    vals = config.parse_env(env.read_text())
    assert len(vals["APP_TOKEN"]) >= 32 and vals["TELEGRAM_BOT_TOKEN"] == ""
    assert vals["APP_TOKEN"] not in msg
    if osutil.is_private(env) is not None:
        assert osutil.is_private(env)
    before = vals["APP_TOKEN"]
    desk_setup.ensure_env_file()
    assert config.parse_env(env.read_text())["APP_TOKEN"] == before  # never rotates silently


def test_health_run_once_restarts_a_dead_desk(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PID_FILE", tmp_path / "desk.pid")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("DESK_NO_CHROME", "1")
    calls = []
    line = health.run_once(wait=0, restart=lambda: calls.append(1))
    assert calls == [1] and line.startswith("dead") and "action=restart" in line
    assert "restart" in (tmp_path / "logs" / "health.log").read_text()


def test_run_desk_pid_helpers(monkeypatch, tmp_path):
    import run_desk

    monkeypatch.setattr(config, "PID_FILE", tmp_path / "p")
    assert run_desk.running_pid() is None
    (tmp_path / "p").write_text(str(os.getpid()))
    assert run_desk.running_pid() == os.getpid()
    (tmp_path / "p").write_text("garbage")
    assert run_desk.read_pid() is None
