"""The acceptance suite (spec section 11): one pytest run, results mapped item by item, written to data/acceptance.json.

  python acceptance.py            run the whole suite, print the table, write data/acceptance.json
  python acceptance.py --junit F  do not run pytest, read this junit file (for checking the mapping)

`passed` is true only if: the full run has no failure, and every item below matched at least one test and all its tests passed.
An item with no matching test, or only skipped tests, is "not run" and keeps `passed` false: a missing test is never a pass.
Not covered here and reported as such: the LIVE gate test (needs the owner's "ok") and everything that needs the owner's laptop, Chrome login or iPhone.
This file only reports the suite. It never changes the mode and it never writes live_test.json.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import config
import osutil

# (id, spec section 11 label, test files, optional regex on the test name)
ITEMS: list[tuple[str, str, tuple[str, ...], str | None]] = [
    ("links", "links: every FOMO URL from links.py, no referral", ("test_links",), None),
    ("gate", "gate: score 69 rejects / 70 passes, one red layer rejects, social off = 4-part score", ("test_rules",), r"score|red_layer|social|missing_data|organic|sniped|flush"),
    ("brain", "brain: sizing cases, caps, skip under $2", ("test_rules",), r"brain"),
    ("risk", "risk: stops, take profit, trail, timeout, dip cases, a looser stop never moves a stop down", ("test_rules", "test_pipeline"),
     r"stop|dead_money|pulled|dev_dumping|no_price|looser|order_of_rules|take_profit|dip|risk|lock_time|night_loss"),
    ("browser", "browser (PAPER): 3 coins, type $2, quote, stop before the button; Withdraw/Deposit/Confirm refused", ("test_browser",), None),
    ("verify", "verify: a fake Bought with no API swap is unverified, no second click", ("test_verify",), r"fake_bought|claim_click|recover|decide|checks_are|chain_na|no_data|sell_is_verified|late_swap|swaps_from_raw"),
    ("metrics", "metrics: warnings kill, holders leaving, insiders, creator in rugs, organic penalties, crash", ("test_analyst", "test_scanner"), None),
    ("secrets", "secrets: no secret value in repo, logs, web, API responses or reports; token 200/401; .env permissions", ("test_secrets", "test_phase0", "test_api", "test_live_report"),
     r"secret|env|token|401|permission"),
    ("cdp_lan", "CDP only on loopback; 8787 needs the token from the LAN IP", ("test_process", "test_scripts"), r"cdp|loopback|lan|token|ping"),
    ("pumpportal", "PumpPortal: one socket, backoff sequence, no trade subscription without a key", ("test_feed",), None),
    ("paths", "no hard-coded absolute paths, no shell scripts, no wallet code", ("test_paths", "test_secrets"), r"path|shell|wallet"),
    ("live_gate", "no code path sets LIVE without the gates; orion MODE == PAPER", ("test_api_dash", "test_browser", "test_orion_bot", "test_telegram"), r"mode|live"),
    ("replay_det", "replay is deterministic", ("test_orion_replay",), None),
    ("jupiter", "Jupiter refuses wallet fields", ("test_sources_p4",), r"jupiter|wallet"),
    ("fees", "the fee tiers", ("test_fees",), None),
    ("qa", "QA (Playwright): 1440 + 390, no console errors, live updates, reconnect, login states, no token visible", ("test_dashboard_qa",), None),
    ("ui_audit", "UI audit: nothing cut off at 5 screen sizes with extreme numbers, every control works, the big number starts at $150", ("test_ui_audit",), None),
    ("no_fake", "the product holds nothing made up: no fake rows, no fake-mode switch, no invented numbers", ("test_no_fake",), None),
    ("replay_accept", "replay acceptance: kill Chrome and the desk, both come back, no order runs twice", ("test_verify", "test_process", "test_orion_bot", "test_pipeline"),
     r"kill_the_desk|relaunch|restart|book_survives|survives"),
    ("restart_wake", "24/7: wake closes stale positions, session re-proved, generated service files parse", ("test_phase8",), None),
    ("night", "night check: coverage, loop lag, restart log", ("test_night_check", "test_live_report"), None),
    ("telegram", "Telegram: owner lock, confirmations, batching, screenshot schedule survives a restart", ("test_telegram", "test_live_report"), None),
]
LIVE_TEST = "LIVE gate test (buy $2, verified, sell 100%, verified): not run (needs Aydin's ok)"
NOT_RUN_HERE = [
    "a real PAPER session on the owner's FOMO login (needs the laptop, Chrome and the owner's own login)",
    "FOMO page selectors and request bodies on the real site (unverified until captured on the laptop)",
    "a full PAPER night on the laptop (python night_check.py says whether it counts)",
    "the dashboard on the owner's iPhone over Wi-Fi, and 9222 unreachable from the phone",
    "reboot returns the desk and Chrome unattended (the OS scheduler files are generated and parsed, not installed here)",
    "live PumpPortal, Telegram and RPC traffic (this container has no route to them)",
]


def parse_junit(path: Path) -> list[dict]:
    out = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        mod = (tc.get("classname") or "").split(".")
        file = next((m for m in mod if m.startswith("test_")), mod[-1] if mod else "")
        status = "failed" if tc.find("failure") is not None or tc.find("error") is not None else "skipped" if tc.find("skipped") is not None else "passed"
        out.append({"file": file, "name": tc.get("name") or "", "status": status})
    return out


def evaluate(tests: list[dict], items=ITEMS) -> list[dict]:
    res = []
    for iid, title, files, pat in items:
        rx = re.compile(pat, re.I) if pat else None
        hit = [t for t in tests if t["file"] in files and (rx is None or rx.search(t["name"]))]
        passed, failed = [t for t in hit if t["status"] == "passed"], [t for t in hit if t["status"] == "failed"]
        skipped = [t for t in hit if t["status"] == "skipped"]
        status = "failed" if failed else "passed" if passed and not skipped else "partly skipped" if passed else "not run"
        res.append({"id": iid, "title": title, "status": status, "tests": len(hit), "passed": len(passed), "failed": [t["name"] for t in failed], "skipped": len(skipped)})
    return res


def verdict(items: list[dict], tests: list[dict]) -> bool:
    return all(i["status"] == "passed" for i in items) and not any(t["status"] == "failed" for t in tests)


def render(items: list[dict], passed: bool, tests: list[dict]) -> str:
    out = [f"{'#':>2}  {'item':<14} {'status':<15} tests  spec 11", "-" * 100]
    for n, i in enumerate(items, 1):
        out.append(f"{n:>2}  {i['id']:<14} {i['status']:<15} {i['passed']}/{i['tests']:<4} {i['title']}" + (f"  FAILED: {', '.join(i['failed'][:3])}" if i["failed"] else ""))
    out += ["", f"{len(tests)} tests in the run, {sum(t['status'] == 'passed' for t in tests)} passed, {sum(t['status'] == 'failed' for t in tests)} failed, {sum(t['status'] == 'skipped' for t in tests)} skipped",
            f"ACCEPTANCE SUITE: {'PASSED' if passed else 'NOT PASSED'}", LIVE_TEST, "", "Not run here (needs the owner's laptop or accounts):"]
    out += [f"  - {x}" for x in NOT_RUN_HERE]
    return "\n".join(out)


def commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True, text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run_suite(junit: Path) -> int:
    return subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"], cwd=config.ROOT)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config.ensure_dirs()
    if "--junit" in argv:
        junit = Path(argv[argv.index("--junit") + 1])
    else:
        junit = Path(tempfile.mkdtemp(prefix="acceptance_")) / "junit.xml"
        run_suite(junit)
    if not junit.exists():
        osutil.say("the test run produced no results: acceptance NOT passed")
        return 1
    tests = parse_junit(junit)
    items = evaluate(tests)
    passed = verdict(items, tests)
    (config.DATA_DIR / "acceptance.json").write_text(json.dumps({"passed": passed, "ts": time.time(), "commit": commit(), "items": items, "tests": len(tests),
                                                                   "live_test": "not run (needs Aydin's ok)", "not_run_here": NOT_RUN_HERE}, indent=2), encoding="utf-8")
    osutil.say(render(items, passed, tests))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
