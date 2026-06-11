from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devauto.core.config import load_settings


class ConfigTest(unittest.TestCase):
    def test_load_settings_rejects_invalid_runtime_ports(self) -> None:
        bad_envs = [
            {"DEVAUTO_BIND_PORT": "0"},
            {"DEVAUTO_BIND_PORT": "65536"},
            {"DEVAUTO_BIND_PORT": "not-a-port"},
            {"DEVAUTO_PREVIEW_BASE_PORT": "0"},
            {"DEVAUTO_PREVIEW_BASE_PORT": "65536"},
            {"DEVAUTO_PREVIEW_PORT_COUNT": "0"},
            {"DEVAUTO_PREVIEW_PORT_COUNT": "-1"},
            {"DEVAUTO_PREVIEW_PORT_COUNT": "not-a-count"},
            {"DEVAUTO_PREVIEW_BASE_PORT": "65535", "DEVAUTO_PREVIEW_PORT_COUNT": "2"},
        ]

        for env in bad_envs:
            with self.subTest(env=env):
                with tempfile.TemporaryDirectory() as tmp:
                    with patch.dict(os.environ, {"DEVAUTO_HOME": str(Path(tmp) / "devauto"), **env}, clear=True):
                        with self.assertRaisesRegex(ValueError, "DEVAUTO_"):
                            load_settings()

    def test_load_settings_accepts_preview_range_ending_at_max_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "DEVAUTO_HOME": str(Path(tmp) / "devauto"),
                    "DEVAUTO_BIND_PORT": "7702",
                    "DEVAUTO_PREVIEW_BASE_PORT": "65535",
                    "DEVAUTO_PREVIEW_PORT_COUNT": "1",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.bind_port, 7702)
        self.assertEqual(settings.preview_base_port, 65535)
        self.assertEqual(settings.preview_port_count, 1)


if __name__ == "__main__":
    unittest.main()
