"""Copy synthetic board sources into a new, credential-free fixture repository."""

import os
from pathlib import Path
import shutil
import subprocess


def prepare_board(destination):
    destination = Path(destination).resolve()
    template = Path(__file__).resolve().parents[1] / "fixtures" / "board"
    if any(path.is_symlink() for path in template.rglob("*")):
        raise ValueError("Board fixture must not contain symlinks")
    shutil.copytree(template, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".aiqa-output", ".git"))
    env = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Board Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Board Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    def git(*args):
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(destination), "-c", "core.hooksPath=/dev/null", *args],
            env=env, text=True, stderr=subprocess.PIPE, timeout=10,
        ).strip()
    git("init", "-b", "main")
    git("add", ".")
    git("commit", "-m", "Synthetic board baseline")
    return {"source": str(destination), "source_commit": git("rev-parse", "HEAD")}


def verification_command(python="python3"):
    report = ".aiqa-output/board-baseline/report.xml"
    return {
        "command_id": "board-baseline", "display_name": "게시판 기존 기능 회귀",
        "kind": "TEST", "parser_id": "junit-xml", "parser_version": 1,
        "argv": [python, "-I", "baseline_checks.py", "--transport", "wsgi", "--junit", report], "cwd": ".",
        "report_path": report, "generated_paths": [".aiqa-output"],
        "timeout_sec": 60, "required": True,
    }
