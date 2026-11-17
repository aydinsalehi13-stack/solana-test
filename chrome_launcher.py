"""Start the dedicated desk Chrome with DevTools on 127.0.0.1 only (hard rule 6). Idempotent. Standard library only.

Chrome 136+ ignores the debug port on the default profile, so a dedicated profile folder is required.
The owner logs in to FOMO by hand in this window; nothing here reads or stores credentials.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import config
import osutil
from links import SIGNUP_URL

SINGLETONS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def cdp_url(port: int | None = None) -> str:
    return f"http://{config.CDP_HOST}:{port or config.cdp_port()}/json/version"


def cdp_alive(port: int | None = None, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(cdp_url(port), timeout=timeout) as r:  # noqa: S310 - loopback only
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def chrome_args(chrome: str, profile: Path, port: int, headless: bool = False, start_url: str = SIGNUP_URL) -> list[str]:
    args = [
        chrome,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={config.CDP_HOST}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--window-size=1440,900",
    ]
    if headless:  # dev boxes without a display only; the real desk is a visible window
        args.append("--headless=new")
    if not osutil.IS_WIN and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chrome refuses to run as root otherwise
    args.append(start_url)
    return args


def profile_pids(profile: Path) -> list[int]:
    """Only Chrome processes started with exactly this profile directory."""
    needle = f"--user-data-dir={profile}"
    me = os.getpid()
    return [pid for pid, cmd in osutil.process_lines() if pid != me and f"{cmd} ".find(f"{needle} ") != -1]


def kill_profile_chrome(profile: Path) -> int:
    pids = profile_pids(profile)
    for pid in pids:
        osutil.terminate(pid, grace=3.0)
    return len(pids)


def clear_singletons(profile: Path) -> None:
    for name in SINGLETONS:
        p = profile / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except OSError:
            pass


def ensure_chrome(wait: float = 30.0) -> dict:
    """Make sure the desk Chrome answers on the CDP port. Never exposes the port beyond loopback."""
    port = config.cdp_port()
    info = cdp_alive(port)
    if info:
        return {"status": "running", "browser": info.get("Browser")}
    chrome = osutil.find_chrome()
    if not chrome:
        return {"status": "failed", "error": "Google Chrome not found (set CHROME_PATH)"}
    profile = config.PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    killed = kill_profile_chrome(profile)
    clear_singletons(profile)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    headless = os.environ.get("DESK_CHROME_HEADLESS") == "1"
    start_url = os.environ.get("DESK_CHROME_URL") or SIGNUP_URL
    with open(config.LOG_DIR / "chrome.log", "ab") as logf:
        proc = osutil.detached_popen(chrome_args(chrome, profile, port, headless, start_url), logf, no_window=False)
    t0 = time.time()
    while time.time() - t0 < wait:
        info = cdp_alive(port)
        if info:
            return {"status": "started", "pid": proc.pid, "killed_old": killed,
                    "seconds": round(time.time() - t0, 1), "browser": info.get("Browser")}
        if proc.poll() is not None:
            return {"status": "failed", "error": f"Chrome exited with code {proc.returncode} (see logs/chrome.log)"}
        time.sleep(0.4)
    return {"status": "failed", "error": f"CDP did not answer within {wait:.0f}s (see logs/chrome.log)"}


def main() -> int:
    config.load_env()
    res = ensure_chrome()
    osutil.say(f"chrome: {res['status']}" + (f" ({res['error']})" if res.get("error") else ""))
    return 0 if res["status"] in ("running", "started") else 1


if __name__ == "__main__":
    sys.exit(main())
