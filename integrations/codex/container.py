import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

from django.conf import settings

from apps.errors import DomainError
from . import adapter


def docker_binary():
    binary = shutil.which("docker")
    if not binary:
        raise DomainError("DOCKER_UNAVAILABLE", "Docker Desktop을 실행해 주세요")
    return binary


def image_name():
    return getattr(settings, "AIQA_DOCKER_IMAGE", "aiqa-runner:local")


def login_file():
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if not path.is_file():
        raise DomainError("LOGIN_REQUIRED", "서버 계정의 Codex 파일 로그인이 필요합니다")
    return path.resolve()


def _mount(source, *, target=None, readonly=False):
    source = Path(source).resolve()
    target = str(target or source)
    if any(character in str(source) + target for character in ",\n\r\x00"):
        raise DomainError("INVALID_INPUT", "Docker에서 사용할 수 없는 경로입니다")
    return ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]


def run_argv(name, scope, command, *, image, auth_file=None, cwd=None):
    workspace = Path(scope.workspace).resolve()
    cwd = Path(cwd or workspace).resolve()
    if not cwd.is_relative_to(workspace) or not workspace.is_dir():
        raise DomainError("PATH_NOT_ALLOWED")
    if not re.fullmatch(r"aiqa-[a-z0-9-]+", name):
        raise DomainError("INVALID_INPUT")
    argv = [docker_binary(), "run", "--rm", "--init", "--interactive", "--name", name,
            "--label", "aiqa.task=true", "--network", "bridge", "--workdir", str(cwd),
            "--env", "HOME=/home/aiqa", "--env", "CODEX_HOME=/home/aiqa/.codex",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "UV_LINK_MODE=copy",
            *_mount(workspace, readonly=not scope.writable_paths)]
    if scope.temporary_dir:
        temporary = Path(scope.temporary_dir).resolve()
        if not temporary.is_relative_to(workspace):
            raise DomainError("PATH_NOT_ALLOWED")
        temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
        argv += _mount(temporary)
        argv += ["--env", f"TMPDIR={temporary}", "--env", f"UV_CACHE_DIR={temporary}/uv-cache"]
    if (workspace / ".git").is_file():
        common = Path(settings.AIQA_DATA_DIR) / "staging" / workspace.name / "worktree-repo" / ".git"
        if not common.is_dir() or common.is_symlink():
            raise DomainError("PATH_NOT_ALLOWED")
        argv += _mount(common)
    if auth_file is not None:
        argv += _mount(auth_file, target="/run/aiqa-auth.json", readonly=True)
        command = ["/bin/sh", "-c",
                   'umask 077; mkdir -p "$CODEX_HOME"; cp /run/aiqa-auth.json "$CODEX_HOME/auth.json"; exec "$@"',
                   "aiqa-login", *command]
    return [*argv, image, *command]


def codex_command(profile, workspace, *, images=()):
    adapter.validate_model_values(profile.get("model_id"), profile.get("effort"))
    argv = ["codex", "exec", "--ignore-user-config", "--strict-config", "--ephemeral", "--json",
            "--color", "never", "--sandbox", "danger-full-access", "--model", profile["model_id"],
            "-c", "model_reasoning_effort=" + json.dumps(profile["effort"]),
            "-c", 'approval_policy="never"', "-c", 'web_search="live"',
            "--cd", str(Path(workspace).resolve()), "--skip-git-repo-check"]
    for path in images:
        path = Path(path).resolve()
        if not path.is_relative_to(Path(workspace).resolve()):
            raise DomainError("PATH_NOT_ALLOWED")
        argv += ["--image", str(path)]
    return [*argv, "-"]


def _control(arguments, *, timeout=15):
    try:
        return subprocess.run([docker_binary(), *arguments], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise DomainError("DOCKER_UNAVAILABLE", "Docker 연결을 확인해 주세요") from None


def ready():
    daemon = _control(["info", "--format", "{{.ServerVersion}}"])
    if daemon.returncode:
        raise DomainError("DOCKER_UNAVAILABLE", "Docker Desktop을 실행해 주세요")
    image = _control(["image", "inspect", image_name(), "--format", "{{.Id}}"])
    if image.returncode or not image.stdout.strip().startswith("sha256:"):
        raise DomainError("DOCKER_IMAGE_MISSING", "실행 이미지를 준비하세요: docker build -t aiqa-runner:local -f integrations/codex/Dockerfile integrations/codex")
    return image.stdout.strip()


def fingerprint():
    image_id = ready()
    return {"cli_binary_hash": hashlib.sha256(image_id.encode()).hexdigest(),
            "cli_version": adapter.SUPPORTED_CLI_VERSION,
            "policy_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "host_os_version": "docker:" + image_id}


def runtime_info():
    image_id = ready()
    name = "aiqa-" + uuid4().hex
    try:
        result = _control(["run", "--rm", "--name", name, image_id, "python3", "-c",
                           "import json,platform,sys; print(json.dumps({'system':platform.system(), 'machine':platform.machine(), 'runtime':f'python-{sys.version_info.major}.{sys.version_info.minor}'}))"], timeout=30)
        if result.returncode:
            raise DomainError("DEPENDENCY_MISSING", "실행 이미지의 Python 환경을 확인하세요")
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise DomainError("DEPENDENCY_MISSING") from None
    finally:
        remove(name)


def remove(name):
    result = _control(["rm", "--force", name])
    if result.returncode and "No such container" not in result.stderr:
        raise DomainError("CONTAINER_TERMINATION_UNCONFIRMED", "이전 컨테이너는 보존되며 새 실행에서 재사용하지 않습니다")


def launch(*, job, scope, command, process_launcher, timeout, stdin_text=None, auth=False, cwd=None, **limits):
    image_id = ready()
    name = "aiqa-" + uuid4().hex
    argv = run_argv(name, scope, command, image=image_id, auth_file=login_file() if auth else None, cwd=cwd)
    try:
        return process_launcher(job=job, argv=argv, cwd=settings.AIQA_DATA_DIR,
                                env=os.environ.copy(), timeout=timeout, stdin_text=stdin_text,
                                container_name=name, **limits)
    finally:
        try:
            remove(name)
        except DomainError:
            job.processes.filter(container_name=name).update(state="ABANDONED", ended_at=None)
            raise


def check_connection():
    from .permissions import PermissionScope
    import tempfile

    try:
        fingerprint_values = fingerprint()
        with tempfile.TemporaryDirectory(prefix="aiqa-docker-login-") as directory:
            scope = PermissionScope(Path(directory), ())
            name = "aiqa-" + uuid4().hex
            argv = run_argv(name, scope, ["/bin/sh", "-c", "codex --version && codex login status"],
                            image=image_name(), auth_file=login_file())
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            finally:
                remove(name)
        version = result.stdout.splitlines()[0].strip() if result.stdout.strip() else "unknown"
        connected = result.returncode == 0 and "logged in" in (result.stdout + result.stderr).lower()
        return {**fingerprint_values, "cli_version": version, "supported_cli": version == adapter.SUPPORTED_CLI_VERSION,
                "connected": connected, "error_code": "" if connected else "LOGIN_REQUIRED"}
    except (DomainError, OSError, subprocess.SubprocessError) as exc:
        return {"connected": False, "supported_cli": False, "cli_version": "unknown",
                "error_code": exc.code if isinstance(exc, DomainError) else "DOCKER_UNAVAILABLE"}
