from __future__ import annotations

import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.runner.subprocesses import is_sensitive_env_key, run_args, run_shell, sanitized_child_env


class SubprocessEnvTest(unittest.TestCase):
    def test_sanitized_child_env_filters_sensitive_values_and_keeps_runtime_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "DEVAUTO_SHARED_TOKEN": "ui-token",
                "OPENAI_API_KEY": "model-token",
                "AWS_ACCESS_KEY_ID": "aws-key",
                "NORMAL_SETTING": "ok",
            },
            clear=True,
        ):
            env = sanitized_child_env({"PREVIEW_PORT": "19001", "DEVAUTO_ENV_FILE": "/tmp/qa.env"})

        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["NORMAL_SETTING"], "ok")
        self.assertEqual(env["PREVIEW_PORT"], "19001")
        self.assertEqual(env["DEVAUTO_ENV_FILE"], "/tmp/qa.env")
        self.assertNotIn("DEVAUTO_SHARED_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)

    def test_sensitive_env_key_detection(self) -> None:
        for key in ["TOKEN", "GITHUB_TOKEN", "API_KEY", "PASSWORD", "GOOGLE_APPLICATION_CREDENTIALS"]:
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_env_key(key))
        for key in ["PATH", "HOME", "SSH_AUTH_SOCK", "NORMAL_SETTING"]:
            with self.subTest(key=key):
                self.assertFalse(is_sensitive_env_key(key))

    def test_timeout_kills_process_group_for_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.txt"
            result = run_args([sys.executable, "-c", self._spawning_script(marker)], Path(tmp), timeout_sec=0.2)

            self.assertEqual(result.exit_code, 124)
            time.sleep(1.3)
            self.assertFalse(marker.exists())

    def test_timeout_kills_process_group_for_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.txt"
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(self._spawning_script(marker))}"
            result = run_shell(command, Path(tmp), timeout_sec=0.2)

            self.assertEqual(result.exit_code, 124)
            time.sleep(1.3)
            self.assertFalse(marker.exists())

    def test_subprocess_rejects_symlink_cwd_before_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)

            with patch("devauto.runner.subprocesses.subprocess.Popen") as popen:
                result = run_args([sys.executable, "-c", "print('should not run')"], link)

        self.assertEqual(result.exit_code, 126)
        self.assertIn("symlink cwd", result.stderr)
        popen.assert_not_called()

    def test_subprocess_rejects_file_cwd_before_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_cwd = Path(tmp) / "not-a-directory"
            file_cwd.write_text("not a dir", encoding="utf-8")

            with patch("devauto.runner.subprocesses.subprocess.Popen") as popen:
                result = run_shell("echo should-not-run", file_cwd)

        self.assertEqual(result.exit_code, 126)
        self.assertIn("cwd가 디렉터리가 아닙니다", result.stderr)
        popen.assert_not_called()

    def _spawning_script(self, marker: Path) -> str:
        child = (
            "import pathlib, time; "
            "time.sleep(1); "
            f"pathlib.Path({str(marker)!r}).write_text('survived')"
        )
        return f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"


if __name__ == "__main__":
    unittest.main()
