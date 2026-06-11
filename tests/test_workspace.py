from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.core.models import ProjectConfig, Run, RunStatus
from devauto.runner.subprocesses import CommandResult
from devauto.runner.workspace import WorkspaceError, provision_workspace, workspace_path_rejection


class WorkspaceTest(unittest.TestCase):
    def test_workspace_git_commands_use_sanitized_environment(self) -> None:
        project = self._project()
        run = self._run()
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run_args(args: list[str], **kwargs: object) -> CommandResult:
            calls.append((args, kwargs))
            return CommandResult(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    os.environ,
                    {"PATH": "/usr/bin", "GITHUB_TOKEN": "git-token", "NORMAL_SETTING": "ok"},
                    clear=True,
                ),
                patch("devauto.runner.workspace.run_args", side_effect=fake_run_args),
            ):
                workspace = provision_workspace(Path(tmp), run, project)

        self.assertEqual(workspace, Path(tmp).resolve() / "runs" / run.id / "workspace")
        self.assertEqual(
            [args[:2] for args, _ in calls],
            [["git", "clone"], ["git", "checkout"], ["git", "checkout"]],
        )
        for _, kwargs in calls:
            env = kwargs["env"]
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["NORMAL_SETTING"], "ok")
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_workspace_path_must_not_be_a_symlink(self) -> None:
        project = self._project()
        run = self._run()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / run.id
            run_dir.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (run_dir / "workspace").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(WorkspaceError, "workspace path는 symlink일 수 없습니다"):
                provision_workspace(root, run, project)

    def test_workspace_path_rejection_rejects_missing_or_non_directory_expected_workspace(self) -> None:
        run = self._run()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "runs" / run.id / "workspace"

            self.assertEqual(
                workspace_path_rejection(root, run.id, workspace),
                "workspace path가 존재하지 않습니다",
            )

            workspace.parent.mkdir(parents=True)
            workspace.write_text("not a directory\n", encoding="utf-8")

            self.assertEqual(
                workspace_path_rejection(root, run.id, workspace),
                "workspace path가 디렉터리가 아닙니다",
            )

    def test_existing_workspace_must_be_a_git_worktree(self) -> None:
        project = self._project()
        run = self._run()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "runs" / run.id / "workspace"
            workspace.mkdir(parents=True)

            with self.assertRaisesRegex(WorkspaceError, "기존 workspace가 git worktree가 아닙니다"):
                provision_workspace(root, run, project)

    def test_existing_workspace_must_be_on_the_run_branch(self) -> None:
        project = self._project()
        run = self._run()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "runs" / run.id / "workspace"
            workspace.mkdir(parents=True)

            def fake_run_args(args: list[str], **_: object) -> CommandResult:
                if args == ["git", "rev-parse", "--show-toplevel"]:
                    return CommandResult(args, 0, f"{workspace.resolve()}\n", "")
                if args == ["git", "branch", "--show-current"]:
                    return CommandResult(args, 0, "main\n", "")
                raise AssertionError(f"unexpected command: {args}")

            with (
                patch("devauto.runner.workspace.run_args", side_effect=fake_run_args),
                self.assertRaisesRegex(WorkspaceError, f"기존 workspace branch는 aiqa/{run.id}여야 합니다"),
            ):
                provision_workspace(root, run, project)

    def test_valid_existing_workspace_can_be_reused(self) -> None:
        project = self._project()
        run = self._run()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "runs" / run.id / "workspace"
            workspace.mkdir(parents=True)
            calls: list[list[str]] = []

            def fake_run_args(args: list[str], **_: object) -> CommandResult:
                calls.append(args)
                if args == ["git", "rev-parse", "--show-toplevel"]:
                    return CommandResult(args, 0, f"{workspace.resolve()}\n", "")
                if args == ["git", "branch", "--show-current"]:
                    return CommandResult(args, 0, f"aiqa/{run.id}\n", "")
                raise AssertionError(f"unexpected command: {args}")

            with patch("devauto.runner.workspace.run_args", side_effect=fake_run_args):
                reused = provision_workspace(root, run, project)

        self.assertEqual(reused, workspace.resolve())
        self.assertEqual(calls, [["git", "rev-parse", "--show-toplevel"], ["git", "branch", "--show-current"]])

    def _project(self) -> ProjectConfig:
        return ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/remote/repo.git", "default_branch": "main"},
                "docker": {"enabled": False},
            }
        )

    def _run(self) -> Run:
        return Run(
            id="20260609-000000-abcdef",
            session_id="session-test",
            project_id="fixture",
            title="Workspace smoke",
            description="",
            target_branch="main",
            mode="human-reviewed",
            status=RunStatus.PREPARING,
            request={},
        )


if __name__ == "__main__":
    unittest.main()
