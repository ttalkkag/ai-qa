import hashlib
import io
import math
import os
import re
import stat
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path, PurePosixPath


MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 10 * 1024 * 1024
MAX_XML_ELEMENTS = 100_000
MAX_XML_DEPTH = 64
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SUMMARY = re.compile(
    r"^=*(?P<body>.*?)\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^\r\n]*\))?\s*=*$"
)
_COUNT = re.compile(
    r"(?P<count>\d+) (?P<kind>passed|failed|skipped|xfailed|xpassed|deselected|warnings?|errors?)"
)


class _InvalidEvidence(ValueError):
    pass


def _counts(passed, failed, skipped):
    return {
        "collected_count": passed + failed + skipped,
        "passed_count": passed,
        "failed_count": failed,
        "skipped_count": skipped,
    }


def _parse_pytest_line(line):
    match = _SUMMARY.fullmatch(line.strip())
    if not match:
        return None
    body = match.group("body").strip("= ")
    if body == "no tests ran":
        return _counts(0, 0, 0)
    values = {}
    for segment in body.split(", "):
        item = _COUNT.fullmatch(segment)
        if not item:
            return None
        kind = item.group("kind")
        if kind in values or len(item.group("count")) > 9:
            return None
        values[kind] = int(item.group("count"))
    if not values:
        return None
    return _counts(
        values.get("passed", 0) + values.get("xpassed", 0),
        values.get("failed", 0) + values.get("error", 0) + values.get("errors", 0),
        values.get("skipped", 0) + values.get("xfailed", 0),
    )


def _pytest_counts(stdout):
    lines = _ANSI.sub("", stdout).strip().splitlines()
    if not lines:
        raise _InvalidEvidence("SUMMARY_MISSING")
    result = _parse_pytest_line(lines[-1])
    if result is None:
        raise _InvalidEvidence("SUMMARY_INVALID")
    return result


def _started_ns(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise _InvalidEvidence("REPORT_START_INVALID")
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidEvidence("REPORT_START_REQUIRED")
    if not math.isfinite(value) or value < 0:
        raise _InvalidEvidence("REPORT_START_INVALID")
    return int(value * 1_000_000_000)


def _read_report(path, registered_path, started_at):
    if not isinstance(registered_path, str) or not registered_path:
        raise _InvalidEvidence("REPORT_PATH_REQUIRED")
    relative = PurePosixPath(registered_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in registered_path:
        raise _InvalidEvidence("REPORT_PATH_INVALID")
    if not relative.parts:
        raise _InvalidEvidence("REPORT_PATH_INVALID")
    if path is None:
        raise _InvalidEvidence("REPORT_MISSING")
    try:
        absolute = Path(os.path.abspath(path))
    except (TypeError, ValueError) as exc:
        raise _InvalidEvidence("REPORT_PATH_INVALID") from exc
    if absolute.parts[-len(relative.parts):] != relative.parts:
        raise _InvalidEvidence("REPORT_PATH_MISMATCH")
    started_ns = _started_ns(started_at)
    directory = None
    descriptor = None
    try:
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for part in absolute.parts[1:-1]:
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REPORT_BYTES:
            raise _InvalidEvidence("REPORT_FILE_INVALID")
        if min(before.st_mtime_ns, before.st_ctime_ns) < started_ns:
            raise _InvalidEvidence("REPORT_STALE")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_REPORT_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_REPORT_BYTES:
                raise _InvalidEvidence("REPORT_TOO_LARGE")
        after = os.stat(absolute.name, dir_fd=directory, follow_symlinks=False)
        identity = lambda entry: (
            entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns
        )
        if identity(before) != identity(after):
            raise _InvalidEvidence("REPORT_CHANGED")
        return b"".join(chunks)
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, _InvalidEvidence):
            raise
        raise _InvalidEvidence("REPORT_UNREADABLE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _junit_counts(payload):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidEvidence("REPORT_ENCODING_INVALID") from exc
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise _InvalidEvidence("REPORT_ENTITY_FORBIDDEN")
    depth = 0
    count = 0
    root = None
    try:
        for event, element in ET.iterparse(io.StringIO(text), events=("start", "end")):
            if event == "start":
                if root is None:
                    root = element
                depth += 1
                count += 1
                if depth > MAX_XML_DEPTH or count > MAX_XML_ELEMENTS:
                    raise _InvalidEvidence("REPORT_COMPLEXITY_LIMIT")
            else:
                depth -= 1
    except ET.ParseError as exc:
        raise _InvalidEvidence("REPORT_XML_INVALID") from exc
    if root is None or _tag(root) not in {"testsuite", "testsuites"}:
        raise _InvalidEvidence("REPORT_ROOT_INVALID")
    for parent in root.iter():
        for child in parent:
            if _tag(child) == "testcase" and _tag(parent) != "testsuite":
                raise _InvalidEvidence("REPORT_STRUCTURE_INVALID")
            if _tag(child) in {"testsuite", "testsuites"} and _tag(parent) not in {"testsuite", "testsuites"}:
                raise _InvalidEvidence("REPORT_STRUCTURE_INVALID")
            if _tag(child) in {"failure", "error", "skipped"} and _tag(parent) != "testcase":
                raise _InvalidEvidence("REPORT_STRUCTURE_INVALID")
    cases = [element for element in root.iter() if _tag(element) == "testcase"]
    outcomes = {}
    for case in cases:
        markers = {_tag(child) for child in case}
        if case.get("status", "run") not in {"run", "notrun", "skipped", "disabled"}:
            raise _InvalidEvidence("REPORT_STATUS_UNSUPPORTED")
        if case.get("result", "completed") not in {"completed", "suppressed", "skipped"}:
            raise _InvalidEvidence("REPORT_STATUS_UNSUPPORTED")
        if (case.get("status") in {"notrun", "skipped", "disabled"}
                or case.get("result") in {"suppressed", "skipped"}):
            markers.add("skipped")
        if "skipped" in markers and markers & {"failure", "error"}:
            raise _InvalidEvidence("REPORT_COUNTS_CONFLICT")
        outcomes[id(case)] = (
            bool(markers & {"failure", "error"}),
            "skipped" in markers,
            "failure" in markers,
            "error" in markers,
        )
    for suite in root.iter():
        if _tag(suite) not in {"testsuite", "testsuites"}:
            continue
        suite_cases = [item for item in suite.iter() if _tag(item) == "testcase"]
        expected = {
            "tests": len(suite_cases),
            "failures": sum(outcomes[id(item)][2] for item in suite_cases),
            "errors": sum(outcomes[id(item)][3] for item in suite_cases),
            "skipped": sum(outcomes[id(item)][1] for item in suite_cases),
        }
        for name, value in expected.items():
            declared = suite.get(name)
            if declared is not None and (len(declared) > 9 or not declared.isascii() or not declared.isdecimal()
                                         or int(declared) != value):
                raise _InvalidEvidence("REPORT_COUNTS_CONFLICT")
    failed = sum(outcome[0] for outcome in outcomes.values())
    skipped = sum(outcome[1] for outcome in outcomes.values())
    return _counts(len(cases) - failed - skipped, failed, skipped)


def assess_check(command, exit_code, stdout, *, output_complete=True, timed_out=False,
                 report_path=None, report_started_at=None):
    parser_id = command.get("parser_id")
    parser_version = command.get("parser_version", 1 if parser_id else None)
    if parser_id in {"pytest-summary-v1", "junit-v1"}:
        parser_id = parser_id[:-3]
    result = {
        "outcome": "UNVERIFIED", "reason_code": "EVIDENCE_INVALID",
        "exit_code": exit_code, "output_complete": output_complete,
        "parser_id": parser_id, "parser_version": parser_version,
        "collected_count": None, "passed_count": None,
        "failed_count": None, "skipped_count": None,
        "report_path": command.get("report_path"), "report_sha256": None,
    }

    def finish(outcome, reason):
        result.update(outcome=outcome, reason_code=reason)
        return result

    if timed_out:
        return finish("TIMEOUT", "DEADLINE_EXCEEDED")
    if exit_code is None:
        return finish("NOT_RUN", "COMMAND_NOT_EXECUTED")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return finish("UNVERIFIED", "EXIT_CODE_INVALID")
    if output_complete is not True or not isinstance(stdout, str):
        return finish("UNVERIFIED", "OUTPUT_INCOMPLETE")
    if len(stdout) > MAX_OUTPUT_CHARS:
        return finish("UNVERIFIED", "OUTPUT_TOO_LARGE")
    if command.get("kind") == "COMMAND":
        if parser_id:
            return finish("UNVERIFIED", "COMMAND_PARSER_NOT_ALLOWED")
        return finish("PASS", "COMMAND_SUCCEEDED") if exit_code == 0 else finish("FAIL", "COMMAND_FAILED")
    if command.get("kind") != "TEST":
        return finish("UNVERIFIED", "CHECK_KIND_UNSUPPORTED")
    if type(parser_version) is not int or parser_version != 1:
        return finish("UNVERIFIED", "PARSER_UNSUPPORTED")
    try:
        if parser_id == "pytest-summary":
            counts = _pytest_counts(stdout)
        elif parser_id in {"junit", "junit-xml"}:
            payload = _read_report(report_path, command.get("report_path"), report_started_at)
            counts = _junit_counts(payload)
            result["report_sha256"] = hashlib.sha256(payload).hexdigest()
            summaries = [
                summary for line in _ANSI.sub("", stdout).splitlines()
                if (summary := _parse_pytest_line(line)) is not None
            ]
            if any(summary != counts for summary in summaries):
                raise _InvalidEvidence("STDOUT_REPORT_CONFLICT")
        else:
            return finish("UNVERIFIED", "PARSER_UNSUPPORTED")
        result.update(counts)
    except _InvalidEvidence as exc:
        return finish("UNVERIFIED", str(exc))
    if result["collected_count"] == 0 or result["collected_count"] == result["skipped_count"]:
        return finish("UNVERIFIED", "NO_EXECUTED_TESTS")
    if exit_code != 0 or result["failed_count"]:
        return finish("FAIL", "TESTS_FAILED")
    return finish("PASS", "TESTS_PASSED")
