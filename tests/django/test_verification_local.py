import hashlib
import os
import time
from datetime import datetime, timezone

import pytest

from apps.runs.verification import MAX_REPORT_BYTES, assess_check


PYTEST = {"kind": "TEST", "parser_id": "pytest-summary", "parser_version": 1}
JUNIT = {
    "kind": "TEST", "parser_id": "junit-xml", "parser_version": 1,
    "report_path": ".aiqa-output/unit/junit.xml",
}


@pytest.mark.parametrize("exit_code,complete,timeout,outcome", [
    (0, True, False, "PASS"), (1, True, False, "FAIL"),
    (0, False, False, "UNVERIFIED"), (1, False, False, "UNVERIFIED"),
    (0, True, True, "TIMEOUT"), (None, True, False, "NOT_RUN"),
    (None, True, True, "TIMEOUT"), (True, True, False, "UNVERIFIED"),
])
def test_command_requires_complete_actual_execution(exit_code, complete, timeout, outcome):
    result = assess_check({"kind": "COMMAND"}, exit_code, "lint output",
                          output_complete=complete, timed_out=timeout)
    assert result["outcome"] == outcome
    assert result["collected_count"] is None


@pytest.mark.parametrize("stdout,exit_code,outcome,counts", [
    ("2 passed in 0.01s", 0, "PASS", (2, 2, 0, 0)),
    ("==== 2 passed, 1 skipped, 1 warning in 0.01s ====", 0, "PASS", (3, 2, 0, 1)),
    ("1 failed, 2 passed in 0.03s", 1, "FAIL", (3, 2, 1, 0)),
    ("1 failed, 2 passed in 0.03s", 0, "FAIL", (3, 2, 1, 0)),
    ("2 passed in 0.01s", 1, "FAIL", (2, 2, 0, 0)),
    ("no tests ran in 0.01s", 5, "UNVERIFIED", (0, 0, 0, 0)),
    ("2 skipped in 0.01s", 0, "UNVERIFIED", (2, 0, 0, 2)),
    ("1 xfailed in 0.01s", 0, "UNVERIFIED", (1, 0, 0, 1)),
    ("1 xpassed, 2 deselected in 0.01s", 0, "PASS", (1, 1, 0, 0)),
    ("1 error in 0.01s", 1, "FAIL", (1, 0, 1, 0)),
    ("2 passed, 1 error in 0.01s", 1, "FAIL", (3, 2, 1, 0)),
    ("\x1b[32m2 passed\x1b[0m in 0.01s", 0, "PASS", (2, 2, 0, 0)),
    ("=== 2 passed in 61.23s (0:01:01) ===", 0, "PASS", (2, 2, 0, 0)),
])
def test_pytest_summary(stdout, exit_code, outcome, counts):
    result = assess_check(PYTEST, exit_code, stdout)
    assert result["outcome"] == outcome
    assert tuple(result[key] for key in (
        "collected_count", "passed_count", "failed_count", "skipped_count"
    )) == counts


@pytest.mark.parametrize("stdout", [
    "", "All tests passed", "201 passed", "1 passed, 2 passed in 0.01s",
    "2 passed in 0.01s\nincomplete trailing output", "2 imaginary in 0.01s",
    "9" * 5_000 + " passed in 0.1s",
])
def test_missing_or_incomplete_summary_is_not_success(stdout):
    assert assess_check(PYTEST, 0, stdout)["outcome"] == "UNVERIFIED"


@pytest.mark.parametrize("configuration", [
    {"kind": "TEST", "parser_id": "unknown", "parser_version": 1},
    {**PYTEST, "parser_version": 2}, {**PYTEST, "parser_version": True},
    {"kind": "COMMAND", "parser_id": "pytest-summary"}, {"kind": "OTHER"},
])
def test_unsupported_configuration_never_passes(configuration):
    result = assess_check(configuration, 0, "1 passed in 0.01s")
    assert result["outcome"] == "UNVERIFIED"
    assert result["collected_count"] is None


def write_report(tmp_path, payload):
    path = tmp_path.resolve() / JUNIT["report_path"]
    path.parent.mkdir(parents=True)
    started = time.time() - 1
    path.write_bytes(payload.encode() if isinstance(payload, str) else payload)
    return path, started


@pytest.mark.parametrize("parser_id,recorded_parser_id", [
    ("junit-xml", "junit-xml"), ("junit", "junit"), ("junit-v1", "junit"),
])
def test_junit_records_counts_and_exact_report_digest(tmp_path, parser_id, recorded_parser_id):
    payload = ('<testsuites tests="3" failures="0" errors="0" skipped="1">'
               '<testsuite tests="3" skipped="1"><testcase name="a"/>'
               '<testcase name="b"/><testcase name="c"><skipped/></testcase>'
               '</testsuite></testsuites>')
    path, started = write_report(tmp_path, payload)
    result = assess_check({**JUNIT, "parser_id": parser_id}, 0, "2 passed, 1 skipped in 0.1s",
                          report_path=path, report_started_at=started)
    assert result["outcome"] == "PASS"
    assert result["parser_id"] == recorded_parser_id
    assert result["parser_version"] == 1
    assert result["collected_count"] == 3
    assert result["passed_count"] == 2
    assert result["skipped_count"] == 1
    assert result["report_sha256"] == hashlib.sha256(payload.encode()).hexdigest()


@pytest.mark.parametrize("payload,outcome,reason", [
    ('<testsuite><testcase><failure/></testcase></testsuite>', "FAIL", "TESTS_FAILED"),
    ('<testsuite><testcase><error/></testcase></testsuite>', "FAIL", "TESTS_FAILED"),
    ('<testsuite/>', "UNVERIFIED", "NO_EXECUTED_TESTS"),
    ('<testsuite><testcase><skipped/></testcase></testsuite>', "UNVERIFIED", "NO_EXECUTED_TESTS"),
    ('<testsuite tests="2"><testcase/></testsuite>', "UNVERIFIED", "REPORT_COUNTS_CONFLICT"),
    ('<testsuite tests="-1"/>', "UNVERIFIED", "REPORT_COUNTS_CONFLICT"),
    ('<testsuite tests="' + '9' * 5_000 + '"/>', "UNVERIFIED", "REPORT_COUNTS_CONFLICT"),
    ('<testsuite><testcase><failure/><skipped/></testcase></testsuite>', "UNVERIFIED", "REPORT_COUNTS_CONFLICT"),
    ('<testsuite><properties><testcase/></properties></testsuite>', "UNVERIFIED", "REPORT_STRUCTURE_INVALID"),
    ('<testsuite><testcase status="notrun"/></testsuite>', "UNVERIFIED", "NO_EXECUTED_TESTS"),
    ('<testsuite><testcase result="suppressed"/></testsuite>', "UNVERIFIED", "NO_EXECUTED_TESTS"),
    ('<testsuite><testcase status="unknown"/></testsuite>', "UNVERIFIED", "REPORT_STATUS_UNSUPPORTED"),
    ('<testsuite>', "UNVERIFIED", "REPORT_XML_INVALID"),
    ('<something/>', "UNVERIFIED", "REPORT_ROOT_INVALID"),
    ('<!DOCTYPE x [<!ENTITY x SYSTEM "file:///fake-secret">]><testsuite/>',
     "UNVERIFIED", "REPORT_ENTITY_FORBIDDEN"),
    (b'\xff', "UNVERIFIED", "REPORT_ENCODING_INVALID"),
    ('<testsuite>\x00</testsuite>', "UNVERIFIED", "REPORT_ENTITY_FORBIDDEN"),
    ('<testsuite>' + '<a>' * 65 + '</a>' * 65 + '</testsuite>',
     "UNVERIFIED", "REPORT_COMPLEXITY_LIMIT"),
])
def test_junit_rejects_untrustworthy_evidence(tmp_path, payload, outcome, reason):
    path, started = write_report(tmp_path, payload)
    result = assess_check(JUNIT, 0, "", report_path=path, report_started_at=started)
    assert result["outcome"] == outcome
    assert result["reason_code"] == reason


def test_junit_stdout_must_agree_with_report(tmp_path):
    path, started = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    result = assess_check(JUNIT, 0, "2 passed in 0.1s", report_path=path, report_started_at=started)
    assert result["outcome"] == "UNVERIFIED"
    assert result["reason_code"] == "STDOUT_REPORT_CONFLICT"


def test_old_report_is_not_reused(tmp_path):
    path, _ = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    os.utime(path, (1, 1))
    result = assess_check(JUNIT, 0, "", report_path=path, report_started_at=time.time())
    assert result["reason_code"] == "REPORT_STALE"


def test_symlink_report_is_rejected(tmp_path):
    path, started = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    original = path.with_suffix(".original")
    path.rename(original)
    path.symlink_to(original)
    result = assess_check(JUNIT, 0, "", report_path=path, report_started_at=started)
    assert result["reason_code"] == "REPORT_UNREADABLE"


def test_symlink_parent_is_rejected(tmp_path):
    path, started = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    actual = path.parent.with_name("actual")
    path.parent.rename(actual)
    path.parent.symlink_to(actual, target_is_directory=True)
    result = assess_check(JUNIT, 0, "", report_path=path, report_started_at=started)
    assert result["reason_code"] == "REPORT_UNREADABLE"


def test_oversized_report_is_bounded(tmp_path):
    path, started = write_report(tmp_path, b" " * (MAX_REPORT_BYTES + 1))
    result = assess_check(JUNIT, 0, "", report_path=path, report_started_at=started)
    assert result["reason_code"] == "REPORT_FILE_INVALID"


@pytest.mark.parametrize("started", [None, True, float("nan"), datetime(2020, 1, 1)])
def test_report_requires_trusted_start_time(tmp_path, started):
    path, _ = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    assert assess_check(JUNIT, 0, "", report_path=path,
                        report_started_at=started)["outcome"] == "UNVERIFIED"


def test_report_supports_timezone_aware_start_time(tmp_path):
    path, started = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    assert assess_check(JUNIT, 0, "", report_path=path,
                        report_started_at=datetime.fromtimestamp(started, timezone.utc))["outcome"] == "PASS"


def test_report_path_must_match_registered_location(tmp_path):
    path, started = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    configuration = {**JUNIT, "report_path": "other.xml"}
    result = assess_check(configuration, 0, "", report_path=path, report_started_at=started)
    assert result["reason_code"] == "REPORT_PATH_MISMATCH"


@pytest.mark.parametrize("registered", ["../junit.xml", "/junit.xml", "a\\junit.xml", ".", ""])
def test_invalid_registered_report_paths_are_rejected(tmp_path, registered):
    path, started = write_report(tmp_path, '<testsuite><testcase/></testsuite>')
    result = assess_check({**JUNIT, "report_path": registered}, 0, "",
                          report_path=path, report_started_at=started)
    assert result["outcome"] == "UNVERIFIED"


def test_missing_report_never_passes(tmp_path):
    path = tmp_path.resolve() / JUNIT["report_path"]
    assert assess_check(JUNIT, 0, "", report_path=path,
                        report_started_at=time.time())["outcome"] == "UNVERIFIED"


def test_parser_v1_alias():
    assert assess_check({**PYTEST, "parser_id": "pytest-summary-v1"}, 0,
                        "1 passed in 0.01s")["outcome"] == "PASS"
