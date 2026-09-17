import os
import subprocess
import sys
from pathlib import Path


def test_pre_commit_does_not_leak_worktree_git_environment(tmp_path):
    hook = Path(".husky/pre-commit").resolve()
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], env=clean_env, stderr=subprocess.STDOUT
        )

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--quiet")
    (source / "file.txt").write_text("original\n")
    git(source, "add", "file.txt")
    git(source, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "commit", "--quiet", "-m", "initial")
    worktree = tmp_path / "worktree"
    git(source, "worktree", "add", "--quiet", "-b", "feat/hook-test", str(worktree))
    before = git(worktree, "config", "--local", "--list")
    source_head = git(source, "rev-parse", "HEAD")
    source_index = git(source, "ls-files", "--stage")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    installed_hook = hooks / "pre-commit"
    installed_hook.write_text(hook.read_text())
    installed_hook.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    make = fake_bin / "make"
    make.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "assert sys.argv[1:] == ['check']\n"
        "subprocess.run(['git', 'init', '--bare', '--quiet', 'fixture.git'], check=True)\n"
        "subprocess.run(['git', '-C', 'fixture.git', 'config', 'user.name', 'Leaked fixture'], check=True)\n"
    )
    make.chmod(0o755)
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgsign=false",
         "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
         "commit", "--allow-empty", "--quiet", "-m", "test hook"],
        cwd=worktree, capture_output=True,
        env={**clean_env, "PATH": f"{fake_bin}{os.pathsep}{clean_env['PATH']}"},
    )
    assert result.returncode == 0, result.stderr.decode()
    assert git(worktree, "config", "--local", "--list") == before
    assert git(source, "rev-parse", "HEAD") == source_head
    assert git(source, "ls-files", "--stage") == source_index
    assert (worktree / "fixture.git" / "HEAD").is_file()
