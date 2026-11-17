import re

from conftest import ROOT

# Built from pieces so this file does not match itself.
FORBIDDEN = [re.compile(p) for p in ("/work" + "space", "/ho" + "me/", "/ro" + r"ot\b", "/Us" + "ers/", "/t" + "mp/", "/m" + "nt/", r"[A-Za-z]:\\+Us" + "ers")]


def test_no_hardcoded_absolute_project_paths(code_files):
    bad = []
    for f in code_files:
        if f.name == "test_paths.py":
            continue
        t = f.read_text(errors="ignore")
        for pat in FORBIDDEN:
            if pat.search(t):
                bad.append((str(f.relative_to(ROOT)), pat.pattern))
    assert not bad, bad


def test_no_shell_scripts_shipped(code_files):
    assert not [f for f in code_files if f.suffix in (".sh", ".ps1")]


def test_config_paths_are_inside_project():
    import config

    for p in (config.DATA_DIR, config.LOG_DIR, config.PROFILE_DIR, config.DB_PATH, config.PID_FILE):
        assert ROOT in p.resolve().parents or p.resolve() == ROOT
