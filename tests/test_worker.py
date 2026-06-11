from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.core.config import load_settings
from devauto.core.db import Database
from devauto.core.models import ProjectConfig, RunStatus
from devauto.runner.pipeline import Pipeline
from devauto.runner.worker import run_pending_once
from devauto.runner.worker import _prepare_run


class WorkerTest(unittest.TestCase):
    def test_worker_prepares_received_run_without_api_background_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            project = self._project(repo)
            db.upsert_project(project)
            run = db.create_run({"project_id": project.id, "title": "Worker run"}, project)

            tick = run_pending_once(settings, db)

            prepared = db.get_run(run.id)
            self.assertEqual(tick.prepared, [run.id])
            self.assertEqual(prepared.status, RunStatus.AWAITING_PLAN_APPROVAL)
            self.assertIsNotNone(prepared.workspace_path)
            self.assertIn("01-plan.md", {artifact.name for artifact in db.list_artifacts(run.id)})

    def test_worker_leaves_queued_run_when_project_has_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            project = self._project(repo)
            db.upsert_project(project)
            active = db.create_run({"project_id": project.id, "title": "Active"}, project)
            queued = db.create_run({"project_id": project.id, "title": "Queued"}, project, status=RunStatus.QUEUED)
            db.update_run(active.id, status=RunStatus.AWAITING_PLAN_APPROVAL)

            tick = run_pending_once(settings, db)

            self.assertEqual(tick.prepared, [])
            self.assertEqual(db.get_run(queued.id).status, RunStatus.QUEUED)

    def test_worker_skips_received_run_when_another_project_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            active_project = self._project(repo, project_id="active-project")
            waiting_project = self._project(repo, project_id="waiting-project")
            db.upsert_project(active_project)
            db.upsert_project(waiting_project)
            active = db.create_run({"project_id": active_project.id, "title": "Active"}, active_project)
            waiting = db.create_run({"project_id": waiting_project.id, "title": "Waiting"}, waiting_project)
            db.update_run(active.id, status=RunStatus.AWAITING_PLAN_APPROVAL)

            tick = run_pending_once(settings, db)

            self.assertEqual(tick.prepared, [])
            self.assertEqual(tick.skipped, [waiting.id])
            self.assertEqual(db.get_run(waiting.id).status, RunStatus.RECEIVED)

    def test_worker_marks_stale_already_claimed_run_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            project = self._project(repo)
            db.upsert_project(project)
            stale_run = db.create_run({"project_id": project.id, "title": "Stale"}, project)
            db.transition_run(stale_run.id, RunStatus.PREPARING)
            prepared: list[str] = []
            skipped: list[str] = []
            errors: list[str] = []

            _prepare_run(Pipeline(settings, db), stale_run, prepared, skipped, errors)

            self.assertEqual(prepared, [])
            self.assertEqual(skipped, [stale_run.id])
            self.assertEqual(errors, [])
            self.assertEqual(db.list_artifacts(stale_run.id), [])

    def test_worker_fails_run_on_unhandled_prepare_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            project = self._project(repo)
            db.upsert_project(project)
            run = db.create_run({"project_id": project.id, "title": "Worker failure"}, project)

            with patch("devauto.runner.pipeline.collect_context", side_effect=RuntimeError("context exploded")):
                tick = run_pending_once(settings, db)

            self.assertEqual(tick.prepared, [run.id])
            completed = db.get_run(run.id)
            self.assertEqual(completed.status, RunStatus.FAILED)
            artifacts = {artifact.name for artifact in db.list_artifacts(run.id)}
            self.assertIn("11-final-report.md", artifacts)
            self.assertIn("13-run-final.json", artifacts)

    def test_worker_can_recover_stale_active_run_before_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._fixture_repo(root / "repo")
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            project = self._project(repo)
            db.upsert_project(project)
            stale_run = db.create_run({"project_id": project.id, "title": "Stale active"}, project)
            db.transition_run(stale_run.id, RunStatus.PREPARING)
            db.transition_run(stale_run.id, RunStatus.PROVISIONING)
            db.transition_run(stale_run.id, RunStatus.EXECUTING)
            queued = db.create_run({"project_id": project.id, "title": "Queued"}, project, status=RunStatus.QUEUED)
            self._set_run_updated_at(db, stale_run.id, "2000-01-01T00:00:00+00:00")

            tick = run_pending_once(settings, db, recover_stale_after_sec=60)

            self.assertEqual(tick.recovered, [stale_run.id])
            self.assertEqual(tick.errors, [])
            self.assertEqual(db.get_run(stale_run.id).status, RunStatus.FAILED)
            self.assertEqual(db.get_run(queued.id).status, RunStatus.AWAITING_PLAN_APPROVAL)

    def _project(self, repo: Path, project_id: str = "fixture") -> ProjectConfig:
        return ProjectConfig.from_mapping(
            {
                "id": project_id,
                "name": project_id,
                "repo": {"url": str(repo), "default_branch": "main"},
                "docker": {"enabled": False},
                "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                "ai": {"planner": {"command": [sys.executable, "-c", ""]}},
            }
        )

    def _fixture_repo(self, path: Path) -> Path:
        path.mkdir(parents=True)
        self._run(["git", "init", "-b", "main"], path)
        self._run(["git", "config", "user.email", "test@example.com"], path)
        self._run(["git", "config", "user.name", "Test User"], path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        self._run(["git", "add", "README.md"], path)
        self._run(["git", "commit", "-m", "initial"], path)
        return path

    def _run(self, args: list[str], cwd: Path) -> None:
        subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)

    def _set_run_updated_at(self, db: Database, run_id: str, updated_at: str) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (updated_at, run_id))


if __name__ == "__main__":
    unittest.main()
