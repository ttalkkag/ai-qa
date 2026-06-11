from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.core.models import ProjectConfig
from devauto.runner.ai_cli import AiCliAdapter
from devauto.runner.subprocesses import CommandResult


class AiCliAdapterTest(unittest.TestCase):
    def test_ai_cli_child_environment_is_sanitized(self) -> None:
        project = ProjectConfig.from_mapping(
            {
                "id": "fixture",
                "name": "Fixture",
                "repo": {"url": "/tmp/repo"},
                "docker": {"enabled": False},
                "ai": {"planner": {"command": ["planner-cli"], "timeout_sec": 10}},
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "DEVAUTO_SHARED_TOKEN": "ui-token",
                        "ANTHROPIC_API_KEY": "model-token",
                        "NORMAL_SETTING": "ok",
                    },
                    clear=True,
                ),
                patch("devauto.runner.ai_cli.run_args", return_value=CommandResult(["planner-cli"], 0, "ok", "")) as run,
            ):
                result = AiCliAdapter(project).run("planner", workspace, "make a plan")

        self.assertTrue(result.ok)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["NORMAL_SETTING"], "ok")
        self.assertNotIn("DEVAUTO_SHARED_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(run.call_args.kwargs["input_text"], "make a plan")


if __name__ == "__main__":
    unittest.main()
