import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from types import SimpleNamespace


PROFILE_NAME = "aiqa-local"
PROTECTED_PATHS = (".git", ".codex", ".aiqa-inputs")
_CHANGE_DIRECTORY = 'cd -- "$1" || exit 125; shift; exec "$@"'


@dataclass(frozen=True)
class PermissionScope:
    workspace: Path
    writable_paths: tuple[Path, ...]
    readable_paths: tuple[Path, ...] = ()
    temporary_dir: Path | None = None
    source_read_paths: tuple[Path, ...] | None = None
    denied_paths: tuple[Path, ...] = ()


def _path(value):
    value = Path(value)
    if not value.is_absolute() or any(character in str(value) for character in "\x00*?[]"):
        raise ValueError("Permission paths must be absolute")
    resolved = value.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError("Broad filesystem scopes are not allowed")
    return resolved


def _profile(scope):
    workspace = _path(scope.workspace)
    denied = tuple(_path(path) for path in scope.denied_paths)
    if any(not path.is_relative_to(workspace) for path in denied):
        raise ValueError("Source deny paths must remain inside the workspace")
    filesystem = {
        ":root": "deny",
        ":minimal": "read",
        ":tmpdir": "deny",
        ":slash_tmp": "deny",
        str(workspace): "read" if scope.source_read_paths is None else "deny",
    }
    if scope.source_read_paths is not None:
        for item in scope.source_read_paths:
            path = _path(item)
            if not path.is_relative_to(workspace):
                raise ValueError("Source read paths must remain inside the workspace")
            if any(path.is_relative_to(parent) for parent in denied):
                raise ValueError("Explicit read grants cannot reopen denied paths")
            filesystem[str(path)] = "read"
    for item in scope.readable_paths:
        path = _path(item)
        if path in workspace.parents:
            raise ValueError("Reading a workspace ancestor is not allowed")
        if any(path.is_relative_to(parent) for parent in denied):
            raise ValueError("Explicit read grants cannot reopen denied paths")
        filesystem[str(path)] = "read"
    protected = tuple(workspace / name for name in PROTECTED_PATHS)
    for item in scope.writable_paths:
        path = _path(item)
        if not path.is_relative_to(workspace):
            raise ValueError("Writable paths must remain inside the workspace")
        if any(path.is_relative_to(parent) for parent in protected):
            raise ValueError("Protected paths cannot be writable")
        if any(path.is_relative_to(parent) for parent in denied):
            raise ValueError("Explicit write grants cannot reopen denied paths")
        filesystem[str(path)] = "write"
    if scope.temporary_dir is not None:
        temporary = _path(scope.temporary_dir)
        if temporary == workspace or temporary in workspace.parents:
            raise ValueError("Temporary storage cannot contain the workspace")
        if any(temporary.is_relative_to(parent) for parent in protected):
            raise ValueError("Temporary storage cannot enter protected paths")
        if any(temporary.is_relative_to(parent) for parent in denied):
            raise ValueError("Temporary storage cannot reopen denied paths")
        filesystem.pop(":tmpdir")
        filesystem[str(temporary)] = "write"
    for path in protected:
        if not any(path.is_relative_to(parent) for parent in denied):
            filesystem[str(path)] = "read"
    for path in denied:
        filesystem[str(path)] = "deny"
    return {
        "filesystem": filesystem,
        "network": {
            "enabled": False,
            "allow_local_binding": False,
            "dangerously_allow_all_unix_sockets": False,
            "unix_sockets": {},
        },
    }


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(str(key)) + "=" + _toml(item)
            for key, item in sorted(value.items())
        ) + "}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def permission_overrides(scope):
    return [
        "-c", "permissions=" + _toml({PROFILE_NAME: _profile(scope)}),
        "-c", "default_permissions=" + _toml(PROFILE_NAME),
    ]


def sandbox_argv(binary, scope, command, config_cwd, *, command_cwd=None):
    config_cwd = _path(config_cwd)
    workspace = _path(scope.workspace)
    command_cwd = workspace if command_cwd is None else _path(command_cwd)
    if not command_cwd.is_relative_to(workspace):
        raise ValueError("Command cwd must remain inside project source")
    if config_cwd == workspace or config_cwd.is_relative_to(workspace):
        raise ValueError("Configuration resolution must be outside project source")
    if (config_cwd / ".codex").exists():
        raise ValueError("Configuration directory must not contain project configuration")
    if not command or any(not isinstance(arg, str) or "\x00" in arg for arg in command):
        raise ValueError("A nonempty command argv is required")
    return [
        str(_path(binary)), "sandbox", "--permission-profile", PROFILE_NAME,
        "--include-managed-config", "--cd", str(config_cwd),
        *permission_overrides(scope), "--", "/bin/sh", "-c", _CHANGE_DIRECTORY,
        "aiqa-sandbox", str(command_cwd), *command,
    ]


def current_policy_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


_PROBE = r'''
import ctypes, errno, json, os, pathlib, socket, sys
fixtures = json.loads(sys.argv[1])
checks = []
def check(name, action, denied=False):
    try:
        action()
    except OSError as error:
        status = "PASS" if denied and error.errno in (errno.EPERM, errno.EACCES) else "UNVERIFIED"
        checks.append({"id": name, "status": status, "reason": "ACCESS_DENIED" if status == "PASS" else "PROBE_FAILED", "detail": "errno=" + str(error.errno)})
    except Exception:
        checks.append({"id": name, "status": "UNVERIFIED", "reason": "PROBE_FAILED", "detail": "Probe did not complete"})
    else:
        checks.append({"id": name, "status": "FAIL" if denied else "PASS", "reason": "ACCESS_ALLOWED", "detail": "Access allowed"})
def read(name):
    pathlib.Path(fixtures[name]).read_bytes()
def write(name):
    pathlib.Path(fixtures[name]).write_text("aiqa-canary-write", encoding="utf-8")
def connect_tcp():
    with socket.create_connection(("127.0.0.1", fixtures["tcp_port"]), timeout=0.5):
        pass
def connect_unix():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        connection.connect(fixtures["unix_path"])
check("allowed_read", lambda: read("source"))
check("allowed_write", lambda: write("writable"))
check("temporary_write", lambda: write("temporary"))
check("readonly_source_write", lambda: write("source"), True)
check("secret_read", lambda: read("secret"), True)
check("outside_write", lambda: write("outside"), True)
check("symlink_escape_read", lambda: read("symlink"), True)
check("loopback_network", connect_tcp, True)
check("unix_socket", connect_unix, True)
if sys.platform != "darwin":
    checks.append({"id": "keychain_helper", "status": "UNVERIFIED", "reason": "PLATFORM_UNSUPPORTED", "detail": "macOS Keychain helper probe requires Darwin"})
else:
    try:
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        bootstrap = ctypes.c_uint.in_dll(system, "bootstrap_port").value
        system.bootstrap_look_up.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
        system.bootstrap_look_up.restype = ctypes.c_int
        results = []
        for service in (b"com.apple.SecurityServer", b"com.apple.securityd.systemkeychain"):
            port = ctypes.c_uint()
            code = system.bootstrap_look_up(bootstrap, service, ctypes.byref(port))
            results.append(code)
            if code == 0:
                task = ctypes.c_uint.in_dll(system, "mach_task_self_").value
                system.mach_port_deallocate(task, port.value)
        status = "FAIL" if 0 in results else "PASS" if all(code in (1100, 159) for code in results) else "UNVERIFIED"
        checks.append({"id": "keychain_helper", "status": status, "reason": "HELPER_REACHABLE" if status == "FAIL" else "ACCESS_DENIED" if status == "PASS" else "PROBE_FAILED", "detail": "Mach service lookup only; no Keychain request sent; codes=" + repr(results)})
    except Exception:
        checks.append({"id": "keychain_helper", "status": "UNVERIFIED", "reason": "PROBE_FAILED", "detail": "Mach service lookup probe unavailable"})
def process_escape_probe(operation):
    check_id = "process_" + operation
    if not hasattr(os, "fork") or not hasattr(os, operation):
        return {"id": check_id, "status": "UNVERIFIED", "reason": "PLATFORM_UNSUPPORTED", "detail": "Bounded fork/" + operation + " probe unavailable"}
    try:
        child = os.fork()
    except OSError as error:
        status = "PASS" if error.errno in (errno.EPERM, errno.EACCES) else "UNVERIFIED"
        return {"id": check_id, "status": status, "reason": "FORK_DENIED" if status == "PASS" else "PROBE_FAILED", "detail": "fork errno=" + str(error.errno)}
    if child == 0:
        try:
            if operation == "setsid":
                os.setsid()
            else:
                os.setpgid(0, 0)
        except OSError as error:
            os._exit(42 if error.errno in (errno.EPERM, errno.EACCES) else 43)
        os._exit(41)
    _, child_status = os.waitpid(child, 0)
    code = os.waitstatus_to_exitcode(child_status)
    status = "PASS" if code == 42 else "FAIL" if code == 41 else "UNVERIFIED"
    return {"id": check_id, "status": status, "reason": "GROUP_ESCAPE_ALLOWED" if code == 41 else "GROUP_ESCAPE_DENIED" if code == 42 else "PROBE_FAILED", "detail": "Bounded child exited and was reaped; " + operation + " exit=" + str(code)}
escape_checks = [process_escape_probe(operation) for operation in ("setsid", "setpgid")]
checks.extend(escape_checks)
escape_status = "FAIL" if any(item["status"] == "FAIL" for item in escape_checks) else "PASS" if all(item["status"] == "PASS" for item in escape_checks) else "UNVERIFIED"
checks.append({"id": "process_escape", "status": escape_status, "reason": "GROUP_ESCAPE_ALLOWED" if escape_status == "FAIL" else "GROUP_ESCAPE_DENIED" if escape_status == "PASS" else "PROBE_FAILED", "detail": "Both setsid and setpgid must be denied; children exited and were reaped"})
print(json.dumps(checks))
'''

_PROBE_IDS = (
    "allowed_read", "allowed_write", "temporary_write", "readonly_source_write",
    "secret_read", "outside_write", "symlink_escape_read",
    "loopback_network", "unix_socket", "keychain_helper", "process_setsid", "process_setpgid", "process_escape",
)


def _standalone_probe(**kwargs):
    if kwargs["job"] is not None:
        raise ValueError("Tracked jobs require the common process launcher")
    with subprocess.Popen(
        kwargs["argv"], cwd=kwargs["cwd"], env=kwargs["env"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    ) as process:
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=kwargs["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=5)
        return SimpleNamespace(
            returncode=process.returncode, stdout=stdout, stderr=stderr,
            timed_out=timed_out, output_complete=not timed_out,
        )


def _unverified(check_id, detail, reason="PROBE_FAILED"):
    return {"id": check_id, "status": "UNVERIFIED", "reason": reason, "detail": detail}


def native_canary(binary, *, data_dir, process_launcher=None, job=None):
    checks = []
    try:
        data_dir = _path(data_dir)
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(prefix="canary-", dir=data_dir) as directory,
            tempfile.TemporaryDirectory(prefix="aiqa-ipc-") as ipc_directory,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix,
        ):
            root = Path(directory).resolve()
            workspace = root / "workspace"
            writable = workspace / "generated"
            temporary = root / "temporary"
            config = root / "config"
            for path in (workspace, writable, temporary, config):
                path.mkdir(mode=0o700)
            (workspace / ".codex").mkdir()
            (workspace / ".codex" / "config.toml").write_text(
                "THIS IS INTENTIONALLY INVALID AIQA CANARY TOML [", encoding="utf-8"
            )
            source = workspace / "source.txt"
            source.write_text("aiqa-readonly-source", encoding="utf-8")
            secret = root / "fake-credential.txt"
            secret.write_text("AIQA_FAKE_CREDENTIAL_NOT_A_SECRET", encoding="utf-8")
            outside = root / "other-workspace.txt"
            outside.write_text("aiqa-outside-unchanged", encoding="utf-8")
            symlink = workspace / "escape"
            symlink.symlink_to(secret)
            tcp.bind(("127.0.0.1", 0))
            tcp.listen(1)
            unix_path = Path(ipc_directory).resolve() / "h.sock"
            unix.bind(str(unix_path))
            unix.listen(1)
            scope = PermissionScope(
                workspace=workspace, writable_paths=(writable,),
                readable_paths=(Path(sys.base_prefix), Path(sys.executable).resolve(), unix_path),
                temporary_dir=temporary,
            )
            fixtures = {
                "source": str(source), "writable": str(writable / "probe.txt"),
                "temporary": str(temporary / "probe.txt"), "secret": str(secret),
                "outside": str(outside), "symlink": str(symlink),
                "tcp_port": tcp.getsockname()[1], "unix_path": str(unix_path),
            }
            command = [str(Path(sys.executable).resolve()), "-I", "-c", _PROBE, json.dumps(fixtures)]
            environment = {
                key: os.environ[key]
                for key in ("HOME", "CODEX_HOME", "LANG", "LC_ALL")
                if key in os.environ
            }
            environment.update({
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
                "TMPDIR": str(temporary), "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1",
            })
            result = (process_launcher or _standalone_probe)(
                job=job, argv=sandbox_argv(binary, scope, command, config),
                cwd=config, env=environment, timeout=15, stdin_text=None,
            )
            if result.returncode != 0 or result.timed_out or not result.output_complete:
                checks.append(_unverified("native_command", "Sandbox probe did not complete"))
            else:
                parsed = json.loads(result.stdout)
                if (
                    not isinstance(parsed, list) or len(parsed) != len(_PROBE_IDS)
                    or [item.get("id") for item in parsed if isinstance(item, dict)] != list(_PROBE_IDS)
                    or any(item.get("status") not in {"PASS", "FAIL", "UNVERIFIED"} for item in parsed)
                ):
                    raise ValueError("Invalid probe evidence")
                checks.extend(parsed)
                checks.append({
                    "id": "source_config_not_loaded", "status": "PASS",
                    "detail": "Sandbox command completed with intentionally invalid source configuration",
                })
                unchanged = (
                    source.read_text(encoding="utf-8") == "aiqa-readonly-source"
                    and outside.read_text(encoding="utf-8") == "aiqa-outside-unchanged"
                )
                checks.append({
                    "id": "outside_files_unchanged", "status": "PASS" if unchanged else "FAIL",
                    "detail": "Compared fake fixture contents after sandbox execution",
                })
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError, TypeError):
        checks.append(_unverified("native_command", "Sandbox probe setup or evidence unavailable"))
    checks.append(_unverified(
        "effective_config", "Effective managed and personal configuration attestation is not implemented",
        "PROBE_NOT_IMPLEMENTED",
    ))
    native_passed = all(check["status"] == "PASS" for check in checks)
    checks.append(_unverified(
        "native_file_tools", "Model file-tool enforcement requires a separate consented probe",
        "CONSENTED_PROBE_REQUIRED",
    ))
    return {
        "checks": checks,
        "native_passed": native_passed,
        "file_tools_passed": False,
        "error_code": "SANDBOX_UNAVAILABLE",
    }
