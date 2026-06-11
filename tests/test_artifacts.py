from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devauto.core.config import load_settings
from devauto.core.db import Database
from devauto.core.models import Artifact
from devauto.runner.artifacts import ArtifactStore, read_artifact_text, redact


class ArtifactTest(unittest.TestCase):
    def test_redact_masks_common_secret_shapes(self) -> None:
        content = """api_key=abc123
token = "tok-456"
OPENAI_API_KEY=openai-secret
GH_TOKEN=gh-secret
password: pass789
Authorization: Bearer header-token-123
Cookie: session=secret-session; theme=dark
repo=https://user:repo-pass@example.test/org/repo.git
deploy --token cli-token --password=cli-pass
regular_log=line-ok
"""

        redacted = redact(content)

        for value in [
            "abc123",
            "tok-456",
            "openai-secret",
            "gh-secret",
            "pass789",
            "header-token-123",
            "secret-session",
            "user:repo-pass",
            "cli-token",
            "cli-pass",
        ]:
            self.assertNotIn(value, redacted)
        self.assertIn("api_key=[REDACTED]", redacted)
        self.assertIn('token = "[REDACTED]"', redacted)
        self.assertIn("OPENAI_API_KEY=[REDACTED]", redacted)
        self.assertIn("GH_TOKEN=[REDACTED]", redacted)
        self.assertIn("password: [REDACTED]", redacted)
        self.assertIn("Authorization: [REDACTED]", redacted)
        self.assertIn("Cookie: [REDACTED]", redacted)
        self.assertIn("https://[REDACTED]@example.test/org/repo.git", redacted)
        self.assertIn("--token [REDACTED]", redacted)
        self.assertIn("--password=[REDACTED]", redacted)
        self.assertIn("regular_log=line-ok", redacted)

    def test_artifact_store_writes_redacted_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp) / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            store = ArtifactStore(settings.data_root, db)

            path = store.write_text(
                "run-1",
                "log",
                "secret.log",
                "secret=should-not-persist\nrepo=https://user:pass@example.test/repo.git\nsafe=ok\n",
            )

            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("should-not-persist", saved)
            self.assertNotIn("user:pass", saved)
            self.assertIn("secret=[REDACTED]", saved)
            self.assertIn("repo=https://[REDACTED]@example.test/repo.git", saved)
            self.assertIn("safe=ok", saved)

    def test_artifact_store_rejects_path_like_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp) / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            store = ArtifactStore(settings.data_root, db)

            for name in ["../secret.txt", "nested/log.txt", "nested\\log.txt", "\x01.log"]:
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        store.write_text("run-1", "log", name, "unsafe\n")

    def test_artifact_store_rejects_symlink_artifact_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            db = Database(settings.database_path)
            db.initialize()
            store = ArtifactStore(settings.data_root, db)
            run_dir = settings.data_root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            outside = root / "outside-artifacts"
            outside.mkdir()
            (run_dir / "artifacts").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "artifact 디렉터리는 symlink일 수 없습니다"):
                store.write_text("run-1", "log", "trace.log", "unsafe\n")

            self.assertEqual(list(outside.iterdir()), [])

    def test_read_artifact_text_rejects_paths_outside_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            outside = root / "outside.log"
            outside.write_text("do not expose\n", encoding="utf-8")
            artifact = Artifact(
                id=1,
                run_id="run-1",
                kind="log",
                name="outside.log",
                path=outside,
                created_at="",
            )

            with self.assertRaises(ValueError):
                read_artifact_text(settings.data_root, artifact)

    def test_read_artifact_text_rejects_symlink_artifact_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root / "devauto")
            artifacts_dir = settings.data_root / "runs" / "run-1" / "artifacts"
            artifacts_dir.mkdir(parents=True)
            outside = root / "outside.log"
            outside.write_text("do not expose\n", encoding="utf-8")
            link = artifacts_dir / "linked.log"
            link.symlink_to(outside)
            artifact = Artifact(
                id=1,
                run_id="run-1",
                kind="log",
                name="linked.log",
                path=link,
                created_at="",
            )

            with self.assertRaisesRegex(ValueError, "artifact path는 symlink일 수 없습니다"):
                read_artifact_text(settings.data_root, artifact)


if __name__ == "__main__":
    unittest.main()
