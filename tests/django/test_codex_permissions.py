import json
import os
from pathlib import Path
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from integrations.codex import permissions
from integrations.codex.permissions import (
    PermissionScope, current_policy_hash, native_canary, permission_overrides, sandbox_argv,
)


def configuration(scope):
    argv = permission_overrides(scope)
    assert all(argv[index] == "-c" for index in range(0, len(argv), 2))
    return tomllib.loads("\n".join(argv[1::2]))


def test_exact_permissions_are_deny_by_default(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    temporary = tmp_path / "temporary"
    config = configuration(PermissionScope(workspace, (output,), temporary_dir=temporary))
    profile = config["permissions"][config["default_permissions"]]
    assert profile["filesystem"] == {
        ":root": "deny", ":minimal": "read", ":slash_tmp": "deny",
        str(workspace): "read", str(output): "write", str(temporary): "write",
        str(workspace / ".git"): "read", str(workspace / ".codex"): "read",
        str(workspace / ".aiqa-inputs"): "read",
    }
    assert profile["network"]["enabled"] is False
    assert profile["network"]["dangerously_allow_all_unix_sockets"] is False
    assert "extends" not in profile
    assert "sandbox_mode" not in config


def test_unregistered_system_temp_is_explicitly_denied(tmp_path):
    config = configuration(PermissionScope(tmp_path, ()))
    assert config["permissions"][permissions.PROFILE_NAME]["filesystem"][":tmpdir"] == "deny"


@pytest.mark.parametrize("pattern", ["*", "?", "[abc]"])
def test_permission_grants_require_exact_paths_not_globs(tmp_path, pattern):
    with pytest.raises(ValueError):
        permission_overrides(PermissionScope(tmp_path, (tmp_path / pattern,)))


def test_workspace_write_does_not_reopen_protected_paths(tmp_path):
    config = configuration(PermissionScope(tmp_path, (tmp_path,)))
    paths = config["permissions"][permissions.PROFILE_NAME]["filesystem"]
    assert paths[str(tmp_path)] == "write"
    assert paths[str(tmp_path / ".git")] == "read"
    assert paths[str(tmp_path / ".codex")] == "read"


def test_source_read_allowlist_and_exact_exclusions(tmp_path):
    config = configuration(PermissionScope(
        tmp_path, (tmp_path / "output",), source_read_paths=(tmp_path / "source",),
        denied_paths=(tmp_path / "source" / "secret", tmp_path / ".codex"),
    ))
    paths = config["permissions"][permissions.PROFILE_NAME]["filesystem"]
    assert paths[str(tmp_path)] == "deny"
    assert paths[str(tmp_path / "source")] == "read"
    assert paths[str(tmp_path / "source" / "secret")] == "deny"
    assert paths[str(tmp_path / ".codex")] == "deny"
    assert paths[str(tmp_path / "output")] == "write"


@pytest.mark.parametrize("grant", ["read", "source_read", "write", "temp"])
def test_specific_grants_cannot_reopen_excluded_tree(tmp_path, grant):
    args = {"workspace": tmp_path, "writable_paths": (), "denied_paths": (tmp_path / "secret",)}
    key = {"read": "readable_paths", "source_read": "source_read_paths", "write": "writable_paths", "temp": "temporary_dir"}[grant]
    path = tmp_path / "secret" / "child"
    args[key] = path if grant == "temp" else (path,)
    with pytest.raises(ValueError):
        permission_overrides(PermissionScope(**args))


@pytest.mark.parametrize("kind", ["relative", "root", "home", "escape", "symlink", "protected", "ancestor", "temporary"])
def test_invalid_scope_is_rejected(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scopes = {
        "relative": PermissionScope(Path("relative"), ()),
        "root": PermissionScope(Path("/"), ()),
        "home": PermissionScope(Path.home(), ()),
        "escape": PermissionScope(workspace, (tmp_path / "outside",)),
        "protected": PermissionScope(workspace, (workspace / ".git/config",)),
        "ancestor": PermissionScope(workspace, (), (tmp_path,)),
        "temporary": PermissionScope(workspace, (), temporary_dir=tmp_path),
    }
    if kind == "symlink":
        (workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
        scope = PermissionScope(workspace, (workspace / "escape/data",))
    else:
        scope = scopes[kind]
    with pytest.raises(ValueError):
        permission_overrides(scope)


def test_toml_and_command_values_are_not_interpolated(tmp_path):
    workspace = tmp_path / 'a"quoted $workspace'
    scope = PermissionScope(workspace, ())
    config = configuration(scope)
    assert config["permissions"][permissions.PROFILE_NAME]["filesystem"][str(workspace)] == "read"
    command = ["/usr/bin/printf", "%s", '$(touch /never) ; " --permission-profile bad']
    argv = sandbox_argv("/usr/bin/codex", scope, command, tmp_path / "config")
    assert argv[-3:] == command
    assert "--include-managed-config" in argv
    assert "--ignore-user-config" not in argv
    assert "--strict-config" not in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path / "config")
    assert argv[argv.index("--") + 1:argv.index("--") + 3] == ["/bin/sh", "-c"]


@pytest.mark.parametrize("nested", [False, True])
def test_config_resolution_cannot_use_source(tmp_path, nested):
    config = tmp_path / "inside" if nested else tmp_path
    with pytest.raises(ValueError):
        sandbox_argv("/usr/bin/codex", PermissionScope(tmp_path, ()), ["/bin/true"], config)


def test_config_directory_cannot_contain_codex_config(tmp_path):
    config = tmp_path / "config"
    (config / ".codex").mkdir(parents=True)
    with pytest.raises(ValueError):
        sandbox_argv("/usr/bin/codex", PermissionScope(tmp_path / "source", ()), ["/bin/true"], config)


def test_check_cwd_does_not_change_permission_root(tmp_path):
    workspace = tmp_path / "source"
    scope = PermissionScope(workspace, ())
    argv = sandbox_argv("/usr/bin/codex", scope, ["/bin/true"], tmp_path / "config", command_cwd=workspace / "subdir")
    assert argv[-2] == str(workspace / "subdir")
    assert argv[argv.index("--cd") + 1] == str(tmp_path / "config")
    with pytest.raises(ValueError):
        sandbox_argv("/usr/bin/codex", scope, ["/bin/true"], tmp_path / "config", command_cwd=tmp_path)


def test_policy_hash_covers_actual_module_source():
    import hashlib

    expected = hashlib.sha256(Path(permissions.__file__).read_bytes()).hexdigest()
    assert current_policy_hash() == expected


def passing_probe(**kwargs):
    assert kwargs["argv"][1] == "sandbox"
    assert kwargs["timeout"] == 15
    assert "GH_TOKEN" not in kwargs["env"]
    assert "SSH_AUTH_SOCK" not in kwargs["env"]
    checks = [{"id": name, "status": "PASS", "detail": "fake test evidence"} for name in permissions._PROBE_IDS]
    return SimpleNamespace(
        returncode=0, stdout=json.dumps(checks), stderr="",
        output_complete=True, timed_out=False,
    )


def test_canary_never_claims_unverified_boundaries_passed(tmp_path):
    result = native_canary("/usr/bin/codex", data_dir=tmp_path, process_launcher=passing_probe)
    assert result["native_passed"] is False
    assert result["file_tools_passed"] is False
    assert result["error_code"] == "SANDBOX_UNAVAILABLE"
    checks = {check["id"]: check["status"] for check in result["checks"]}
    assert checks["allowed_read"] == "PASS"
    assert checks["keychain_helper"] == "PASS"
    assert checks["effective_config"] == "UNVERIFIED"
    assert checks["native_file_tools"] == "UNVERIFIED"
    assert list(tmp_path.iterdir()) == []


def test_canary_preserves_existing_auth_location_without_copying_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "existing-cli-location"))
    monkeypatch.setenv("GH_TOKEN", "fake-test-token")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "fake-agent.sock"))

    def launch(**kwargs):
        assert kwargs["env"]["CODEX_HOME"] == os.environ["CODEX_HOME"]
        if "HOME" in os.environ:
            assert kwargs["env"]["HOME"] == os.environ["HOME"]
        return passing_probe(**kwargs)

    native_canary("/usr/bin/codex", data_dir=tmp_path, process_launcher=launch)


@pytest.mark.parametrize("failure", ["returncode", "timeout", "incomplete", "malformed", "missing", "exception"])
def test_incomplete_native_canary_is_not_security_evidence(tmp_path, failure):
    def launch(**kwargs):
        result = passing_probe(**kwargs)
        if failure == "returncode":
            result.returncode = 1
        elif failure == "timeout":
            result.timed_out = True
        elif failure == "incomplete":
            result.output_complete = False
        elif failure == "malformed":
            result.stdout = "not JSON"
        elif failure == "missing":
            result.stdout = "[]"
        else:
            raise subprocess.TimeoutExpired("codex", 15)
        return result

    result = native_canary("/usr/bin/codex", data_dir=tmp_path, process_launcher=launch)
    assert result["native_passed"] is False
    assert not any(check["status"] == "PASS" for check in result["checks"])


def test_standalone_canary_cannot_bypass_job_process_tracking(tmp_path):
    result = native_canary("/usr/bin/codex", data_dir=tmp_path, job=object())
    assert result["native_passed"] is False
    assert result["checks"][0]["id"] == "native_command"
    assert result["checks"][0]["status"] == "UNVERIFIED"


def test_group_escape_results_are_required_separately(tmp_path):
    def launch(**kwargs):
        result = passing_probe(**kwargs)
        result.stdout = json.dumps([
            item for item in json.loads(result.stdout) if item["id"] != "process_setpgid"
        ])
        return result

    result = native_canary("/usr/bin/codex", data_dir=tmp_path, process_launcher=launch)
    assert result["native_passed"] is False
    assert not any(check["status"] == "PASS" for check in result["checks"])
