import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SETTING_KEYS = {
    "AIQA_BIND", "AIQA_PUBLIC_ORIGIN", "AIQA_ACCESS_MODE", "AIQA_CODEX_BIN",
    "AIQA_TRUST_PROXY", "AIQA_DEBUG", "AIQA_DOCKER_IMAGE",
}


def data_directory(environ=None):
    env = os.environ if environ is None else environ
    raw = Path(env.get("AIQA_DATA_DIR", str(Path.home() / "Library/Application Support/AI QA"))).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("AIQA_DATA_DIR must be an absolute, non-symlink directory")
    root = raw.resolve()
    if root in (Path("/"), Path.home()) or root == SOURCE_ROOT or root in SOURCE_ROOT.parents or SOURCE_ROOT in root.parents:
        raise ValueError("AIQA_DATA_DIR must be outside the application source and must not contain it")
    return root


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Runtime directory must not be a symlink")
    missing = []
    parent = path
    while not parent.exists():
        if parent.is_symlink():
            raise ValueError("Runtime directory must not be a symlink")
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
                raise ValueError("Concurrent runtime directory creation is not private") from None
    if not path.is_dir():
        raise ValueError("Runtime path is not a directory")
    if path.stat().st_mode & 0o077:
        raise ValueError("Runtime directories must already have private permissions")
    return path


def runtime_root(path):
    root = Path(path)
    marker = root / ".aiqa-runtime"
    if root.exists():
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Runtime root must be a private application directory")
        names = {entry.name for entry in root.iterdir()}
        if marker.exists() and not marker.is_symlink():
            if not marker.is_file() or marker.stat().st_size > 64 or marker.read_text().strip() != "aiqa-local-v1":
                raise ValueError("Invalid application data marker")
        elif names - {"settings.json"}:
            raise ValueError("Refusing to adopt an existing non-application directory")
    private_directory(root)
    private_value(marker, lambda: "aiqa-local-v1")
    return root


def private_value(path, factory):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Runtime secret permissions must be 0600")
            value = stream.read(4096).strip()
        if not value:
            raise ValueError("Runtime secret is empty")
        return value
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        value = factory()
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return value


def bool_setting(value, name):
    if value in (True, "true", "1"):
        return True
    if value in (False, "false", "0"):
        return False
    raise ValueError(f"{name} must be true or false")


def load_runtime(environ=None):
    env = os.environ if environ is None else environ
    root = runtime_root(data_directory(env))
    if (root / "restore-incomplete.json").exists() or (root / "restore-incomplete.json").is_symlink():
        raise ValueError("Restore did not complete; do not start this runtime")
    defaults = {
        "AIQA_BIND": "127.0.0.1:8000", "AIQA_PUBLIC_ORIGIN": "http://127.0.0.1:8000",
        "AIQA_ACCESS_MODE": "loopback", "AIQA_CODEX_BIN": shutil.which("codex") or "",
        "AIQA_TRUST_PROXY": False, "AIQA_DEBUG": False, "AIQA_DOCKER_IMAGE": "aiqa-runner:local",
    }
    config_path = root / "settings.json"
    if config_path.exists() or config_path.is_symlink():
        if config_path.is_symlink() or not config_path.is_file() or config_path.stat().st_size > 65536:
            raise ValueError("Invalid settings.json")
        if config_path.stat().st_mode & 0o077:
            raise ValueError("settings.json permissions must be 0600")
        values = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or set(values) - SETTING_KEYS:
            raise ValueError("Unknown settings.json keys")
        defaults.update(values)
    defaults.update({key: env[key] for key in SETTING_KEYS if key in env})
    for key in ("AIQA_TRUST_PROXY", "AIQA_DEBUG"):
        defaults[key] = bool_setting(defaults[key], key)
    mode = defaults["AIQA_ACCESS_MODE"]
    if mode not in ("loopback", "lan_https"):
        raise ValueError("Unknown access mode")
    bind = urlsplit("http://" + str(defaults["AIQA_BIND"]))
    if bind.hostname not in ("127.0.0.1", "::1") or not bind.port or bind.username or bind.path:
        raise ValueError("Django must bind a loopback address and explicit port")
    origin = urlsplit(str(defaults["AIQA_PUBLIC_ORIGIN"]))
    if not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        raise ValueError("AIQA_PUBLIC_ORIGIN must be an exact origin")
    if mode == "loopback":
        if origin.scheme != "http" or origin.hostname not in ("127.0.0.1", "localhost", "::1") or (origin.port or 80) != bind.port:
            raise ValueError("Loopback origin must match the local HTTP port")
        if defaults["AIQA_TRUST_PROXY"]:
            raise ValueError("Loopback mode does not accept proxy headers")
    elif origin.scheme != "https" or not defaults["AIQA_TRUST_PROXY"]:
        raise ValueError("LAN requires an HTTPS origin and explicit trusted proxy configuration")
    binary = defaults["AIQA_CODEX_BIN"]
    if binary and (not isinstance(binary, str) or not Path(binary).is_absolute()):
        raise ValueError("AIQA_CODEX_BIN must be an absolute path")
    for name in ("locks", "artifacts", "uploads", "dependencies", "logs", "staging", "backups"):
        private_directory(root / name)
    secret = private_value(root / "secret-key", lambda: secrets.token_urlsafe(64))
    instance = private_value(root / "instance-id", lambda: str(uuid.uuid4()))
    uuid.UUID(instance)
    proxy_token = private_value(root / "proxy-token", lambda: secrets.token_urlsafe(48)) if mode == "lan_https" else ""
    digest = hashlib.sha256(json.dumps({**defaults, "data_dir": str(root)}, sort_keys=True).encode()).hexdigest()
    return {**defaults, "data_dir": root, "secret_key": secret, "instance_id": instance,
            "config_hash": digest, "proxy_token": proxy_token, "host": origin.hostname}


@contextmanager
def runtime_lock(data_dir=None, exclusive=False, config_hash=None):
    root = runtime_root(data_directory({"AIQA_DATA_DIR": str(data_dir)}) if data_dir is not None else data_directory())
    directory = private_directory(root / "locks")
    fd = os.open(directory / "maintenance.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    config_fd = None
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Runtime is busy; stop web, worker and maintenance processes first") from exc
        if config_hash is not None:
            if exclusive or not isinstance(config_hash, str) or len(config_hash) != 64 or any(c not in "0123456789abcdef" for c in config_hash):
                raise ValueError("Shared runtime configuration requires a SHA-256 digest")
            config_fd = os.open(directory / "configuration.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(config_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError("Configuration lock must be a private regular file")
            try:
                fcntl.flock(config_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    fcntl.flock(config_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("Runtime configuration is being initialized; retry startup") from exc
            else:
                os.ftruncate(config_fd, 0)
                os.write(config_fd, config_hash.encode("ascii"))
                os.fsync(config_fd)
                fcntl.flock(config_fd, fcntl.LOCK_SH)
            os.lseek(config_fd, 0, os.SEEK_SET)
            if os.read(config_fd, 65) != config_hash.encode("ascii"):
                raise RuntimeError("Runtime configuration mismatch; stop all web and worker processes before changing settings")
        yield
    finally:
        if config_fd is not None:
            fcntl.flock(config_fd, fcntl.LOCK_UN)
            os.close(config_fd)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
