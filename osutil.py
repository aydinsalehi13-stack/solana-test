"""Small cross-platform helpers (Windows / macOS / Linux). Standard library only."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Windows creation flags
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def say(msg: str) -> None:
    """print() that survives pythonw (no stdout)."""
    if sys.stdout is not None:
        print(msg, flush=True)


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie is not alive
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def terminate(pid: int, grace: float = 10.0) -> bool:
    """Ask a process to stop, then force it. Returns True when it is gone."""
    if not pid_alive(pid):
        return True
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.time() + grace
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.2)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(0.2)
    return not pid_alive(pid)


def detached_popen(args, stdout, cwd=None, env=None, no_window: bool = True) -> subprocess.Popen:
    kw: dict = {}
    if IS_WIN:
        kw["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | (_CREATE_NO_WINDOW if no_window else 0)
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(
        [str(a) for a in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=stdout, stderr=subprocess.STDOUT, close_fds=True, **kw,
    )


def find_chrome() -> str | None:
    """Google Chrome for this OS. CHROME_PATH overrides (used for dev boxes)."""
    override = os.environ.get("CHROME_PATH")
    if override and Path(override).exists():
        return override
    cands: list[str] = []
    if IS_WIN:
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                cands.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    elif IS_MAC:
        cands.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        cands.append(str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for c in cands:
        if Path(c).exists():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def process_lines() -> list[tuple[int, str]]:
    """(pid, command line) for every process. Used to find Chrome by its profile directory."""
    out: list[tuple[int, str]] = []
    if IS_WIN:
        ps = ("Get-CimInstance Win32_Process | Where-Object {$_.Name -like 'chrome*'} | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            pid, _, cmd = line.partition("\t")
            if pid.strip().isdigit():
                out.append((int(pid), cmd))
        return out
    # ww = never truncate the command line
    r = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1]))
    return out


def lan_ip() -> str | None:
    """This machine's LAN address, or None when only loopback exists."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET, nothing is sent
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def lock_down(path: Path) -> bool:
    """Make a file readable by this user only (chmod 600 / user-only ACL). Best effort."""
    try:
        if IS_WIN:
            user = os.environ.get("USERNAME", "")
            r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True)
            return r.returncode == 0
        os.chmod(path, 0o600)
        return True
    except OSError:
        return False


def is_private(path: Path) -> bool | None:
    """True/False on POSIX; None where the mode bits mean nothing (Windows)."""
    if IS_WIN:
        return None
    return (path.stat().st_mode & 0o077) == 0
