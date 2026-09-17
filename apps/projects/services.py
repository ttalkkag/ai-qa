import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.services import require_access
from apps.errors import DomainError
from .models import DependencyBundle, PreparationCommand, Project, ProjectAudit, ProjectConfigRevision, ProjectHealth, VerificationCommand


DEFAULT_EXCLUDED_PATHS = [
    ".git", ".env", ".env.*", "**/.env", "**/.env.*", "*.pem", "**/*.pem",
    "*.key", "**/*.key", "id_rsa*", "**/id_rsa*", "id_ed25519*", "**/id_ed25519*",
    "credentials.json", "**/credentials.json", "auth.json", "**/auth.json",
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build",
    ".aiqa-inputs", ".aiqa-output",
]
RESERVED_PATHS = (".aiqa-inputs", ".aiqa-output")
BRANCH_NAME_PATTERN = r"(?:main|master|develop|staging|[a-z]+/[a-z0-9][a-z0-9._\-]*(?:/[a-z0-9][a-z0-9._\-]*)*)"
BRANCH_INPUT_PATTERN = r"(?!.*\.\.)(?!.*\.lock(?:/|$))(?!.*\.$)(?:refs/heads/|(?!refs/))" + BRANCH_NAME_PATTERN
BRANCH_NAME_MESSAGE = "main·master·develop·staging 또는 소문자 type/description 형식을 사용하세요 예: temp/ai-qa-testing, fix/api/login"
COMMAND_FIELDS = ("command_id", "display_name", "kind", "parser_id", "parser_version", "report_path", "argv", "cwd", "timeout_sec", "required", "generated_paths", "order")
PREPARATION_FIELDS = ("command_id", "argv", "cwd", "timeout_sec", "dependency_bundle_id", "order")
GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LANG": "C", "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_PROTOCOL_FROM_USER": "0",
}


@dataclass(frozen=True)
class ExecutionInput:
    project: Project
    config_revision: ProjectConfigRevision
    base_commit_sha: str
    policy: dict


def _fail(code, message=None, status=400):
    raise DomainError(code, message, status=status)


def get_project(project_id, *, for_update=False):
    query = Project.objects.select_for_update() if for_update else Project.objects
    try:
        return query.get(pk=project_id)
    except (Project.DoesNotExist, ValidationError, ValueError, TypeError):
        _fail("NOT_FOUND", status=404)


def _git(repo, *args, allow_failure=False):
    executable = shutil.which("git", path=GIT_ENV["PATH"])
    if not executable:
        _fail("REPOSITORY_UNAVAILABLE", "호스트에 Git이 설치되어 있지 않습니다.")
    command = [executable, "--no-optional-locks", "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-c", "submodule.recurse=false", "-C", str(repo), *args]
    if args and args[0] == "status":
        names = _git(repo, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$", allow_failure=True)
        if names.returncode not in (0, 1):
            _fail("INVALID_REPOSITORY", "Git 파일 필터 구성을 안전하게 확인할 수 없습니다.")
        try:
            driver_names = names.stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeError:
            _fail("INVALID_REPOSITORY", "지원하지 않는 Git 파일 필터 구성이 있습니다.")
        filter_settings = []
        for name in driver_names:
            match = re.fullmatch(r"filter\.(.+)\.(?:clean|smudge|process|required)", name)
            if not match or _has_control(match[1]) or "=" in match[1]:
                _fail("INVALID_REPOSITORY", "지원하지 않는 Git 파일 필터 구성이 있습니다.")
            for key, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
                filter_settings.extend(["-c", "filter." + match[1] + "." + key + "=" + value])
        command[1:1] = filter_settings
    try:
        result = subprocess.run(command, capture_output=True, env=GIT_ENV.copy(), timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        _fail("REPOSITORY_UNAVAILABLE", "저장소를 제한 시간 안에 읽을 수 없습니다.")
    if len(result.stdout) > 8 * 1024 * 1024:
        _fail("REPOSITORY_UNAVAILABLE", "저장소 메타데이터가 로컬 점검 한도를 초과했습니다.")
    if result.returncode and not allow_failure:
        _fail("INVALID_REPOSITORY", "저장소 메타데이터를 읽을 수 없습니다.")
    return result


def _overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def validate_repository_path(local_path):
    if not isinstance(local_path, (str, Path)) or _has_control(str(local_path)):
        _fail("PATH_NOT_ALLOWED", "프로젝트 절대 경로를 입력하세요.")
    original = Path(local_path).expanduser()
    if not original.is_absolute():
        _fail("PATH_NOT_ALLOWED", "프로젝트는 절대 경로로 등록해야 합니다.")
    try:
        root = original.resolve(strict=True)
    except (OSError, RuntimeError):
        _fail("REPOSITORY_UNAVAILABLE", "저장소 디렉터리에 접근할 수 없습니다.")
    runtime = Path(settings.AIQA_DATA_DIR).resolve()
    if not root.is_dir() or root == Path(root.anchor) or root == Path.home().resolve() or _overlaps(root, runtime):
        _fail("PATH_NOT_ALLOWED", "이 위치는 프로젝트로 등록할 수 없습니다.")
    result = _git(root, "rev-parse", "--show-toplevel")
    try:
        top = Path(os.fsdecode(result.stdout).strip()).resolve(strict=True)
    except (ValueError, OSError, RuntimeError):
        _fail("INVALID_REPOSITORY", "정상 Git 프로젝트 root가 아닙니다.")
    if top != root:
        _fail("INVALID_REPOSITORY", "Git 프로젝트의 최상위 디렉터리를 등록하세요.")
    return root


def validate_source_ref(source_ref):
    if not isinstance(source_ref, str) or not source_ref or len(source_ref) > 255:
        _fail("INVALID_SOURCE_REF", "기준 로컬 브랜치명을 입력하세요.")
    branch = source_ref.removeprefix("refs/heads/")
    if (re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", branch)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or branch == "HEAD"
            or ".." in branch or "//" in branch or branch.endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
            or source_ref.startswith("refs/") and not source_ref.startswith("refs/heads/")):
        _fail("INVALID_SOURCE_REF", "로컬 브랜치명만 사용할 수 있습니다. 커밋 SHA와 Git 표현식은 입력할 수 없습니다.")
    return branch


def default_source_ref(root):
    for branch in ("develop", "main", "master"):
        if _git(root, "show-ref", "--verify", "--quiet", "refs/heads/" + branch, allow_failure=True).returncode == 0:
            return branch
    result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    if result.returncode == 0:
        branch = validate_source_ref(result.stdout.decode("utf-8").strip())
        if _git(root, "show-ref", "--verify", "--quiet", "refs/heads/" + branch, allow_failure=True).returncode == 0:
            return branch
    _fail("BASE_REF_NOT_FOUND", "develop·main·master 또는 현재 브랜치를 찾을 수 없습니다. 커밋이 있는 로컬 브랜치를 만들거나 기준 브랜치를 직접 입력하세요.")


def validate_requested_source_ref(source_ref):
    branch = validate_source_ref(source_ref)
    if not re.fullmatch(BRANCH_NAME_PATTERN, branch):
        _fail("INVALID_SOURCE_REF", BRANCH_NAME_MESSAGE)
    return branch


def resolve_base_sha(root, source_ref):
    source_ref = validate_source_ref(source_ref)
    ref = "refs/heads/" + source_ref
    result = _git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", allow_failure=True)
    if result.returncode:
        _fail("BASE_REF_NOT_FOUND", "기준 commit을 찾을 수 없습니다. 관리자가 local ref를 확인해야 합니다.")
    sha = result.stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        _fail("BASE_REF_NOT_FOUND", "정상 기준 commit을 확인할 수 없습니다.")
    return sha


def _ensure_source_branch(root, source_ref):
    try:
        resolve_base_sha(root, source_ref)
        return
    except DomainError as error:
        if error.code != "BASE_REF_NOT_FOUND":
            raise
    start = resolve_base_sha(root, default_source_ref(root))
    result = _git(root, "update-ref", "--no-deref", "refs/heads/" + validate_source_ref(source_ref),
                  start, "0" * len(start), allow_failure=True)
    if result.returncode:
        _fail("BRANCH_CREATE_FAILED", "로컬 브랜치를 생성하지 못했습니다. 같은 이름의 브랜치·경로 충돌과 저장소 권한을 확인하세요.", 409)


def committed_paths(root, sha):
    output = _git(root, "ls-tree", "-rz", "--full-tree", sha).stdout
    paths = []
    for entry in output.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.split(b" ")
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeError):
            _fail("UNSUPPORTED_SOURCE", "지원하지 않는 파일 이름이 저장소에 있습니다.")
        if _has_control(path):
            _fail("UNSUPPORTED_SOURCE", "제어 문자가 포함된 파일 이름은 지원하지 않습니다.")
        if mode == b"160000":
            _fail("UNSUPPORTED_SOURCE", "submodule은 이 로컬 실행에서 지원하지 않습니다.")
        if any(_within(path, reserved) for reserved in RESERVED_PATHS):
            _fail("RESERVED_PATH", "저장소가 앱 예약 경로를 사용하고 있습니다.")
        paths.append(path)
    return paths


def _has_control(value):
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _relative_path(value, *, allow_glob=False, allow_root=True):
    if not isinstance(value, str) or not value or len(value) > 1024 or _has_control(value) or "\\" in value:
        _fail("PATH_NOT_ALLOWED", "프로젝트 상대 경로 형식이 올바르지 않습니다.")
    value = value.rstrip("/")
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or not allow_root and str(path) == "."
            or not allow_glob and any(char in value for char in "*?[]")):
        _fail("PATH_NOT_ALLOWED", "경로 탈출이나 절대 경로를 사용할 수 없습니다.")
    return str(path)


def _within(path, allowed):
    return allowed == "." or path == allowed or path.startswith(allowed + "/")


def _excluded(path, pattern):
    return _within(path, pattern) or fnmatch.fnmatchcase(path, pattern) or any(
        fnmatch.fnmatchcase(part, pattern) for part in PurePosixPath(path).parts
    )


def file_allowed(path, policy, write=False):
    try:
        path = _relative_path(path)
    except DomainError:
        return False
    if any(part == ".git" for part in PurePosixPath(path).parts):
        return False
    return any(_within(path, item) for item in policy["write_paths" if write else "read_paths"]) and not any(
        _excluded(path, item) for item in policy["excluded_paths"]
    )


def _validate_path_list(values, root, *, allow_glob=False, allow_empty=False):
    if not isinstance(values, list) or len(values) > 100 or not values and not allow_empty:
        _fail("INVALID_POLICY", "경로 목록을 확인하세요.")
    result = []
    for value in values:
        normalized = _relative_path(value, allow_glob=allow_glob)
        if not any(char in normalized for char in "*?[]"):
            try:
                resolved = (root / normalized).resolve()
            except (OSError, RuntimeError):
                _fail("PATH_NOT_ALLOWED", "심볼릭 링크 경로를 확인할 수 없습니다.")
            if resolved != root and root not in resolved.parents:
                _fail("PATH_NOT_ALLOWED", "저장소 밖을 가리키는 경로는 허용하지 않습니다.")
        if normalized not in result:
            result.append(normalized)
    return result


def _command_base(command, root):
    if not isinstance(command, dict):
        _fail("INVALID_COMMAND", "명령은 구조화된 객체로 등록하세요.")
    command_id = command.get("command_id")
    if not isinstance(command_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", command_id):
        _fail("INVALID_COMMAND", "명령 ID는 64자 이내 영문·숫자·밑줄·하이픈이어야 합니다.")
    argv = command.get("argv")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or _has_control(arg) for arg in argv)):
        _fail("INVALID_COMMAND", "argv는 비어 있지 않은 문자열 배열이어야 합니다.")
    cwd = _relative_path(command.get("cwd", "."))
    _validate_path_list([cwd], root)
    junit_argument = None
    if command.get("parser_id") == "junit-xml" and command.get("report_path"):
        report_path = _relative_path(command["report_path"], allow_root=False)
        junit_argument = posixpath.relpath(report_path, cwd)
    executable = argv[0]
    executable_path = Path(executable)
    if executable_path.name.lower() in {"sh", "bash", "zsh", "dash", "fish", "csh", "ksh", "cmd", "cmd.exe", "powershell", "pwsh", "env", "sudo", "su", "ssh", "curl", "wget"}:
        _fail("INVALID_COMMAND", "셸·권한 변경·원격 실행 wrapper는 등록할 수 없습니다.")
    if executable_path.is_absolute():
        try:
            resolved = executable_path.resolve(strict=True)
        except (OSError, RuntimeError):
            _fail("INVALID_COMMAND", "호스트 실행 파일이 존재하지 않습니다.")
        if not resolved.is_file() or not os.access(resolved, os.X_OK) or _overlaps(resolved, root) or _overlaps(resolved, Path(settings.AIQA_DATA_DIR).resolve()):
            _fail("INVALID_COMMAND", "이 호스트 실행 파일은 사용할 수 없습니다.")
    elif "/" in executable:
        _relative_path(executable, allow_root=False)
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", executable):
        _fail("INVALID_COMMAND", "실행 파일 이름이 올바르지 않습니다.")
    for arg in argv:
        if any(marker in arg for marker in ("$(", "${", "`", "{{", "}}")) or re.search(r"(?i)(?:token|password|secret|api[_-]?key)\s*=|authorization\s*:|bearer\s|-----BEGIN .*PRIVATE KEY", arg) or re.search(r"https?://[^/\s]+:[^/\s]+@|\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)", arg):
            _fail("INVALID_COMMAND", "명령에 비밀값이나 요청 치환식을 넣을 수 없습니다.")
    for arg in argv[1:]:
        if arg.lower() in {"--token", "--password", "--secret", "--api-key", "--api_key"}:
            _fail("INVALID_COMMAND", "명령에 인증정보를 전달할 수 없습니다.")
        if "=" in arg:
            argument_value = arg.split("=", 1)[1]
        else:
            argument_value = arg
        if argument_value.startswith(("/", "~")) or ".." in PurePosixPath(argument_value).parts and argument_value != junit_argument:
            _fail("INVALID_COMMAND", "명령의 파일 인자는 작업 사본 상대 경로만 사용할 수 있습니다.")
        if str(root) in arg:
            _fail("INVALID_COMMAND", "명령은 원본 저장소 경로를 참조할 수 없습니다.")
    timeout = command.get("timeout_sec", 300)
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        _fail("INVALID_COMMAND", "명령 제한 시간은 1~1,800초여야 합니다.")
    return {"command_id": command_id, "argv": argv, "cwd": cwd, "timeout_sec": timeout}


def _normalize_commands(commands, root, paths):
    if not isinstance(commands, list) or len(commands) > 30:
        _fail("INVALID_COMMAND", "검증 명령은 최대 30개까지 등록할 수 있습니다.")
    result = []
    seen = set()
    for order, command in enumerate(commands):
        normalized = _command_base(command, root)
        if set(command) - set(COMMAND_FIELDS):
            _fail("INVALID_COMMAND", "알 수 없는 명령 설정이 있습니다.")
        if normalized["command_id"] in seen:
            _fail("INVALID_COMMAND", "명령 ID가 중복되었습니다.")
        seen.add(normalized["command_id"])
        kind = command.get("kind")
        parser_id = command.get("parser_id") or ""
        parser_version = command.get("parser_version")
        if not isinstance(parser_id, str):
            _fail("INVALID_COMMAND", "테스트 parser ID는 문자열이어야 합니다.")
        if kind not in VerificationCommand.Kind.values:
            _fail("INVALID_COMMAND", "명령 종류는 TEST 또는 COMMAND여야 합니다.")
        if kind == "TEST" and (parser_id not in {"pytest-summary", "junit-xml"} or type(parser_version) is not int or parser_version != 1):
            _fail("INVALID_COMMAND", "지원하는 테스트 parser와 버전 1을 지정하세요.")
        if kind == "COMMAND" and (parser_id or parser_version is not None):
            _fail("INVALID_COMMAND", "일반 명령에는 테스트 parser를 지정하지 않습니다.")
        display_name = command.get("display_name", normalized["command_id"])
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 120 or _has_control(display_name):
            _fail("INVALID_COMMAND", "검증 표시 이름을 확인하세요.")
        required = command.get("required", True)
        if type(required) is not bool:
            _fail("INVALID_COMMAND", "required는 true 또는 false여야 합니다.")
        generated = _validate_path_list(command.get("generated_paths", [".aiqa-output"]), root)
        if any(_within(path, generated_path) for path in paths for generated_path in generated):
            _fail("INVALID_COMMAND", "생성물 경로에 commit된 소스가 포함되어 있습니다.")
        report_path = command.get("report_path") or ""
        if parser_id == "junit-xml":
            report_path = _relative_path(report_path, allow_root=False)
            expected_root = ".aiqa-output/" + normalized["command_id"]
            if not _within(report_path, expected_root) or not report_path.endswith(".xml"):
                _fail("INVALID_COMMAND", "JUnit 보고서는 명령별 .aiqa-output/<명령 ID>/ 아래 고정 XML 경로여야 합니다.")
            if not any(_within(report_path, folder) for folder in generated):
                _fail("INVALID_COMMAND", "JUnit 보고서 경로는 생성 허용 경로 안에 있어야 합니다.")
            report_argument = posixpath.relpath(report_path, normalized["cwd"])
            if not any(arg == report_argument or arg.endswith("=" + report_argument) for arg in normalized["argv"]):
                _fail("INVALID_COMMAND", "명령 출력 경로와 JUnit report_path가 일치해야 합니다.")
        elif report_path:
            _fail("INVALID_COMMAND", "JUnit parser만 report_path를 사용합니다.")
        normalized.update(display_name=display_name.strip(), kind=kind, parser_id=parser_id,
                          parser_version=parser_version, report_path=report_path, required=required,
                          generated_paths=generated, order=order)
        result.append(normalized)
    return result


def _normalize_preparations(commands, root, project):
    if not isinstance(commands, list) or len(commands) > 20:
        _fail("INVALID_COMMAND", "준비 명령은 최대 20개까지 등록할 수 있습니다.")
    result = []
    seen = set()
    for order, command in enumerate(commands):
        normalized = _command_base(command, root)
        if set(command) - set(PREPARATION_FIELDS) or normalized["command_id"] in seen:
            _fail("INVALID_COMMAND", "준비 명령 설정이나 중복 ID를 확인하세요.")
        seen.add(normalized["command_id"])
        bundle_id = command.get("dependency_bundle_id")
        if bundle_id:
            try:
                bundle_id = str(uuid.UUID(str(bundle_id)))
                valid_bundle = project is not None and DependencyBundle.objects.filter(pk=bundle_id, project=project).exists()
            except (ValueError, TypeError, ValidationError):
                valid_bundle = False
            if not valid_bundle:
                _fail("DEPENDENCY_MISSING", "이 프로젝트에 등록된 의존성 묶음이 아닙니다.")
        normalized.update(dependency_bundle_id=bundle_id or None, order=order)
        result.append(normalized)
    return result


def normalize_configuration(root, source_ref, policy, *, project=None, allow_new_branch=False):
    if not isinstance(policy, dict) or set(policy) - {"source_ref", "read_paths", "write_paths", "excluded_paths", "verification_commands", "preparation_commands"}:
        _fail("INVALID_POLICY", "지원하는 작업 정책 필드만 입력하세요.")
    source_ref = policy.get("source_ref", source_ref)
    if source_ref in (None, ""):
        source_ref = default_source_ref(root)
    else:
        source_ref = validate_requested_source_ref(source_ref) if allow_new_branch else validate_source_ref(source_ref)
    read_paths = _validate_path_list(policy.get("read_paths", ["."]), root)
    write_paths = _validate_path_list(policy.get("write_paths", ["."]), root, allow_empty=True)
    excluded_paths = _validate_path_list(policy.get("excluded_paths", DEFAULT_EXCLUDED_PATHS), root, allow_glob=True, allow_empty=True)
    if any(not any(_within(write, read) for read in read_paths) for write in write_paths):
        _fail("INVALID_POLICY", "수정 허용 범위는 읽기 허용 범위 안에 있어야 합니다.")
    if ".git" not in excluded_paths:
        excluded_paths.append(".git")
    try:
        paths = committed_paths(root, resolve_base_sha(root, source_ref))
    except DomainError as error:
        if error.code != "BASE_REF_NOT_FOUND":
            raise
        paths = committed_paths(root, resolve_base_sha(root, default_source_ref(root))) if allow_new_branch else []
    return {
        "source_ref": source_ref, "read_paths": read_paths, "write_paths": write_paths,
        "excluded_paths": excluded_paths,
        "verification_commands": _normalize_commands(policy.get("verification_commands", []), root, paths),
        "preparation_commands": _normalize_preparations(policy.get("preparation_commands", []), root, project),
    }


def configuration_snapshot(revision):
    return {
        "source_ref": revision.source_ref, "read_paths": list(revision.read_paths),
        "write_paths": list(revision.write_paths), "excluded_paths": list(revision.excluded_paths),
        "verification_commands": list(revision.verification_commands.values(*COMMAND_FIELDS)),
        "preparation_commands": [
            {**record, "dependency_bundle_id": str(record["dependency_bundle_id"]) if record["dependency_bundle_id"] else None}
            for record in revision.preparation_commands.values(*PREPARATION_FIELDS)
        ],
    }


def _create_revision(project, actor, policy):
    revision = ProjectConfigRevision.objects.create(
        project=project, project_config_version=project.project_config_version,
        canonical_repo_path=project.canonical_repo_path, created_by=actor,
        **{key: policy[key] for key in ("source_ref", "read_paths", "write_paths", "excluded_paths")},
    )
    for command in policy["verification_commands"]:
        VerificationCommand.objects.create(config_revision=revision, **command)
    for command in policy["preparation_commands"]:
        PreparationCommand.objects.create(config_revision=revision, **command)
    return revision


def assert_project_active(project):
    if project.state != Project.State.ACTIVE:
        _fail("PROJECT_ARCHIVED", "아카이브된 프로젝트는 새 작업을 시작할 수 없습니다.", 409)
    return project


def is_project_busy(project_id):
    try:
        job = apps.get_model("runs", "Job")
    except LookupError:
        return False
    if job.objects.filter(project_id=project_id, state="RUNNING").exists():
        return True
    process = apps.get_model("runs", "ProcessRecord")
    return process.objects.filter(job__project_id=project_id, ended_at__isnull=True).exclude(state__in=["STOPPED", "NOT_STARTED", "ABANDONED"]).exists()


def register_project(admin, name, local_path, source_ref=None, description="", policy=None):
    actor = require_access(admin, "project.manage")
    name, description = _project_labels(name, description)
    root = validate_repository_path(local_path)
    normalized = normalize_configuration(root, source_ref, {} if policy is None else policy, allow_new_branch=True)
    try:
        with transaction.atomic():
            actor = require_access(actor, "project.manage")
            project = Project.objects.create(name=name, description=description, canonical_repo_path=str(root), created_by=actor)
            _create_revision(project, actor, normalized)
            ProjectAudit.objects.create(project=project, actor=actor, action="registered", to_version=1)
            _ensure_source_branch(root, normalized["source_ref"])
            _inspect(project)
    except IntegrityError:
        _fail("PROJECT_ALREADY_REGISTERED", "이 저장소는 이미 등록되어 있습니다.", 409)
    return project


def _project_labels(name, description):
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or _has_control(name):
        _fail("INVALID_INPUT", "프로젝트 이름은 120자 이내로 입력하세요.")
    if not isinstance(description, str) or len(description) > 4000:
        _fail("INVALID_INPUT", "설명은 4,000자 이내로 입력하세요.")
    return name.strip(), description.strip()


@transaction.atomic
def update_configuration(admin, project_id, expected_version, policy, local_path=None):
    actor = require_access(admin, "project.manage", project_id=project_id)
    project = get_project(project_id, for_update=True)
    if type(expected_version) is not int or project.project_config_version != expected_version:
        _fail("VERSION_CONFLICT", "프로젝트 설정이 변경되었습니다. 새로고침 후 확인하세요.", 409)
    if is_project_busy(project_id):
        _fail("PROJECT_BUSY", "진행 중인 프로젝트 작업이 종료된 뒤 설정을 변경하세요.", 409)
    root = validate_repository_path(local_path if local_path is not None else project.canonical_repo_path)
    if Project.objects.exclude(pk=project.pk).filter(canonical_repo_path=str(root)).exists():
        _fail("PROJECT_ALREADY_REGISTERED", "이 저장소는 이미 등록되어 있습니다.", 409)
    normalized = normalize_configuration(root, project.current_revision.source_ref, policy, project=project, allow_new_branch=True)
    project.canonical_repo_path = str(root)
    project.project_config_version += 1
    project.save(update_fields=["canonical_repo_path", "project_config_version", "updated_at"])
    revision = _create_revision(project, actor, normalized)
    ProjectAudit.objects.create(project=project, actor=actor, action="configuration_changed", from_version=expected_version, to_version=project.project_config_version)
    _ensure_source_branch(root, normalized["source_ref"])
    _inspect(project)
    return revision


@transaction.atomic
def update_project_details(admin, project_id, name, description=""):
    actor = require_access(admin, "project.manage", project_id=project_id)
    project = get_project(project_id, for_update=True)
    project.name, project.description = _project_labels(name, description)
    project.save(update_fields=["name", "description", "updated_at"])
    ProjectAudit.objects.create(project=project, actor=actor, action="details_changed")
    return project


def _inspect(project):
    reasons = []
    root = ""
    sha = ""
    dirty_count = 0
    source_ready = False
    readiness = ProjectHealth.Readiness.SETUP_REQUIRED
    revision = project.current_revision
    try:
        root = validate_repository_path(project.canonical_repo_path)
        sha = resolve_base_sha(root, revision.source_ref)
        paths = committed_paths(root, sha)
        policy = configuration_snapshot(revision)
        normalize_configuration(root, revision.source_ref, policy, project=project)
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        entries = iter(status.split(b"\0"))
        for entry in entries:
            if not entry:
                continue
            dirty_count += 1
            if entry[:1] in {b"R", b"C"} or entry[1:2] in {b"R", b"C"}:
                next(entries, None)
        source_ready = True
        for command in policy["verification_commands"] + policy["preparation_commands"]:
            cwd = command["cwd"]
            if cwd != "." and not any(_within(path, cwd) for path in paths):
                reasons.append("COMMAND_CWD_MISSING")
            dependencies = [command["argv"][0], cwd]
            for argument in command["argv"][1:]:
                value = argument.split("=", 1)[-1]
                if not value.startswith("-"):
                    dependencies.append(str(PurePosixPath(cwd) / value))
            for dependency in dependencies:
                if dependency != "." and not Path(dependency).is_absolute() and any(_within(path, dependency) for path in paths) and not file_allowed(dependency, policy):
                    reasons.append("COMMAND_SOURCE_NOT_READABLE")
        if not revision.verification_commands.filter(required=True).exists():
            reasons.append("REQUIRED_CHECK_MISSING")
        if not revision.write_paths:
            reasons.append("WRITE_SCOPE_EMPTY")
    except DomainError as error:
        reasons.append(error.code)
        if error.code in {"REPOSITORY_UNAVAILABLE", "INVALID_REPOSITORY"}:
            readiness = ProjectHealth.Readiness.UNAVAILABLE
    if project.state != Project.State.ACTIVE:
        reasons.append("PROJECT_ARCHIVED")
        source_ready = False
    reasons = list(dict.fromkeys(reasons))
    if source_ready and not reasons:
        readiness = ProjectHealth.Readiness.READY
    health, _ = ProjectHealth.objects.update_or_create(project=project, defaults={
        "actual_repo_path": str(root), "base_commit_sha": sha, "is_dirty": bool(dirty_count),
        "dirty_count": dirty_count, "readiness": readiness, "source_ready": source_ready,
        "modification_ready": source_ready and not reasons, "blocking_reasons": reasons,
    })
    return health


def inspect_project(actor, project_id):
    require_access(actor, "project.inspect", project_id=project_id)
    return _inspect(get_project(project_id))


def resolve_execution_input(actor, project_id, expected_version=None, expected_sha=None, *, require_modification=True):
    require_access(actor, "project.view", project_id=project_id)
    project = get_project(project_id)
    assert_project_active(project)
    if expected_version is not None and project.project_config_version != expected_version:
        _fail("VERSION_CONFLICT", "프로젝트 설정이 바뀌었습니다. 명세를 다시 확인하세요.", 409)
    health = _inspect(project)
    if not health.source_ready or require_modification and not health.modification_ready:
        _fail("PROJECT_NOT_READY", "프로젝트 준비 항목을 관리자가 확인해야 합니다.", 409)
    if expected_sha is not None and health.base_commit_sha != expected_sha:
        _fail("BASE_CHANGED", "기준 코드가 바뀌었습니다. 새로운 기준으로 명세를 다시 확인하세요.", 409)
    revision = project.current_revision
    return ExecutionInput(project, revision, health.base_commit_sha, configuration_snapshot(revision))


@transaction.atomic
def set_project_state(admin, project_id, state, reason):
    actor = require_access(admin, "project.manage", project_id=project_id)
    if state not in Project.State.values or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        _fail("INVALID_INPUT", "프로젝트 상태와 변경 사유를 확인하세요.")
    project = get_project(project_id, for_update=True)
    if project.state == state:
        return project
    if is_project_busy(project_id):
        _fail("PROJECT_BUSY", "진행 중인 프로젝트 작업이 종료된 뒤 상태를 변경하세요.", 409)
    project.state = state
    project.save(update_fields=["state", "updated_at"])
    ProjectAudit.objects.create(project=project, actor=actor, action="state_changed", reason=reason.strip())
    _inspect(project)
    return project


def bundle_manifest(path):
    root = Path(path).resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor) or root == Path.home().resolve():
        _fail("PATH_NOT_ALLOWED", "의존성 자료 전용 디렉터리를 지정하세요.")
    manifest = []
    total_size = 0
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            _fail("PATH_NOT_ALLOWED", "의존성 묶음에는 심볼릭 링크를 사용할 수 없습니다.")
        if item.is_dir():
            continue
        if not item.is_file():
            _fail("PATH_NOT_ALLOWED", "의존성 묶음에는 일반 파일만 허용합니다.")
        relative = item.relative_to(root).as_posix()
        _relative_path(relative, allow_root=False)
        size = item.stat().st_size
        total_size += size
        if len(manifest) >= 10000 or total_size > 2 * 1024 * 1024 * 1024:
            _fail("BUNDLE_TOO_LARGE", "의존성 묶음은 파일 10,000개·2GiB를 초과할 수 없습니다.")
        digest = hashlib.sha256()
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest.append({"path": relative, "size": size, "sha256": digest.hexdigest()})
    if not manifest:
        _fail("DEPENDENCY_MISSING", "의존성 묶음이 비어 있습니다.")
    checksum = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return manifest, checksum


def register_dependency_bundle(admin, project_id, local_path, lock_file_hash, platform, runtime):
    actor = require_access(admin, "project.manage", project_id=project_id)
    project = get_project(project_id)
    if not isinstance(lock_file_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", lock_file_hash):
        _fail("INVALID_INPUT", "의존성 lock 파일의 SHA-256이 필요합니다.")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 80 or _has_control(value) for value in (platform, runtime)):
        _fail("INVALID_INPUT", "플랫폼과 runtime을 명시하세요.")
    if not isinstance(local_path, (str, Path)) or _has_control(str(local_path)) or not Path(local_path).is_absolute():
        _fail("PATH_NOT_ALLOWED", "의존성 자료는 절대 경로로 등록하세요.")
    try:
        root = Path(local_path).resolve(strict=True)
        data_dir = Path(settings.AIQA_DATA_DIR).resolve()
        if _overlaps(root, Path(project.canonical_repo_path)) or _overlaps(root, data_dir) and not root.is_relative_to(data_dir / "dependencies"):
            _fail("PATH_NOT_ALLOWED", "원본 저장소·앱 운영 데이터와 분리된 의존성 자료를 지정하세요.")
        manifest, checksum = bundle_manifest(root)
    except (OSError, RuntimeError):
        _fail("DEPENDENCY_MISSING", "의존성 묶음을 읽을 수 없습니다.")
    dependencies_root = Path(settings.AIQA_DATA_DIR) / "dependencies"
    if dependencies_root.is_symlink():
        _fail("PATH_NOT_ALLOWED", "앱 의존성 저장 위치를 확인해야 합니다.")
    dependencies_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    bundle_id = uuid.uuid4()
    destination = dependencies_root / str(bundle_id)
    temporary = Path(tempfile.mkdtemp(prefix=".incoming-", dir=dependencies_root))
    created_directory = temporary
    try:
        for entry in manifest:
            source = root / entry["path"]
            target = temporary / entry["path"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
                _fail("DEPENDENCY_CHANGED", "복사 중 의존성 자료 경로가 변경되었습니다.")
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as incoming, target.open("xb") as outgoing:
                remaining = entry["size"]
                while remaining:
                    chunk = incoming.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        _fail("DEPENDENCY_CHANGED", "복사 중 의존성 파일 크기가 변경되었습니다.")
                    outgoing.write(chunk)
                    remaining -= len(chunk)
                if incoming.read(1):
                    _fail("DEPENDENCY_CHANGED", "복사 중 의존성 파일 크기가 변경되었습니다.")
            target.chmod(0o600)
        copied_manifest, copied_checksum = bundle_manifest(temporary)
        if copied_manifest != manifest or copied_checksum != checksum:
            _fail("DEPENDENCY_CHANGED", "복사 중 의존성 자료가 변경되었습니다.")
        temporary.rename(destination)
        created_directory = destination
        with transaction.atomic():
            actor = require_access(actor, "project.manage", project_id=project_id)
            bundle = DependencyBundle.objects.create(id=bundle_id, project=project, registered_path=str(destination),
                lock_file_hash=lock_file_hash, platform=platform.strip(), runtime=runtime.strip(),
                manifest=manifest, manifest_sha256=checksum, created_by=actor)
            ProjectAudit.objects.create(project=project, actor=actor, action="dependency_registered")
        return bundle
    except OSError:
        shutil.rmtree(created_directory)
        _fail("DEPENDENCY_MISSING", "의존성 자료를 안전하게 복사할 수 없습니다.")
    except Exception:
        shutil.rmtree(created_directory)
        raise


def verify_dependency_bundle(bundle):
    try:
        manifest, checksum = bundle_manifest(bundle.registered_path)
    except (OSError, RuntimeError):
        _fail("DEPENDENCY_MISSING", "등록한 의존성 묶음을 읽을 수 없습니다.")
    if checksum != bundle.manifest_sha256 or manifest != bundle.manifest:
        _fail("DEPENDENCY_CHANGED", "등록 이후 의존성 자료가 변경되었습니다.")
    return bundle
