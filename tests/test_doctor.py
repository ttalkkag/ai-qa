from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.core.doctor import run_project_doctor
from devauto.core.models import ProjectConfig
from devauto.runner.subprocesses import CommandResult


class DoctorTest(unittest.TestCase):
    def test_doctor_passes_local_project_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": f"{sys.executable} -c \"print('gate ok')\""},
                    "ai": {
                        "reviewer_a": {
                            "command": [sys.executable, "-c", "print('DECISION: pass')"],
                        }
                    },
                    "publish": {"mode": "patch_only"},
                }
            )

            report = run_project_doctor(project)

            self.assertEqual(report.status, "pass")
            checks = {check.name: check.status for check in report.checks}
            self.assertEqual(checks["repo.url"], "pass")
            self.assertEqual(checks["command:unit_test"], "pass")
            self.assertEqual(checks["ai:reviewer_a"], "pass")
            self.assertEqual(checks["publish"], "pass")

    def test_doctor_rejects_tag_when_default_branch_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            self._run(["git", "tag", "release"], repo)
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "release"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": "echo ok"},
                    "publish": {"mode": "patch_only"},
                }
            )

            report = run_project_doctor(project)

            self.assertEqual(report.status, "fail")
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["repo.default_branch"].status, "fail")
            self.assertIn("release", checks["repo.default_branch"].message)

    def test_doctor_fails_qa_preview_without_compose_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": "echo ok"},
                    "publish": {"mode": "patch_only", "require_qa_approval": True},
                }
            )

            report = run_project_doctor(project)

            self.assertEqual(report.status, "fail")
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["publish.require_qa_approval"].status, "fail")

    def test_doctor_requires_human_approval_for_deploy_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": "echo ok"},
                    "publish": {
                        "mode": "deploy_command",
                        "require_human_approval": False,
                        "deploy_command": "./scripts/deploy-staging.sh",
                    },
                }
            )

            report = run_project_doctor(project)

            self.assertEqual(report.status, "fail")
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["publish.require_human_approval"].status, "fail")

    def test_doctor_requires_human_approval_for_push_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": "echo ok"},
                    "publish": {"mode": "push_branch", "require_human_approval": False},
                }
            )

            report = run_project_doctor(project)

            self.assertEqual(report.status, "fail")
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["publish.require_human_approval"].status, "fail")

    def test_doctor_blocks_dangerous_deploy_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            with self.assertRaisesRegex(ValueError, "publish.deploy_command"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": str(repo), "default_branch": "main"},
                        "docker": {"enabled": False},
                        "commands": {"unit_test": "echo ok"},
                        "ai": {"reviewer_a": {"command": [sys.executable, "-c", "print('DECISION: pass')"]}},
                        "publish": {
                            "mode": "deploy_command",
                            "require_human_approval": True,
                            "deploy_command": "git push origin main",
                        },
                    }
                )

    def test_doctor_accepts_approved_deploy_command_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": "echo ok"},
                    "ai": {"reviewer_a": {"command": [sys.executable, "-c", "print('DECISION: pass')"]}},
                    "publish": {
                        "mode": "deploy_command",
                        "require_human_approval": True,
                        "deploy_command": "./scripts/deploy-staging.sh",
                    },
                }
            )

            report = run_project_doctor(project)

            self.assertEqual(report.status, "pass")
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["publish"].status, "pass")

    def test_doctor_checks_docker_env_file_without_reading_secret_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            (repo / "qa.env").write_text("TOKEN=secret\n", encoding="utf-8")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"], "env_file": "qa.env"},
                }
            )

            with (
                patch("devauto.core.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
                patch("devauto.core.doctor.run_args", side_effect=self._fake_run),
            ):
                report = run_project_doctor(project)

            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["docker.env_file"].status, "pass")
            self.assertIn("qa.env", checks["docker.env_file"].message)
            self.assertNotIn("TOKEN", checks["docker.env_file"].message)

    def test_doctor_child_commands_use_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"]},
                }
            )
            calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_run(args: list[str], **kwargs: object) -> CommandResult:
                calls.append((args, kwargs))
                return CommandResult(args=args, exit_code=0, stdout="ok\n", stderr="")

            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "DEVAUTO_SHARED_TOKEN": "ui-token",
                        "OPENAI_API_KEY": "model-token",
                        "NORMAL_SETTING": "ok",
                    },
                    clear=True,
                ),
                patch("devauto.core.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
                patch("devauto.core.doctor.run_args", side_effect=fake_run),
            ):
                report = run_project_doctor(project)

            self.assertIn(report.status, {"pass", "fail"})
            self.assertGreaterEqual(len(calls), 2)
            for _, kwargs in calls:
                env = kwargs["env"]
                self.assertEqual(env["PATH"], "/usr/bin")
                self.assertEqual(env["NORMAL_SETTING"], "ok")
                self.assertNotIn("DEVAUTO_SHARED_TOKEN", env)
                self.assertNotIn("OPENAI_API_KEY", env)

    def test_doctor_fails_missing_docker_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(repo), "default_branch": "main"},
                    "docker": {"enabled": True, "compose_files": ["docker-compose.yml"], "env_file": "missing.env"},
                }
            )

            with (
                patch("devauto.core.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
                patch("devauto.core.doctor.run_args", side_effect=self._fake_run),
            ):
                report = run_project_doctor(project)

            self.assertEqual(report.status, "fail")
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["docker.env_file"].status, "fail")
            self.assertIn("missing.env", checks["docker.env_file"].message)

    def test_doctor_fails_existing_local_path_that_is_not_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            non_git_repo = Path(tmp) / "not-git"
            non_git_repo.mkdir()
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(non_git_repo), "default_branch": "main"},
                    "docker": {"enabled": False},
                }
            )

            report = run_project_doctor(project)

            self.assertEqual(report.status, "fail")
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["repo.url"].status, "fail")
            self.assertIn("git repo가 아닙니다", checks["repo.url"].message)

    def test_doctor_fails_forbidden_ai_role_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            with self.assertRaisesRegex(ValueError, "ai.executor.command"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": str(repo), "default_branch": "main"},
                        "docker": {"enabled": False},
                        "commands": {"unit_test": "echo ok"},
                        "ai": {"executor": {"command": ["git", "push", "--force"]}},
                    }
                )

    def test_doctor_fails_ai_role_commit_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            with self.assertRaisesRegex(ValueError, "ai.executor.command"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": str(repo), "default_branch": "main"},
                        "docker": {"enabled": False},
                        "commands": {"unit_test": "echo ok"},
                        "ai": {"executor": {"command": ["git", "-C", str(repo), "commit", "-m", "oops"]}},
                    }
                )

    def test_doctor_blocks_deploy_command_that_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            with self.assertRaisesRegex(ValueError, "publish.deploy_command"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": str(repo), "default_branch": "main"},
                        "docker": {"enabled": False},
                        "commands": {"unit_test": "echo ok"},
                        "publish": {
                            "mode": "deploy_command",
                            "require_human_approval": True,
                            "deploy_command": "git -C repo commit -m release",
                        },
                    }
                )

    def test_doctor_enforces_harness_forbidden_commands_even_when_policy_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            with self.assertRaisesRegex(ValueError, "ai.executor.command"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": str(repo), "default_branch": "main"},
                        "docker": {"enabled": False},
                        "commands": {"unit_test": "git push origin main"},
                        "ai": {"executor": {"command": ["git", "push", "origin", "main"]}},
                        "policy": {"forbidden_commands": []},
                    }
                )

    def test_doctor_blocks_forbidden_command_whitespace_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._fixture_repo(Path(tmp) / "repo")
            with self.assertRaisesRegex(ValueError, "commands.unit_test"):
                ProjectConfig.from_mapping(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "repo": {"url": str(repo), "default_branch": "main"},
                        "docker": {"enabled": False},
                        "commands": {"unit_test": "docker   system\nprune"},
                        "publish": {
                            "mode": "deploy_command",
                            "require_human_approval": True,
                            "deploy_command": "git\tpush origin main",
                        },
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

    def _fake_run(self, args: list[str], cwd: Path, **_: object) -> CommandResult:
        return CommandResult(args=args, exit_code=0, stdout="ok\n", stderr="")


if __name__ == "__main__":
    unittest.main()
