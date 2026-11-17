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
