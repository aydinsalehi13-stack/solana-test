"""Start the desk if it is not running (what the OS scheduler runs).

  python run_desk.py             start if not running
  python run_desk.py --restart   stop the running desk, then start
  python run_desk.py --stop      stop it
  python run_desk.py --no-chrome do not touch Chrome (also DESK_NO_CHROME=1)
  python run_desk.py --watch     start if needed, then stay alive while the desk lives; exit 1 when it is gone.
                                 The OS service (launchd KeepAlive, systemd Restart=always, Task Scheduler restart) runs this,
                                 so "the desk died" becomes "the service exited" and the OS starts it again.
"""
from __future__ import annotations

import os
import sys
import time

import config
import osutil


def read_pid() -> int | None:
    try:
        return int(config.PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def running_pid() -> int | None:
    pid = read_pid()
    return pid if pid and osutil.pid_alive(pid) else None


def stop() -> bool:
    pid = running_pid()
    if pid is None:
        return True
    ok = osutil.terminate(pid)
    if ok:
        try:
            config.PID_FILE.unlink()
        except OSError:
            pass
    return ok


def ensure_dependencies() -> None:
    import setup

    if setup.missing_modules():
        osutil.say("a dependency is missing, running setup")
        if setup.main([]) != 0:
            raise SystemExit("setup failed")


def start(use_chrome: bool = True) -> int:
    config.ensure_dirs()
    pid = running_pid()
    if pid:
        osutil.say(f"desk already running (pid {pid})")
        return 0
    ensure_dependencies()
    if use_chrome:
        import chrome_launcher

        res = chrome_launcher.ensure_chrome()
        osutil.say(f"chrome: {res['status']}" + (f" ({res['error']})" if res.get("error") else ""))
    with open(config.LOG_DIR / "main.out", "ab") as out:
        osutil.detached_popen([sys.executable, str(config.ROOT / "main.py")], out, cwd=config.ROOT)
    for _ in range(100):  # main.py writes its own pid file once it is up
        time.sleep(0.1)
        if running_pid():
            osutil.say(f"desk started (pid {running_pid()})")
            return 0
    osutil.say("desk did not come up, see logs/main.out")
    return 1


def watch(use_chrome: bool = True, poll: float = 5.0, alive=None, sleep=time.sleep, start_fn=None) -> int:
    """Block while the desk process is alive. Returns the start result if it failed, else 1 once the desk is gone (that is what makes the OS restart us)."""
    rc = (start_fn or start)(use_chrome)
    if rc:
        return rc
    alive = alive or running_pid
    while alive():
        sleep(poll)
    osutil.say("the desk is no longer running")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config.load_env()
    if "--stop" in argv:
        return 0 if stop() else 1
    if "--restart" in argv and not stop():
        osutil.say("could not stop the running desk")
        return 1
    use_chrome = "--no-chrome" not in argv and os.environ.get("DESK_NO_CHROME") != "1"
    if "--watch" in argv:
        return watch(use_chrome)
    return start(use_chrome=use_chrome)


if __name__ == "__main__":
    sys.exit(main())
