from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.core.models import ProjectConfig, Run, RunStatus
from devauto.runner.publisher import PublishResult, Publisher
from devauto.runner.subprocesses import CommandResult


class PublisherTest(unittest.TestCase):
    def test_local_branch_git_environment_is_sanitized(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "publish": {"mode": "local_branch"},
            }
        )
        run = self._run("Local branch smoke")
        calls = []

        def fake_run_args(args: list[str], **kwargs: object) -> CommandResult:
            calls.append((args, kwargs))
            if args == ["git", "branch", "--show-current"]:
                return CommandResult(args, 0, "aiqa/20260609-000000-abcdef\n", "")
            return CommandResult(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    os.environ,
                    {"PATH": "/usr/bin", "GITHUB_TOKEN": "git-token", "NORMAL_SETTING": "ok"},
                    clear=True,
                ),
                patch("devauto.runner.publisher.run_args", side_effect=fake_run_args),
            ):
                result = Publisher().publish(run, project, Path(tmp))

        self.assertTrue(result.ok)
        self.assertGreaterEqual(len(calls), 3)
        for _, kwargs in calls:
            env = kwargs["env"]
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["NORMAL_SETTING"], "ok")
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_local_branch_refuses_unexpected_workspace_branch(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo", "branch_prefix": "aiqa/"},
                "docker": {"enabled": False},
                "publish": {"mode": "local_branch"},
            }
        )
        run = self._run("Wrong branch smoke")
        calls = []

        def fake_run_args(args: list[str], **kwargs: object) -> CommandResult:
            calls.append(args)
            if args == ["git", "branch", "--show-current"]:
                return CommandResult(args, 0, "aiqa/other-run\n", "")
            return CommandResult(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("devauto.runner.publisher.run_args", side_effect=fake_run_args):
                result = Publisher().publish(run, project, Path(tmp))

        self.assertFalse(result.ok)
        self.assertEqual(result.branch, "aiqa/other-run")
        self.assertIn("예상값 aiqa/20260609-000000-abcdef", result.summary)
        self.assertNotIn(["git", "add", "--all"], calls)

    def test_push_branch_requires_human_approval(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "publish": {"mode": "push_branch", "require_human_approval": False},
            }
        )
        run = Run(
            id="20260609-000000-abcdef",
            session_id="session-test",
            project_id="fixture",
            title="Push smoke",
            description="",
            target_branch="main",
            mode="human-reviewed",
            status=RunStatus.PUBLISHING,
            request={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = Publisher().publish(run, project, Path(tmp))

        self.assertFalse(result.ok)
        self.assertEqual(result.mode, "push_branch")
        self.assertIn("publish approval이 필요합니다", result.summary)

    def test_push_branch_git_environment_is_sanitized(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo", "branch_prefix": "aiqa/"},
                "docker": {"enabled": False},
                "publish": {"mode": "push_branch", "require_human_approval": True},
            }
        )
        run = self._run("Push branch smoke")
        calls = []

        def fake_run_args(args: list[str], **kwargs: object) -> CommandResult:
            calls.append((args, kwargs))
            if args[:4] == ["git", "ls-remote", "--exit-code", "--heads"]:
                return CommandResult(args, 2, "", "")
            if args[:2] == ["git", "push"]:
                return CommandResult(args, 0, "pushed\n", "")
            return CommandResult(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    os.environ,
                    {"PATH": "/usr/bin", "GITHUB_TOKEN": "git-token", "NORMAL_SETTING": "ok"},
                    clear=True,
                ),
                patch.object(
                    Publisher,
                    "_publish_local_branch",
                    return_value=PublishResult(
                        ok=True,
                        mode="local_branch",
                        summary="변경을 commit했습니다",
                        branch="aiqa/20260609-000000-abcdef",
                        commit_hash="abc123",
                    ),
                ),
                patch("devauto.runner.publisher.run_args", side_effect=fake_run_args),
            ):
                result = Publisher().publish(run, project, Path(tmp))

        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "push_branch")
        self.assertEqual(len(calls), 2)
        for _, kwargs in calls:
            env = kwargs["env"]
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["NORMAL_SETTING"], "ok")
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_push_branch_refuses_unexpected_workspace_branch(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo", "branch_prefix": "aiqa/"},
                "docker": {"enabled": False},
                "publish": {"mode": "push_branch", "require_human_approval": True},
            }
        )
        run = self._run("Push wrong branch smoke")
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    Publisher,
                    "_publish_local_branch",
                    return_value=PublishResult(
                        ok=True,
                        mode="local_branch",
                        summary="Committed changes",
                        branch="aiqa/other-run",
                        commit_hash="abc123",
                    ),
                ),
                patch("devauto.runner.publisher.run_args") as run_args,
            ):
                result = Publisher().publish(run, project, Path(tmp))

        self.assertFalse(result.ok)
        self.assertEqual(result.mode, "push_branch")
        self.assertIn("예상값 aiqa/20260609-000000-abcdef", result.summary)
        run_args.assert_not_called()

    def test_deploy_command_environment_is_sanitized(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "publish": {
                    "mode": "deploy_command",
                    "require_human_approval": True,
                    "deploy_command": "./deploy.sh ${RUN_ID}",
                },
            }
        )
        run = Run(
            id="20260609-000000-abcdef",
            session_id="session-test",
            project_id="fixture",
            title="Deploy smoke",
            description="",
            target_branch="main",
            mode="human-reviewed",
            status=RunStatus.PUBLISHING,
            request={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "DEVAUTO_SHARED_TOKEN": "ui-token",
                        "GITHUB_TOKEN": "git-token",
                        "NORMAL_SETTING": "ok",
                    },
                    clear=True,
                ),
                patch("devauto.runner.publisher.run_shell", return_value=CommandResult("./deploy.sh", 0, "ok", "")) as run_shell,
            ):
                result = Publisher().publish(run, project, workspace)

        self.assertTrue(result.ok)
        env = run_shell.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["NORMAL_SETTING"], "ok")
        self.assertNotIn("DEVAUTO_SHARED_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_deploy_command_quotes_runtime_placeholders_before_shell_execution(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "publish": {
                    "mode": "deploy_command",
                    "require_human_approval": True,
                    "deploy_command": "./deploy.sh --title=${TASK_TITLE} --run=${RUN_ID} --project=${PROJECT_ID}",
                },
            }
        )
        run = self._run("Fix docs; touch injected")
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "devauto.runner.publisher.run_shell",
                return_value=CommandResult("./deploy.sh", 0, "ok", ""),
            ) as run_shell:
                result = Publisher().publish(run, project, Path(tmp))

        self.assertTrue(result.ok)
        command = run_shell.call_args.args[0]
        self.assertEqual(
            command,
            "./deploy.sh --title='Fix docs; touch injected' "
            "--run=20260609-000000-abcdef --project=fixture",
        )

    def _run(self, title: str) -> Run:
        return Run(
            id="20260609-000000-abcdef",
            session_id="session-test",
            project_id="fixture",
            title=title,
            description="",
            target_branch="main",
            mode="human-reviewed",
            status=RunStatus.PUBLISHING,
            request={},
        )


if __name__ == "__main__":
    unittest.main()
