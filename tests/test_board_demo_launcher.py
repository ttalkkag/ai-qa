import importlib.util
from pathlib import Path


def load_launcher():
    path = Path(__file__).parent / "e2e/serve-board-demo.py"
    spec = importlib.util.spec_from_file_location("board_demo_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_app_environment_preserves_local_codex_login_without_unrelated_credentials(tmp_path):
    launcher = load_launcher()
    local_login = {
        "HOME": "/synthetic/local-home", "CODEX_HOME": "/synthetic/codex-home",
        "PATH": "/usr/bin:/bin", "TMPDIR": "/synthetic/tmp", "LANG": "en_US.UTF-8", "LC_ALL": "C",
    }
    unrelated = {
        "OPENAI_API_KEY": "synthetic-api-key", "GH_TOKEN": "synthetic-gh-token",
        "GITHUB_TOKEN": "synthetic-github-token", "AWS_SECRET_ACCESS_KEY": "synthetic-aws-secret",
        "SSH_AUTH_SOCK": "/synthetic/ssh-agent.sock", "HTTP_PROXY": "http://synthetic-proxy",
        "PYTHONPATH": "/synthetic/python-injection", "DJANGO_SETTINGS_MODULE": "config.test_settings",
        "AIQA_ACCESS_MODE": "lan_https", "AIQA_TRUST_PROXY": "true", "UNRECOGNIZED_SECRET": "synthetic-secret",
    }
    original = {**local_login, **unrelated}

    actual = launcher.app_environment(original, tmp_path / "runtime", 8123, "/synthetic/bin/codex")

    assert all(actual[key] == value for key, value in local_login.items())
    assert not (set(unrelated) - {"PYTHONPATH", "DJANGO_SETTINGS_MODULE", "AIQA_ACCESS_MODE", "AIQA_TRUST_PROXY"}) & actual.keys()
    assert actual["DJANGO_SETTINGS_MODULE"] == "config.settings"
    assert actual["PYTHONPATH"] == str(launcher.SOURCE_ROOT)
    assert actual["AIQA_ACCESS_MODE"] == "loopback"
    assert actual["AIQA_TRUST_PROXY"] == "false"
    assert actual["AIQA_BIND"] == "127.0.0.1:8123"
    assert actual["AIQA_PUBLIC_ORIGIN"] == "http://127.0.0.1:8123"
    assert actual["AIQA_CODEX_BIN"] == "/synthetic/bin/codex"
    assert actual["AIQA_DATA_DIR"] == str(tmp_path / "runtime")
    assert original == {**local_login, **unrelated}


def test_board_environment_is_credential_free_and_uses_an_isolated_home(tmp_path, monkeypatch):
    launcher = load_launcher()
    monkeypatch.setenv("HOME", "/synthetic/host-home")
    monkeypatch.setenv("CODEX_HOME", "/synthetic/host-codex")
    monkeypatch.setenv("GH_TOKEN", "synthetic-gh-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-aws-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/host-agent.sock")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/python-injection")

    actual = launcher.board_environment(tmp_path / "board-home", tmp_path / "board-tmp")

    assert actual == {
        "HOME": str(tmp_path / "board-home"), "TMPDIR": str(tmp_path / "board-tmp"),
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }
