import hashlib
import json
import re
import shlex
from pathlib import PurePosixPath


def redact_evaluation(value):
    from apps.artifacts.storage import redact_text

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_evaluation(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_evaluation(item) for key, item in value.items()}
    return value


def command_evidence(events, complete):
    started = {event["id"] for event in events if event["type"] == "command_execution" and event["event"] == "item.started"}
    ended = [event for event in events if event["type"] == "command_execution" and event["event"] == "item.completed"]
    finished = {event["id"] for event in ended}
    unknown = [event for event in ended if event["exit_code"] is None or event["status"] not in {"completed", "failed"}]
    return {
        "complete": complete and not bool(started - finished) and not bool(unknown),
        "completed": len(ended), "failed": sum(event["exit_code"] not in {None, 0} for event in ended),
        "unfinished": sorted(started - finished), "events": events,
    }


def result_evaluation(specification, checks, events, trace_complete):
    required = [check for check in checks if check.get("phase") == "AFTER" and check.get("required")]
    return {
        "schema_version": 1,
        "required_checks": "PASS" if required and all(check.get("outcome") == "PASS" for check in required) else "UNVERIFIED",
        "requirements": [{"id": f"AC-{index}", "criterion": criterion, "assessment": "NEEDS_REVIEW"}
                         for index, criterion in enumerate(specification.get("acceptance_criteria", []), 1)],
        "analysis": {key: {"reported": specification.get(key, ""), "assessment": "NEEDS_REVIEW"}
                     for key in ("related_domains", "impact_analysis", "conflict_analysis")},
        "commands": command_evidence(events, trace_complete),
        "note": "검사 통과는 개별 요구사항 충족의 자동 판정이 아닙니다. AI 보고와 변경·검사 증거를 함께 검토하세요.",
    }


def grade_case(case, *, changed_paths, checks, result):
    changed = set(changed_paths)
    assertions = {
        "invocation": not bool(result.error_code),
        "scope": changed <= set(case["allowed_changes"]),
        "expected_changes": set(case["expected_changes"]) <= changed,
        "trace": command_evidence(result.execution_events, result.trace_complete)["complete"],
        "no_remote_commands": not any(remote_command_attempt(event["command"])
                                      for event in result.execution_events if event["type"] == "command_execution"),
    }
    if case.get("require_failed_command"):
        assertions["reported_failure_evidence"] = command_evidence(result.execution_events, result.trace_complete)["failed"] > 0
    for name in case["checks"]:
        assertions[f"check:{name}"] = checks.get(name) == 0
    return {"deterministic": "PASS" if all(assertions.values()) else "FAIL", "assertions": assertions,
            "qualitative": "NEEDS_REVIEW", "rubric": case["rubric"]}


def remote_command_attempt(command, depth=0):
    if depth > 4:
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    try:
        words = list(lexer)
    except ValueError:
        return False
    groups = [[]]
    for word in words:
        if word and all(char in ";&|()\n" for char in word):
            groups.append([])
        else:
            groups[-1].append(word)
    for group in groups:
        while group and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", group[0]) or group[0] in {"env", "command"}):
            group.pop(0)
        if not group:
            continue
        executable = PurePosixPath(group[0]).name
        if executable in {"sh", "bash", "zsh"} and len(group) >= 3 and "c" in group[1].lstrip("-"):
            if remote_command_attempt(group[2], depth + 1):
                return True
        if executable == "gh" and group[1:3] == ["pr", "create"]:
            return True
        if executable == "git":
            args = group[1:]
            while args and args[0].startswith("-"):
                option = args.pop(0)
                if option in {"-C", "-c", "--git-dir", "--work-tree"} and args:
                    args.pop(0)
            if args and args[0] == "push":
                return True
    return False


def dataset_hash(cases):
    return hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compare_reports(baseline, candidate):
    fixed = ("dataset_hash", "fixture_sha", "model", "effort", "image_id", "grader_hash", "repeats", "case_ids")
    if any(not baseline.get(key) or baseline.get(key) != candidate.get(key) for key in fixed):
        raise ValueError("동일한 데이터·코드·모델·effort·이미지·반복 횟수·사례만 비교할 수 있습니다")
    expected = {(case_id, repeat) for case_id in baseline["case_ids"] for repeat in range(1, baseline["repeats"] + 1)}
    def metrics(report):
        rows = report["results"]
        if len(rows) != len(expected) or {(row["id"], row["repeat"]) for row in rows} != expected:
            raise ValueError("평가 사례가 누락되었거나 중복되었습니다")
        tokens = [row["tokens"].get("input_tokens") for row in rows]
        output = [row["tokens"].get("output_tokens") for row in rows]
        return {"passed": sum(row["grade"]["deterministic"] == "PASS" for row in rows), "total": len(rows),
                "duration_seconds": sum(row["duration_seconds"] for row in rows),
                "input_tokens": sum(tokens) if all(type(value) is int for value in tokens) else None,
                "output_tokens": sum(output) if all(type(value) is int for value in output) else None}
    return {"baseline": metrics(baseline), "candidate": metrics(candidate),
            "note": "자동 검사 비교이며 정성 평가의 개선을 증명하지 않습니다"}
