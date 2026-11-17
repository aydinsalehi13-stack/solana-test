import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _section0(text):
    m = re.search(r"## 0\. HARD RULES.*?(?=\n## 1\. )", text, re.S)
    assert m, "section 0 not found"
    return m.group(0).strip()


def test_claude_md_has_section0_verbatim():
    spec = _section0((ROOT / "docs/SPEC.md").read_text())
    claude = (ROOT / "CLAUDE.md").read_text()
    assert spec in claude
    assert len(re.findall(r"^\d+\. \*\*", spec, re.M)) == 13


def test_env_example_has_no_values():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, val = line.split("=", 1)
            if key.endswith(("KEY", "TOKEN", "URL")):
                assert val == "", f"{key} must be empty in .env.example"


def test_env_is_gitignored():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    for needed in (".env", "logs/", "*.log", "chrome-profile/"):
        assert needed in ignore


def test_no_env_file_committed():
    assert not (ROOT / ".env").exists() or ".env" in (ROOT / ".gitignore").read_text()
