"""Phase 8: 24/7 generators (parsed, not eyeballed), keep-awake, wake handling, health notice, run_desk --watch."""
import asyncio
import configparser
import json
import plistlib
import re
import sqlite3
import types
import xml.etree.ElementTree as ET

import pytest

import autostart as au
import config
import health
import keepawake
import run_desk
from bots.orion import LONG_SLEEP_S, STRATEGY
from botkit import T0
from bus import Hub
from conftest import ROOT
from main import Supervisor
from test_orion_bot import MINT, go_live, make

SECRETS = {"APP_TOKEN": "tok-VALUE-1234567890", "TELEGRAM_BOT_TOKEN": "123456:SECRETVALUE-abcdef", "HELIUS_API_KEY": "helius-VALUE-xyz"}

# Other-OS home folders are built from pieces so the no-absolute-paths guard (test_paths) keeps covering this file.
UM, UL, UW = "/Us" + "ers/aydin", "/ho" + "me/aydin", "C:\\Us" + "ers\\aydin"
MAC = au.Ctx(root=f"{UM}/My Desk", python=f"{UM}/My Desk/.venv/bin/python", home=UM, user="aydin")
LIN = au.Ctx(root=f"{UL}/desk dir", python=f"{UL}/desk dir/.venv/bin/python", home=UL, user="aydin")
WIN = au.Ctx(root=f"{UW}\\Memecoin bot", python=f"{UW}\\Memecoin bot\\.venv\\Scripts\\python.exe", home=UW, user="aydin", sep="\\")


# ---- macOS ------------------------------------------------------------------------------------------------------------------
def test_macos_plists_parse_and_keep_the_desk_alive():
    files = au.macos_files(MAC)
    desk = plistlib.loads(files[f"{au.LABEL_DESK}.plist"].encode())
    health_ = plistlib.loads(files[f"{au.LABEL_HEALTH}.plist"].encode())
    assert desk["Label"] == au.LABEL_DESK and desk["RunAtLoad"] is True and desk["KeepAlive"] is True
    assert desk["ProgramArguments"] == [MAC.python, f"{UM}/My Desk/run_desk.py", "--watch"]  # a path with a space stays one argument
    assert desk["WorkingDirectory"] == MAC.root and desk["AbandonProcessGroup"] is True  # the desk outlives its launcher
    assert health_["StartInterval"] == 300 and health_["RunAtLoad"] is False and health_["ProgramArguments"][1].endswith("health.py")


def test_macos_guide_has_the_spec_pmset_line_and_bootstraps_both_agents():
    g = au.guide("macos", MAC, f"{UM}/My Desk/deploy/macos")
    assert "sudo pmset -c sleep 0 disksleep 0 powernap 0 standby 0 autorestart 1" in g and "sudo pmset -a disablesleep 1" in g
    assert g.count("launchctl bootstrap gui/$(id -u)") == 2 and "caffeinate -dimsu" in g


# ---- Linux ------------------------------------------------------------------------------------------------------------------
def unit(text):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string(text)
    return cp


def test_systemd_units_parse():
    f = au.linux_files(LIN)
    svc = unit(f["fomo-desk.service"])
    assert svc["Service"]["Restart"] == "always" and svc["Service"]["RestartSec"] == "10" and svc["Service"]["Environment"] == "DISPLAY=:0"
    assert svc["Service"]["ExecStart"] == f'"{UL}/desk dir/.venv/bin/python" "{UL}/desk dir/run_desk.py" --watch'
    assert svc["Service"]["WorkingDirectory"] == f'"{UL}/desk dir"' and svc["Install"]["WantedBy"] == "default.target"
    assert unit(f["fomo-health.service"])["Service"]["Type"] == "oneshot"
    timer = unit(f["fomo-health.timer"])
    assert timer["Timer"]["OnUnitActiveSec"] == "300s" and timer["Timer"]["Unit"] == "fomo-health.service" and timer["Install"]["WantedBy"] == "timers.target"
    lid = unit(f["logind-fomo-desk.conf"])["Login"]
    assert lid["HandleLidSwitch"] == "ignore" and lid["HandleLidSwitchExternalPower"] == "ignore"


def test_linux_guide_masks_sleep_and_enables_linger():
    g = au.guide("linux", LIN, f"{UL}/desk dir/deploy/linux")
    assert "sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target" in g
    assert "loginctl enable-linger $USER" in g and "systemctl --user enable --now fomo-desk.service fomo-health.timer" in g


# ---- Windows ----------------------------------------------------------------------------------------------------------------
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_windows_task_xml_parses_with_logon_trigger_and_one_minute_restart():
    f = au.windows_files(WIN)
    start = ET.fromstring(f["desk_start.xml"].encode("utf-16"))
    assert start.find("t:Triggers/t:LogonTrigger", NS) is not None  # at log on of the owner, not a SYSTEM service
    assert start.find("t:Principals/t:Principal/t:LogonType", NS).text == "InteractiveToken"
    assert start.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"
    assert start.find("t:Settings/t:RestartOnFailure/t:Interval", NS).text == "PT1M"
    ex = start.find("t:Actions/t:Exec", NS)
    assert ex.find("t:Command", NS).text.endswith("pythonw.exe") and ex.find("t:Arguments", NS).text == f'"{UW}\\Memecoin bot\\run_desk.py" --watch'
    assert ex.find("t:WorkingDirectory", NS).text == WIN.root
    hl = ET.fromstring(f["desk_health.xml"].encode("utf-16"))
    assert hl.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT5M" and hl.find("t:Settings/t:RestartOnFailure", NS) is None


def test_windows_guide_has_the_spec_powercfg_lines_and_two_tasks():
    g = au.guide("windows", WIN, f"{UW}\\Memecoin bot\\deploy\\windows")
    for line in ("powercfg /change standby-timeout-ac 0", "powercfg /change hibernate-timeout-ac 0",
                 "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0", "powercfg /setactive SCHEME_CURRENT"):
        assert line in g
    assert 'schtasks /Create /TN "FOMO desk start"' in g and 'schtasks /Create /TN "FOMO desk health"' in g and "netplwiz" in g and "Active hours" in g


# ---- all OS: what the generators must never do ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("target,ctx", [("macos", MAC), ("linux", LIN), ("windows", WIN)])
def test_generated_files_hold_no_secret_and_admin_lines_are_labelled(target, ctx, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    text = "\n".join(au.files_for(target, ctx).values()) + au.guide(target, ctx)
    assert not [v for v in SECRETS.values() if v in text] and "APP_TOKEN" not in text and ".env" not in "\n".join(au.files_for(target, ctx).values())
    steps = au.GENERATORS[target][1](ctx, "d")  # every sudo / powercfg line is an admin step; user-level lines are not
    assert all(admin for what, cmd, admin in steps if cmd.startswith(("sudo", "powercfg")))
    assert not any(admin for what, cmd, admin in steps if cmd.startswith(("schtasks", "launchctl", "systemctl --user")))


def test_the_generator_never_runs_anything():
    src = (ROOT / "autostart.py").read_text()
    assert not re.search(r"\bsubprocess\b|os\.system|os\.popen|\bPopen\b", src)


def test_write_files_makes_readable_files_in_the_right_encoding(tmp_path):
    out = au.write_files("windows", WIN, tmp_path / "w")
    assert {p.name for p in out} == {"desk_start.xml", "desk_health.xml"}
    assert ET.fromstring((tmp_path / "w" / "desk_start.xml").read_bytes()).tag.endswith("Task")  # UTF-16 with a BOM, as Task Scheduler wants
    mac = au.write_files("macos", MAC, tmp_path / "m")
    assert plistlib.loads(mac[0].read_bytes())["Label"].startswith("local.fomo-desk")


def test_cli_prints_the_guide_for_the_chosen_os(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert au.main(["--os", "linux", "--write"]) == 0
    out = capsys.readouterr().out
    assert "24/7 setup for linux" in out and (tmp_path / "deploy" / "linux" / "fomo-desk.service").exists()
    assert (ROOT / ".gitignore").read_text().count("deploy/") >= 1  # generated, holds this machine's paths: not committed


# ---- keep-awake -------------------------------------------------------------------------------------------------------------
class FakeProc:
    def __init__(self, args, **kw):
        self.args, self.terminated = args, False

    def terminate(self):
        self.terminated = True


def test_macos_keeps_caffeinate_as_a_child_tied_to_the_desk_pid():
    procs = []
    ka = keepawake.KeepAwake(pid=4242, is_mac=True, is_win=False, popen=lambda a, **k: procs.append(FakeProc(a)) or procs[-1], which=lambda n: f"/usr/bin/{n}")
    assert ka.start() == "caffeinate" and procs[0].args == ["/usr/bin/caffeinate", "-dimsu", "-w", "4242"]
    ka.stop()
    assert procs[0].terminated and ka.method == "none"


def test_no_caffeinate_or_linux_means_honestly_no_keep_awake():
    assert keepawake.KeepAwake(is_mac=True, is_win=False, which=lambda n: None).start() == "none"
    assert keepawake.KeepAwake(is_mac=False, is_win=False).start() == "none"


def test_windows_asks_for_system_required_from_its_own_thread(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(SetThreadExecutionState=lambda f: calls.append(f))))
    import sys

    monkeypatch.setitem(sys.modules, "ctypes", fake)
    ka = keepawake.KeepAwake(is_mac=False, is_win=True)
    assert ka.start() == "SetThreadExecutionState"
    for _ in range(100):
        if calls:
            break
        asyncio.run(asyncio.sleep(0.02))
    ka.stop()
    assert calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED, keepawake.ES_CONTINUOUS]  # asked, then released on stop


# ---- run_desk --watch ---------------------------------------------------------------------------------------------------------
def test_watch_returns_1_when_the_desk_is_gone_so_the_os_restarts_the_service():
    seq = iter([4242, 4242, None])
    sleeps = []
    rc = run_desk.watch(use_chrome=False, alive=lambda: next(seq), sleep=sleeps.append, start_fn=lambda chrome: 0)
    assert rc == 1 and len(sleeps) == 2


def test_watch_passes_a_failed_start_straight_back():
    assert run_desk.watch(use_chrome=False, start_fn=lambda chrome: 1, alive=lambda: pytest.fail("must not wait")) == 1


# ---- wake handling ----------------------------------------------------------------------------------------------------------------
def test_the_supervisor_tells_everyone_how_long_the_sleep_was():
    hub = Hub()
    sup = Supervisor(hub)
    sup.on_wake(3600.0)
    assert hub.bus.recent(topic="wake")[-1]["data"] == {"gap_s": 3600.0}


async def test_a_long_sleep_closes_orion_positions_at_the_last_real_price(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    await go_live(d, o)
    await o.step(T0 + 120)
    assert MINT in o.engine.positions
    d.hub.bus.publish("wake", {"gap_s": 4 * 3600.0})
    await o.step(T0 + 4 * 3600 + 120)
    assert not o.engine.positions
    await d.db.execute("SELECT 1")
    row = await d.db.fetchone("SELECT closed_at, exit_rule, exit_price FROM strat_positions")
    assert row["closed_at"] is not None and row["exit_rule"] == "desk restart · marked at last real price" and row["exit_price"] > 0
    assert o.engine.trades[-1]["exit_rule"] == "desk restart · marked at last real price"
    await d.db.stop()


async def test_a_short_freeze_leaves_positions_alone_and_an_unknown_gap_counts_as_long(tmp_path):
    d, o, fb, feed = await make(tmp_path)
    await go_live(d, o)
    await o.step(T0 + 120)
    d.hub.bus.publish("wake", {"gap_s": LONG_SLEEP_S - 1})
    await o.step(T0 + 130)
    assert MINT in o.engine.positions
    d.hub.bus.publish("wake", {"gap_s": None})
    await o.step(T0 + 140)
    assert not o.engine.positions
    await d.db.stop()


async def test_the_session_is_proved_again_after_a_wake_without_any_click(tmp_path):
    from desk import PromptDesk

    calls = []

    class Lane:
        def hold(self, bot):
            calls.append(("lane", bot))
            return _Ctx()

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class B:
        lane = Lane()

        async def ensure_ready(self, fresh=True):
            calls.append(("ensure_ready", fresh))
            return {"avatar": True, "cash": "$150"}

        def __getattr__(self, name):
            pytest.fail(f"a wake re-proof must not use {name}")

    hub = Hub()
    me = types.SimpleNamespace(d=types.SimpleNamespace(browser=B(), hub=hub, clock=lambda: 5.0))
    res = await PromptDesk.reprove_session(me)
    assert res == {"ts": 5.0, "ok": True, "error": None} and calls == [("lane", "wake"), ("ensure_ready", True)]
    assert hub.status["session_proof"]()["ok"] is True
    me.d.browser = None
    assert (await PromptDesk.reprove_session(me))["ok"] is False  # no Chrome: says so, never a pass


# ---- health: Telegram notice ----------------------------------------------------------------------------------------------------
def test_health_tells_telegram_only_when_it_restarts_and_logs_it(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PID_FILE", tmp_path / "desk.pid")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("DESK_NO_CHROME", "1")
    told, restarted = [], []
    line = health.run_once(wait=0, restart=lambda: restarted.append(1), notify=lambda status, pid: told.append((status, pid)) or True)
    assert restarted == [1] and told == [("dead", None)] and "action=restart+telegram" in line


def test_notify_restart_needs_a_token_and_an_owner_and_never_logs_the_token(monkeypatch, tmp_path):
    db = tmp_path / "d.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert health.notify_restart("dead", None, urlopen=lambda *a, **k: pytest.fail("no token: no call")) is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRETS["TELEGRAM_BOT_TOKEN"])
    assert health.notify_restart("dead", None, urlopen=lambda *a, **k: pytest.fail("no owner chat yet")) is False
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at REAL)")
    con.execute("INSERT INTO settings VALUES ('telegram_owner_chat_id', '777', 0)")
    con.commit()
    con.close()
    sent = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake(req, timeout=0):
        sent.append((req.full_url, json.loads(req.data)))
        return R()

    assert health.notify_restart("stale", 99, urlopen=fake) is True
    url, body = sent[0]
    assert body["chat_id"] == 777 and body["text"].startswith("PAPER \u00b7") and "stale" in body["text"] and SECRETS["TELEGRAM_BOT_TOKEN"] not in body["text"]
    assert url.startswith("https://api.telegram.org/bot")
