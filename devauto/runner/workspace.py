from __future__ import annotations

from pathlib import Path

from devauto.core.models import ProjectConfig, Run
from devauto.runner.subprocesses import run_args, sanitized_child_env


class WorkspaceError(RuntimeError):
    pass


def expected_workspace_path(data_root: Path, run_id: str) -> Path:
    return data_root.expanduser().resolve() / "runs" / run_id / "workspace"


def workspace_path_rejection(data_root: Path, run_id: str, workspace: Path | None) -> str | None:
    if workspace is None:
        return None
    expected = expected_workspace_path(data_root, run_id)
    actual = workspace.expanduser().resolve(strict=False)
    expected_resolved = expected.resolve(strict=False)
    if actual != expected_resolved:
        return "workspace path가 run data root 밖에 있습니다"
    if path_contains_symlink(expected, data_root.expanduser().resolve()):
        return "workspace path에 symlink가 포함되어 있습니다"
    if not expected.exists():
        return "workspace path가 존재하지 않습니다"
    if not expected.is_dir():
        return "workspace path가 디렉터리가 아닙니다"
    return None


def workspace_display_path(data_root: Path, run_id: str, workspace: Path | None) -> str | None:
    if workspace is None:
        return None
    if workspace_path_rejection(data_root, run_id, workspace):
        return "사용 불가"
    return str(expected_workspace_path(data_root, run_id))


def path_contains_symlink(path: Path, root: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return True
    current = root.absolute()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def provision_workspace(data_root: Path, run: Run, project: ProjectConfig) -> Path:
    data_root = data_root.resolve()
    runs_root = data_root / "runs"
    if runs_root.is_symlink():
        raise WorkspaceError("runs 디렉터리는 symlink일 수 없습니다")
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = runs_root / run.id
    if run_dir.is_symlink():
        raise WorkspaceError("run 디렉터리는 symlink일 수 없습니다")
    run_dir = run_dir.resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise WorkspaceError("run 디렉터리는 data root 안에 있어야 합니다") from exc

    workspace = run_dir / "workspace"
    env = sanitized_child_env()
    expected_branch = f"{project.branch_prefix}{run.id}"
    if workspace.is_symlink():
        raise WorkspaceError("workspace path는 symlink일 수 없습니다")
    if workspace.exists():
        validate_existing_workspace(workspace, expected_branch, env)
        return workspace
    run_dir.mkdir(parents=True, exist_ok=True)
    if not project.repo_url:
        raise WorkspaceError("project repo.url이 필요합니다")

    clone_cmd = ["git", "clone"]
    if not Path(project.repo_url).expanduser().exists():
        clone_cmd.extend(["--depth", "50"])
    clone_cmd.extend([project.repo_url, str(workspace)])
    clone = run_args(clone_cmd, cwd=run_dir, timeout_sec=1800, env=env)
    if clone.exit_code != 0:
        raise WorkspaceError(clone.combined_output)

    checkout = run_args(["git", "checkout", run.target_branch], cwd=workspace, env=env)
    if checkout.exit_code != 0:
        raise WorkspaceError(checkout.combined_output)

    branch_name = f"{project.branch_prefix}{run.id}"
    branch = run_args(["git", "checkout", "-b", branch_name], cwd=workspace, env=env)
    if branch.exit_code != 0:
        raise WorkspaceError(branch.combined_output)
    return workspace


def validate_existing_workspace(workspace: Path, expected_branch: str, env: dict[str, str]) -> None:
    if not workspace.is_dir():
        raise WorkspaceError("기존 workspace path는 디렉터리여야 합니다")
    workspace_root = workspace.resolve()
    try:
        workspace_root.relative_to(workspace.parent.resolve())
    except ValueError as exc:
        raise WorkspaceError("workspace path는 해당 run 디렉터리 안에 있어야 합니다") from exc

    root = run_args(["git", "rev-parse", "--show-toplevel"], cwd=workspace, env=env)
    if root.exit_code != 0:
        raise WorkspaceError("기존 workspace가 git worktree가 아닙니다")
    git_root = Path(root.combined_output.strip()).resolve()
    if git_root != workspace_root:
        raise WorkspaceError("기존 workspace는 git worktree root여야 합니다")

    branch = run_args(["git", "branch", "--show-current"], cwd=workspace, env=env)
    if branch.exit_code != 0:
        raise WorkspaceError("기존 workspace branch를 확인할 수 없습니다")
    current_branch = branch.combined_output.strip()
    if current_branch != expected_branch:
        raise WorkspaceError(
            f"기존 workspace branch는 {expected_branch}여야 합니다. 현재 branch: {current_branch or 'detached'}"
        )
