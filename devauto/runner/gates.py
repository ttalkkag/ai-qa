from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from devauto.core.models import GateResult, ProjectConfig
from devauto.core.policy import command_is_allowed
from devauto.runner.artifacts import ArtifactStore
from devauto.runner.subprocesses import run_args, run_shell, sanitized_child_env


GATE_ORDER = [
    "install",
    "format_check",
    "lint",
    "typecheck",
    "unit_test",
    "build",
    "integration_test",
    "smoke_test",
]


@dataclass(frozen=True)
class GateRun:
    ok: bool
    results: list[GateResult]
    failed: GateResult | None = None


class DockerGateRunner:
    def __init__(
        self,
        project: ProjectConfig,
        artifact_store: ArtifactStore,
        run_id: str,
        preview_port: int,
        artifact_suffix: str = "",
    ) -> None:
        self.project = project
        self.artifact_store = artifact_store
        self.run_id = run_id
        self.preview_port = preview_port
        self.artifact_suffix = artifact_suffix

    def run_all(self, workspace: Path) -> GateRun:
        commands = [(name, self.project.commands[name]) for name in GATE_ORDER if self.project.commands.get(name)]
        if not commands:
            path = self.artifact_store.write_text(
                self.run_id,
                "gate",
                "gate-noop.log",
                "이 프로젝트에는 deterministic gate가 설정되어 있지 않습니다.\n",
            )
            result = GateResult("noop", "", 0, path, 0)
            return GateRun(ok=True, results=[result])

        compose_started = False
        if self._uses_compose():
            compose_started = self.compose_up(workspace)

        results: list[GateResult] = []
        try:
            for gate_name, command in commands:
                if not command_is_allowed(command, self.project.policy):
                    path = self.artifact_store.write_text(
                        self.run_id,
                        "gate",
                        self._artifact_name(f"gate-{gate_name}.log"),
                        f"금지된 command를 차단했습니다: {command}\n",
                    )
                    result = GateResult(gate_name, command, 126, path, 0)
                    results.append(result)
                    return GateRun(ok=False, results=results, failed=result)

                result = self._run_gate(workspace, gate_name, command)
                results.append(result)
                if not result.ok:
                    return GateRun(ok=False, results=results, failed=result)
            return GateRun(ok=True, results=results)
        finally:
            if compose_started and any(not result.ok for result in results):
                self.compose_down(workspace)

    def compose_up(self, workspace: Path) -> bool:
        if not self._uses_compose():
            return False
        args = self._compose_base_args() + ["up", "-d", "--build"]
        completed = run_args(args, cwd=workspace, timeout_sec=1800, env=self._compose_env())
        log = f"$ {' '.join(args)}\n\n{completed.combined_output}\n"
        self.artifact_store.write_text(self.run_id, "gate", self._artifact_name("docker-compose-up.log"), log)
        if completed.exit_code != 0:
            raise RuntimeError(completed.combined_output)
        return True

    def compose_down(self, workspace: Path) -> None:
        if not self._uses_compose():
            return
        args = self._compose_base_args() + ["down", "-v", "--remove-orphans"]
        completed = run_args(args, cwd=workspace, timeout_sec=600, env=self._compose_env())
        log = f"$ {' '.join(args)}\n\n{completed.combined_output}\n"
        self.artifact_store.write_text(self.run_id, "gate", self._artifact_name("docker-compose-down.log"), log)

    def compose_health_check(self, workspace: Path) -> bool:
        if not self._uses_compose():
            return False
        args = self._compose_base_args() + ["ps"]
        completed = run_args(args, cwd=workspace, timeout_sec=120, env=self._compose_env())
        log = f"$ {' '.join(args)}\n\n{completed.combined_output}\n"
        self.artifact_store.write_text(self.run_id, "gate", self._artifact_name("docker-compose-health.log"), log)
        if completed.exit_code != 0:
            raise RuntimeError(completed.combined_output)
        return True

    def _run_gate(self, workspace: Path, gate_name: str, command: str) -> GateResult:
        started = time.monotonic()
        if self._uses_compose():
            args = self._compose_base_args() + [
                "exec",
                "-T",
                self.project.docker.preview_service,
                "sh",
                "-lc",
                command,
            ]
            completed = run_args(args, cwd=workspace, timeout_sec=1800, env=self._compose_env())
            command_label = " ".join(args)
        else:
            env = sanitized_child_env({"PREVIEW_PORT": str(self.preview_port)})
            completed = run_shell(command, cwd=workspace, timeout_sec=1800, env=env)
            command_label = command
        duration_ms = int((time.monotonic() - started) * 1000)
        log = f"$ {command_label}\n\n{completed.combined_output}\n"
        path = self.artifact_store.write_text(self.run_id, "gate", self._artifact_name(f"gate-{gate_name}.log"), log)
        return GateResult(gate_name, command_label, completed.exit_code, path, duration_ms)

    def _compose_base_args(self) -> list[str]:
        args = ["docker", "compose", "-p", f"{self.project.docker.project_name_prefix}_{self.run_id}"]
        if self.project.docker.env_file:
            args.extend(["--env-file", self.project.docker.env_file])
        for compose_file in self.project.docker.compose_files:
            args.extend(["-f", compose_file])
        return args

    def _compose_env(self) -> dict[str, str]:
        env = sanitized_child_env(
            {
                "PREVIEW_PORT": str(self.preview_port),
                "HOST_BIND_IP": self.project.docker.host_bind_ip,
            }
        )
        if self.project.docker.env_file:
            env["DEVAUTO_ENV_FILE"] = self.project.docker.env_file
        return env

    def _uses_compose(self) -> bool:
        return self.project.docker.enabled and bool(self.project.docker.compose_files)

    def _artifact_name(self, name: str) -> str:
        if not self.artifact_suffix:
            return name
        stem, separator, extension = name.rpartition(".")
        if not separator:
            return f"{name}{self.artifact_suffix}"
        return f"{stem}{self.artifact_suffix}.{extension}"
