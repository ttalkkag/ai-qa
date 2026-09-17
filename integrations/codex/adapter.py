import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from apps.errors import DomainError


SUPPORTED_CLI_VERSION = "codex-cli 0.154.0"
MAX_EVENT_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 200 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
DISABLED_FEATURES = (
    "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "goals", "hooks", "memories", "multi_agent", "multi_agent_v2",
    "plugins", "remote_plugin", "recommended_plugins", "shell_snapshot", "shell_snapshot_v2",
    "skill_mcp_dependency_install", "skill_search", "image_generation", "in_app_browser",
    "in_app_local_automation", "workspace_dependencies", "code_mode", "code_mode_host",
    "auth_elicitation", "tool_call_mcp_elicitation", "sleep_tool", "tool_suggest",
)


@dataclass(frozen=True)
class InvocationResult:
    final_response: str = ""
    effective_model: str = "not_reported"
    effective_effort: str = "not_reported"
    reported_tokens: dict = field(default_factory=dict)
    exit_code: int | None = None
    error_code: str = ""
    execution_events: list = field(default_factory=list)
    trace_complete: bool = False


def redact(text):
    value = str(text)
    value = re.sub(r"(?i)\b(bearer)\s+[^\s\"']+", r"\1 [REDACTED]", value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    return value


def resolve_binary(configured=None):
    value = configured or shutil.which("codex")
    if not value:
        raise DomainError("CODEX_NOT_FOUND", "호스트에 Codex CLI를 설치해 주세요.")
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise DomainError("CODEX_NOT_FOUND", "Codex 실행 파일의 절대 경로를 확인해 주세요.")
    path = path.resolve()
    if path.name == "codex.js" and path.parent.name == "bin":
        target = {("Darwin", "arm64"): ("codex-darwin-arm64", "aarch64-apple-darwin"),
                  ("Darwin", "x86_64"): ("codex-darwin-x64", "x86_64-apple-darwin"),
                  ("Linux", "x86_64"): ("codex-linux-x64", "x86_64-unknown-linux-musl"),
                  ("Linux", "aarch64"): ("codex-linux-arm64", "aarch64-unknown-linux-musl")}.get((platform.system(), platform.machine()))
        if target:
            package, triple = target
            native = path.parent.parent / "node_modules" / "@openai" / package / "vendor" / triple / "bin" / "codex"
            if native.is_file() and os.access(native, os.X_OK):
                path = native.resolve()
    return str(path)


def parent_environment(temporary_dir=None):
    env = {key: os.environ[key] for key in ("HOME", "CODEX_HOME", "LANG", "LC_ALL") if key in os.environ}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    env["NO_COLOR"] = "1"
    if temporary_dir is not None:
        env["TMPDIR"] = str(temporary_dir)
        env["UV_CACHE_DIR"] = str(Path(temporary_dir) / "uv-cache")
        env["UV_LINK_MODE"] = "copy"
    return env


def host_os_version():
    info = os.uname()
    return " ".join((info.sysname, info.release, info.version, info.machine))


def configuration_marker(data_dir):
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    paths = [codex_root / "config.toml", codex_root / "requirements.toml",
             Path("/etc/codex/config.toml"), Path("/etc/codex/requirements.toml"),
             Path(data_dir) / "settings.json"]
    rules = codex_root / "rules"
    if rules.is_dir():
        paths.extend(rules.rglob("*.rules"))
    records = [{"home": str(Path.home()), "codex_home": str(codex_root)}]
    for path in sorted(paths):
        try:
            info = path.lstat()
            record = [str(path), info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            if path.is_symlink():
                target = path.stat()
                record.extend([str(path.resolve()), target.st_ino, target.st_size, target.st_mtime_ns, target.st_ctime_ns])
        except FileNotFoundError:
            record = [str(path), "absent"]
        records.append(record)
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def runtime_read_paths():
    return (Path(sys.base_prefix).resolve(), Path(sys.executable).resolve())


def check_connection(binary=None, *, cwd=None):
    try:
        binary = resolve_binary(binary)
        version = subprocess.run([binary, "--version"], cwd=cwd, env=parent_environment(), capture_output=True, timeout=10, check=False)
        version_text = version.stdout[:200].decode("utf-8", errors="replace").strip()
        if version.returncode or not re.fullmatch(r"codex-cli \d+\.\d+\.\d+", version_text):
            return {"connected": False, "error_code": "CONFIG_UNSUPPORTED", "cli_version": "unknown"}
        login = subprocess.run([binary, "login", "status"], cwd=cwd, env=parent_environment(), capture_output=True, timeout=10, check=False)
        status_output = (login.stdout + login.stderr)[:4096].decode("utf-8", errors="replace").lower()
        logged_in = login.returncode == 0 and "logged in" in status_output and "not logged in" not in status_output
        error = "" if logged_in else ("LOGIN_REQUIRED" if "not logged in" in status_output else "LOGIN_CHECK_FAILED")
        return {
            "connected": logged_in, "error_code": error, "binary": binary,
            "cli_version": version_text, "cli_binary_hash": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
            "supported_cli": version_text == SUPPORTED_CLI_VERSION,
            "host_os_version": host_os_version(),
        }
    except DomainError as exc:
        return {"connected": False, "error_code": exc.code, "cli_version": "unknown"}
    except (OSError, subprocess.SubprocessError):
        return {"connected": False, "error_code": "LOGIN_CHECK_FAILED", "cli_version": "unknown"}


def validate_model_values(model_id, effort):
    if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", model_id):
        raise DomainError("MODEL_UNAVAILABLE", "유효한 model ID가 필요합니다.")
    if effort not in {"minimal", "low", "medium", "high", "xhigh", "ultra"}:
        raise DomainError("MODEL_UNAVAILABLE", "명시적인 effort 설정이 필요합니다.")


def exec_argv(binary, profile, scope, *, images=()):
    from .permissions import permission_overrides

    validate_model_values(profile.get("model_id"), profile.get("effort"))
    argv = [
        binary, "exec", "--ignore-user-config", "--strict-config", "--ephemeral",
        "--json", "--color", "never", "--model", profile["model_id"],
        "-c", "model_reasoning_effort=" + json.dumps(profile["effort"]),
        "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
        "-c", "allow_login_shell=false", "-c", "project_doc_max_bytes=0",
        "-c", 'shell_environment_policy.inherit="none"',
        "-c", "shell_environment_policy.experimental_use_profile=false",
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        "-c", 'shell_environment_policy.set={PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",LANG="en_US.UTF-8"}',
        "-c", "features.skip_host_skill_discovery=true",
        "-c", f"projects.{json.dumps(str(scope.workspace))}.trust_level=\"untrusted\"",
    ]
    for feature in DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    argv.extend(permission_overrides(scope))
    if scope.temporary_dir is not None:
        environment = parent_environment(scope.temporary_dir)
        for key in ("TMPDIR", "UV_CACHE_DIR", "UV_LINK_MODE"):
            argv.extend(["-c", "shell_environment_policy.set." + key + "=" + json.dumps(environment[key])])
    for image in images:
        path = Path(image)
        if not path.is_absolute() or not path.resolve().is_relative_to(scope.workspace.resolve()):
            raise DomainError("CONTEXT_INVALID")
        argv.extend(["--image", str(path)])
    argv.extend(["--cd", str(scope.workspace), "--skip-git-repo-check", "-"])
    return argv


def inspect_configuration_contract(binary, config_cwd, *, job=None, process_launcher=None):
    checks = []
    commands = [("cli_exec_contract", [binary, "exec", "--help"], ("--ignore-user-config", "--strict-config", "--ephemeral", "--json")),
                ("cli_sandbox_contract", [binary, "sandbox", "--help"], ("--permission-profile", "--include-managed-config"))]
    feature_argv = [binary, "features", "list"]
    for feature in DISABLED_FEATURES:
        feature_argv.extend(["--disable", feature])
    commands.append(("disabled_features", feature_argv, ()))
    for check_id, argv, required in commands:
        try:
            if process_launcher is not None:
                result = process_launcher(job=job, argv=argv, cwd=config_cwd, env=parent_environment(), timeout=10, stdin_text=None)
                stdout = result.stdout
                complete = result.output_complete and not result.timed_out
            else:
                result = subprocess.run(argv, cwd=config_cwd, env=parent_environment(), capture_output=True, text=True, timeout=10, check=False)
                stdout = result.stdout
                complete = len(stdout.encode("utf-8")) <= MAX_EVENT_BYTES
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8")
            if check_id == "disabled_features":
                rows = {line.split()[0]: line.split()[-1] for line in stdout.splitlines() if len(line.split()) >= 3}
                matched = all(rows.get(name) == "false" for name in DISABLED_FEATURES)
            else:
                matched = all(value in stdout for value in required)
            passed = result.returncode == 0 and complete and matched
            checks.append({"id": check_id, "status": "PASS" if passed else "FAIL",
                           "reason": "CONTRACT_CONFIRMED" if passed else "CONFIG_UNSUPPORTED",
                           "detail": "Read-only CLI contract checked; this is not full exec configuration attestation"})
        except (OSError, UnicodeError, subprocess.SubprocessError):
            checks.append({"id": check_id, "status": "UNVERIFIED", "reason": "DIAGNOSTIC_UNAVAILABLE", "detail": "Read-only CLI inspection did not complete"})
    return checks


def classify_error(value):
    message = str(value).lower()
    if any(word in message for word in ("429", "rate limit", "quota", "usage limit")):
        return "RATE_LIMITED"
    if any(word in message for word in ("model_not_found", "unsupported model", "model is not supported", "unsupported reasoning")):
        return "MODEL_UNAVAILABLE"
    return "AI_FAILED"


def parse_jsonl(output, *, returncode=0, output_complete=True, timed_out=False, requested_model=None, requested_effort=None):
    if timed_out:
        return InvocationResult(exit_code=returncode, error_code="CLI_TIMEOUT")
    if isinstance(output, str):
        output = output.encode("utf-8")
    if not isinstance(output, bytes) or not output_complete or len(output) > MAX_STREAM_BYTES:
        return InvocationResult(exit_code=returncode, error_code="CLI_OUTPUT_INVALID")
    final = ""
    model = effort = "not_reported"
    tokens = {}
    execution_events = []
    trace_complete = True
    completed = False
    error = ""
    try:
        for line in output.splitlines():
            if not line:
                continue
            if len(line) > MAX_EVENT_BYTES:
                raise ValueError("event too large")
            event = json.loads(line.decode("utf-8"))
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("invalid event")
            event_type = event["type"]
            if event_type in {"thread.started", "turn.started", "turn.completed"}:
                reported_model = event.get("model", event.get("model_id"))
                reported_effort = event.get("reasoning_effort", event.get("effort"))
                if reported_model is not None:
                    if not isinstance(reported_model, str):
                        raise ValueError("invalid model")
                    if model != "not_reported" and model != reported_model:
                        error = "MODEL_MISMATCH"
                    model = reported_model
                if reported_effort is not None:
                    if not isinstance(reported_effort, str):
                        raise ValueError("invalid effort")
                    if effort != "not_reported" and effort != reported_effort:
                        error = "MODEL_MISMATCH"
                    effort = reported_effort
            if event_type in {"turn.failed", "error"}:
                error = error or classify_error(event.get("error", event.get("message", "")))
            if event_type == "turn.completed":
                if completed:
                    raise ValueError("duplicate final turn")
                completed = True
                usage = event.get("usage", {})
                if not isinstance(usage, dict):
                    raise ValueError("invalid usage")
                for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                    if name in usage:
                        if type(usage[name]) is not int or usage[name] < 0:
                            raise ValueError("invalid token count")
                        tokens[name] = usage[name]
            if event_type.startswith("item."):
                item = event.get("item")
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise ValueError("invalid item")
                if item["type"] in {"mcp_tool_call"}:
                    error = "CONFIG_UNSUPPORTED"
                if item["type"] in {"command_execution", "file_change"}:
                    if len(execution_events) >= 1000:
                        trace_complete = False
                    else:
                        command = redact(item.get("command", ""))
                        if len(command) > 4096:
                            trace_complete = False
                        exit_code = item.get("exit_code")
                        execution_events.append({
                            "sequence": len(execution_events) + 1, "event": event_type,
                            "id": str(item.get("id", ""))[:200], "type": item["type"],
                            "command": command[:4096], "status": str(item.get("status", ""))[:100],
                            "exit_code": exit_code if type(exit_code) is int else None,
                        })
                if event_type == "item.completed" and item["type"] == "agent_message":
                    value = item.get("text")
                    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_RESPONSE_BYTES:
                        raise ValueError("invalid final response")
                    if completed:
                        raise ValueError("response after completed")
                    final = value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return InvocationResult(exit_code=returncode, error_code="CLI_OUTPUT_INVALID")
    if requested_model is not None and model not in {"not_reported", requested_model}:
        error = "MODEL_MISMATCH"
    if requested_effort is not None and effort not in {"not_reported", requested_effort}:
        error = "MODEL_MISMATCH"
    if not error and (returncode != 0 or not completed or not final.strip()):
        error = "AI_FAILED" if returncode != 0 else "CLI_OUTPUT_INVALID"
    return InvocationResult(
        final_response=redact(final) if not error else "", effective_model=model,
        effective_effort=effort, reported_tokens=tokens, exit_code=returncode, error_code=error,
        execution_events=execution_events, trace_complete=trace_complete and completed,
    )
