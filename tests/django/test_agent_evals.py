import copy
import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command, CommandError

from apps.ai.evaluation import command_evidence, compare_reports, grade_case, redact_evaluation, remote_command_attempt, result_evaluation
from apps.ai.prompts import EXECUTION_BOUNDARY, implementation_prompt
from integrations.codex.adapter import InvocationResult, parse_jsonl


def test_case_set_covers_negative_and_positive_requests():
    cases = json.loads((Path(__file__).parents[1] / "fixtures/agent_evals/cases.json").read_text())
    assert 10 <= len(cases) <= 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert any(not case["allowed_changes"] for case in cases)
    assert any(case["expected_changes"] for case in cases)
    for case in cases:
        assert case["checks"] and case["rubric"]


def test_success_claim_cannot_override_failed_or_missing_grader():
    case = {"allowed_changes": ["app.py"], "expected_changes": ["app.py"], "checks": {"test": ""}, "rubric": "review"}
    result = InvocationResult(final_response="Everything passed", trace_complete=True)
    for checks in ({}, {"test": 1}):
        assert grade_case(case, changed_paths=["app.py"], checks=checks, result=result)["deterministic"] == "FAIL"
    assert grade_case(case, changed_paths=["app.py", "auth.py"], checks={"test": 0}, result=result)["deterministic"] == "FAIL"
    passed = grade_case(case, changed_paths=["app.py"], checks={"test": 0}, result=result)
    assert passed["deterministic"] == "PASS"
    assert passed["qualitative"] == "NEEDS_REVIEW"


def test_no_change_request_rejects_edits_even_when_checks_pass():
    case = {"allowed_changes": [], "expected_changes": [], "checks": {}, "rubric": "review"}
    result = InvocationResult(trace_complete=True)
    assert grade_case(case, changed_paths=["app.py"], checks={}, result=result)["deterministic"] == "FAIL"


def test_started_command_is_not_completed_evidence():
    events = [{"id": "one", "type": "command_execution", "event": "item.started", "exit_code": None, "status": "in_progress", "command": "pytest"}]
    assert not command_evidence(events, True)["complete"]
    events.append({**events[0], "event": "item.completed", "status": "completed", "exit_code": 0})
    assert command_evidence(events, True)["complete"]
    assert not command_evidence(events, False)["complete"]


def test_requirements_remain_unverified_by_generic_tests():
    report = result_evaluation({"acceptance_criteria": ["버튼을 저장으로 변경"]},
                              [{"phase": "AFTER", "required": True, "outcome": "PASS"}], [], False)
    assert report["required_checks"] == "PASS"
    assert report["requirements"][0]["assessment"] == "NEEDS_REVIEW"
    assert report["analysis"]["impact_analysis"]["assessment"] == "NEEDS_REVIEW"


def test_trace_redacts_credentials_and_marks_oversized_commands_incomplete():
    command = "API_KEY=sk-syntheticNotRealKey123456 " + "x" * 5000
    stream = "\n".join(json.dumps(event) for event in [
        {"type": "item.completed", "item": {"id": "cmd", "type": "command_execution", "command": command, "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed"},
    ])
    result = parse_jsonl(stream)
    assert "sk-synthetic" not in str(result.execution_events)
    assert not result.trace_complete
    assert len(result.execution_events[0]["command"]) <= 4096


def test_eval_requires_explicit_live_flag_without_accessing_docker(monkeypatch):
    monkeypatch.setattr("integrations.codex.container.ready", lambda: pytest.fail("Must not access Docker"))
    with pytest.raises(CommandError, match="--live"):
        call_command("eval_agents", stdout=StringIO())


def test_redacted_report_stays_valid_json():
    report = {"requirements": [{"criterion": 'Authorization: "Bearer private"\nother text'}], "count": 2}
    serialized = json.dumps(redact_evaluation(report))
    assert "private" not in serialized
    assert json.loads(serialized)["count"] == 2


def test_prompt_comparison_keeps_execution_boundaries():
    for variant in ("current", "minimal"):
        assert EXECUTION_BOUNDARY in implementation_prompt({"goal": "test"}, variant=variant)


@pytest.mark.parametrize("command,expected", [
    ('rg -n "git push|gh pr create" .', False),
    ('/bin/bash -lc \'git status && rg "git push|gh pr create" .\'', False),
    ('/bin/bash -lc "git -C . push origin main"', True),
    ('git status; gh pr create --title test', True),
    ('echo "git push"', False),
    ('git -c core.hooksPath=/dev/null push', True),
])
def test_remote_command_eval_distinguishes_search_arguments(command, expected):
    assert remote_command_attempt(command) is expected


def test_comparison_requires_same_conditions_and_complete_runs():
    baseline = {"dataset_hash": "one", "fixture_sha": "sha", "model": "model", "effort": "low", "image_id": "image", "grader_hash": "grader",
                "repeats": 1, "case_ids": ["label"], "results": [{"id": "label", "repeat": 1, "tokens": {},
                    "duration_seconds": 1, "grade": {"deterministic": "PASS"}}]}
    assert compare_reports(baseline, baseline)["baseline"]["input_tokens"] is None
    changed = copy.deepcopy(baseline)
    changed["model"] = "another"
    with pytest.raises(ValueError):
        compare_reports(baseline, changed)
    changed = copy.deepcopy(baseline)
    changed["results"] = []
    with pytest.raises(ValueError):
        compare_reports(baseline, changed)
