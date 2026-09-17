from pathlib import Path

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from config.middleware import LocalAccessMiddleware
from config.runtime import SOURCE_ROOT, data_directory, load_runtime, private_directory, runtime_lock


def runtime_env(tmp_path, **changes):
    return {"AIQA_DATA_DIR": str(tmp_path / "app-data"), **changes}


def test_runtime_initialization_persists_private_secrets(tmp_path):
    first = load_runtime(runtime_env(tmp_path))
    second = load_runtime(runtime_env(tmp_path))
    assert first["secret_key"] == second["secret_key"]
    assert first["instance_id"] == second["instance_id"]
    assert first["config_hash"] == second["config_hash"]
    assert first["data_dir"].stat().st_mode & 0o777 == 0o700
    assert (first["data_dir"] / "secret-key").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("root", ["/", str(Path.home()), str(SOURCE_ROOT), str(SOURCE_ROOT / "runtime"), "relative"])
def test_runtime_rejects_broad_or_source_paths(root):
    with pytest.raises(ValueError):
        data_directory({"AIQA_DATA_DIR": root})


def test_runtime_rejects_symlink_secret(tmp_path):
    root = tmp_path / "app-data"
    root.mkdir()
    target = tmp_path / "unrelated-secret"
    target.write_text("keep unchanged")
    (root / "secret-key").symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        load_runtime(runtime_env(tmp_path))
    assert target.read_text() == "keep unchanged"


def test_settings_unknown_keys_fail_closed(tmp_path):
    root = tmp_path / "app-data"
    root.mkdir(mode=0o700)
    config = root / "settings.json"
    config.write_text('{"disable_sandbox": true}')
    config.chmod(0o600)
    with pytest.raises(ValueError, match="Unknown"):
        load_runtime(runtime_env(tmp_path))


def test_environment_overrides_settings_without_relocating_root(tmp_path):
    root = tmp_path / "app-data"
    root.mkdir(mode=0o700)
    config = root / "settings.json"
    config.write_text('{"AIQA_DEBUG": true}')
    config.chmod(0o600)
    result = load_runtime(runtime_env(tmp_path, AIQA_DEBUG="false"))
    assert result["AIQA_DEBUG"] is False
    config.write_text('{"AIQA_DATA_DIR": "/tmp/elsewhere"}')
    with pytest.raises(ValueError):
        load_runtime(runtime_env(tmp_path))


def test_existing_unrelated_directory_is_never_chmodded_or_adopted(tmp_path):
    root = tmp_path / "unrelated"
    root.mkdir(mode=0o755)
    (root / "important.txt").write_text("preserve")
    mode_before = root.stat().st_mode
    with pytest.raises(ValueError, match="non-application"):
        load_runtime({"AIQA_DATA_DIR": str(root)})
    with pytest.raises(ValueError):
        with runtime_lock(root):
            pytest.fail("adopted unrelated directory")
    assert root.stat().st_mode == mode_before
    assert sorted(p.name for p in root.iterdir()) == ["important.txt"]


def test_failed_restore_is_not_startable(tmp_path):
    config = load_runtime(runtime_env(tmp_path))
    (config["data_dir"] / "restore-incomplete.json").write_text("{}")
    with pytest.raises(ValueError, match="Restore did not complete"):
        load_runtime(runtime_env(tmp_path))


def test_new_intermediate_directories_are_private_without_chmodding_existing_parent(tmp_path):
    mode = tmp_path.stat().st_mode
    target = private_directory(tmp_path / "one" / "two" / "three")
    for directory in (target, target.parent, target.parent.parent):
        assert directory.stat().st_mode & 0o777 == 0o700
    assert tmp_path.stat().st_mode == mode


@pytest.mark.parametrize("changes", [
    {"AIQA_BIND": "0.0.0.0:8000"},
    {"AIQA_PUBLIC_ORIGIN": "http://evil.example:8000"},
    {"AIQA_PUBLIC_ORIGIN": "http://localhost:9000"},
    {"AIQA_ACCESS_MODE": "lan_https"},
    {"AIQA_ACCESS_MODE": "lan_https", "AIQA_PUBLIC_ORIGIN": "https://qa.example"},
    {"AIQA_PUBLIC_ORIGIN": "http://127.0.0.1:8000/path"},
    {"AIQA_TRUST_PROXY": "true"},
])
def test_incomplete_or_unsafe_network_configuration_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        load_runtime(runtime_env(tmp_path, **changes))


def test_lan_requires_explicit_authenticated_proxy(tmp_path):
    result = load_runtime(runtime_env(tmp_path, AIQA_ACCESS_MODE="lan_https", AIQA_PUBLIC_ORIGIN="https://qa.example", AIQA_TRUST_PROXY="true"))
    assert result["proxy_token"]
    assert (result["data_dir"] / "proxy-token").stat().st_mode & 0o777 == 0o600


def test_maintenance_excludes_live_shared_processes(tmp_path):
    with runtime_lock(tmp_path):
        with runtime_lock(tmp_path):
            with pytest.raises(RuntimeError, match="busy"):
                with runtime_lock(tmp_path, exclusive=True):
                    pytest.fail("exclusive lock acquired over live writers")
    with runtime_lock(tmp_path, exclusive=True):
        with pytest.raises(RuntimeError):
            with runtime_lock(tmp_path):
                pytest.fail("writer started during maintenance")


def request_response(*, peer="127.0.0.1", host="localhost", **headers):
    request = RequestFactory().get("/", REMOTE_ADDR=peer, HTTP_HOST=host, **headers)
    response = LocalAccessMiddleware(lambda req: HttpResponse(req.scheme))(request)
    return response


def test_live_runtime_config_must_match_and_can_change_after_stop(tmp_path):
    with runtime_lock(tmp_path, config_hash="a" * 64):
        with runtime_lock(tmp_path, config_hash="a" * 64):
            with pytest.raises(RuntimeError, match="configuration mismatch"):
                with runtime_lock(tmp_path, config_hash="b" * 64):
                    pytest.fail("mismatched web and worker configuration")
        with pytest.raises(RuntimeError, match="busy"):
            with runtime_lock(tmp_path, exclusive=True):
                pytest.fail("maintenance started over configured runtime")
    with runtime_lock(tmp_path, config_hash="b" * 64):
        assert (tmp_path / "locks" / "configuration.lock").read_text() == "b" * 64


def test_runtime_config_lock_rejects_symlink(tmp_path):
    with runtime_lock(tmp_path):
        target = tmp_path / "unrelated"
        target.write_text("preserve")
        (tmp_path / "locks" / "configuration.lock").symlink_to(target)
        with pytest.raises(OSError):
            with runtime_lock(tmp_path, config_hash="a" * 64):
                pytest.fail("configuration lock followed symlink")
        assert target.read_text() == "preserve"


@override_settings(AIQA_ACCESS_MODE="loopback", ALLOWED_HOSTS=["localhost"])
def test_http_network_boundary_and_forwarded_header_spoof():
    assert request_response(peer="192.168.1.8").status_code == 403
    assert request_response(host="evil.example").status_code == 400
    response = request_response(HTTP_X_FORWARDED_PROTO="https")
    assert response.content == b"http"
    assert response["Cache-Control"] == "no-store, private"
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]


@override_settings(AIQA_ACCESS_MODE="lan_https", ALLOWED_HOSTS=["qa.example"], RUNTIME={"proxy_token": "synthetic-proxy-token"})
def test_lan_proxy_headers_are_not_trusted_without_token():
    assert request_response(host="qa.example", HTTP_X_FORWARDED_PROTO="https").status_code == 403
    assert request_response(host="qa.example", HTTP_X_AIQA_PROXY_TOKEN="synthetic-proxy-token").status_code == 403
    response = request_response(host="qa.example", HTTP_X_FORWARDED_PROTO="https", HTTP_X_AIQA_PROXY_TOKEN="synthetic-proxy-token")
    assert response.status_code == 200
    assert response.content == b"https"
