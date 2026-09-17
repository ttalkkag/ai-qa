"""Run an isolated manual browser demo with the host's existing Codex login."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT))

from tests.e2e.board_fixture import prepare_board, verification_command


def app_environment(environ, runtime, port, binary):
    env = {key: environ[key] for key in ("HOME", "CODEX_HOME", "PATH", "TMPDIR", "LANG", "LC_ALL") if key in environ}
    env.update({
        "PYTHONPATH": str(SOURCE_ROOT), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "DJANGO_SETTINGS_MODULE": "config.settings", "AIQA_DATA_DIR": str(runtime),
        "AIQA_BIND": f"127.0.0.1:{port}", "AIQA_PUBLIC_ORIGIN": f"http://127.0.0.1:{port}",
        "AIQA_ACCESS_MODE": "loopback", "AIQA_TRUST_PROXY": "false", "AIQA_DEBUG": "false",
        "AIQA_CODEX_BIN": binary,
    })
    return env


def board_environment(home, temporary_dir):
    return {
        "HOME": str(home), "TMPDIR": str(temporary_dir), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def reserve_port(port):
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
    except BaseException:
        listener.close()
        raise
    return listener


def bootstrap_admin():
    import django

    django.setup()
    from django.contrib.auth.password_validation import validate_password
    from django.db import transaction

    from apps.accounts.models import AccountAudit, User

    password = secrets.token_urlsafe(32)
    user = User(
        username="demo-admin", display_name="데모 관리자",
        role=User.Role.ADMIN, must_change_password=False,
    )
    validate_password(password, user)
    user.set_password(password)
    user.full_clean(exclude=["password"])
    with transaction.atomic():
        if User.objects.exists():
            raise RuntimeError("Refusing to bootstrap a database with existing accounts")
        user.save()
        AccountAudit.objects.create(
            subject=user, bootstrap_identity="local-board-demo", action="account.bootstrap",
            after={"username": user.username, "role": user.role},
        )
    return {"username": user.username, "password": password}


def stop(child):
    def signal_group(value):
        try:
            os.killpg(child.pid, value)
        except ProcessLookupError:
            pass

    signal_group(signal.SIGTERM)
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        child.wait(timeout=3)
    # The group may outlive its original child after an unexpected service failure.
    signal_group(signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="App loopback port (default: choose a free port)")
    parser.add_argument("--board-port", type=int, default=0, help="Board loopback port (default: choose a free port)")
    options = parser.parse_args()
    if any(not 0 <= port <= 65535 for port in (options.port, options.board_port)):
        parser.error("Ports must be between 0 and 65535")
    python = SOURCE_ROOT / ".venv/bin/python"
    if not python.is_file():
        parser.error("Run make init first; the repository .venv is required")
    binary = shutil.which(os.environ.get("AIQA_CODEX_BIN") or "codex")
    if not binary:
        parser.error("Install Codex CLI and log in locally before starting this demo")
    binary = str(Path(binary).resolve())
    work = Path(tempfile.mkdtemp(prefix="aiqa-board-demo-")).resolve()
    work.chmod(0o700)
    log_path = work / "services.log"
    log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
    manifest_path = work / "manifest.json"
    manifest = {"root": str(work), "status": "starting", "children": {}}
    private_json(manifest_path, manifest)
    print(f"Demo evidence: {work}", flush=True)
    children = []
    reservations = []
    fixture = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        reservations.append(reserve_port(options.port))
        reservations.append(reserve_port(options.board_port))
        app_port, board_port = [listener.getsockname()[1] for listener in reservations]
        urls = {"app": f"http://127.0.0.1:{app_port}", "board": f"http://127.0.0.1:{board_port}"}
        fixture = prepare_board(work / "board")
        runtime = work / "runtime"
        env = app_environment(os.environ, runtime, app_port, binary)
        board_home = work / "board-home"
        board_tmp = work / "board-tmp"
        board_home.mkdir(mode=0o700)
        board_tmp.mkdir(mode=0o700)
        board_env = board_environment(board_home, board_tmp)
        for command in (["manage.py", "migrate", "--noinput"], ["manage.py", "collectstatic", "--noinput"]):
            subprocess.run([str(python), *command], cwd=SOURCE_ROOT, env=env,
                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, check=True, timeout=90)
        os.environ.clear()
        os.environ.update(env)
        credentials = bootstrap_admin()
        credentials_path = work / "credentials.json"
        private_json(credentials_path, credentials)
        manifest.update({
            **fixture, "runtime": str(runtime), "urls": urls, "username": credentials["username"],
            "verification_command": verification_command(str(python)), "python": str(python),
            "codex_binary": binary, "requirements": str(Path(fixture["source"]) / "docs/requirements.md"),
        })

        def start(name, args, environment, cwd=SOURCE_ROOT):
            child = subprocess.Popen([str(python), *args], cwd=cwd, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            children.append(child)
            manifest["children"][name] = child.pid
            private_json(manifest_path, manifest)
            return child

        reservations[0].close()
        web = start("web", ["-m", "gunicorn", "--config", "python:config.gunicorn", "config.wsgi:application"], env)
        start("worker", ["manage.py", "process_jobs"], env)
        reservations[1].close()
        start("board", [str(Path(fixture["source"]) / "board.py"), "--port", str(board_port)], board_env, fixture["source"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"A demo service exited; inspect {log_path}")
            contents = log_path.read_text(encoding="utf-8", errors="replace")
            # Confirm our own services bound successfully before accepting any HTTP response.
            bound = f"({web.pid})" in contents and f"게시판: {urls['board']}/" in contents
            if bound:
                try:
                    with urlopen(urls["app"] + "/accounts/login/", timeout=1) as app:
                        with urlopen(urls["board"] + "/", timeout=1) as board:
                            if app.status == board.status == 200:
                                break
                except OSError:
                    pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Demo startup timed out; inspect {log_path}")
        manifest["status"] = "ready"
        private_json(manifest_path, manifest)
        print(f"App: {urls['app']}/accounts/login/", flush=True)
        print(f"Board: {urls['board']}/", flush=True)
        print(f"Username: {credentials['username']}", flush=True)
        print(f"Credentials (0600): {credentials_path}", flush=True)
        print(f"Manifest (0600): {manifest_path}", flush=True)
        print("Only the initial administrator exists. Register accounts and the board project through the app UI.", flush=True)
        print("Host Codex login is available; no model call or sandbox PASS has been seeded. Stop with Ctrl+C.", flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"A demo service exited; inspect {log_path}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        manifest["status"] = "stopped"
    except Exception:
        manifest["status"] = "failed"
        raise
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        failures = []
        for child in reversed(children):
            try:
                stop(child)
            except Exception as error:
                failures.append(str(error))
        for listener in reservations:
            listener.close()
        log.close()
        if fixture:
            try:
                git_env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
                def git(*args):
                    return subprocess.check_output(["/usr/bin/git", "-C", fixture["source"], *args], env=git_env, text=True, timeout=10).strip()
                integrity = {"clean": not git("status", "--porcelain"), "commit_unchanged": git("rev-parse", "HEAD") == fixture["source_commit"], "no_remote": not git("remote")}
                manifest["source_integrity"] = integrity
                if not all(integrity.values()):
                    failures.append("Board source changed; preserved for inspection")
            except Exception as error:
                failures.append(str(error))
        manifest["cleanup_failures"] = failures
        private_json(manifest_path, manifest)
        print(f"Stopped. Runtime, logs and source evidence retained at {work}", flush=True)
        if failures:
            raise RuntimeError("; ".join(failures))


if __name__ == "__main__":
    main()
