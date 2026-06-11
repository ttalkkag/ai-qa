from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from devauto.core.models import ProjectConfig, Run
from devauto.core.policy import output_command_is_allowed
from devauto.runner.subprocesses import CommandResult, run_args, run_shell, sanitized_child_env


@dataclass(frozen=True)
class PublishResult:
    ok: bool
    mode: str
    summary: str
    branch: str | None = None
    commit_hash: str | None = None
    command: str | None = None


class Publisher:
    def publish(self, run: Run, project: ProjectConfig, workspace: Path) -> PublishResult:
        if project.publish.mode == "patch_only":
            return PublishResult(ok=True, mode="patch_only", summary="Patch artifact가 준비되었습니다. commit은 생성하지 않았습니다.")
        if project.publish.mode == "local_branch":
            return self._publish_local_branch(run, project, workspace)
        if project.publish.mode == "push_branch":
            return self._publish_push_branch(run, project, workspace)
        if project.publish.mode == "deploy_command":
            return self._publish_deploy_command(run, project, workspace)
        return PublishResult(
            ok=False,
            mode=project.publish.mode,
            summary=f"지원하지 않는 publish mode입니다: {project.publish.mode}",
        )

    def _publish_local_branch(self, run: Run, project: ProjectConfig, workspace: Path) -> PublishResult:
        branch = current_branch(workspace)
        expected_branch = expected_workspace_branch(run, project)
        if branch != expected_branch:
            return PublishResult(
                ok=False,
                mode="local_branch",
                summary=f"예상하지 못한 workspace branch라 publish를 거부합니다: {branch or '알 수 없음'}; 예상값 {expected_branch}",
                branch=branch,
            )

        add = run_git(["git", "add", "--all"], workspace)
        if add.exit_code != 0:
            return PublishResult(ok=False, mode="local_branch", summary=add.combined_output, branch=branch)

        staged = run_git(["git", "diff", "--cached", "--quiet"], workspace)
        if staged.exit_code == 0:
            return PublishResult(
                ok=True,
                mode="local_branch",
                summary="commit할 변경이 없습니다. workspace branch를 검토할 수 있습니다.",
                branch=branch,
            )

        self._ensure_git_identity(workspace)
        message = render_commit_message(project.publish.commit_message_template, run, project)
        commit = run_git(["git", "commit", "-m", message], workspace)
        if commit.exit_code != 0:
            return PublishResult(ok=False, mode="local_branch", summary=commit.combined_output, branch=branch)

        commit_hash = run_git(["git", "rev-parse", "HEAD"], workspace).combined_output.strip()
        return PublishResult(
            ok=True,
            mode="local_branch",
            summary="격리된 workspace branch에 변경을 commit했습니다. origin으로 push하지 않았습니다.",
            branch=branch,
            commit_hash=commit_hash,
        )

    def _publish_push_branch(self, run: Run, project: ProjectConfig, workspace: Path) -> PublishResult:
        if not project.publish.require_human_approval:
            return PublishResult(
                ok=False,
                mode="push_branch",
                summary="Output Layer가 origin으로 push하기 전에 push_branch는 publish approval이 필요합니다.",
            )

        local_result = self._publish_local_branch(run, project, workspace)
        branch = local_result.branch or current_branch(workspace)
        commit_hash = local_result.commit_hash or head_hash(workspace)
        if not local_result.ok:
            return PublishResult(
                ok=False,
                mode="push_branch",
                summary=local_result.summary,
                branch=branch,
                commit_hash=commit_hash,
            )
        if not branch:
            return PublishResult(ok=False, mode="push_branch", summary="workspace branch를 확인할 수 없습니다")
        expected_branch = expected_workspace_branch(run, project)
        if branch != expected_branch:
            return PublishResult(
                ok=False,
                mode="push_branch",
                summary=f"예상하지 못한 workspace branch라 push를 거부합니다: {branch}; 예상값 {expected_branch}",
                branch=branch,
                commit_hash=commit_hash,
            )

        exists = run_git(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], workspace)
        if exists.exit_code == 0:
            return PublishResult(
                ok=False,
                mode="push_branch",
                summary=f"remote branch가 이미 존재합니다: {branch}",
                branch=branch,
                commit_hash=commit_hash,
            )
        if exists.exit_code not in {2}:
            return PublishResult(
                ok=False,
                mode="push_branch",
                summary=exists.combined_output or "remote branch 존재 여부를 확인하지 못했습니다",
                branch=branch,
                commit_hash=commit_hash,
            )

        refspec = f"HEAD:refs/heads/{branch}"
        push = run_git(["git", "push", "origin", refspec], workspace)
        return PublishResult(
            ok=push.exit_code == 0,
            mode="push_branch",
            summary=(
                f"격리된 workspace branch를 origin의 `{branch}`로 push했습니다."
                if push.exit_code == 0
                else push.combined_output
            ),
            branch=branch,
            commit_hash=commit_hash,
            command=f"git push origin {refspec}",
        )

    def _publish_deploy_command(self, run: Run, project: ProjectConfig, workspace: Path) -> PublishResult:
        command = render_shell_command(project.publish.deploy_command, run, project)
        if not command:
            return PublishResult(ok=False, mode="deploy_command", summary="publish.deploy_command가 비어 있습니다")
        if not project.publish.require_human_approval:
            return PublishResult(
                ok=False,
                mode="deploy_command",
                command=command,
                summary="Output Layer 실행 전에 deploy_command는 publish approval이 필요합니다.",
            )
        if not output_command_is_allowed(command, project.policy):
            return PublishResult(
                ok=False,
                mode="deploy_command",
                command=command,
                summary=f"금지된 deploy command입니다: {command}",
            )

        result = run_shell(
            command,
            cwd=workspace,
            timeout_sec=project.publish.deploy_timeout_sec,
            env=sanitized_child_env(),
        )
        ok = result.exit_code == 0
        summary = (
            "Deploy command가 성공적으로 완료되었습니다."
            if ok
            else f"Deploy command가 exit code {result.exit_code}로 실패했습니다."
        )
        if result.combined_output:
            summary = f"{summary}\n\n{result.combined_output}"
        return PublishResult(ok=ok, mode="deploy_command", command=command, summary=summary)

    def _ensure_git_identity(self, workspace: Path) -> None:
        name = run_git(["git", "config", "user.name"], workspace)
        if name.exit_code != 0 or not name.combined_output.strip():
            run_git(["git", "config", "user.name", "devauto"], workspace)
        email = run_git(["git", "config", "user.email"], workspace)
        if email.exit_code != 0 or not email.combined_output.strip():
            run_git(["git", "config", "user.email", "devauto@localhost"], workspace)


def render_commit_message(template: str, run: Run, project: ProjectConfig) -> str:
    return render_command(template, run, project)


def render_shell_command(template: str, run: Run, project: ProjectConfig) -> str:
    return render_command(template, run, project, quote_values=True)


def render_command(template: str, run: Run, project: ProjectConfig, quote_values: bool = False) -> str:
    run_id = quote_runtime_value(run.id, quote_values)
    title = quote_runtime_value(run.title, quote_values)
    project_id = quote_runtime_value(project.id, quote_values)
    return (
        template.replace("${RUN_ID}", run_id)
        .replace("${TASK_TITLE}", title)
        .replace("${PROJECT_ID}", project_id)
    )


def quote_runtime_value(value: str, enabled: bool) -> str:
    return shlex.quote(value) if enabled else value


def current_branch(workspace: Path) -> str:
    return run_git(["git", "branch", "--show-current"], workspace).combined_output.strip()


def expected_workspace_branch(run: Run, project: ProjectConfig) -> str:
    return f"{project.branch_prefix}{run.id}"


def head_hash(workspace: Path) -> str:
    return run_git(["git", "rev-parse", "HEAD"], workspace).combined_output.strip()


def run_git(args: list[str], workspace: Path) -> CommandResult:
    return run_args(args, cwd=workspace, env=sanitized_child_env())
