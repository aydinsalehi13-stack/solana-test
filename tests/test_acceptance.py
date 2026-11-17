"""The acceptance runner maps spec section 11 onto real tests; a missing or skipped test is never a pass."""
import json
import subprocess
import sys

import acceptance as ac
import config
from conftest import ROOT


def junit(tmp_path, cases):
    body = "".join(f'<testcase classname="tests.{f}" name="{n}">{"<failure/>" if s == "failed" else "<skipped/>" if s == "skipped" else ""}</testcase>' for f, n, s in cases)
    p = tmp_path / "j.xml"
    p.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{body}</testsuite></testsuites>')
    return p


ONE = (("t", "links", ("test_links",), None),)


def test_parse_junit_reads_file_name_and_status(tmp_path):
    p = junit(tmp_path, [("test_links", "test_a", "passed"), ("test_links", "test_b", "failed"), ("test_links", "test_c", "skipped")])
    assert [(t["file"], t["name"], t["status"]) for t in ac.parse_junit(p)] == [("test_links", "test_a", "passed"), ("test_links", "test_b", "failed"), ("test_links", "test_c", "skipped")]


def test_an_item_passes_only_when_it_matched_tests_and_none_failed_or_skipped(tmp_path):
    def status(cases, items=ONE):
        return ac.evaluate(ac.parse_junit(junit(tmp_path, cases)), items)[0]["status"]

    assert status([("test_links", "test_a", "passed")]) == "passed"
    assert status([("test_links", "test_a", "passed"), ("test_links", "test_b", "failed")]) == "failed"
    assert status([("test_links", "test_a", "passed"), ("test_links", "test_b", "skipped")]) == "partly skipped"
    assert status([("test_links", "test_a", "skipped")]) == "not run"
    assert status([("test_other", "test_a", "passed")]) == "not run"  # no matching test: never a pass
    assert status([("test_rules", "test_brain_x", "passed"), ("test_rules", "test_score_x", "passed")], (("b", "brain", ("test_rules",), r"brain"),)) == "passed"


def test_the_verdict_is_false_with_a_failure_anywhere_or_an_item_not_run(tmp_path):
    tests = ac.parse_junit(junit(tmp_path, [("test_links", "test_a", "passed"), ("test_zzz", "test_boom", "failed")]))
    assert ac.verdict(ac.evaluate(tests, ONE), tests) is False  # the failure is in a file no item lists, but the run still failed
    tests = ac.parse_junit(junit(tmp_path, [("test_links", "test_a", "passed")]))
    assert ac.verdict(ac.evaluate(tests, ONE), tests) is True and ac.verdict(ac.evaluate(tests), tests) is False  # the real items are not all covered


def test_main_writes_the_result_file_reports_live_not_run_and_never_writes_live_test(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    code = ac.main(["--junit", str(junit(tmp_path, [("test_links", "test_a", "passed")]))])
    out = capsys.readouterr().out
    data = json.loads((tmp_path / "data" / "acceptance.json").read_text())
    assert code == 1 and data["passed"] is False and data["live_test"] == "not run (needs Aydin's ok)"  # one file of tests is not the whole suite
    assert "ACCEPTANCE SUITE: NOT PASSED" in out and "not run (needs Aydin's ok)" in out and "iPhone" in out
    assert not (tmp_path / "data" / "live_test.json").exists()


def test_every_item_matches_real_tests_so_the_mapping_cannot_go_stale():
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True, timeout=300).stdout
    tests = [{"file": line.split("::")[0].split("/")[-1][:-3], "name": line.split("::")[-1].split("[")[0], "status": "passed"} for line in out.splitlines() if "::" in line]
    assert len(tests) > 400
    empty = [i["id"] for i in ac.evaluate(tests) if i["tests"] == 0]
    assert empty == [], f"acceptance items that match no test: {empty}"
