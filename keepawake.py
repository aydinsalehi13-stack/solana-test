"""Keep the laptop awake while the desk runs (spec §10). Standard library only.

  macOS    `caffeinate -dimsu -w <desk pid>` as a child process: it exits by itself when the desk does.
  Windows  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) from a thread that lives as long as the desk.
  Linux    nothing here: the power settings (masking sleep.target, logind lid switch) do it. See autostart.py.

This only asks the OS not to idle-sleep. It never changes a power setting; those need admin rights and the owner types them.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading

import osutil

log = logging.getLogger("desk.keepawake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def caffeinate_args(pid: int) -> list[str]:
    return ["caffeinate", "-dimsu", "-w", str(pid)]  # -d display, -i idle, -m disk, -s system (on AC), -u user active


class KeepAwake:
    def __init__(self, pid: int | None = None, is_mac: bool | None = None, is_win: bool | None = None, popen=subprocess.Popen, which=shutil.which):
        self.pid = pid or os.getpid()
        self.is_mac = osutil.IS_MAC if is_mac is None else is_mac
        self.is_win = osutil.IS_WIN if is_win is None else is_win
        self._popen, self._which = popen, which
        self.proc = None
        self.method = "none"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        if self.is_mac:
            exe = self._which("caffeinate")
            if exe:
                try:
                    self.proc = self._popen([exe, *caffeinate_args(self.pid)[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.method = "caffeinate"
                except OSError as e:
                    log.warning("caffeinate did not start: %s", e)
        elif self.is_win:
            self._thread = threading.Thread(target=self._win_loop, name="keepawake", daemon=True)
            self._thread.start()
            self.method = "SetThreadExecutionState"
        log.info("keep-awake: %s", self.method)
        return self.method

    def _win_loop(self) -> None:  # the flag belongs to the thread that set it, so this thread sets it and waits
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self._stop.wait()
        k32.SetThreadExecutionState(ES_CONTINUOUS)

    def stop(self) -> None:
        self._stop.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.method = "none"
