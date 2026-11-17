import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODE_SUFFIXES = {".py", ".js", ".html", ".css", ".json", ".ini", ".toml", ".txt", ".sh", ".ps1"}


def repo_files(suffixes=None):
    """Tracked and untracked-but-not-ignored files."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    files = [ROOT / p for p in out if (ROOT / p).is_file()]
    return [f for f in files if suffixes is None or f.suffix in suffixes]


@pytest.fixture
def code_files():
    return repo_files(CODE_SUFFIXES)


# ---- shared real-Chrome fixture (headless, loopback CDP on a free port) -----------------------------------
import glob
import os
import shutil
import tempfile

import osutil


def _chrome_path():
    return osutil.find_chrome() or next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux64/chrome"))), None)


@pytest.fixture(scope="session")
def chrome_port():
    chrome = _chrome_path()
    if not chrome:
        pytest.skip("no Chrome on this machine")
    import chrome_launcher
    import config

    from pathlib import Path

    tmp = tempfile.mkdtemp(prefix="deskchrome-")
    port = osutil.free_port()
    saved = {k: os.environ.get(k) for k in ("CHROME_PATH", "DESK_CDP_PORT", "DESK_CHROME_HEADLESS", "DESK_CHROME_URL")}
    old_profile, old_log = config.PROFILE_DIR, config.LOG_DIR
    profile = Path(tmp) / "profile"

    def with_tmp_paths(fn):
        """Point config at the temp dirs only for the duration of fn, so other tests see the real ones."""
        config.PROFILE_DIR, config.LOG_DIR = profile, Path(tmp) / "logs"
        try:
            return fn()
        finally:
            config.PROFILE_DIR, config.LOG_DIR = old_profile, old_log

    os.environ.update(CHROME_PATH=chrome, DESK_CDP_PORT=str(port), DESK_CHROME_HEADLESS="1", DESK_CHROME_URL="about:blank")
    res = with_tmp_paths(lambda: chrome_launcher.ensure_chrome(wait=40))
    assert res["status"] == "started", res
    yield port
    with_tmp_paths(lambda: chrome_launcher.kill_profile_chrome(profile))
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    shutil.rmtree(tmp, ignore_errors=True)
