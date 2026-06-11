from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.cli import main


class CliTest(unittest.TestCase):
    def test_server_host_and_port_overrides_are_visible_to_app_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["devauto", "--host", "0.0.0.0", "--port", "7777"]
            with (
                patch.dict(os.environ, {"DEVAUTO_HOME": str(Path(tmp) / "devauto")}, clear=True),
                patch.object(sys, "argv", argv),
                patch("devauto.cli.uvicorn.run") as uvicorn_run,
            ):
                main()

                self.assertEqual(os.environ["DEVAUTO_BIND_HOST"], "0.0.0.0")
                self.assertEqual(os.environ["DEVAUTO_BIND_PORT"], "7777")

            uvicorn_run.assert_called_once()
            self.assertEqual(uvicorn_run.call_args.kwargs["host"], "0.0.0.0")
            self.assertEqual(uvicorn_run.call_args.kwargs["port"], 7777)

    def test_worker_does_not_apply_server_host_and_port_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["devauto", "--host", "0.0.0.0", "--port", "7777", "worker", "--once"]
            with (
                patch.dict(os.environ, {"DEVAUTO_HOME": str(Path(tmp) / "devauto")}, clear=True),
                patch.object(sys, "argv", argv),
                patch("devauto.cli.run_worker") as run_worker,
            ):
                main()

                self.assertNotIn("DEVAUTO_BIND_HOST", os.environ)
                self.assertNotIn("DEVAUTO_BIND_PORT", os.environ)

            run_worker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
