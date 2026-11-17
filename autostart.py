"""Generate the 24/7 files and the exact copy-paste commands for this laptop (spec §10). Standard library only.

  python autostart.py                 print the guide for this OS (nothing is written, nothing is run)
  python autostart.py --os macos      the same for another OS (windows | macos | linux)
  python autostart.py --write         also write the generated files into deploy/<os>/

This script never runs an admin command and never installs anything. You copy the lines yourself.
The generated files hold paths only: no token, no key, no password. Secrets stay in .env, which the desk reads itself.

All three OS run `run_desk.py --watch`: it starts the desk (and Chrome) if needed and exits when the desk is gone,
so the OS service manager (Task Scheduler, launchd, systemd) restarts it. `health.py` runs every 5 minutes on its own timer.
"""
from __future__ import annotations

import argparse
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import config
import osutil

OSES = ("windows", "macos", "linux")
LABEL_DESK = "local.fomo-desk"
LABEL_HEALTH = "local.fomo-desk-health"
WIN_TASK_DESK = "FOMO desk start"
WIN_TASK_HEALTH = "FOMO desk health"
HEALTH_EVERY_S = 300


@dataclass(frozen=True)
class Ctx:
    root: str  # project root, as the target OS writes it
    python: str  # interpreter (the venv one when there is one)
    home: str = ""
    user: str = ""
    sep: str = "/"

    def p(self, *parts: str) -> str:
        return self.sep.join([self.root.rstrip("/\\"), *parts])

    @property
    def pythonw(self) -> str:  # no console window on Windows
        return self.python[:-10] + "pythonw.exe" if self.python.lower().endswith("python.exe") else self.python


def current_os() -> str:
    return "windows" if osutil.IS_WIN else "macos" if osutil.IS_MAC else "linux"


def default_ctx(target: str) -> Ctx:
    import getpass
    import os

    root, py = str(config.ROOT), sys.executable
    return Ctx(root=root, python=py, home=str(Path.home()), user=getpass.getuser() if target != "windows" else os.environ.get("USERNAME", getpass.getuser()),
               sep="\\" if target == "windows" else "/")


# ===================================== Windows: Task Scheduler XML =====================================================================================
def _win_task(ctx: Ctx, command: str, script: str, args: str, trigger: str, restart: bool, description: str) -> str:
    restart_xml = "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>" if restart else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    {restart_xml}
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(command)}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(ctx.root)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def windows_files(ctx: Ctx) -> dict[str, str]:
    logon = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"  # at log on of the owner: Chrome needs the owner's desktop
    every5 = f"<TimeTrigger><Repetition><Interval>PT{HEALTH_EVERY_S // 60}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>2000-01-01T00:00:00</StartBoundary><Enabled>true</Enabled></TimeTrigger>"
    return {
        "desk_start.xml": _win_task(ctx, ctx.pythonw, "run_desk.py", f'"{ctx.p("run_desk.py")}" --watch', logon, True, "Starts the FOMO desk when you log on; restarts it every minute if it stops."),
        "desk_health.xml": _win_task(ctx, ctx.pythonw, "health.py", f'"{ctx.p("health.py")}"', every5, False, "Health check of the FOMO desk every 5 minutes."),
    }


def windows_steps(ctx: Ctx, d: str) -> list[tuple[str, str, bool]]:  # (what, line, needs admin)
    return [
        ("Open PowerShell as Administrator (Start, type PowerShell, right-click, Run as administrator) for the next 4 lines", "", True),
        ("never sleep on mains", "powercfg /change standby-timeout-ac 0", True),
        ("never hibernate on mains", "powercfg /change hibernate-timeout-ac 0", True),
        ("closing the lid does nothing on mains", "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0", True),
        ("apply the plan", "powercfg /setactive SCHEME_CURRENT", True),
        ("Back in a normal PowerShell: register the start task (runs at your log on)", f'schtasks /Create /TN "{WIN_TASK_DESK}" /XML "{d}\\desk_start.xml" /F', False),
        ("register the 5-minute health task", f'schtasks /Create /TN "{WIN_TASK_HEALTH}" /XML "{d}\\desk_health.xml" /F', False),
        ("start the desk now without rebooting", f'schtasks /Run /TN "{WIN_TASK_DESK}"', False),
        ("Windows Update active hours: Settings > Windows Update > Advanced options > Active hours, set a long window so it does not restart overnight (by hand)", "", False),
        ("Optional, your choice: auto-login after a reboot with netplwiz (the desk needs your desktop). Skip it if the laptop is not private.", "", False),
    ]


# ===================================== macOS: LaunchAgents =============================================================================================
def macos_files(ctx: Ctx) -> dict[str, str]:
    env = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    desk = {"Label": LABEL_DESK, "ProgramArguments": [ctx.python, ctx.p("run_desk.py"), "--watch"], "WorkingDirectory": ctx.root, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 10, "AbandonProcessGroup": True, "EnvironmentVariables": env,
            "StandardOutPath": ctx.p("logs", "launchd_desk.out"), "StandardErrorPath": ctx.p("logs", "launchd_desk.out")}
    health = {"Label": LABEL_HEALTH, "ProgramArguments": [ctx.python, ctx.p("health.py")], "WorkingDirectory": ctx.root, "StartInterval": HEALTH_EVERY_S, "RunAtLoad": False,
              "EnvironmentVariables": env, "StandardOutPath": ctx.p("logs", "launchd_health.out"), "StandardErrorPath": ctx.p("logs", "launchd_health.out")}
    return {f"{LABEL_DESK}.plist": plistlib.dumps(desk).decode(), f"{LABEL_HEALTH}.plist": plistlib.dumps(health).decode()}


def macos_steps(ctx: Ctx, d: str) -> list[tuple[str, str, bool]]:
    la = "~/Library/LaunchAgents"
    return [
        ("never sleep on mains (asks for your Mac password in the Terminal; type it there, never in chat)", "sudo pmset -c sleep 0 disksleep 0 powernap 0 standby 0 autorestart 1", True),
        ("Optional, MacBook only: keep running with the lid closed", "sudo pmset -a disablesleep 1", True),
        ("make the LaunchAgents folder", f"mkdir -p {la}", False),
        ("copy the two agents in", f'cp "{d}/{LABEL_DESK}.plist" "{d}/{LABEL_HEALTH}.plist" {la}/', False),
        ("load the desk agent (starts now and at every log in)", f"launchctl bootstrap gui/$(id -u) {la}/{LABEL_DESK}.plist", False),
        ("load the health agent (every 5 minutes)", f"launchctl bootstrap gui/$(id -u) {la}/{LABEL_HEALTH}.plist", False),
        ("the desk keeps `caffeinate -dimsu` as a child, so idle sleep is also blocked while it runs", "", False),
    ]


# ===================================== Linux: systemd user units =======================================================================================
def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def linux_files(ctx: Ctx) -> dict[str, str]:
    return {
        "fomo-desk.service": f"""[Unit]
Description=FOMO desk (paper trading desk)
After=network-online.target

[Service]
Type=simple
WorkingDirectory={_q(ctx.root)}
Environment=DISPLAY=:0
ExecStart={_q(ctx.python)} {_q(ctx.p("run_desk.py"))} --watch
Restart=always
RestartSec=10
KillMode=process

[Install]
WantedBy=default.target
""",
        "fomo-health.service": f"""[Unit]
Description=FOMO desk health check

[Service]
Type=oneshot
WorkingDirectory={_q(ctx.root)}
Environment=DISPLAY=:0
ExecStart={_q(ctx.python)} {_q(ctx.p("health.py"))}
""",
        "fomo-health.timer": f"""[Unit]
Description=FOMO desk health check every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec={HEALTH_EVERY_S}s
Unit=fomo-health.service

[Install]
WantedBy=timers.target
""",
        "logind-fomo-desk.conf": "[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\n",
    }


def linux_steps(ctx: Ctx, d: str) -> list[tuple[str, str, bool]]:
    ud = "~/.config/systemd/user"
    return [
        ("never suspend or hibernate", "sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target", True),
        ("lid switch does nothing: make the folder", "sudo mkdir -p /etc/systemd/logind.conf.d", True),
        ("lid switch does nothing: install the setting", f'sudo cp "{d}/logind-fomo-desk.conf" /etc/systemd/logind.conf.d/fomo-desk.conf', True),
        ("tell logind to reload (no logout)", "sudo systemctl kill -s HUP systemd-logind", True),
        ("keep your user services running when you are logged out", "sudo loginctl enable-linger $USER", True),
        ("make the user unit folder", f"mkdir -p {ud}", False),
        ("copy the units in", f'cp "{d}/fomo-desk.service" "{d}/fomo-health.service" "{d}/fomo-health.timer" {ud}/', False),
        ("reload systemd", "systemctl --user daemon-reload", False),
        ("start the desk now and at every boot, plus the 5-minute health timer", "systemctl --user enable --now fomo-desk.service fomo-health.timer", False),
    ]


GENERATORS = {"windows": (windows_files, windows_steps), "macos": (macos_files, macos_steps), "linux": (linux_files, linux_steps)}

ALL_OS_NOTES = [
    "Keep the laptop plugged in (a desk that runs on battery stops when it dies).",
    "Lid open, or lid-close set to do nothing (the Windows / macOS / Linux lines above).",
    "Ethernet if you can: Wi-Fi sleeps and drops more often.",
    "Battery charge limit 80% if your laptop supports it (a vendor setting, not a command here).",
    "After every step, `python health.py` prints one line and adds it to logs/health.log; it should say ok.",
]


def files_for(target: str, ctx: Ctx | None = None) -> dict[str, str]:
    return GENERATORS[target][0](ctx or default_ctx(target))


def guide(target: str, ctx: Ctx | None = None, deploy_dir: str | None = None) -> str:
    ctx = ctx or default_ctx(target)
    d = deploy_dir or ctx.p("deploy", target)
    out = [f"24/7 setup for {target}. Do the numbered lines in order. Lines marked [admin] need your OS password or admin rights: you type them, this tool never does.", ""]
    n = 0
    for what, line, admin in GENERATORS[target][1](ctx, d):
        if line:
            n += 1
            out.append(f"{n}. {what}{' [admin]' if admin else ''}\n     {line}")
        else:
            out.append(f"   - {what}")
    out += ["", "Every OS:"] + [f"   - {x}" for x in ALL_OS_NOTES]
    out += ["", "After a reboot: the desk and Chrome come back by themselves. Log in to FOMO in that Chrome once (by hand) if it asks."]
    return "\n".join(out)


def write_files(target: str, ctx: Ctx | None = None, out_dir: Path | None = None) -> list[Path]:
    out_dir = Path(out_dir) if out_dir else config.ROOT / "deploy" / target
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files_for(target, ctx).items():
        p = out_dir / name
        p.write_text(text, encoding="utf-16" if name.endswith(".xml") else "utf-8", newline="\n")  # Task Scheduler wants UTF-16 XML
        written.append(p)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--os", choices=OSES, default=current_os())
    ap.add_argument("--write", action="store_true", help="write the generated files into deploy/<os>/")
    a = ap.parse_args(argv)
    ctx = default_ctx(a.os)
    if a.write:
        for p in write_files(a.os, ctx):
            osutil.say(f"wrote {p.relative_to(config.ROOT)}")
    osutil.say(guide(a.os, ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
