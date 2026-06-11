from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


SENSITIVE_ENV_KEY_PARTS = (
    "ACCESS_KEY",
    "API_KEY",
    "AUTHORIZATION",
    "COOKIE",
    "CREDENTIAL",
    "PASSWD",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


@dataclass(frozen=True)
class CommandResult:
    args: list[str] | str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def combined_output(self) -> str:
        if self.stderr:
            return f"{self.stdout}\n{self.stderr}".strip()
        return self.stdout


def run_args(
    args: list[str],
    cwd: Path,
    timeout_sec: int = 900,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    return _run_command(args, cwd=cwd, timeout_sec=timeout_sec, input_text=input_text, env=env, shell=False)


def run_shell(command: str, cwd: Path, timeout_sec: int = 900, env: dict[str, str] | None = None) -> CommandResult:
    return _run_command(command, cwd=cwd, timeout_sec=timeout_sec, input_text=None, env=env, shell=True)


def _run_command(
    command: list[str] | str,
    cwd: Path,
    timeout_sec: int,
    input_text: str | None,
    env: dict[str, str] | None,
    shell: bool,
) -> CommandResult:
    checked_cwd = validate_cwd(cwd, command)
    if isinstance(checked_cwd, CommandResult):
        return checked_cwd

    process_kwargs: dict[str, object] = {}
    if os.name != "nt":
        process_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            cwd=checked_cwd,
            shell=shell,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_text is not None else None,
            env=env,
            **process_kwargs,
        )
    except FileNotFoundError as exc:
        return CommandResult(args=command, exit_code=127, stdout="", stderr=str(exc))

    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        if not stdout and isinstance(exc.stdout, str):
            stdout = exc.stdout
        if not stderr and isinstance(exc.stderr, str):
            stderr = exc.stderr
        return CommandResult(args=command, exit_code=124, stdout=stdout or "", stderr=stderr or f"{timeout_sec}s 후 timeout")
    return CommandResult(args=command, exit_code=process.returncode, stdout=stdout or "", stderr=stderr or "")


def validate_cwd(cwd: Path, command: list[str] | str) -> Path | CommandResult:
    cwd_path = Path(cwd)
    if cwd_path.is_symlink():
        return CommandResult(args=command, exit_code=126, stdout="", stderr=f"symlink cwd에서는 실행하지 않습니다: {cwd_path}")
    if not cwd_path.exists():
        return CommandResult(args=command, exit_code=127, stdout="", stderr=f"cwd가 존재하지 않습니다: {cwd_path}")
    if not cwd_path.is_dir():
        return CommandResult(args=command, exit_code=126, stdout="", stderr=f"cwd가 디렉터리가 아닙니다: {cwd_path}")
    return cwd_path.resolve()


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def sanitized_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not is_sensitive_env_key(key)}
    if extra:
        env.update(extra)
    return env


def is_sensitive_env_key(key: str) -> bool:
    normalized = key.upper()
    return any(part in normalized for part in SENSITIVE_ENV_KEY_PARTS)
