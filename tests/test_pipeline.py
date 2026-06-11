from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from devauto.core.config import load_settings
from devauto.core.db import Database
from devauto.core.models import GateResult, ProjectConfig, RunStatus
from devauto.runner.ai_cli import AiCliResult
from devauto.runner.subprocesses import CommandResult
from devauto.runner.pipeline import Pipeline, source_repo_snapshot, status_output_paths


class PipelineTest(unittest.TestCase):
    def test_preview_port_uses_configured_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(load_settings(root / "devauto"), preview_base_port=19000, preview_port_count=8)
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            port = pipeline._preview_port("20260609-040247-c694ae")

            self.assertGreaterEqual(port, 19000)
            self.assertLess(port, 19008)

    def test_prepare_then_approve_runs_gates_and_publishes_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {"planner": {"command": [sys.executable, "-c", ""]}},
                }
            )
            db.upsert_project(project)
            run = db.create_run(
                {
                    "project_id": "fixture",
                    "title": "Update fixture",
                    "description": "Exercise the local harness.",
                },
                project,
            )

            prepared = pipeline.prepare_run(run.id)
            self.assertEqual(prepared.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertIsNotNone(prepared.workspace_path)
            self.assertTrue((prepared.workspace_path / "README.md").exists())
            prepared_artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("00-request.json", prepared_artifacts)
            self.assertIn("00-doctor.md", prepared_artifacts)
            self.assertIn("01-plan.md", prepared_artifacts)
            self.assertNotIn("02-execution-grant.json", prepared_artifacts)
            context = db.get_artifact(run.id, "00-context.md").path.read_text()
            self.assertIn("## 프로젝트 컨텍스트 스니펫", context)
            self.assertIn("# fixture", context)
            self.assertIn(f"- session_id: {run.session_id}", context)
            request_trace = json.loads(db.get_artifact(run.id, "00-request.json").path.read_text())
            self.assertEqual(request_trace["request"]["title"], "Update fixture")
            self.assertEqual(request_trace["run"]["session_id"], run.session_id)
            self.assertEqual(request_trace["run"]["status"], "PREPARING")
            self.assertEqual(request_trace["project"]["id"], "fixture")

            completed = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-execution-grant.json", artifacts)
            self.assertIn("10-final.patch", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("12-publish.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            grant = json.loads(db.get_artifact(run.id, "02-execution-grant.json").path.read_text())
            self.assertEqual(grant["run_id"], run.id)
            self.assertEqual(grant["session_id"], run.session_id)
            self.assertEqual(grant["workspace"], str(completed.workspace_path))
            self.assertFalse(grant["publish_allowed"])
            self.assertEqual(grant["scope"]["allowed_files"], [])
            self.assertEqual(grant["allowed_commands"]["unit_test"], f"{sys.executable} -c \"print('gate ok')\"")
            self.assertIn("git push", grant["forbidden_commands"])
            self.assertIn("deploy", grant["forbidden_commands"])
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["session_id"], run.session_id)
            self.assertEqual(final_trace["run"]["status"], "PUBLISHED")
            self.assertEqual(final_trace["request"]["title"], "Update fixture")
            self.assertEqual(final_trace["approvals"][0]["approval_type"], "plan")
            self.assertEqual(final_trace["gates"][-1]["gate_name"], "unit_test")
            self.assertIn("12-publish.md", {artifact["name"] for artifact in final_trace["artifacts"]})
            gates = db.list_gate_results(run.id)
            self.assertEqual(gates[-1].gate_name, "unit_test")
            self.assertEqual(gates[-1].exit_code, 0)
            self.assertIn("09-judge.md", artifacts)

    def test_executor_prompt_includes_execution_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "import sys; from pathlib import Path; Path('executor-prompt.txt').write_text(sys.stdin.read())",
                            ]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Prompt grant"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id, "approved scope")

            prompt = (completed.workspace_path / "executor-prompt.txt").read_text()
            self.assertIn("Execution Grant:", prompt)
            self.assertIn('"publish_allowed": false', prompt)
            self.assertIn('"comment": "approved scope"', prompt)
            self.assertIn(str(completed.workspace_path), prompt)

    def test_reject_plan_replans_with_feedback_without_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {"planner": {"command": [sys.executable, "-c", ""]}},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Need better plan"}, project)

            pipeline.prepare_run(run.id)
            replanned = pipeline.reject_plan(run.id, "Add the missing edge case before execution.")

            self.assertEqual(replanned.status, RunStatus.AWAITING_PLAN_APPROVAL)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-plan-rejection.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            plan = db.get_artifact(run.id, "01-plan.md").path.read_text()
            self.assertIn("Add the missing edge case before execution.", plan)

    def test_plan_scope_violation_escalates_before_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            plan_doc = self._plan_doc(["- README.md"])

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                    "ai": {
                        "planner": {"command": [sys.executable, "-c", f"import sys; sys.stdout.write({plan_doc!r})"]},
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('other.txt').write_text('out of scope')",
                            ]
                        },
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Scope drift"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertEqual(db.list_gate_results(run.id), [])
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-scope-violation.md", artifacts)
            self.assertNotIn("07-diff.patch", artifacts)
            self.assertNotIn("10-final.patch", artifacts)
            self.assertNotIn("12-publish.md", artifacts)
            grant = json.loads(db.get_artifact(run.id, "02-execution-grant.json").path.read_text())
            self.assertEqual(grant["scope"]["allowed_files"], ["README.md"])
            violation = db.get_artifact(run.id, "02-scope-violation.md").path.read_text()
            self.assertIn("README.md", violation)
            self.assertIn("other.txt", violation)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("승인된 Plan Doc scope 밖", report)

    def test_plan_scope_allows_candidate_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            plan_doc = self._plan_doc(["- `README.md` - requested documentation update"])

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "planner": {"command": [sys.executable, "-c", f"import sys; sys.stdout.write({plan_doc!r})"]},
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('README.md').write_text('# fixture\\n\\nin scope\\n')",
                            ]
                        },
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Scoped readme change"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            grant = json.loads(db.get_artifact(run.id, "02-execution-grant.json").path.read_text())
            self.assertEqual(grant["scope"]["allowed_files"], ["README.md"])
            self.assertIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_gate_fix_scope_violation_escalates_before_gate_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            plan_doc = self._plan_doc(["- README.md"])

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"import sys; print('gate fail'); sys.exit(1)\""},
                    "ai": {
                        "planner": {"command": [sys.executable, "-c", f"import sys; sys.stdout.write({plan_doc!r})"]},
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                (
                                    "import sys; from pathlib import Path; "
                                    "prompt=sys.stdin.read().lower(); "
                                    "Path('other.txt').write_text('out of scope') "
                                    "if 'deterministic gate 실패' in prompt "
                                    "else Path('README.md').write_text('# fixture\\n\\nin scope\\n')"
                                ),
                            ]
                        },
                    },
                    "policy": {"max_inner_gate_fixes": 1, "max_outer_ai_fixes": 0},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Scoped gate fix"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("03-fix-1.log", artifacts)
            self.assertIn("02-scope-violation.md", artifacts)
            self.assertNotIn("gate-unit_test-2.log", artifacts)
            gate_results = db.list_gate_results(run.id)
            self.assertEqual(len(gate_results), 1)
            violation = db.get_artifact(run.id, "02-scope-violation.md").path.read_text()
            self.assertIn("other.txt", violation)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("승인된 Plan Doc scope 밖", report)

    def test_review_fix_scope_violation_escalates_before_gate_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            plan_doc = self._plan_doc(["- README.md"])

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "planner": {"command": [sys.executable, "-c", f"import sys; sys.stdout.write({plan_doc!r})"]},
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                (
                                    "import sys; from pathlib import Path; "
                                    "prompt=sys.stdin.read().lower(); "
                                    "Path('other.txt').write_text('out of scope') "
                                    "if 'reviewer feedback' in prompt "
                                    "else Path('README.md').write_text('# fixture\\n\\nin scope\\n')"
                                ),
                            ]
                        },
                        "reviewer_a": {"command": [sys.executable, "-c", "print('DECISION: fix\\nReviewer feedback')"]},
                    },
                    "policy": {"max_inner_gate_fixes": 0, "max_outer_ai_fixes": 1},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Scoped review fix"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("03-review-fix-1.log", artifacts)
            self.assertIn("02-scope-violation.md", artifacts)
            self.assertNotIn("gate-unit_test-2.log", artifacts)
            gate_results = db.list_gate_results(run.id)
            self.assertEqual(len(gate_results), 1)
            violation = db.get_artifact(run.id, "02-scope-violation.md").path.read_text()
            self.assertIn("other.txt", violation)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("승인된 Plan Doc scope 밖", report)

    def test_malformed_planner_output_uses_validated_fallback_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {"planner": {"command": [sys.executable, "-c", "print('looks good')"]}},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Malformed plan"}, project)

            prepared = pipeline.prepare_run(run.id)

            self.assertEqual(prepared.status, RunStatus.AWAITING_PLAN_APPROVAL)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("01-plan-invalid.md", artifacts)
            self.assertIn("01-plan.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            invalid = db.get_artifact(run.id, "01-plan-invalid.md").path.read_text()
            self.assertIn("Context Summary", invalid)
            self.assertIn("looks good", invalid)
            plan = db.get_artifact(run.id, "01-plan.md").path.read_text()
            for section in [
                "## 1. Context Summary",
                "## 2. Change Goal",
                "## 3. Candidate Files",
                "## 4. Test Scenarios",
                "## 5. Edge Cases",
                "## 6. Open Questions",
            ]:
                self.assertIn(section, plan)
            self.assertIn("planner output에 필수 Plan Doc 섹션", plan)
            self.assertNotIn("Planner AI is not configured", plan)
            review = db.get_artifact(run.id, "02-plan-review.md").path.read_text()
            self.assertIn("fallback Plan Doc 사용", review)
            self.assertIn("planner output에 필수 섹션이 없습니다", review)

    def test_auto_mode_small_valid_plan_skips_human_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            plan_doc = self._plan_doc(["- README.md"])

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "planner": {"command": [sys.executable, "-c", f"import sys; sys.stdout.write({plan_doc!r})"]},
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('README.md').write_text('# fixture\\n\\nauto docs\\n')",
                            ]
                        },
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Docs README copy", "mode": "auto"}, project)

            completed = pipeline.prepare_run(run.id)

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertEqual((completed.workspace_path / "README.md").read_text(), "# fixture\n\nauto docs\n")
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-auto-policy.md", artifacts)
            self.assertIn("02-execution-grant.json", artifacts)
            self.assertIn("12-publish.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            auto_policy = db.get_artifact(run.id, "02-auto-policy.md").path.read_text()
            self.assertIn("- 결정: 자동 승인", auto_policy)
            self.assertIn("- 변경 등급: small", auto_policy)
            approvals = db.list_approvals(run.id)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0].approval_type, "plan")
            self.assertEqual(approvals[0].decision, "approved")
            self.assertIn("자동 승인", approvals[0].comment)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["status"], "PUBLISHED")
            self.assertEqual(final_trace["approvals"][0]["decision"], "approved")

    def test_auto_mode_standard_change_waits_for_human_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            plan_doc = self._plan_doc(["- README.md"])

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                    "ai": {"planner": {"command": [sys.executable, "-c", f"import sys; sys.stdout.write({plan_doc!r})"]}},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Fixture change", "mode": "auto"}, project)

            prepared = pipeline.prepare_run(run.id)

            self.assertEqual(prepared.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertEqual(prepared.change_class, "standard")
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-auto-policy.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            self.assertEqual(db.list_approvals(run.id), [])
            auto_policy = db.get_artifact(run.id, "02-auto-policy.md").path.read_text()
            self.assertIn("- 결정: 수동 승인 필요", auto_policy)
            self.assertIn("small 변경만 자동 승인", auto_policy)

    def test_auto_mode_fallback_plan_waits_for_human_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Docs README fallback", "mode": "auto"}, project)

            prepared = pipeline.prepare_run(run.id)

            self.assertEqual(prepared.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertEqual(prepared.change_class, "small")
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-auto-policy.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            self.assertEqual(db.list_approvals(run.id), [])
            auto_policy = db.get_artifact(run.id, "02-auto-policy.md").path.read_text()
            self.assertIn("- 결정: 수동 승인 필요", auto_policy)
            self.assertIn("Fallback Plan Doc은 human approval이 필요합니다", auto_policy)

    def test_plan_reviewer_passes_before_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "plan_reviewer": {
                            "command": [sys.executable, "-c", "print('DECISION: pass\\nPlan is scoped enough')"]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Reviewed plan"}, project)

            prepared = pipeline.prepare_run(run.id)

            self.assertEqual(prepared.status, RunStatus.AWAITING_PLAN_APPROVAL)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-plan-review.md", artifacts)
            self.assertIn("02-plan-review-ai.md", artifacts)
            review = db.get_artifact(run.id, "02-plan-review.md").path.read_text()
            self.assertIn("## AI Plan Review", review)
            self.assertIn("- role: plan_reviewer", review)
            self.assertIn("- decision: pass", review)
            self.assertIn("Plan is scoped enough", review)
            self.assertNotIn("02-execution-grant.json", artifacts)

    def test_plan_reviewer_fix_escalates_before_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                    "ai": {
                        "plan_reviewer": {
                            "command": [sys.executable, "-c", "print('DECISION: fix\\nAdd explicit candidate files')"]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Plan needs review fix"}, project)

            prepared = pipeline.prepare_run(run.id)

            self.assertEqual(prepared.status, RunStatus.ESCALATED)
            self.assertEqual(self._approval_count(db), 0)
            self.assertEqual(db.list_gate_results(run.id), [])
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-plan-review.md", artifacts)
            self.assertIn("02-plan-review-ai.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            review = db.get_artifact(run.id, "02-plan-review.md").path.read_text()
            self.assertIn("- decision: fix", review)
            self.assertIn("Add explicit candidate files", review)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("plan reviewer가 fix를 요청", report)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["status"], "ESCALATED")
            self.assertEqual(final_trace["gates"], [])

    def test_plan_reviewer_workspace_write_escalates_before_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                    "ai": {
                        "plan_reviewer": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('plan-reviewer.txt').write_text('changed'); print('DECISION: pass')",
                            ]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Plan reviewer writes"}, project)

            prepared = pipeline.prepare_run(run.id)

            self.assertEqual(prepared.status, RunStatus.ESCALATED)
            self.assertEqual(self._approval_count(db), 0)
            self.assertEqual(db.list_gate_results(run.id), [])
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-plan-review-ai.md", artifacts)
            self.assertIn("02-plan-review-write-violation.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            violation = db.get_artifact(run.id, "02-plan-review-write-violation.md").path.read_text()
            self.assertIn("plan-reviewer.txt", violation)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("plan reviewer가 plan 승인 전에 workspace를 변경", report)

    def test_approval_actions_require_waiting_status_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Out of state approval"}, project)

            with self.assertRaisesRegex(ValueError, "plan 승인에는 AWAITING_PLAN_APPROVAL"):
                pipeline.approve_plan_and_execute(run.id)
            with self.assertRaisesRegex(ValueError, "plan 반려에는 AWAITING_PLAN_APPROVAL"):
                pipeline.reject_plan(run.id)
            with self.assertRaisesRegex(ValueError, "publish 승인에는 AWAITING_PUBLISH_APPROVAL"):
                pipeline.approve_publish(run.id)
            with self.assertRaisesRegex(ValueError, "publish 반려에는 AWAITING_PUBLISH_APPROVAL"):
                pipeline.reject_publish(run.id)
            with self.assertRaisesRegex(ValueError, "QA preview 승인에는 AWAITING_QA_APPROVAL"):
                pipeline.approve_qa_preview(run.id)
            with self.assertRaisesRegex(ValueError, "QA preview 반려에는 AWAITING_QA_APPROVAL"):
                pipeline.reject_qa_preview(run.id)

            self.assertEqual(db.get_run(run.id).status, RunStatus.RECEIVED)
            self.assertEqual(self._approval_count(db), 0)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertNotIn("02-execution-grant.json", artifacts)
            self.assertNotIn("02-plan-rejection.md", artifacts)
            self.assertNotIn("12-publish.md", artifacts)

    def test_prepare_run_is_idempotent_when_run_is_already_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Already claimed"}, project)
            db.transition_run(run.id, RunStatus.PREPARING)

            prepared = pipeline.prepare_run(run.id)

            self.assertEqual(prepared.status, RunStatus.PREPARING)
            self.assertEqual(db.list_artifacts(run.id), [])

    def test_plan_approval_is_idempotent_when_run_is_already_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Approval already claimed"}, project)
            pipeline.prepare_run(run.id)
            db.transition_run(run.id, RunStatus.PROVISIONING)

            approved = pipeline.approve_plan_and_execute(run.id, "duplicate approval")

            self.assertEqual(approved.status, RunStatus.PROVISIONING)
            self.assertEqual(self._approval_count(db), 0)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("01-plan.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            self.assertNotIn("10-final.patch", artifacts)

    def test_plan_rejection_is_idempotent_when_run_is_already_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Reject already claimed"}, project)
            pipeline.prepare_run(run.id)
            db.transition_run(run.id, RunStatus.PREPARING)

            rejected = pipeline.reject_plan(run.id, "duplicate rejection")

            self.assertEqual(rejected.status, RunStatus.PREPARING)
            self.assertEqual(self._approval_count(db, "plan"), 0)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("01-plan.md", artifacts)
            self.assertNotIn("02-plan-rejection.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)

    def test_publish_approval_is_idempotent_when_run_is_already_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "publish": {"mode": "local_branch", "require_human_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Publish already claimed"}, project)
            db.update_run(run.id, status=RunStatus.PUBLISHING)

            with patch("devauto.runner.pipeline.Publisher.publish") as publish:
                approved = pipeline.approve_publish(run.id, "duplicate approval")

            self.assertEqual(approved.status, RunStatus.PUBLISHING)
            self.assertEqual(self._approval_count(db), 0)
            publish.assert_not_called()

    def test_publish_run_cannot_bypass_publish_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Publish guard"}, project)

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)

            with patch("devauto.runner.pipeline.Publisher.publish") as publish:
                with self.assertRaisesRegex(ValueError, "publish에는 READY_TO_PUBLISH"):
                    pipeline.publish_run(run.id)

            self.assertEqual(db.get_run(run.id).status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertEqual(self._approval_count(db, "publish"), 0)
            self.assertNotIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})
            publish.assert_not_called()

    def test_qa_preview_approval_is_idempotent_when_run_is_already_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "publish": {
                        "mode": "local_branch",
                        "require_qa_approval": True,
                        "require_human_approval": True,
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "QA already claimed"}, project)
            db.update_run(run.id, status=RunStatus.AWAITING_PUBLISH_APPROVAL)

            approved = pipeline.approve_qa_preview(run.id, "duplicate QA approval")

            self.assertEqual(approved.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertEqual(self._approval_count(db), 0)

    def test_cancel_is_idempotent_and_starts_next_queued_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)
            first = db.create_run({"project_id": "fixture", "title": "Cancel once"}, project)
            second = db.create_run({"project_id": "fixture", "title": "Queued"}, project, status=RunStatus.QUEUED)
            pipeline.prepare_run(first.id)

            cancelled = pipeline.cancel_run(first.id)
            next_run = db.get_run(second.id)

            self.assertEqual(cancelled.status, RunStatus.CANCELLED)
            self.assertEqual(next_run.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertEqual(self._artifact_count(db, first.id, "11-final-report.md"), 1)
            self.assertEqual(self._artifact_count(db, first.id, "13-run-final.json"), 1)
            report = db.get_artifact(first.id, "11-final-report.md").path.read_text()
            self.assertIn("# 취소 보고서", report)
            self.assertIn("AWAITING_PLAN_APPROVAL 상태에서 run이 취소되었습니다.", report)

            duplicate = pipeline.cancel_run(first.id)

            self.assertEqual(duplicate.status, RunStatus.CANCELLED)
            self.assertEqual(db.get_run(second.id).status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertEqual(self._artifact_count(db, first.id, "11-final-report.md"), 1)
            self.assertEqual(self._artifact_count(db, first.id, "13-run-final.json"), 1)

    def test_prepare_run_safely_fails_unhandled_prepare_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Prepare exception"}, project)

            with patch("devauto.runner.pipeline.collect_context", side_effect=RuntimeError("context exploded")):
                completed = pipeline.prepare_run_safely(run.id)

            self.assertEqual(completed.status, RunStatus.FAILED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("예상하지 못한 준비 실패: RuntimeError: context exploded", report)
            self.assertIn("- 상태: PREPARING", report)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["status"], "FAILED")

    def test_plan_approval_safely_fails_unhandled_execution_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Execution exception"}, project)
            pipeline.prepare_run(run.id)

            with patch.object(pipeline, "execute_run", side_effect=RuntimeError("executor exploded")):
                completed = pipeline.approve_plan_and_execute_safely(run.id, "approved")

            self.assertEqual(completed.status, RunStatus.FAILED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-execution-grant.json", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            self.assertEqual(db.list_approvals(run.id)[0].decision, "approved")
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("예상하지 못한 실행 실패: RuntimeError: executor exploded", report)
            self.assertIn("- 상태: EXECUTING", report)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["status"], "FAILED")

    def test_plan_approval_refuses_external_workspace_before_executor_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "External executor workspace"}, project)
            pipeline.prepare_run(run.id)
            outside_workspace = root / "outside-workspace"
            outside_workspace.mkdir()
            db.update_run(run.id, workspace_path=outside_workspace)

            with patch("devauto.runner.pipeline.AiCliAdapter.run", side_effect=AssertionError("executor used external workspace")):
                completed = pipeline.approve_plan_and_execute(run.id, "approved")

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertNotIn("02-execution-grant.json", artifacts)
            self.assertNotIn("03-execution.log", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("workspace path가 run data root 밖", report)
            self.assertIn("workspace `사용 불가`", report)
            self.assertNotIn(str(outside_workspace), report)

    def test_plan_approval_refuses_missing_workspace_before_executor_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Missing executor workspace"}, project)
            prepared = pipeline.prepare_run(run.id)
            self.assertIsNotNone(prepared.workspace_path)
            shutil.rmtree(prepared.workspace_path)

            with patch("devauto.runner.pipeline.AiCliAdapter.run", side_effect=AssertionError("executor used missing workspace")):
                completed = pipeline.approve_plan_and_execute(run.id, "approved")

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertNotIn("02-execution-grant.json", artifacts)
            self.assertNotIn("03-execution.log", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("workspace path가 존재하지 않습니다", report)
            self.assertIn("workspace `사용 불가`", report)

    def test_failure_report_does_not_read_gate_log_outside_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Bad gate log path"}, project)
            db.transition_run(run.id, RunStatus.PREPARING)
            outside_workspace = root / "outside-workspace"
            outside_workspace.mkdir()
            (outside_workspace / "outside-secret.txt").write_text("do not read outside workspace\n", encoding="utf-8")
            db.update_run(run.id, workspace_path=outside_workspace)
            outside_log = root / "outside-gate.log"
            outside_log.write_text("do not leak this external gate log\n", encoding="utf-8")
            outside_artifact = root / "outside-artifact.md"
            outside_artifact.write_text("do not expose this artifact path\n", encoding="utf-8")
            db.add_artifact(run.id, "report", "outside-artifact.md", outside_artifact)
            db.add_gate_result(run.id, GateResult("unit_test", "pytest", 1, outside_log, 1))

            with patch("devauto.runner.pipeline.run_pipeline_git", side_effect=AssertionError("read outside workspace")):
                completed = pipeline.fail_run(run.id, "gate failed with external log path")

            self.assertEqual(completed.status, RunStatus.FAILED)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("workspace path가 run data root 밖", report)
            self.assertIn("workspace `사용 불가`", report)
            self.assertNotIn(str(outside_workspace), report)
            self.assertNotIn("do not read outside workspace", report)
            self.assertIn("실패 로그를 읽을 수 없습니다", report)
            self.assertIn("artifact path가 run artifact directory 밖으로 벗어났습니다", report)
            self.assertNotIn("do not leak this external gate log", report)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            final_trace_text = json.dumps(final_trace)
            self.assertNotIn(str(outside_workspace), final_trace_text)
            self.assertNotIn(str(outside_log), final_trace_text)
            self.assertNotIn(str(outside_artifact), final_trace_text)
            self.assertEqual(final_trace["run"]["workspace_path"], "사용 불가")
            self.assertIn(
                {"name": "outside-artifact.md", "path": "사용 불가"},
                [{"name": artifact["name"], "path": artifact["path"]} for artifact in final_trace["artifacts"]],
            )
            self.assertEqual(final_trace["gates"][0]["log_path"], "사용 불가")

    def test_preview_cleanup_refuses_external_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(root / "repo"), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                }
            )
            db.upsert_project(project)
            run = db.create_run(
                {"project_id": "fixture", "title": "Reject external preview"},
                project,
                status=RunStatus.AWAITING_QA_APPROVAL,
            )
            outside_workspace = root / "outside-workspace"
            outside_workspace.mkdir()
            db.update_run(run.id, workspace_path=outside_workspace)

            with (
                patch("devauto.runner.pipeline.run_pipeline_git", side_effect=AssertionError("read outside workspace")),
                patch(
                    "devauto.runner.pipeline.DockerGateRunner.compose_down",
                    side_effect=AssertionError("cleanup used external workspace"),
                ),
            ):
                completed = pipeline.reject_qa_preview(run.id, "not ready")

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("workspace path가 run data root 밖", report)
            self.assertIn("workspace `사용 불가`", report)
            self.assertNotIn(str(outside_workspace), report)
            self.assertNotIn("docker-compose-down.log", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_recover_stale_active_run_fails_it_and_starts_next_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)
            stale = db.create_run({"project_id": "fixture", "title": "Stale active"}, project)
            db.transition_run(stale.id, RunStatus.PREPARING)
            queued = db.create_run({"project_id": "fixture", "title": "Next queued"}, project, status=RunStatus.QUEUED)
            self._set_run_updated_at(db, stale.id, "2000-01-01T00:00:00+00:00")

            recovered = pipeline.recover_stale_runs(older_than_seconds=60)

            self.assertEqual([run.id for run in recovered], [stale.id])
            self.assertEqual(db.get_run(stale.id).status, RunStatus.FAILED)
            self.assertEqual(db.get_run(queued.id).status, RunStatus.AWAITING_PLAN_APPROVAL)
            artifacts = {artifact.name for artifact in db.list_artifacts(stale.id)}
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            report = db.get_artifact(stale.id, "11-final-report.md").path.read_text()
            self.assertIn("상태 갱신이 없어 stale active run을 복구했습니다", report)

    def test_recover_stale_runs_ignores_human_waiting_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Waiting for human"}, project)
            prepared = pipeline.prepare_run(run.id)
            self._set_run_updated_at(db, prepared.id, "2000-01-01T00:00:00+00:00")

            recovered = pipeline.recover_stale_runs(older_than_seconds=0)

            self.assertEqual(recovered, [])
            self.assertEqual(db.get_run(run.id).status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertNotIn("11-final-report.md", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_cancelled_run_stops_after_executor_before_gates_or_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Cancel during execute"}, project)

            pipeline.prepare_run(run.id)

            def cancel_after_executor(role: str, *_: object) -> AiCliResult:
                self.assertEqual(role, "executor")
                db.transition_run(run.id, RunStatus.CANCELLED)
                return AiCliResult(role=role, skipped=False, exit_code=0, output="executor finished after cancellation")

            with patch("devauto.runner.pipeline.AiCliAdapter.run", side_effect=cancel_after_executor):
                completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.CANCELLED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("03-execution.log", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            self.assertNotIn("07-diff.patch", artifacts)
            self.assertNotIn("10-final.patch", artifacts)
            self.assertNotIn("12-publish.md", artifacts)
            self.assertEqual(db.list_gate_results(run.id), [])
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("# 취소 보고서", report)
            self.assertIn("실행 경계에서 run cancellation을 감지했습니다.", report)

    def test_planner_workspace_write_escalates_before_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
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
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Planner writes"}, project)

            completed = pipeline.prepare_run(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("01-planner.log", artifacts)
            self.assertIn("01-planner-write-violation.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertNotIn("01-plan.md", artifacts)
            self.assertNotIn("02-execution-grant.json", artifacts)
            violation = db.get_artifact(run.id, "01-planner-write-violation.md").path.read_text()
            self.assertIn("README.md", violation)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("planner가 plan 승인 전에 workspace를 변경", report)

    def test_source_repo_mutation_escalates_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            outside_path = repo / "outside.txt"

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                f"from pathlib import Path; Path({str(outside_path)!r}).write_text('outside')",
                            ]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Break isolation"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertTrue(outside_path.exists())
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("02-execution-grant.json", artifacts)
            self.assertNotIn("12-publish.md", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("run workspace 밖 source repo가 변경", report)
            self.assertIn("outside.txt", report)

    def test_pipeline_internal_git_commands_use_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_run_args(args: list[str], **kwargs: object) -> CommandResult:
                calls.append((args, kwargs))
                if args == ["git", "rev-parse", "--show-toplevel"]:
                    return CommandResult(args, 0, f"{repo}\n", "")
                if args == ["git", "rev-parse", "HEAD"]:
                    return CommandResult(args, 0, "abc123\n", "")
                if args[:2] == ["git", "status"]:
                    return CommandResult(args, 0, "?? new.txt\n", "")
                if args[:2] == ["git", "diff"]:
                    return CommandResult(args, 0, "diff --git a/new.txt b/new.txt\n", "")
                return CommandResult(args, 0, "", "")

            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "GITHUB_TOKEN": "git-token",
                        "DEVAUTO_SHARED_TOKEN": "ui-token",
                        "NORMAL_SETTING": "ok",
                    },
                    clear=True,
                ),
                patch("devauto.runner.pipeline.run_args", side_effect=fake_run_args),
            ):
                snapshot = source_repo_snapshot(str(repo), settings.data_root)
                diff = pipeline._git_diff(repo)
                status = pipeline._workspace_status_lines(repo)

            self.assertTrue(snapshot["enabled"])
            self.assertIn("diff --git", diff)
            self.assertEqual(status, ["?? new.txt"])
            self.assertGreaterEqual(len(calls), 6)
            for _, kwargs in calls:
                env = kwargs["env"]
                self.assertEqual(env["PATH"], "/usr/bin")
                self.assertEqual(env["NORMAL_SETTING"], "ok")
                self.assertNotIn("GITHUB_TOKEN", env)
                self.assertNotIn("DEVAUTO_SHARED_TOKEN", env)

    def test_data_root_inside_source_repo_is_excluded_from_source_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(repo / ".devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Runtime data under repo"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            grant = json.loads(db.get_artifact(run.id, "02-execution-grant.json").path.read_text())
            self.assertEqual(grant["source_repo_snapshot"]["excluded_paths"], [".devauto"])

    def test_project_doctor_failure_escalates_before_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(root / "missing-repo"), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Invalid repo"}, project)

            completed = pipeline.prepare_run(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertIsNone(completed.workspace_path)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("00-doctor.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            doctor = db.get_artifact(run.id, "00-doctor.md").path.read_text()
            self.assertIn("repo.url", doctor)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("project doctor가 실패했습니다", report)

    def test_project_doctor_fails_existing_non_git_repo_before_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            non_git_repo = root / "not-git"
            non_git_repo.mkdir()
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(non_git_repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Non git repo"}, project)

            completed = pipeline.prepare_run(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertIsNone(completed.workspace_path)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("00-doctor.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            doctor = db.get_artifact(run.id, "00-doctor.md").path.read_text()
            self.assertIn("fail: repo.url - local path는 존재하지만 git repo가 아닙니다", doctor)

    def test_workspace_symlink_escalates_before_context_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Symlink workspace"}, project)
            run_dir = settings.data_root / "runs" / run.id
            run_dir.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (run_dir / "workspace").symlink_to(outside, target_is_directory=True)

            completed = pipeline.prepare_run(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertIsNone(completed.workspace_path)
            self.assertEqual(db.list_gate_results(run.id), [])
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("00-doctor.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            self.assertNotIn("00-context.md", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("workspace 준비 실패: workspace path는 symlink일 수 없습니다", report)

    def test_project_doctor_fails_missing_ai_executable_before_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {"executor": {"command": ["definitely-missing-ai-executable"]}},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Missing executor command"}, project)

            completed = pipeline.prepare_run(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertIsNone(completed.workspace_path)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("00-doctor.md", artifacts)
            self.assertNotIn("01-plan.md", artifacts)
            doctor = db.get_artifact(run.id, "00-doctor.md").path.read_text()
            self.assertIn("fail: ai:executor - 실행 파일을 찾을 수 없습니다: definitely-missing-ai-executable", doctor)

    def test_relative_data_root_provisions_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            previous_cwd = Path.cwd()
            os.chdir(root)
            try:
                settings = load_settings(Path("devauto"))
                db = Database(settings.database_path)
                db.initialize()
                pipeline = Pipeline(settings, db)

                project = ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": str(repo), "default_branch": "main"},
                        "docker": {"enabled": False},
                        "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    }
                )
                db.upsert_project(project)
                run = db.create_run({"project_id": "fixture", "title": "Relative data root"}, project)

                prepared = pipeline.prepare_run(run.id)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(prepared.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertIsNotNone(prepared.workspace_path)
            self.assertTrue(prepared.workspace_path.is_absolute())
            self.assertTrue((prepared.workspace_path / "README.md").exists())

    def test_workspace_retention_prunes_only_old_terminal_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(root / "repo"), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "workspace": {"keep_success_runs": 1, "keep_failed_runs": 1},
                }
            )
            db.upsert_project(project)
            success_old = self._retention_run(db, project, root, "success-old", RunStatus.PUBLISHED, "2026-01-01T00:00:00+00:00")
            success_new = self._retention_run(db, project, root, "success-new", RunStatus.PUBLISHED, "2026-01-02T00:00:00+00:00")
            failed_old = self._retention_run(db, project, root, "failed-old", RunStatus.ESCALATED, "2026-01-03T00:00:00+00:00")
            failed_new = self._retention_run(db, project, root, "failed-new", RunStatus.CANCELLED, "2026-01-04T00:00:00+00:00")

            pipeline._prune_terminal_workspaces(project, success_new.id)

            self.assertFalse(success_old.workspace_path.exists())
            self.assertTrue(success_new.workspace_path.exists())
            self.assertFalse(failed_old.workspace_path.exists())
            self.assertTrue(failed_new.workspace_path.exists())
            self.assertTrue(success_old.workspace_path.parent.joinpath("artifacts", "trace.md").exists())
            self.assertTrue(failed_old.workspace_path.parent.joinpath("artifacts", "trace.md").exists())
            retention = db.get_artifact(success_new.id, "13-retention.md").path.read_text()
            self.assertIn("keep_success_runs: 1", retention)
            self.assertIn("keep_failed_runs: 1", retention)
            self.assertIn(success_old.id, retention)
            self.assertIn(failed_old.id, retention)
            self.assertNotIn(success_new.id, retention)
            self.assertNotIn(failed_new.id, retention)

    def test_workspace_retention_refuses_workspace_path_outside_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(root / "repo"), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "workspace": {"keep_success_runs": 0, "keep_failed_runs": 0},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "External workspace"}, project, status=RunStatus.PUBLISHED)
            outside_workspace = root / "outside-workspace"
            outside_workspace.mkdir()
            (outside_workspace / "marker.txt").write_text("do not prune\n", encoding="utf-8")
            db.update_run(run.id, workspace_path=outside_workspace, created_at="2026-01-01T00:00:00+00:00")

            pipeline._prune_terminal_workspaces(project, run.id)

            self.assertTrue(outside_workspace.exists())
            self.assertTrue((outside_workspace / "marker.txt").exists())
            retention = db.get_artifact(run.id, "13-retention.md").path.read_text()
            self.assertIn("run data root 밖 workspace는 정리하지 않습니다", retention)

    def test_workspace_retention_refuses_symlinked_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(root / "repo"), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "workspace": {"keep_success_runs": 0, "keep_failed_runs": 0},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Symlink workspace"}, project, status=RunStatus.PUBLISHED)
            outside_workspace = root / "outside-workspace"
            outside_workspace.mkdir()
            (outside_workspace / "marker.txt").write_text("do not prune\n", encoding="utf-8")
            run_dir = settings.data_root / "runs" / run.id
            run_dir.mkdir(parents=True)
            workspace_link = run_dir / "workspace"
            workspace_link.symlink_to(outside_workspace, target_is_directory=True)
            db.update_run(run.id, workspace_path=workspace_link, created_at="2026-01-01T00:00:00+00:00")

            pipeline._prune_terminal_workspaces(project, run.id)

            self.assertTrue(workspace_link.is_symlink())
            self.assertTrue((outside_workspace / "marker.txt").exists())
            retention = db.get_artifact(run.id, "13-retention.md").path.read_text()
            self.assertIn("symlink가 포함된 workspace path는 정리하지 않습니다", retention)

    def test_reviewer_fix_loop_reruns_gates_and_then_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            reviewer_prompt_path = root / "reviewer-prompt.txt"

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                (
                                    "import sys; from pathlib import Path; "
                                    "prompt=sys.stdin.read().lower(); "
                                    "Path('reviewed.txt').write_text('fixed') if 'reviewer feedback' in prompt else None"
                                ),
                            ]
                        },
                        "reviewer_a": {
                            "command": [
                                sys.executable,
                                "-c",
                                (
                                    "import sys; from pathlib import Path; "
                                    f"Path({str(reviewer_prompt_path)!r}).write_text(sys.stdin.read()); "
                                    "print('DECISION: pass' if Path('reviewed.txt').exists() else 'DECISION: fix')"
                                ),
                            ]
                        },
                    },
                    "policy": {"max_inner_gate_fixes": 0, "max_outer_ai_fixes": 1},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Reviewer fix"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertEqual(completed.current_retry, 1)
            self.assertEqual(completed.max_retries, 1)
            self.assertTrue((completed.workspace_path / "reviewed.txt").exists())
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("03-review-fix-1.log", artifacts)
            self.assertIn("08-reviewer_a.md", artifacts)
            self.assertIn("09-judge.md", artifacts)
            self.assertGreaterEqual(len(db.list_gate_results(run.id)), 2)
            reviewer_prompt = reviewer_prompt_path.read_text()
            self.assertIn(f"세션: {run.session_id}", reviewer_prompt)
            self.assertIn("변경 등급:", reviewer_prompt)
            review_artifact = db.get_artifact(run.id, "08-reviewer_a.md").path.read_text()
            judge = db.get_artifact(run.id, "09-judge.md").path.read_text()
            self.assertIn(f"- session_id: {run.session_id}", review_artifact)
            self.assertIn(f"- session_id: {run.session_id}", judge)
            self.assertIn(f"- review_depth: {completed.review_depth}", judge)

    def test_gate_fix_limit_uses_inner_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"import sys; print('gate fail'); sys.exit(1)\""},
                    "ai": {"executor": {"command": [sys.executable, "-c", "print('fix attempted')"]}},
                    "policy": {"max_inner_gate_fixes": 1, "max_outer_ai_fixes": 3},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Gate retry limit"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertEqual(completed.current_retry, 1)
            self.assertEqual(completed.max_retries, 4)
            gate_results = db.list_gate_results(run.id)
            self.assertEqual(len(gate_results), 2)
            self.assertEqual([result.log_path.name for result in gate_results], ["gate-unit_test.log", "gate-unit_test-2.log"])
            self.assertTrue(gate_results[0].log_path.exists())
            self.assertTrue(gate_results[1].log_path.exists())
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("03-fix-1.log", artifacts)
            self.assertNotIn("03-fix-2.log", artifacts)
            self.assertIn("gate-unit_test.log", artifacts)
            self.assertIn("gate-unit_test-2.log", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("deterministic gate 실패가 허용 횟수를 초과했습니다", report)
            self.assertIn("- 재시도: 1/4", report)
            self.assertIn("- 실패한 게이트: unit_test", report)
            self.assertIn("## 실패 로그", report)
            self.assertIn("gate fail", report)
            self.assertIn("## 수동 다음 단계", report)

    def test_reviewer_escalation_writes_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "reviewer_a": {
                            "command": [
                                sys.executable,
                                "-c",
                                "print('DECISION: escalate\\nUnsafe scope')",
                            ]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Reviewer escalates"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("08-reviewer_a.md", artifacts)
            self.assertIn("09-judge.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("Unsafe scope", report)

    def test_reviewer_workspace_write_escalates_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
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
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Reviewer writes"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("08-reviewer_a.md", artifacts)
            self.assertIn("08-reviewer-write-violation.md", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertNotIn("12-publish.md", artifacts)
            violation = db.get_artifact(run.id, "08-reviewer-write-violation.md").path.read_text()
            self.assertIn("reviewer.txt", violation)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("reviewer가 deterministic gate 이후 workspace를 변경", report)

    def test_local_branch_publish_commits_only_after_publish_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('README.md').write_text('# fixture\\n\\nchanged\\n')",
                            ]
                        }
                    },
                    "publish": {
                        "mode": "local_branch",
                        "require_human_approval": True,
                        "commit_message_template": "fix: ${TASK_TITLE}\n\nRun ${RUN_ID}",
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Change readme"}, project)

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertEqual(self._output(["git", "rev-list", "--count", "HEAD"], waiting.workspace_path), "1")

            completed = pipeline.approve_publish(run.id)
            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertEqual(self._output(["git", "rev-list", "--count", "HEAD"], completed.workspace_path), "2")
            self.assertEqual(self._output(["git", "rev-list", "--count", "HEAD"], repo), "1")
            self.assertEqual(self._approval_count(db, "publish"), 1)

            duplicate = pipeline.approve_publish(run.id, "duplicate")
            self.assertEqual(duplicate.status, RunStatus.PUBLISHED)
            self.assertEqual(self._output(["git", "rev-list", "--count", "HEAD"], completed.workspace_path), "2")
            self.assertEqual(self._approval_count(db, "publish"), 1)

            publish_report = db.get_artifact(run.id, "12-publish.md").path.read_text()
            self.assertIn("local_branch", publish_report)
            self.assertIn("변경을 commit했습니다", publish_report)
            self.assertRegex(publish_report, r"## 커밋\n[0-9a-f]{40}")

    def test_push_branch_publish_pushes_only_after_publish_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._fixture_repo(root / "source")
            origin = root / "origin.git"
            self._run(["git", "clone", "--bare", str(source), str(origin)], root)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": f"file://{origin}", "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('README.md').write_text('# fixture\\n\\npushed\\n')",
                            ]
                        }
                    },
                    "publish": {
                        "mode": "push_branch",
                        "require_human_approval": True,
                        "commit_message_template": "fix: ${TASK_TITLE}\n\nRun ${RUN_ID}",
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Push readme"}, project)
            branch = f"aiqa/{run.id}"

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertEqual(self._output(["git", "rev-list", "--count", "main"], origin), "1")
            self.assertNotIn(branch, self._output(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], origin))

            completed = pipeline.approve_publish(run.id, "push isolated branch")

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertIn(branch, self._output(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], origin))
            self.assertEqual(self._output(["git", "rev-list", "--count", branch], origin), "2")
            self.assertEqual(self._output(["git", "rev-list", "--count", "main"], origin), "1")
            publish_report = db.get_artifact(run.id, "12-publish.md").path.read_text()
            self.assertIn("push_branch", publish_report)
            self.assertIn("격리된 workspace branch를 origin", publish_report)
            self.assertIn(f"HEAD:refs/heads/{branch}", publish_report)
            self.assertRegex(publish_report, r"## 커밋\n[0-9a-f]{40}")

    def test_deploy_command_runs_only_after_publish_approval_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            deploy_command = (
                f"{sys.executable!r} -c "
                "\"from pathlib import Path; Path('deploy-output.txt').write_text('deployed ${RUN_ID}')\""
            )

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {
                        "mode": "deploy_command",
                        "require_human_approval": True,
                        "deploy_command": deploy_command,
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Deploy fixture"}, project)

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertFalse((waiting.workspace_path / "deploy-output.txt").exists())

            completed = pipeline.approve_publish(run.id, "deploy to local staging")

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertTrue((completed.workspace_path / "deploy-output.txt").exists())
            self.assertEqual((completed.workspace_path / "deploy-output.txt").read_text(), f"deployed {run.id}")
            self.assertFalse((repo / "deploy-output.txt").exists())
            publish_report = db.get_artifact(run.id, "12-publish.md").path.read_text()
            self.assertIn("deploy_command", publish_report)
            self.assertIn("Deploy command가 성공적으로 완료되었습니다.", publish_report)
            self.assertIn("## 명령", publish_report)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["project"]["publish"]["mode"], "deploy_command")
            self.assertIn("deploy-output.txt", publish_report)

    def test_publish_exception_fails_run_with_final_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Publish failure"}, project)

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)

            with patch("devauto.runner.pipeline.Publisher.publish", side_effect=RuntimeError("publisher crashed")):
                failed = pipeline.approve_publish_safely(run.id, "publish it")

            self.assertEqual(failed.status, RunStatus.FAILED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("예상하지 못한 publish 실패: RuntimeError: publisher crashed", report)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["status"], "FAILED")
            approvals = db.list_approvals(run.id)
            self.assertEqual(approvals[-1].approval_type, "publish")
            self.assertEqual(approvals[-1].decision, "approved")

    def test_source_repo_mutation_during_publish_wait_escalates_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Publish after source change"}, project)

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            (repo / "late-source-change.txt").write_text("changed outside workspace\n", encoding="utf-8")

            completed = pipeline.approve_publish(run.id, "publish after source change")

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertNotIn("12-publish.md", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("run workspace 밖 source repo가 변경", report)
            self.assertIn("late-source-change.txt", report)

    def test_publish_refuses_external_workspace_before_output_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Publish external workspace"}, project)
            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            outside_workspace = root / "outside-workspace"
            outside_workspace.mkdir()
            db.update_run(run.id, workspace_path=outside_workspace)

            with patch("devauto.runner.pipeline.Publisher.publish", side_effect=AssertionError("published external workspace")):
                completed = pipeline.approve_publish(run.id, "go")

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertNotIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("workspace path가 run data root 밖", report)
            self.assertIn("workspace `사용 불가`", report)
            self.assertNotIn(str(outside_workspace), report)

    def test_deploy_command_source_repo_mutation_escalates_after_publish_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            outside_path = repo / "deploy-source-change.txt"
            deploy_command = (
                f"{sys.executable!r} -c "
                f"\"from pathlib import Path; Path({str(outside_path)!r}).write_text('outside')\""
            )

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {
                        "mode": "deploy_command",
                        "require_human_approval": True,
                        "deploy_command": deploy_command,
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Deploy source mutation"}, project)

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)

            completed = pipeline.approve_publish(run.id, "deploy")

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertTrue(outside_path.exists())
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("12-publish.md", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("run workspace 밖 source repo가 변경", report)
            self.assertIn("deploy-source-change.txt", report)

    def test_high_risk_change_requires_publish_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": False},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Update auth flow"}, project)

            prepared = pipeline.prepare_run(run.id)
            self.assertEqual(prepared.change_class, "high-risk")
            self.assertEqual(prepared.review_depth, 3)

            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertNotIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})

            completed = pipeline.approve_publish(run.id, "reviewed high-risk change")
            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_publish_rejection_escalates_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "publish": {"mode": "patch_only", "require_human_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Reject publish"}, project)

            pipeline.prepare_run(run.id)
            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)

            completed = pipeline.reject_publish(run.id, "hold for release window")

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            approvals = [(approval.approval_type, approval.decision, approval.comment) for approval in db.list_approvals(run.id)]
            self.assertIn(("publish", "rejected", "hold for release window"), approvals)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)
            self.assertNotIn("12-publish.md", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("Publish가 반려되었습니다: hold for release window", report)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["status"], "ESCALATED")
            self.assertIn(
                {"approval_type": "publish", "decision": "rejected", "comment": "hold for release window"},
                [
                    {
                        "approval_type": approval["approval_type"],
                        "decision": approval["decision"],
                        "comment": approval["comment"],
                    }
                    for approval in final_trace["approvals"]
                ],
            )
            final_report_count = self._artifact_count(db, run.id, "11-final-report.md")
            final_trace_count = self._artifact_count(db, run.id, "13-run-final.json")
            duplicate = pipeline.reject_publish(run.id, "hold for release window")
            self.assertEqual(duplicate.status, RunStatus.ESCALATED)
            self.assertEqual(self._approval_count(db, "publish"), 1)
            self.assertEqual(self._artifact_count(db, run.id, "11-final-report.md"), final_report_count)
            self.assertEqual(self._artifact_count(db, run.id, "13-run-final.json"), final_trace_count)

    def test_plan_candidate_high_risk_path_requires_publish_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            (repo / "auth").mkdir()
            (repo / "auth" / "session.py").write_text("SESSION = 'old'\n", encoding="utf-8")
            self._run(["git", "add", "auth/session.py"], repo)
            self._run(["git", "commit", "-m", "add auth fixture"], repo)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)
            plan_doc = self._plan_doc(["- auth/session.py"])

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "planner": {"command": [sys.executable, "-c", f"import sys; sys.stdout.write({plan_doc!r})"]},
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('auth/session.py').write_text('SESSION = \\'new\\'\\n')",
                            ]
                        },
                    },
                    "policy": {"high_risk_paths": ["auth/**"]},
                    "publish": {"mode": "patch_only", "require_human_approval": False},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Fix session bug"}, project)

            prepared = pipeline.prepare_run(run.id)
            self.assertEqual(prepared.change_class, "high-risk")
            self.assertEqual(prepared.review_depth, 3)
            review = db.get_artifact(run.id, "02-plan-review.md").path.read_text()
            self.assertIn("candidate files: auth/session.py", review)

            waiting = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertNotIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_compose_gate_is_cleaned_after_direct_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {"unit_test": "echo gate ok"},
                    "publish": {"mode": "patch_only"},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Compose cleanup"}, project)

            with (
                patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run) as docker_run,
            ):
                pipeline.prepare_run(run.id)
                completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertIsNone(completed.preview_url)
            self.assertEqual(self._docker_call_count(docker_run, "up"), 1)
            self.assertEqual(self._docker_call_count(docker_run, "exec"), 1)
            self.assertEqual(self._docker_call_count(docker_run, "down"), 1)
            self.assertIn("docker-compose-down.log", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_compose_gate_is_cleaned_before_publish_approval_without_qa_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {"unit_test": "echo gate ok"},
                    "publish": {"mode": "patch_only", "require_human_approval": False},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Update auth flow"}, project)

            with (
                patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run) as docker_run,
            ):
                pipeline.prepare_run(run.id)
                waiting = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(waiting.status, RunStatus.AWAITING_PUBLISH_APPROVAL)
            self.assertIsNone(waiting.preview_url)
            self.assertEqual(self._docker_call_count(docker_run, "up"), 1)
            self.assertEqual(self._docker_call_count(docker_run, "exec"), 1)
            self.assertEqual(self._docker_call_count(docker_run, "down"), 1)
            self.assertIn("docker-compose-down.log", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_terminal_run_starts_next_queued_run_for_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(project)
            first = db.create_run({"project_id": "fixture", "title": "First"}, project)
            second = db.create_run({"project_id": "fixture", "title": "Second"}, project, status=RunStatus.QUEUED)

            pipeline.prepare_run(first.id)
            completed = pipeline.approve_plan_and_execute(first.id)
            next_run = db.get_run(second.id)

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertEqual(next_run.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertIsNotNone(next_run.workspace_path)
            self.assertIn("01-plan.md", {artifact.name for artifact in db.list_artifacts(second.id)})

    def test_prepare_run_queues_received_run_when_another_project_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            active_project = ProjectConfig.from_mapping(
                {
                    "id": "active-project",
                    "name": "Active Project",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            waiting_project = ProjectConfig.from_mapping(
                {
                    "id": "waiting-project",
                    "name": "Waiting Project",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(active_project)
            db.upsert_project(waiting_project)
            active = db.create_run({"project_id": active_project.id, "title": "Active"}, active_project)
            waiting = db.create_run({"project_id": waiting_project.id, "title": "Waiting"}, waiting_project)
            db.transition_run(active.id, RunStatus.PREPARING)

            result = pipeline.prepare_run(waiting.id)

            self.assertEqual(result.status, RunStatus.QUEUED)
            self.assertEqual(db.get_run(waiting.id).status, RunStatus.QUEUED)
            self.assertEqual(db.list_artifacts(waiting.id), [])

    def test_terminal_run_starts_next_queued_run_across_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            first_project = ProjectConfig.from_mapping(
                {
                    "id": "fixture-a",
                    "name": "Fixture A",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            second_project = ProjectConfig.from_mapping(
                {
                    "id": "fixture-b",
                    "name": "Fixture B",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                }
            )
            db.upsert_project(first_project)
            db.upsert_project(second_project)
            first = db.create_run({"project_id": first_project.id, "title": "First"}, first_project)
            second = db.create_run(
                {"project_id": second_project.id, "title": "Second"},
                second_project,
                status=RunStatus.QUEUED,
            )

            pipeline.prepare_run(first.id)
            completed = pipeline.approve_plan_and_execute(first.id)
            next_run = db.get_run(second.id)

            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            self.assertEqual(next_run.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertIsNotNone(next_run.workspace_path)
            self.assertIn("01-plan.md", {artifact.name for artifact in db.list_artifacts(second.id)})

    def test_qa_preview_approval_is_required_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('README.md').write_text('# fixture\\n\\nqa preview\\n')",
                            ]
                        }
                    },
                    "publish": {
                        "mode": "local_branch",
                        "require_qa_approval": True,
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "QA preview change"}, project)

            with (
                patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run) as docker_run,
            ):
                pipeline.prepare_run(run.id)
                waiting = pipeline.approve_plan_and_execute(run.id)
                self.assertEqual(waiting.status, RunStatus.AWAITING_QA_APPROVAL)
                self.assertEqual(self._output(["git", "rev-list", "--count", "HEAD"], waiting.workspace_path), "1")
                self.assertTrue(waiting.preview_url)
                self.assertNotIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})
                self.assertEqual(self._docker_call_count(docker_run, "up"), 1)
                self.assertEqual(self._docker_call_count(docker_run, "ps"), 1)
                self.assertEqual(self._docker_call_count(docker_run, "down"), 0)
                self.assertIn("docker-compose-health.log", {artifact.name for artifact in db.list_artifacts(run.id)})

                completed = pipeline.approve_qa_preview(run.id, "looks good")
                self.assertEqual(completed.status, RunStatus.PUBLISHED)
                self.assertEqual(self._output(["git", "rev-list", "--count", "HEAD"], completed.workspace_path), "2")
                self.assertIn("12-publish.md", {artifact.name for artifact in db.list_artifacts(run.id)})
                self.assertEqual(self._docker_call_count(docker_run, "down"), 1)

    def test_qa_preview_url_uses_browser_host_for_wildcard_preview_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = replace(
                load_settings(root / "devauto"),
                preview_host="0.0.0.0",
                preview_base_port=19000,
                preview_port_count=1,
            )
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {},
                    "publish": {"mode": "patch_only", "require_qa_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "QA preview URL"}, project)

            with (
                patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run),
            ):
                pipeline.prepare_run(run.id)
                waiting = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(waiting.status, RunStatus.AWAITING_QA_APPROVAL)
            self.assertEqual(waiting.preview_url, "http://localhost:19000")
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("http://localhost:19000", report)
            self.assertNotIn("http://0.0.0.0:19000", report)

    def test_qa_preview_report_and_trace_include_lan_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = replace(
                load_settings(root / "devauto"),
                bind_host="0.0.0.0",
                preview_host="0.0.0.0",
                preview_base_port=19000,
                preview_port_count=1,
            )
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {},
                    "publish": {"mode": "patch_only", "require_qa_approval": True},
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "QA preview LAN report"}, project)

            with (
                patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run),
                patch(
                    "devauto.core.network.socket.getaddrinfo",
                    return_value=[
                        (0, 0, 0, "", ("127.0.0.1", 0)),
                        (0, 0, 0, "", ("192.168.0.42", 0)),
                        (0, 0, 0, "", ("fd00::10", 0, 0, 0)),
                    ],
                ),
            ):
                pipeline.prepare_run(run.id)
                waiting = pipeline.approve_plan_and_execute(run.id)
                completed = pipeline.approve_qa_preview(run.id)

            expected = ["http://192.168.0.42:19000", "http://[fd00::10]:19000"]
            self.assertEqual(waiting.status, RunStatus.AWAITING_QA_APPROVAL)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("## 미리보기 LAN URL", report)
            for url in expected:
                self.assertIn(f"- {url}", report)
            self.assertEqual(completed.status, RunStatus.PUBLISHED)
            final_trace = json.loads(db.get_artifact(run.id, "13-run-final.json").path.read_text())
            self.assertEqual(final_trace["run"]["preview_lan_urls"], expected)

    def test_qa_preview_health_failure_escalates_without_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {},
                    "publish": {
                        "mode": "patch_only",
                        "require_qa_approval": True,
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Unhealthy QA preview"}, project)

            with (
                patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_health_failure) as docker_run,
            ):
                pipeline.prepare_run(run.id)
                completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("docker-compose-health.log", artifacts)
            self.assertIn("11-final-report.md", artifacts)
            self.assertNotIn("12-publish.md", artifacts)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("preview health check 실패", report)
            self.assertEqual(self._docker_call_count(docker_run, "down"), 1)

    def test_qa_preview_rejection_escalates_without_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                    "commands": {},
                    "publish": {
                        "mode": "patch_only",
                        "require_qa_approval": True,
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "QA rejects"}, project)

            with (
                patch("devauto.core.doctor.run_args", side_effect=self._fake_docker_run),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run) as docker_run,
            ):
                pipeline.prepare_run(run.id)
                waiting = pipeline.approve_plan_and_execute(run.id)
                self.assertEqual(waiting.status, RunStatus.AWAITING_QA_APPROVAL)

                completed = pipeline.reject_qa_preview(run.id, "visual regression")
                self.assertEqual(completed.status, RunStatus.ESCALATED)
                artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
                self.assertIn("11-final-report.md", artifacts)
                self.assertNotIn("12-publish.md", artifacts)
                report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
                self.assertIn("QA preview가 반려되었습니다: visual regression", report)
                self.assertEqual(self._docker_call_count(docker_run, "down"), 1)
                final_report_count = self._artifact_count(db, run.id, "11-final-report.md")
                final_trace_count = self._artifact_count(db, run.id, "13-run-final.json")
                duplicate = pipeline.reject_qa_preview(run.id, "visual regression")
                self.assertEqual(duplicate.status, RunStatus.ESCALATED)
                self.assertEqual(self._approval_count(db, "qa_preview"), 1)
                self.assertEqual(self._artifact_count(db, run.id, "11-final-report.md"), final_report_count)
                self.assertEqual(self._artifact_count(db, run.id, "13-run-final.json"), final_trace_count)
                self.assertEqual(self._docker_call_count(docker_run, "down"), 1)

    def test_forbidden_path_diff_escalates_before_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; Path('.env').write_text('fixture=abc')",
                            ]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Touch env"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)
            self.assertEqual(completed.status, RunStatus.ESCALATED)
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("forbidden path가 변경되었습니다", report)

    def test_status_output_paths_unquotes_git_porcelain_paths(self) -> None:
        output = '?? "secrets/with space.txt"\n?? "secrets/tab\\tname.txt"\n'

        self.assertEqual(
            status_output_paths(output),
            ["secrets/with space.txt", "secrets/tab\tname.txt"],
        )

    def test_forbidden_path_with_quoted_git_status_path_escalates_before_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            pipeline = Pipeline(settings, db)

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate should not run')\""},
                    "ai": {
                        "executor": {
                            "command": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; "
                                "Path('secrets').mkdir(exist_ok=True); "
                                "Path('secrets/with space.txt').write_text('fixture=abc')",
                            ]
                        }
                    },
                }
            )
            db.upsert_project(project)
            run = db.create_run({"project_id": "fixture", "title": "Touch quoted secret path"}, project)

            pipeline.prepare_run(run.id)
            completed = pipeline.approve_plan_and_execute(run.id)

            self.assertEqual(completed.status, RunStatus.ESCALATED)
            self.assertEqual(db.list_gate_results(run.id), [])
            report = db.get_artifact(run.id, "11-final-report.md").path.read_text()
            self.assertIn("forbidden path가 변경되었습니다", report)
            self.assertIn("secrets/with space.txt", report)

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

    def _retention_run(
        self,
        db: Database,
        project: ProjectConfig,
        root: Path,
        title: str,
        status: RunStatus,
        created_at: str,
    ):
        run = db.create_run({"project_id": project.id, "title": title}, project, status=status)
        run_dir = root / "devauto" / "runs" / run.id
        workspace = run_dir / "workspace"
        artifacts = run_dir / "artifacts"
        workspace.mkdir(parents=True)
        artifacts.mkdir(parents=True)
        (workspace / "README.md").write_text(title, encoding="utf-8")
        (artifacts / "trace.md").write_text(title, encoding="utf-8")
        return db.update_run(run.id, workspace_path=workspace, created_at=created_at)

    def _plan_doc(self, candidate_lines: list[str]) -> str:
        candidates = "\n".join(candidate_lines)
        return f"""# Plan Doc

## 1. Context Summary
- Fixture repository context was collected.

## 2. Change Goal
- Apply the requested fixture change.

## 3. Candidate Files
{candidates}

## 4. Test Scenarios
- Run the configured unit test gate.

## 5. Edge Cases
- Stop if scope expansion is required.

## 6. Open Questions
- None.
"""

    def _run(self, args: list[str], cwd: Path) -> None:
        subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)

    def _output(self, args: list[str], cwd: Path | None) -> str:
        self.assertIsNotNone(cwd)
        completed = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
        return completed.stdout.strip()

    def _fake_docker_run(self, args: list[str], cwd: Path, **_: object) -> CommandResult:
        return CommandResult(args=args, exit_code=0, stdout="docker ok\n", stderr="")

    def _fake_docker_health_failure(self, args: list[str], cwd: Path, **_: object) -> CommandResult:
        if "ps" in args:
            return CommandResult(args=args, exit_code=1, stdout="", stderr="preview is unhealthy\n")
        return self._fake_docker_run(args, cwd)

    def _docker_call_count(self, docker_run: Mock, command: str) -> int:
        return sum(1 for call in docker_run.call_args_list if command in call.args[0])

    def _approval_count(self, db: Database, approval_type: str | None = None) -> int:
        with db.connect() as conn:
            if approval_type is None:
                row = conn.execute("SELECT COUNT(*) AS count FROM approvals").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM approvals WHERE approval_type = ?",
                    (approval_type,),
                ).fetchone()
        return int(row["count"])

    def _artifact_count(self, db: Database, run_id: str, name: str) -> int:
        return sum(1 for artifact in db.list_artifacts(run_id) if artifact.name == name)

    def _set_run_updated_at(self, db: Database, run_id: str, updated_at: str) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (updated_at, run_id))


if __name__ == "__main__":
    unittest.main()
