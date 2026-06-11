from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DEVAUTO_HOME", tempfile.mkdtemp(prefix="devauto-test-home-"))

try:
    from fastapi.testclient import TestClient

    from devauto.api.main import create_app
except (ModuleNotFoundError, RuntimeError):
    TestClient = None
    create_app = None

from devauto.core.config import load_settings
from devauto.core.models import RunStatus
from devauto.runner.subprocesses import CommandResult


@unittest.skipUnless(TestClient and create_app, "FastAPI test dependencies are not installed")
class ApiTest(unittest.TestCase):
    def test_api_run_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post(
                "/api/runs",
                json={
                    "project_id": "fixture",
                    "title": "API lifecycle",
                    "description": "Exercise API background preparation.",
                },
            )
            self.assertEqual(run_response.status_code, 200)
            created_run = run_response.json()["run"]
            run_id = created_run["id"]
            session_id = created_run["session_id"]
            self.assertTrue(session_id.startswith("session-"))

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["session_id"], session_id)
            self.assertEqual(detail["run"]["status"], "AWAITING_PLAN_APPROVAL")
            self.assertIn("01-plan.md", {artifact["name"] for artifact in detail["artifacts"]})
            plan_page = client.get(f"/runs/{run_id}")
            self.assertEqual(plan_page.status_code, 200)
            self.assertIn("판단 컨텍스트", plan_page.text)
            self.assertIn("02-plan-review.md", plan_page.text)
            self.assertIn("01-plan.md", plan_page.text)
            self.assertIn("# Plan Doc", plan_page.text)
            self.assertIn("전체 산출물 열기", plan_page.text)

            approve_response = client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": ""})
            self.assertEqual(approve_response.status_code, 200)

            completed = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(completed["run"]["session_id"], session_id)
            self.assertEqual(completed["run"]["status"], "PUBLISHED")
            self.assertIn("02-execution-grant.json", {artifact["name"] for artifact in completed["artifacts"]})
            self.assertIn("10-final.patch", {artifact["name"] for artifact in completed["artifacts"]})
            self.assertEqual(completed["gates"][-1]["exit_code"], 0)
            self.assertEqual(completed["approvals"][0]["approval_type"], "plan")
            self.assertEqual(completed["approvals"][0]["decision"], "approved")
            run_page = client.get(f"/runs/{run_id}")
            self.assertEqual(run_page.status_code, 200)
            self.assertIn("승인 이력", run_page.text)
            self.assertIn(session_id, run_page.text)
            self.assertIn("approved", run_page.text)
            self.assertIn("Exercise API background preparation.", run_page.text)
            self.assertIn("변경 등급", run_page.text)
            self.assertIn("재시도</dt><dd>0/4", run_page.text)
            self.assertIn("판단 컨텍스트", run_page.text)
            self.assertIn("11-final-report.md", run_page.text)
            self.assertIn("10-final.patch", run_page.text)
            self.assertIn('id="run-status"', run_page.text)
            self.assertIn('id="artifact-count"', run_page.text)
            self.assertIn('id="latest-artifact"', run_page.text)
            self.assertIn("new EventSource", run_page.text)
            self.assertIn(f"/api/runs/{run_id}/events", run_page.text)
            self.assertIn(f"/runs/{run_id}/artifacts", run_page.text)
            artifacts_page = client.get(f"/runs/{run_id}/artifacts")
            self.assertEqual(artifacts_page.status_code, 200)
            self.assertIn("01-plan.md", artifacts_page.text)
            self.assertIn("10-final.patch", artifacts_page.text)
            self.assertIn(f"/api/runs/{run_id}/artifacts/10-final.patch", artifacts_page.text)
            events = client.get(f"/api/runs/{run_id}/events")
            self.assertEqual(events.status_code, 200)
            event_line = next(line for line in events.text.splitlines() if line.startswith("data: "))
            event_payload = json.loads(event_line.removeprefix("data: "))
            self.assertEqual(event_payload["id"], run_id)
            self.assertEqual(event_payload["session_id"], session_id)
            self.assertEqual(event_payload["status"], "PUBLISHED")
            self.assertEqual(event_payload["artifact_count"], len(completed["artifacts"]))
            self.assertEqual(event_payload["gate_count"], len(completed["gates"]))
            self.assertEqual(event_payload["approval_count"], len(completed["approvals"]))
            self.assertEqual(event_payload["latest_approval"], "plan")
            self.assertTrue(event_payload["latest_artifact"])

    def test_api_background_prepare_exception_marks_run_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            with patch("devauto.runner.pipeline.collect_context", side_effect=RuntimeError("context exploded")):
                run_response = client.post(
                    "/api/runs",
                    json={
                        "project_id": "fixture",
                        "title": "API prepare failure",
                        "description": "Force a background task exception.",
                    },
                )

            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]
            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "FAILED")
            artifacts = {artifact["name"] for artifact in detail["artifacts"]}
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            report = client.get(f"/api/runs/{run_id}/artifacts/11-final-report.md").text
            self.assertIn("예상하지 못한 준비 실패: RuntimeError: context exploded", report)

    def test_api_does_not_serve_artifacts_outside_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Artifact guard"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]
            approve_response = client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": ""})
            self.assertEqual(approve_response.status_code, 200)

            outside = root / "outside-report.md"
            outside.write_text("do not expose\n", encoding="utf-8")
            outside_workspace = root / "outside-workspace"
            outside_workspace.mkdir()
            app.state.db.add_artifact(run_id, "report", "11-final-report.md", outside)
            app.state.db.update_run(run_id, workspace_path=outside_workspace)

            response = client.get(f"/api/runs/{run_id}/artifacts/11-final-report.md")
            self.assertEqual(response.status_code, 404)
            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertNotIn(str(outside), json.dumps(detail))
            self.assertNotIn(str(outside_workspace), json.dumps(detail))
            self.assertEqual(detail["run"]["workspace_path"], "사용 불가")
            self.assertIn(
                {"name": "11-final-report.md", "path": "사용 불가"},
                [{"name": artifact["name"], "path": artifact["path"]} for artifact in detail["artifacts"]],
            )
            run_page = client.get(f"/runs/{run_id}")
            self.assertEqual(run_page.status_code, 200)
            self.assertNotIn("do not expose", run_page.text)
            self.assertNotIn(str(outside_workspace), run_page.text)
            self.assertIn("<dt>작업공간</dt><dd>사용 불가</dd>", run_page.text)
            artifacts_page = client.get(f"/runs/{run_id}/artifacts")
            self.assertNotIn(str(outside_workspace), artifacts_page.text)
            self.assertIn("<dt>작업공간</dt><dd>사용 불가</dd>", artifacts_page.text)

    def test_api_rejects_out_of_state_approval_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Approval guard"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]
            self.assertEqual(client.get(f"/api/runs/{run_id}").json()["run"]["status"], "AWAITING_PLAN_APPROVAL")

            approve_response = client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": ""})
            self.assertEqual(approve_response.status_code, 200)
            completed = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(completed["run"]["status"], "PUBLISHED")

            second_approve = client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": "again"})
            self.assertEqual(second_approve.status_code, 409)
            reject_plan = client.post(f"/api/runs/{run_id}/reject-plan", json={"comment": "again"})
            self.assertEqual(reject_plan.status_code, 409)

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "PUBLISHED")
            grant_count = sum(1 for artifact in detail["artifacts"] if artifact["name"] == "02-execution-grant.json")
            self.assertEqual(grant_count, 1)

    def test_api_run_fails_when_project_doctor_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(root / "missing-repo"),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Invalid repo"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "ESCALATED")
            artifacts = {artifact["name"] for artifact in detail["artifacts"]}
            self.assertIn("00-doctor.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)

    def test_api_reject_plan_replans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Needs replan"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]
            self.assertEqual(client.get(f"/api/runs/{run_id}").json()["run"]["status"], "AWAITING_PLAN_APPROVAL")

            reject = client.post(
                f"/api/runs/{run_id}/reject-plan",
                json={"comment": "Cover the empty input case before execution."},
            )
            self.assertEqual(reject.status_code, 200)

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "AWAITING_PLAN_APPROVAL")
            artifacts = {artifact["name"] for artifact in detail["artifacts"]}
            self.assertIn("02-plan-rejection.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            self.assertEqual(detail["approvals"][0]["approval_type"], "plan")
            self.assertEqual(detail["approvals"][0]["decision"], "rejected")
            self.assertEqual(detail["approvals"][0]["comment"], "Cover the empty input case before execution.")

            plan = client.get(f"/api/runs/{run_id}/artifacts/01-plan.md").text
            self.assertIn("Cover the empty input case before execution.", plan)

    def test_html_plan_rejection_form_preserves_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)
            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "HTML replan"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]

            page = client.get(f"/runs/{run_id}")
            self.assertEqual(page.status_code, 200)
            self.assertIn('name="comment"', page.text)

            reject = client.post(
                f"/runs/{run_id}/reject-plan",
                data={"comment": "Explain this edge case before execution."},
                follow_redirects=False,
            )
            self.assertEqual(reject.status_code, 303)

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "AWAITING_PLAN_APPROVAL")
            self.assertEqual(detail["approvals"][0]["approval_type"], "plan")
            self.assertEqual(detail["approvals"][0]["decision"], "rejected")
            self.assertEqual(detail["approvals"][0]["comment"], "Explain this edge case before execution.")
            plan = client.get(f"/api/runs/{run_id}/artifacts/01-plan.md").text
            self.assertIn("Explain this edge case before execution.", plan)

    def test_html_run_form_accepts_target_branch_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            self._run(["git", "checkout", "-b", "feature/test-branch"], repo)
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            runs_page = client.get("/runs")
            self.assertEqual(runs_page.status_code, 200)
            self.assertIn('name="target_branch"', runs_page.text)
            self.assertIn('name="mode"', runs_page.text)

            create = client.post(
                "/runs",
                data={
                    "project_id": "fixture",
                    "title": "HTML branch run",
                    "description": "Use a non-default branch.",
                    "target_branch": "feature/test-branch",
                    "mode": "human-reviewed",
                },
                follow_redirects=False,
            )
            self.assertEqual(create.status_code, 303)
            run_id = create.headers["location"].split("/runs/", 1)[1]

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["target_branch"], "feature/test-branch")
            self.assertEqual(detail["run"]["mode"], "human-reviewed")
            self.assertEqual(detail["run"]["status"], "AWAITING_PLAN_APPROVAL")
            run_page = client.get(f"/runs/{run_id}")
            self.assertIn("<dt>대상 브랜치</dt><dd>feature/test-branch</dd>", run_page.text)
            self.assertIn("<dt>모드</dt><dd>human-reviewed</dd>", run_page.text)

    def test_html_run_form_rejects_unknown_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            response = client.post(
                "/runs",
                data={
                    "project_id": "missing",
                    "title": "Missing project",
                    "description": "",
                    "target_branch": "",
                    "mode": "",
                },
            )

            self.assertEqual(response.status_code, 404)
            self.assertIn("프로젝트를 찾을 수 없습니다: missing", response.text)
            self.assertEqual(client.get("/api/runs").json()["runs"], [])

    def test_run_html_and_events_return_404_for_unknown_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(load_settings(Path(tmp) / "devauto"))
            client = TestClient(app)

            run_page = client.get("/runs/missing-run")
            artifacts_page = client.get("/runs/missing-run/artifacts")
            events = client.get("/api/runs/missing-run/events")

            self.assertEqual(run_page.status_code, 404)
            self.assertEqual(artifacts_page.status_code, 404)
            self.assertEqual(events.status_code, 404)
            self.assertIn("실행을 찾을 수 없습니다: missing-run", run_page.text)

    def test_run_html_actions_return_404_for_unknown_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(load_settings(Path(tmp) / "devauto"))
            client = TestClient(app)

            paths = [
                "/runs/missing-run/approve-plan",
                "/runs/missing-run/reject-plan",
                "/runs/missing-run/approve-qa-preview",
                "/runs/missing-run/reject-qa-preview",
                "/runs/missing-run/approve-publish",
                "/runs/missing-run/reject-publish",
                "/runs/missing-run/cancel",
            ]

            for path in paths:
                with self.subTest(path=path):
                    response = client.post(path, data={"comment": ""}, follow_redirects=False)
                    self.assertEqual(response.status_code, 404)
                    self.assertIn("실행을 찾을 수 없습니다: missing-run", response.text)

    def test_api_planner_write_escalates_before_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "planner": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('README.md').write_text('# modified by planner\\n')",
                            ]
                        }
                    },
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Planner writes"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "ESCALATED")
            artifacts = {artifact["name"] for artifact in detail["artifacts"]}
            self.assertIn("01-planner-write-violation.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertNotIn("01-plan.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)

    def test_api_qa_preview_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {},
                    "publish": {"mode": "patch_only", "require_qa_approval": True},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            with patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run):
                run_response = client.post(
                    "/api/runs",
                    json={
                        "project_id": "fixture",
                        "title": "API QA preview",
                        "description": "Stop for QA preview approval.",
                    },
                )
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]

            with patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run):
                self.assertEqual(client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": ""}).status_code, 200)
                waiting = client.get(f"/api/runs/{run_id}").json()
                self.assertEqual(waiting["run"]["status"], "AWAITING_QA_APPROVAL")
                self.assertTrue(waiting["run"]["preview_url"])

                approve = client.post(f"/api/runs/{run_id}/approve-qa-preview", json={"comment": "ok"})
                self.assertEqual(approve.status_code, 200)
            completed = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(completed["run"]["status"], "PUBLISHED")
            self.assertIn("12-publish.md", {artifact["name"] for artifact in completed["artifacts"]})
            approvals = [(approval["approval_type"], approval["decision"], approval["comment"]) for approval in completed["approvals"]]
            self.assertIn(("plan", "approved", ""), approvals)
            self.assertIn(("qa_preview", "approved", "ok"), approvals)

    def test_api_publish_rejection_escalates_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": True},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Reject publish"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]
            self.assertEqual(client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": ""}).status_code, 200)

            waiting = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(waiting["run"]["status"], "AWAITING_PUBLISH_APPROVAL")
            run_page = client.get(f"/runs/{run_id}")
            self.assertIn("Publish 반려", run_page.text)
            self.assertIn(f"/runs/{run_id}/reject-publish", run_page.text)

            reject = client.post(
                f"/api/runs/{run_id}/reject-publish",
                json={"comment": "hold for release window"},
            )
            self.assertEqual(reject.status_code, 200)

            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "ESCALATED")
            approvals = [(approval["approval_type"], approval["decision"], approval["comment"]) for approval in detail["approvals"]]
            self.assertIn(("publish", "rejected", "hold for release window"), approvals)
            artifacts = {artifact["name"] for artifact in detail["artifacts"]}
            self.assertIn("11-final-report.md", artifacts)
            self.assertNotIn("12-publish.md", artifacts)
            report = client.get(f"/api/runs/{run_id}/artifacts/11-final-report.md").text
            self.assertIn("Publish가 반려되었습니다: hold for release window", report)

    def test_api_publish_exception_fails_run_without_500(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": True},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Publish exception"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]
            self.assertEqual(client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": ""}).status_code, 200)
            waiting = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(waiting["run"]["status"], "AWAITING_PUBLISH_APPROVAL")

            with patch("devauto.runner.pipeline.Publisher.publish", side_effect=RuntimeError("publisher crashed")):
                approve_publish = client.post(f"/api/runs/{run_id}/approve-publish", json={"comment": "go"})

            self.assertEqual(approve_publish.status_code, 200)
            self.assertEqual(approve_publish.json()["run"]["status"], "FAILED")
            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "FAILED")
            self.assertIn("13-run-final.json", {artifact["name"] for artifact in detail["artifacts"]})
            report = client.get(f"/api/runs/{run_id}/artifacts/11-final-report.md").text
            self.assertIn("예상하지 못한 publish 실패: RuntimeError: publisher crashed", report)

    def test_api_reviewer_write_escalates_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "reviewer_a": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('reviewer.txt').write_text('changed'); print('DECISION: pass')",
                            ]
                        }
                    },
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post("/api/runs", json={"project_id": "fixture", "title": "Reviewer writes"})
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]
            self.assertEqual(client.get(f"/api/runs/{run_id}").json()["run"]["status"], "AWAITING_PLAN_APPROVAL")

            approve = client.post(f"/api/runs/{run_id}/approve-plan", json={"comment": ""})
            self.assertEqual(approve.status_code, 200)
            detail = client.get(f"/api/runs/{run_id}").json()
            self.assertEqual(detail["run"]["status"], "ESCALATED")
            artifacts = {artifact["name"] for artifact in detail["artifacts"]}
            self.assertIn("08-reviewer_a.md", artifacts)
            self.assertIn("08-reviewer-write-violation.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertNotIn("12-publish.md", artifacts)

    def test_api_queues_second_run_for_same_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            first = client.post("/api/runs", json={"project_id": "fixture", "title": "First"}).json()["run"]
            second = client.post("/api/runs", json={"project_id": "fixture", "title": "Second"}).json()["run"]
            self.assertEqual(client.get(f"/api/runs/{first['id']}").json()["run"]["status"], "AWAITING_PLAN_APPROVAL")
            self.assertEqual(second["status"], "QUEUED")

            approve = client.post(f"/api/runs/{first['id']}/approve-plan", json={"comment": ""})
            self.assertEqual(approve.status_code, 200)

            self.assertEqual(client.get(f"/api/runs/{first['id']}").json()["run"]["status"], "PUBLISHED")
            self.assertEqual(client.get(f"/api/runs/{second['id']}").json()["run"]["status"], "AWAITING_PLAN_APPROVAL")

    def test_api_queues_cross_project_run_when_local_runner_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            for project_id in ("fixture-a", "fixture-b"):
                project_response = client.post(
                    "/api/projects",
                    json={
                        "id": project_id,
                        "name": project_id,
                        "repo_url": str(repo),
                        "default_branch": "main",
                        "docker": {"enabled": False},
                        "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    },
                )
                self.assertEqual(project_response.status_code, 200)

            first = client.post("/api/runs", json={"project_id": "fixture-a", "title": "First"}).json()["run"]
            second = client.post("/api/runs", json={"project_id": "fixture-b", "title": "Second"}).json()["run"]
            self.assertEqual(client.get(f"/api/runs/{first['id']}").json()["run"]["status"], "AWAITING_PLAN_APPROVAL")
            self.assertEqual(second["status"], "QUEUED")

            approve = client.post(f"/api/runs/{first['id']}/approve-plan", json={"comment": ""})
            self.assertEqual(approve.status_code, 200)

            self.assertEqual(client.get(f"/api/runs/{first['id']}").json()["run"]["status"], "PUBLISHED")
            self.assertEqual(client.get(f"/api/runs/{second['id']}").json()["run"]["status"], "AWAITING_PLAN_APPROVAL")

    def test_api_imports_project_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: yaml-fixture
name: YAML Fixture
repo:
  url: {repo}
  default_branch: main
docker:
  enabled: false
commands:
  unit_test: "{sys.executable} -c \\"print('gate ok')\\""
ai:
  reviewer_a:
    command: [{sys.executable!r}, "-c", "print('DECISION: pass')"]
publish:
  mode: local_branch
  require_human_approval: true
"""
                },
            )
            self.assertEqual(response.status_code, 200)
            project = response.json()["project"]
            self.assertEqual(project["id"], "yaml-fixture")
            self.assertEqual(project["commands"]["unit_test"], f"{sys.executable} -c \"print('gate ok')\"")
            self.assertEqual(project["publish"]["mode"], "local_branch")
            self.assertTrue(project["publish"]["require_human_approval"])
            config_path = Path(project["config_path"])
            self.assertTrue(config_path.exists())
            self.assertEqual(config_path.name, "yaml-fixture.yaml")
            self.assertIn("id: yaml-fixture", config_path.read_text(encoding="utf-8"))

            projects = client.get("/api/projects").json()["projects"]
            stored = next(item for item in projects if item["id"] == "yaml-fixture")
            self.assertEqual(stored["config_path"], str(config_path))

            page = client.get("/projects")
            self.assertEqual(page.status_code, 200)
            self.assertIn("설정 파일", page.text)
            self.assertIn(str(config_path), page.text)

            doctor = client.get("/api/projects/yaml-fixture/doctor").json()["doctor"]
            self.assertEqual(doctor["status"], "pass")

    def test_api_rejects_invalid_project_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(load_settings(Path(tmp) / "devauto"))
            client = TestClient(app)

            response = client.post("/api/projects/yaml", json={"yaml_text": "- not\n- a\n- mapping\n"})
            self.assertEqual(response.status_code, 400)

    def test_api_rejects_symlink_project_yaml_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)
            outside = root / "outside-projects"
            outside.mkdir()
            (root / "devauto" / "projects").symlink_to(outside, target_is_directory=True)

            response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: symlink-registry
name: Symlink Registry
repo:
  url: {repo}
docker:
  enabled: false
"""
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("프로젝트 설정 디렉터리", response.text)
            self.assertFalse((outside / "symlink-registry.yaml").exists())
            self.assertEqual(client.get("/api/projects").json()["projects"], [])

    def test_api_rejects_unsafe_project_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "../bad",
                    "name": "Bad",
                    "repo_url": str(root / "repo"),
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(json_response.status_code, 400)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad/project
name: Bad Project
repo:
  url: {root / "repo"}
docker:
  enabled: false
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects").exists())

    def test_api_rejects_unsafe_project_contract_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-keys",
                    "name": "Bad Keys",
                    "repo_url": str(repo),
                    "docker": {"enabled": False},
                    "commands": {"../gate": "pytest"},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("command name", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-role
name: Bad Role
repo:
  url: {repo}
docker:
  enabled: false
ai:
  reviewer/a:
    command: ["codex", "exec"]
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("ai role name", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-role.yaml").exists())

    def test_api_rejects_invalid_project_contract_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-shape-json",
                    "name": "Bad Shape JSON",
                    "repo_url": str(repo),
                    "docker": {"enabled": False, "compose_files": "docker-compose.yml"},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("docker.compose_files", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-shape-yaml
name: Bad Shape YAML
repo:
  url: {repo}
docker:
  enabled: false
ai:
  executor:
    command: codex
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("ai.executor.command", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-shape-yaml.yaml").exists())

    def test_api_rejects_repo_urls_with_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-repo-url",
                    "name": "Bad Repo URL",
                    "repo_url": "https://user:token@example.test/org/repo.git",
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("repo.url", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": """
id: bad-yaml-repo-url
name: Bad YAML Repo URL
repo:
  url: https://token@example.test/org/repo.git
docker:
  enabled: false
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("repo.url", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-yaml-repo-url.yaml").exists())

    def test_api_rejects_project_commands_with_inline_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-command-secret",
                    "name": "Bad Command Secret",
                    "repo_url": str(root / "repo"),
                    "docker": {"enabled": False},
                    "commands": {"unit_test": "curl --token abc123"},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("inline secret", json_response.text)
            self.assertNotIn("abc123", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-yaml-command-secret
name: Bad YAML Command Secret
repo:
  url: {root / "repo"}
docker:
  enabled: false
ai:
  executor:
    command: ["codex", "exec", "--api-key", "abc123"]
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("inline secret", yaml_response.text)
            self.assertNotIn("abc123", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-yaml-command-secret.yaml").exists())

    def test_api_rejects_project_commands_with_forbidden_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-ai-command",
                    "name": "Bad AI Command",
                    "repo_url": str(root / "repo"),
                    "docker": {"enabled": False},
                    "ai": {"executor": {"command": ["git", "-C", str(root / "repo"), "commit", "-m", "oops"]}},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("ai.executor.command", json_response.text)
            self.assertIn("금지된 command", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": """
id: bad-yaml-side-effect
name: Bad YAML Side Effect
repo:
  url: /tmp/repo
docker:
  enabled: false
commands:
  unit_test: git -C repo push origin main
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("commands.unit_test", yaml_response.text)
            self.assertIn("금지된 command", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-yaml-side-effect.yaml").exists())

    def test_api_rejects_docker_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-compose",
                    "name": "Bad Compose",
                    "repo_url": str(repo),
                    "docker": {"enabled": True, "compose_files": ["../docker-compose.yml"]},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("docker.compose_files", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-env-file
name: Bad Env File
repo:
  url: {repo}
docker:
  enabled: true
  compose_files:
    - docker-compose.yml
  env_file: ../qa.env
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("docker.env_file", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-env-file.yaml").exists())

    def test_api_rejects_unsafe_docker_runtime_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-docker-prefix",
                    "name": "Bad Docker Prefix",
                    "repo_url": str(repo),
                    "docker": {"enabled": True, "project_name_prefix": "Bad Prefix"},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("docker.project_name_prefix", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-docker-service
name: Bad Docker Service
repo:
  url: {repo}
docker:
  enabled: true
  preview_service: --bad
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("docker.preview_service", yaml_response.text)

            bind_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-docker-bind",
                    "name": "Bad Docker Bind",
                    "repo_url": str(repo),
                    "docker": {"enabled": True, "host_bind_ip": "localhost"},
                },
            )
            self.assertEqual(bind_response.status_code, 400)
            self.assertIn("docker.host_bind_ip", bind_response.text)

            yaml_bind_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-docker-bind-yaml
name: Bad Docker Bind YAML
repo:
  url: {repo}
docker:
  enabled: true
  host_bind_ip: localhost
"""
                },
            )
            self.assertEqual(yaml_bind_response.status_code, 400)
            self.assertIn("docker.host_bind_ip", yaml_bind_response.text)

            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-docker-service.yaml").exists())
            self.assertFalse((root / "devauto" / "projects" / "bad-docker-bind-yaml.yaml").exists())

    def test_api_rejects_unsupported_run_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post(
                "/api/runs",
                json={"project_id": "fixture", "title": "Bad mode", "mode": "surprise"},
            )
            self.assertEqual(run_response.status_code, 400)
            self.assertIn("run mode", run_response.text)
            self.assertEqual(client.get("/api/runs").json()["runs"], [])

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-mode
name: Bad Mode
repo:
  url: {repo}
policy:
  default_mode: surprise
docker:
  enabled: false
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertFalse((root / "devauto" / "projects" / "bad-mode.yaml").exists())

    def test_api_normalizes_and_validates_project_boolean_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "string-bools",
                    "name": "String Bools",
                    "repo_url": str(repo),
                    "docker": {"enabled": "false"},
                    "publish": {"require_qa_approval": "false", "require_human_approval": "true"},
                },
            )
            self.assertEqual(json_response.status_code, 200)
            project = json_response.json()["project"]
            self.assertFalse(project["docker"]["enabled"])
            self.assertFalse(project["publish"]["require_qa_approval"])
            self.assertTrue(project["publish"]["require_human_approval"])

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-bool
name: Bad Bool
repo:
  url: {repo}
docker:
  enabled: maybe
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("docker.enabled", yaml_response.text)
            self.assertFalse((root / "devauto" / "projects" / "bad-bool.yaml").exists())

    def test_api_rejects_unsafe_numeric_project_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-numeric-json",
                    "name": "Bad Numeric JSON",
                    "repo_url": str(repo),
                    "docker": {"enabled": False},
                    "policy": {"max_inner_gate_fixes": -1},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("policy.max_inner_gate_fixes", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-numeric-yaml
name: Bad Numeric YAML
repo:
  url: {repo}
docker:
  enabled: false
publish:
  deploy_timeout_sec: 0
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("publish.deploy_timeout_sec", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-numeric-yaml.yaml").exists())

    def test_api_rejects_unsupported_publish_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            json_response = client.post(
                "/api/projects",
                json={
                    "id": "bad-publish",
                    "name": "Bad Publish",
                    "repo_url": str(repo),
                    "docker": {"enabled": False},
                    "publish": {"mode": "surprise"},
                },
            )
            self.assertEqual(json_response.status_code, 400)
            self.assertIn("publish.mode", json_response.text)

            yaml_response = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-publish-yaml
name: Bad Publish YAML
repo:
  url: {repo}
docker:
  enabled: false
publish:
  mode: surprise
"""
                },
            )
            self.assertEqual(yaml_response.status_code, 400)
            self.assertIn("publish.mode", yaml_response.text)
            self.assertEqual(client.get("/api/projects").json()["projects"], [])
            self.assertFalse((root / "devauto" / "projects" / "bad-publish-yaml.yaml").exists())

    def test_api_rejects_unsafe_git_branch_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            run_response = client.post(
                "/api/runs",
                json={"project_id": "fixture", "title": "Bad branch", "target_branch": "-bad"},
            )
            self.assertEqual(run_response.status_code, 400)
            self.assertIn("target_branch", run_response.text)
            self.assertEqual(client.get("/api/runs").json()["runs"], [])

            bad_default = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-default
name: Bad Default
repo:
  url: {repo}
  default_branch: bad branch
docker:
  enabled: false
"""
                },
            )
            self.assertEqual(bad_default.status_code, 400)

            bad_prefix = client.post(
                "/api/projects/yaml",
                json={
                    "yaml_text": f"""
id: bad-prefix
name: Bad Prefix
repo:
  url: {repo}
  branch_prefix: -bad/
docker:
  enabled: false
"""
                },
            )
            self.assertEqual(bad_prefix.status_code, 400)
            self.assertFalse((root / "devauto" / "projects" / "bad-prefix.yaml").exists())

    def test_api_rejects_invalid_run_request_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            app = create_app(load_settings(root / "devauto"))
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            blank_title = client.post(
                "/api/runs",
                json={"project_id": "fixture", "title": "   "},
            )
            self.assertEqual(blank_title.status_code, 400)
            self.assertIn("run title이 필요합니다", blank_title.text)

            bad_description = client.post(
                "/api/runs",
                json={"project_id": "fixture", "title": "Valid", "description": "Bad\x00body"},
            )
            self.assertEqual(bad_description.status_code, 400)
            self.assertIn("run description에 제어 문자가 포함되어 있습니다", bad_description.text)
            self.assertEqual(client.get("/api/runs").json()["runs"], [])

    def test_settings_api_and_page_show_runtime_contract_without_token_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                load_settings(root / "devauto"),
                bind_host="0.0.0.0",
                bind_port=7700,
                preview_host="192.168.0.23",
                preview_base_port=19000,
                preview_port_count=8,
                shared_token="secret",
            )
            app = create_app(settings)
            client = TestClient(app)

            project_response = client.post(
                "/api/projects?token=secret",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(root / "repo"),
                    "default_branch": "main",
                    "docker": {"enabled": True, "env_file": str(root / "qa.env")},
                    "ai": {"executor": {"command": [sys.executable, "-c", "print('ai')"]}},
                    "workspace": {"keep_success_runs": 3, "keep_failed_runs": 9},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            response = client.get("/api/settings?token=secret")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["settings"]["bind_host"], "0.0.0.0")
            self.assertEqual(payload["settings"]["qa_web_url"], "http://192.168.0.23:7700")
            self.assertEqual(payload["settings"]["preview_port_start"], 19000)
            self.assertEqual(payload["settings"]["preview_port_end"], 19007)
            self.assertEqual(payload["settings"]["preview_url_template"], "http://192.168.0.23:{preview_port}")
            self.assertEqual(payload["settings"]["lan_access"], "lan-ready")
            self.assertTrue(payload["settings"]["shared_token_configured"])
            self.assertNotIn("secret", json.dumps(payload))
            project = payload["projects"][0]
            self.assertEqual(project["config_path"], "")
            self.assertEqual(project["docker_env_file"], str(root / "qa.env"))
            self.assertEqual(project["keep_success_runs"], 3)
            self.assertEqual(project["keep_failed_runs"], 9)
            self.assertTrue(project["ai_roles"]["executor"]["configured"])

            page = client.get("/settings?token=secret")
            self.assertEqual(page.status_code, 200)
            self.assertIn("http://192.168.0.23:7700", page.text)
            self.assertIn("19000-19007", page.text)
            self.assertIn("http://192.168.0.23:{preview_port}", page.text)
            self.assertIn("LAN 접근</dt><dd>lan-ready", page.text)
            self.assertIn("공유 토큰</dt><dd>설정됨", page.text)
            self.assertIn(str(root / "qa.env"), page.text)
            self.assertIn("<td>3/9</td>", page.text)

    def test_settings_warn_when_lan_bind_uses_loopback_preview_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                load_settings(Path(tmp) / "devauto"),
                bind_host="0.0.0.0",
                bind_port=7700,
                preview_host="127.0.0.1",
            )
            app = create_app(settings)
            client = TestClient(app)

            response = client.get("/api/settings")

            self.assertEqual(response.status_code, 200)
            payload = response.json()["settings"]
            self.assertEqual(payload["qa_web_url"], "http://127.0.0.1:7700")
            self.assertEqual(payload["preview_url_template"], "http://127.0.0.1:{preview_port}")
            self.assertEqual(payload["lan_access"], "lan-host-needed")

    def test_settings_show_lan_candidate_urls_for_wildcard_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                load_settings(Path(tmp) / "devauto"),
                bind_host="0.0.0.0",
                bind_port=7700,
                preview_host="127.0.0.1",
            )
            app = create_app(settings)
            client = TestClient(app)

            with patch(
                "devauto.core.network.socket.getaddrinfo",
                return_value=[
                    (0, 0, 0, "", ("127.0.0.1", 0)),
                    (0, 0, 0, "", ("192.168.0.42", 0)),
                    (0, 0, 0, "", ("169.254.1.1", 0)),
                    (0, 0, 0, "", ("fd00::10", 0, 0, 0)),
                ],
            ):
                response = client.get("/api/settings")
                page = client.get("/settings")

            self.assertEqual(response.status_code, 200)
            payload = response.json()["settings"]
            self.assertEqual(
                payload["qa_lan_urls"],
                ["http://192.168.0.42:7700", "http://[fd00::10]:7700"],
            )
            self.assertEqual(
                payload["preview_lan_url_templates"],
                ["http://192.168.0.42:{preview_port}", "http://[fd00::10]:{preview_port}"],
            )
            self.assertEqual(page.status_code, 200)
            self.assertIn("QA LAN URL</dt><dd>http://192.168.0.42:7700, http://[fd00::10]:7700", page.text)

    def test_run_preview_shows_lan_candidates_for_wildcard_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                load_settings(root / "devauto"),
                bind_host="0.0.0.0",
                bind_port=7700,
                preview_host="127.0.0.1",
            )
            app = create_app(settings)
            client = TestClient(app)
            project_response = client.post(
                "/api/projects",
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(root / "repo"),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(project_response.status_code, 200)

            db = app.state.db
            project = db.get_project("fixture")
            run = db.create_run(
                {"project_id": project.id, "title": "Preview LAN", "description": ""},
                project,
                status=RunStatus.PUBLISHED,
            )
            run = db.update_run(run.id, preview_url="http://localhost:19000/path?x=1")

            with patch(
                "devauto.core.network.socket.getaddrinfo",
                return_value=[
                    (0, 0, 0, "", ("127.0.0.1", 0)),
                    (0, 0, 0, "", ("192.168.0.42", 0)),
                    (0, 0, 0, "", ("fd00::10", 0, 0, 0)),
                ],
            ):
                detail = client.get(f"/api/runs/{run.id}")
                runs = client.get("/api/runs")
                events = client.get(f"/api/runs/{run.id}/events")
                run_page = client.get(f"/runs/{run.id}")
                runs_page = client.get("/runs")

            expected = ["http://192.168.0.42:19000/path?x=1", "http://[fd00::10]:19000/path?x=1"]
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["run"]["preview_lan_urls"], expected)
            self.assertEqual(runs.status_code, 200)
            self.assertEqual(runs.json()["runs"][0]["preview_lan_urls"], expected)
            self.assertEqual(events.status_code, 200)
            event_line = next(line for line in events.text.splitlines() if line.startswith("data: "))
            event_payload = json.loads(event_line.removeprefix("data: "))
            self.assertEqual(event_payload["preview_lan_urls"], expected)
            self.assertEqual(run_page.status_code, 200)
            self.assertEqual(runs_page.status_code, 200)
            self.assertIn("LAN:", run_page.text)
            for url in expected:
                self.assertIn(url, run_page.text)
                self.assertIn(url, runs_page.text)

    def test_shared_token_protects_api_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(load_settings(Path(tmp) / "devauto"), shared_token="secret")
            app = create_app(settings)
            client = TestClient(app)

            self.assertEqual(client.get("/api/projects").status_code, 401)
            self.assertEqual(client.get("/api/settings").status_code, 401)
            self.assertEqual(client.get("/api/projects", headers={"X-Devauto-Token": "secret"}).status_code, 200)
            self.assertEqual(client.get("/api/settings", headers={"X-Devauto-Token": "secret"}).status_code, 200)
            self.assertEqual(client.get("/api/projects?token=secret").status_code, 200)

            self.assertEqual(client.get("/runs").status_code, 401)
            self.assertEqual(client.get("/settings").status_code, 401)
            page = client.get("/runs?token=secret")
            self.assertEqual(page.status_code, 200)
            self.assertIn('name="token" value="secret"', page.text)
            self.assertIn('/projects?token=secret', page.text)

            self.assertEqual(
                client.post(
                    "/projects",
                    data={"id": "blocked", "name": "Blocked", "repo_url": "/tmp/repo", "default_branch": "main"},
                ).status_code,
                401,
            )
            allowed = client.post(
                "/projects",
                data={
                    "token": "secret",
                    "id": "allowed",
                    "name": "Allowed",
                    "repo_url": "/tmp/repo",
                    "default_branch": "main",
                },
                follow_redirects=False,
            )
            self.assertEqual(allowed.status_code, 303)
            self.assertIn("/projects?token=secret", allowed.headers["location"])

    def test_shared_token_links_are_url_encoded_for_browser_flows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            token = "a&b c"
            encoded = "a%26b+c"
            settings = replace(load_settings(root / "devauto"), shared_token=token)
            app = create_app(settings)
            client = TestClient(app)

            project_response = client.post(
                "/api/projects",
                headers={"X-Devauto-Token": token},
                json={
                    "id": "fixture",
                    "name": "Fixture",
                    "repo_url": str(repo),
                    "default_branch": "main",
                    "docker": {"enabled": False},
                },
            )
            self.assertEqual(project_response.status_code, 200)
            run_response = client.post(
                "/api/runs",
                headers={"X-Devauto-Token": token},
                json={"project_id": "fixture", "title": "Encoded token"},
            )
            self.assertEqual(run_response.status_code, 200)
            run_id = run_response.json()["run"]["id"]

            runs_page = client.get(f"/runs?token={encoded}")
            self.assertEqual(runs_page.status_code, 200)
            self.assertIn(f'/projects?token={encoded}', runs_page.text)
            self.assertIn('name="token" value="a&amp;b c"', runs_page.text)

            run_page = client.get(f"/runs/{run_id}?token={encoded}")
            self.assertEqual(run_page.status_code, 200)
            self.assertIn(f'"/api/runs/{run_id}/events?token={encoded}"', run_page.text)
            self.assertIn(f"/runs/{run_id}/artifacts?token={encoded}", run_page.text)

            allowed = client.post(
                f"/projects?token={encoded}",
                data={
                    "token": token,
                    "id": "allowed",
                    "name": "Allowed",
                    "repo_url": str(repo),
                    "default_branch": "main",
                },
                follow_redirects=False,
            )
            self.assertEqual(allowed.status_code, 303)
            self.assertIn(f"/projects?token={encoded}", allowed.headers["location"])

    def _fixture_repo(self, path: Path) -> Path:
        path.mkdir(parents=True)
        self._run(["git", "init", "-b", "main"], path)
        self._run(["git", "config", "user.email", "test@example.com"], path)
        self._run(["git", "config", "user.name", "Test User"], path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        (path / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: busybox\n    command: sh -c 'sleep 3600'\n",
            encoding="utf-8",
        )
        self._run(["git", "add", "README.md", "docker-compose.yml"], path)
        self._run(["git", "commit", "-m", "initial"], path)
        return path

    def _run(self, args: list[str], cwd: Path) -> None:
        subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)

    def _fake_docker_run(self, args: list[str], cwd: Path, **_: object) -> CommandResult:
        return CommandResult(args=args, exit_code=0, stdout="docker ok\n", stderr="")


if __name__ == "__main__":
    unittest.main()
