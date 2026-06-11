from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from devauto.core.config import load_settings
from devauto.core.db import Database
from devauto.core.models import ProjectConfig
from devauto.runner.artifacts import ArtifactStore
from devauto.runner.gates import DockerGateRunner
from devauto.runner.subprocesses import CommandResult


class DockerGateRunnerTest(unittest.TestCase):
    def test_install_runs_before_other_deterministic_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            artifacts = ArtifactStore(settings.data_root, db)
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(workspace), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {
                        "unit_test": "echo unit",
                        "install": "echo install",
                    },
                }
            )
            run_id = "20260609-000000-abcdef"

            with patch("devauto.runner.gates.run_shell", side_effect=self._fake_shell_run):
                runner = DockerGateRunner(project, artifacts, run_id, 19001)
                gate_run = runner.run_all(workspace)

            self.assertTrue(gate_run.ok)
            self.assertEqual([result.gate_name for result in gate_run.results], ["install", "unit_test"])
            self.assertEqual([artifact.name for artifact in db.list_artifacts(run_id)], ["gate-install.log", "gate-unit_test.log"])

    def test_local_gate_environment_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            artifacts = ArtifactStore(settings.data_root, db)
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(workspace), "default_branch": "main"},
                    "docker": {"enabled": False},
                    "commands": {"unit_test": "echo unit"},
                }
            )
            run_id = "20260609-000000-abcdef"

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
                patch("devauto.runner.gates.run_shell", side_effect=self._fake_shell_run) as shell_run,
            ):
                runner = DockerGateRunner(project, artifacts, run_id, 19001)
                gate_run = runner.run_all(workspace)

            self.assertTrue(gate_run.ok)
            env = shell_run.call_args.kwargs["env"]
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["NORMAL_SETTING"], "ok")
            self.assertEqual(env["PREVIEW_PORT"], "19001")
            self.assertNotIn("DEVAUTO_SHARED_TOKEN", env)
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_compose_commands_receive_preview_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            artifacts = ArtifactStore(settings.data_root, db)
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(workspace), "default_branch": "main"},
                    "docker": {
                        "enabled": True,
                        "compose_files": ["docker-compose.yml"],
                        "host_bind_ip": "0.0.0.0",
                        "env_file": str(root / "qa.env"),
                    },
                    "commands": {"unit_test": "echo gate ok"},
                }
            )
            run_id = "20260609-000000-abcdef"

            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "DEVAUTO_SHARED_TOKEN": "ui-token",
                        "OPENAI_API_KEY": "model-token",
                    },
                    clear=True,
                ),
                patch("devauto.runner.gates.run_args", side_effect=self._fake_docker_run) as docker_run,
            ):
                runner = DockerGateRunner(project, artifacts, run_id, 19001)
                gate_run = runner.run_all(workspace)
                runner.compose_health_check(workspace)
                runner.compose_down(workspace)

            self.assertTrue(gate_run.ok)
            for command in ["up", "exec", "ps", "down"]:
                args = self._docker_args(docker_run, command)
                self.assertIn("--env-file", args)
                self.assertEqual(args[args.index("--env-file") + 1], str(root / "qa.env"))
                env = self._docker_env(docker_run, command)
                self.assertEqual(env["PREVIEW_PORT"], "19001")
                self.assertEqual(env["HOST_BIND_IP"], "0.0.0.0")
                self.assertEqual(env["DEVAUTO_ENV_FILE"], str(root / "qa.env"))
                self.assertNotIn("DEVAUTO_SHARED_TOKEN", env)
                self.assertNotIn("OPENAI_API_KEY", env)

    def _fake_docker_run(self, args: list[str], cwd: Path, **_: object) -> CommandResult:
        return CommandResult(args=args, exit_code=0, stdout="docker ok\n", stderr="")

    def _fake_shell_run(self, command: str, cwd: Path, **_: object) -> CommandResult:
        return CommandResult(args=command, exit_code=0, stdout=f"{command}\n", stderr="")

    def _docker_env(self, docker_run: Mock, command: str) -> dict[str, str]:
        for call in docker_run.call_args_list:
            if command in call.args[0]:
                env = call.kwargs.get("env")
                self.assertIsInstance(env, dict)
                return env
        self.fail(f"docker compose {command} was not called")

    def _docker_args(self, docker_run: Mock, command: str) -> list[str]:
        for call in docker_run.call_args_list:
            args = call.args[0]
            if command in args:
                return args
        self.fail(f"docker compose {command} was not called")


if __name__ == "__main__":
    unittest.main()
