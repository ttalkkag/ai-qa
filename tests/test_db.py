from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from devauto.core.db import Database
from devauto.core.models import BLOCKED_INLINE_SECRET_ARGS, BLOCKED_INLINE_SECRET_COMMAND, ProjectConfig


class DatabaseTest(unittest.TestCase):
    def test_initialize_migrates_projects_config_path_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "devauto.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE projects (
                      id TEXT PRIMARY KEY,
                      name TEXT NOT NULL,
                      config_json TEXT NOT NULL,
                      created_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            db = Database(db_path)
            db.initialize()
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "config_path": "/tmp/fixture.yaml",
                    "repo": {"url": "/tmp/repo", "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)

            stored = db.get_project("fixture")
            self.assertEqual(stored.config_path, "/tmp/fixture.yaml")

            with db.connect() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
                row = conn.execute("SELECT config_path FROM projects WHERE id = ?", ("fixture",)).fetchone()
            self.assertIn("config_path", columns)
            self.assertEqual(row["config_path"], "/tmp/fixture.yaml")

    def test_initialize_migrates_runs_session_id_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "devauto.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE runs (
                      id TEXT PRIMARY KEY,
                      project_id TEXT NOT NULL,
                      title TEXT NOT NULL,
                      description TEXT NOT NULL,
                      target_branch TEXT NOT NULL,
                      mode TEXT NOT NULL,
                      request_json TEXT NOT NULL,
                      status TEXT NOT NULL,
                      change_class TEXT,
                      review_depth INTEGER DEFAULT 1,
                      workspace_path TEXT,
                      preview_url TEXT,
                      current_retry INTEGER DEFAULT 0,
                      max_retries INTEGER DEFAULT 2,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            db = Database(db_path)
            db.initialize()

            with db.connect() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            self.assertIn("session_id", columns)

    def test_create_run_rejects_unsupported_mode_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "devauto.sqlite3")
            db.initialize()
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": "/tmp/repo", "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)

            with self.assertRaisesRegex(ValueError, "run mode"):
                db.create_run({"project_id": "fixture", "title": "Bad mode", "mode": "surprise"}, project)

            self.assertEqual(db.list_runs(), [])

    def test_create_run_rejects_unsafe_target_branch_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "devauto.sqlite3")
            db.initialize()
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": "/tmp/repo", "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)

            with self.assertRaisesRegex(ValueError, "target_branch"):
                db.create_run(
                    {"project_id": "fixture", "title": "Bad branch", "target_branch": "-bad"},
                    project,
                )

            self.assertEqual(db.list_runs(), [])

    def test_create_run_normalizes_task_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "devauto.sqlite3")
            db.initialize()
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": "/tmp/repo", "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)

            run = db.create_run(
                {
                    "project_id": "wrong",
                    "title": "  Fix    login  ",
                    "description": "\r\nKeep session alive.\r\n",
                },
                project,
            )

            self.assertEqual(run.title, "Fix login")
            self.assertTrue(run.session_id.startswith("session-"))
            self.assertEqual(run.description, "Keep session alive.")
            self.assertEqual(run.target_branch, "main")
            self.assertEqual(run.mode, "human-reviewed")
            self.assertEqual(
                run.request,
                {
                    "project_id": "fixture",
                    "title": "Fix login",
                    "description": "Keep session alive.",
                    "target_branch": "main",
                    "mode": "human-reviewed",
                },
            )

    def test_create_run_rejects_invalid_task_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "devauto.sqlite3")
            db.initialize()
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": "/tmp/repo", "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )
            db.upsert_project(project)

            with self.assertRaisesRegex(ValueError, "run title이 필요합니다"):
                db.create_run({"project_id": "fixture", "title": "   "}, project)
            with self.assertRaisesRegex(ValueError, "run title에 제어 문자가 포함되어 있습니다"):
                db.create_run({"project_id": "fixture", "title": "Bad\x00title"}, project)
            with self.assertRaisesRegex(ValueError, "run description에 제어 문자가 포함되어 있습니다"):
                db.create_run({"project_id": "fixture", "title": "Good", "description": "Bad\x00body"}, project)

            self.assertEqual(db.list_runs(), [])

    def test_legacy_project_with_inline_secret_is_redacted_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "devauto.sqlite3")
            db.initialize()
            config = {
                "id": "legacy",
                "name": "Legacy",
                "repo": {"url": "/tmp/repo", "default_branch": "main"},
                "docker": {"enabled": False},
                "commands": {"unit_test": "curl --token abc123"},
                "ai": {"executor": {"command": ["codex", "exec", "--api-key", "abc123"]}},
                "publish": {
                    "mode": "deploy_command",
                    "require_human_approval": True,
                    "deploy_command": "./deploy --password=abc123",
                },
            }
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO projects (id, name, config_path, config_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("legacy", "Legacy", "", json.dumps(config), "2026-06-09T00:00:00Z"),
                )

            project = db.get_project("legacy")
            serialized = json.dumps(project.to_mapping())

            self.assertNotIn("abc123", serialized)
            self.assertEqual(project.commands["unit_test"], BLOCKED_INLINE_SECRET_COMMAND)
            self.assertEqual(project.ai["executor"].command, list(BLOCKED_INLINE_SECRET_ARGS))
            self.assertEqual(project.publish.deploy_command, BLOCKED_INLINE_SECRET_COMMAND)


if __name__ == "__main__":
    unittest.main()
