from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


class PackagingTest(unittest.TestCase):
    def test_dev_extra_uses_testclient_dependency_name(self) -> None:
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]

        self.assertTrue(any(item.startswith("httpx") for item in dev_deps))
        self.assertFalse(any(item.startswith("httpx2") for item in dev_deps))
        self.assertTrue(any(item.startswith("pytest") for item in dev_deps))


if __name__ == "__main__":
    unittest.main()
