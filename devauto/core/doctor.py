from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from devauto.core.models import ProjectConfig
from devauto.core.policy import command_is_allowed, output_command_is_allowed
from devauto.runner.subprocesses import run_args, sanitized_child_env


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str

    def to_mapping(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class DoctorReport:
    status: str
    checks: list[DoctorCheck]

    def to_mapping(self) -> dict[str, object]:
        return {"status": self.status, "checks": [check.to_mapping() for check in self.checks]}


def run_project_doctor(project: ProjectConfig, target_branch: str | None = None) -> DoctorReport:
    branch = target_branch or project.default_branch
    checks = [
        check_repo(project, branch),
        check_git(),
        *check_docker(project),
        *check_commands(project),
        *check_ai(project),
        check_publish(project),
    ]
    if any(check.status == "fail" for check in checks):
        status = "fail"
    elif any(check.status == "warn" for check in checks):
        status = "warn"
    else:
        status = "pass"
    return DoctorReport(status=status, checks=checks)


def format_doctor_report(report: DoctorReport) -> str:
    rows = "\n".join(f"- {check.status}: {check.name} - {check.message}" for check in report.checks)
    return f"""# 프로젝트 진단

## 상태
{report.status}

## 검사
{rows}
"""


def check_repo(project: ProjectConfig, branch: str | None = None) -> DoctorCheck:
    if not project.repo_url:
        return DoctorCheck("repo.url", "fail", "repo.url이 필요합니다")
    branch = branch or project.default_branch
    path = Path(project.repo_url).expanduser()
    if path.exists():
        git_dir = path / ".git"
        if not git_dir.exists():
            return DoctorCheck("repo.url", "fail", f"local path는 존재하지만 git repo가 아닙니다: {path}")
        branch_check = run_args(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=path,
            env=sanitized_child_env(),
        )
        if branch_check.exit_code != 0:
            check_name = "repo.default_branch" if branch == project.default_branch else "repo.target_branch"
            return DoctorCheck(check_name, "fail", f"local repo에서 branch를 찾을 수 없습니다: {branch}")
        return DoctorCheck("repo.url", "pass", f"local git repo를 사용할 수 있습니다: {path}")
    if "://" in project.repo_url or project.repo_url.startswith("git@"):
        return DoctorCheck("repo.url", "warn", "remote repo URL은 clone 없이 검증할 수 없습니다")
    return DoctorCheck("repo.url", "fail", f"local repo path가 존재하지 않습니다: {path}")


def check_git() -> DoctorCheck:
    path = shutil.which("git")
    if path:
        return DoctorCheck("git", "pass", f"git 발견: {path}")
    return DoctorCheck("git", "fail", "git 실행 파일을 찾을 수 없습니다")


def check_docker(project: ProjectConfig) -> list[DoctorCheck]:
    if not project.docker.enabled:
        return [DoctorCheck("docker", "pass", "이 프로젝트는 docker가 비활성화되어 있습니다")]

    checks: list[DoctorCheck] = []
    docker_path = shutil.which("docker")
    if docker_path:
        checks.append(DoctorCheck("docker", "pass", f"docker 발견: {docker_path}"))
        compose = run_args(
            ["docker", "compose", "version"],
            cwd=Path.cwd(),
            timeout_sec=20,
            env=sanitized_child_env(),
        )
        if compose.exit_code == 0:
            checks.append(DoctorCheck("docker.compose", "pass", compose.combined_output.strip() or "docker compose가 동작합니다"))
        else:
            checks.append(DoctorCheck("docker.compose", "fail", compose.combined_output or "docker compose 실패"))
    else:
        checks.append(DoctorCheck("docker", "fail", "docker 실행 파일을 찾을 수 없습니다"))
        checks.append(DoctorCheck("docker.compose", "fail", "docker 없이 docker compose를 확인할 수 없습니다"))

    if project.docker.compose_files:
        repo_path = Path(project.repo_url).expanduser()
        if repo_path.exists():
            for compose_file in project.docker.compose_files:
                path = repo_path / compose_file
                checks.append(
                    DoctorCheck(
                        f"docker.compose_file:{compose_file}",
                        "pass" if path.exists() else "fail",
                        f"{'발견' if path.exists() else '없음'}: {path}",
                    )
                )
        else:
            checks.append(DoctorCheck("docker.compose_files", "warn", "compose file은 clone 후 확인합니다"))
    else:
        checks.append(DoctorCheck("docker.compose_files", "warn", "docker는 활성화되어 있지만 compose file이 설정되지 않았습니다"))
    if project.docker.env_file:
        checks.append(check_docker_env_file(project))
    return checks


def check_docker_env_file(project: ProjectConfig) -> DoctorCheck:
    env_file = Path(os.path.expandvars(project.docker.env_file or "")).expanduser()
    if not env_file.is_absolute():
        repo_path = Path(project.repo_url).expanduser()
        if not repo_path.exists():
            return DoctorCheck(
                "docker.env_file",
                "warn",
                f"relative env_file은 clone 후 확인합니다: {project.docker.env_file}",
            )
        env_file = repo_path / env_file
    if not env_file.exists():
        return DoctorCheck("docker.env_file", "fail", f"없음: {env_file}")
    if not env_file.is_file():
        return DoctorCheck("docker.env_file", "fail", f"파일이 아닙니다: {env_file}")
    return DoctorCheck("docker.env_file", "pass", f"발견: {env_file}")


def check_commands(project: ProjectConfig) -> list[DoctorCheck]:
    if not project.commands:
        return [DoctorCheck("commands", "warn", "deterministic gate command가 설정되지 않았습니다")]
    checks: list[DoctorCheck] = []
    for name, command in project.commands.items():
        if command_is_allowed(command, project.policy):
            checks.append(DoctorCheck(f"command:{name}", "pass", command))
        else:
            checks.append(DoctorCheck(f"command:{name}", "fail", f"금지된 command: {command}"))
    return checks


def check_ai(project: ProjectConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for role, config in sorted(project.ai.items()):
        if not config.command:
            checks.append(DoctorCheck(f"ai:{role}", "warn", "command가 설정되지 않았습니다"))
            continue
        command = " ".join(config.command)
        if not command_is_allowed(command, project.policy):
            checks.append(DoctorCheck(f"ai:{role}", "fail", f"금지된 command: {command}"))
            continue
        executable = config.command[0]
        path = Path(executable).expanduser()
        if path.exists() or shutil.which(executable):
            checks.append(DoctorCheck(f"ai:{role}", "pass", command))
        else:
            checks.append(DoctorCheck(f"ai:{role}", "fail", f"실행 파일을 찾을 수 없습니다: {executable}"))
    if not checks:
        checks.append(DoctorCheck("ai", "warn", "AI role이 설정되지 않았습니다"))
    return checks


def check_publish(project: ProjectConfig) -> DoctorCheck:
    if project.publish.mode not in {"patch_only", "local_branch", "push_branch", "deploy_command"}:
        return DoctorCheck("publish.mode", "fail", f"지원하지 않는 publish mode: {project.publish.mode}")
    if project.publish.require_qa_approval and (not project.docker.enabled or not project.docker.compose_files):
        return DoctorCheck("publish.require_qa_approval", "fail", "QA approval에는 Docker Compose preview가 필요합니다")
    if project.publish.mode == "push_branch" and not project.publish.require_human_approval:
        return DoctorCheck("publish.require_human_approval", "fail", "push_branch에는 publish approval이 필요합니다")
    if project.publish.mode == "deploy_command":
        if not project.publish.require_human_approval:
            return DoctorCheck("publish.require_human_approval", "fail", "deploy_command에는 publish approval이 필요합니다")
        if not project.publish.deploy_command:
            return DoctorCheck("publish.deploy_command", "fail", "deploy_command mode에는 publish.deploy_command가 필요합니다")
        if not output_command_is_allowed(project.publish.deploy_command, project.policy):
            return DoctorCheck("publish.deploy_command", "fail", f"금지된 deploy command: {project.publish.deploy_command}")
    return DoctorCheck("publish", "pass", f"mode={project.publish.mode}")
