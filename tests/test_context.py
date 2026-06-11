from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devauto.core.models import ProjectConfig
from devauto.runner.context import collect_context_snippets, format_context_snippets, iter_workspace_files


class ContextTest(unittest.TestCase):
    def test_iter_workspace_files_excludes_forbidden_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (workspace / ".env").write_text("token=secret\n", encoding="utf-8")
            (workspace / "secrets").mkdir()
            (workspace / "secrets" / "service.env").write_text("token=secret\n", encoding="utf-8")

            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(workspace), "default_branch": "main"},
                }
            )

            files = iter_workspace_files(workspace, project, max_depth=3)

            self.assertEqual(files, ["src/app.py"])

    def test_collect_context_snippets_reads_standard_project_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "README.md").write_text("# Fixture\n\nHow to run it.\n", encoding="utf-8")
            (workspace / "AGENTS.md").write_text("Use existing patterns.\n", encoding="utf-8")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(workspace), "default_branch": "main"},
                }
            )

            snippets = collect_context_snippets(workspace, project)
            rendered = format_context_snippets(snippets)

            self.assertEqual([name for name, _ in snippets], ["AGENTS.md", "README.md"])
            self.assertIn("Use existing patterns.", rendered)
            self.assertIn("How to run it.", rendered)

    def test_collect_context_snippets_excludes_forbidden_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "README.md").write_text("safe docs\n", encoding="utf-8")
            (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
            project = ProjectConfig.from_mapping(
                {
                    "id": "fixture",
                    "name": "Fixture",
                    "repo": {"url": str(workspace), "default_branch": "main"},
                }
            )

            snippets = collect_context_snippets(workspace, project, candidates=[".env", "docs/README.md"])

            self.assertEqual(snippets, [("docs/README.md", "safe docs")])


if __name__ == "__main__":
    unittest.main()
