import hashlib
import io
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from django.conf import settings

from apps.errors import DomainError


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(content):
    return hashlib.sha256(content).hexdigest()


def relative_key(value):
    value = str(value)
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DomainError("UNSAFE_ARTIFACT_PATH")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise DomainError("UNSAFE_ARTIFACT_PATH")
    return path


def data_root():
    root = Path(settings.AIQA_DATA_DIR).absolute()
    for part in (root, *root.parents):
        if part.is_symlink():
            raise DomainError("UNSAFE_ARTIFACT_PATH")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def read_private(key, expected_hash=None, expected_size=None):
    parts = relative_key(key).parts
    fd = os.open(data_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise DomainError("UNSAFE_ARTIFACT_PATH")
            if expected_size is not None and before.st_size != expected_size:
                raise DomainError("ARTIFACT_CORRUPTED")
            content = source.read()
            after = os.fstat(source.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise DomainError("ARTIFACT_CORRUPTED")
    except OSError as exc:
        raise DomainError("ARTIFACT_UNAVAILABLE") from exc
    finally:
        os.close(fd)
    if expected_hash is not None and digest(content) != expected_hash:
        raise DomainError("ARTIFACT_CORRUPTED")
    return content


def write_private(key, content):
    parts = relative_key(key).parts
    fd = os.open(data_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(file_fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.fsync(fd)
    except OSError as exc:
        raise DomainError("ARTIFACT_STORAGE_FAILED") from exc
    finally:
        os.close(fd)


def unlink_private(key):
    parts = relative_key(key).parts
    fd = os.open(data_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise DomainError("UNSAFE_ARTIFACT_PATH")
        os.unlink(parts[-1], dir_fd=fd)
        os.fsync(fd)
    except FileNotFoundError:
        raise DomainError("ARTIFACT_CORRUPTED")
    finally:
        os.close(fd)


def publish_directory(staging_key, destination_key):
    source_parts = relative_key(staging_key).parts
    target_parts = relative_key(destination_key).parts
    if len(source_parts) != 2 or source_parts[0] != "staging" or len(target_parts) != 3 or target_parts[0] != "artifacts":
        raise DomainError("UNSAFE_ARTIFACT_PATH")
    source_fd = os.open(data_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    target_fd = os.dup(source_fd)
    try:
        child = os.open("staging", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source_fd)
        os.close(source_fd)
        source_fd = child
        for part in target_parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=target_fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target_fd)
            os.close(target_fd)
            target_fd = child
        try:
            os.stat(target_parts[-1], dir_fd=target_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DomainError("RESULT_VERSION_CONFLICT")
        os.rename(source_parts[-1], target_parts[-1], src_dir_fd=source_fd, dst_dir_fd=target_fd)
        os.fsync(source_fd)
        os.fsync(target_fd)
    except OSError as exc:
        raise DomainError("ARTIFACT_STORAGE_FAILED") from exc
    finally:
        os.close(source_fd)
        os.close(target_fd)


SECRET = re.compile(r"(?im)(?:authorization|(?:set-)?cookie)[\"']?\s*[:=][^\r\n]+|(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|password)[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+|\bBearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{12,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def redact_text(text):
    text = SECRET.sub("[REDACTED]", str(text))
    text = ANSI.sub("", text)
    return "".join(c for c in text if ord(c) >= 32 or c in "\n\r\t")


def contains_secret(content):
    return bool(SECRET.search(content.decode("utf-8", errors="replace")))


def attachment_content(uploaded):
    name = str(uploaded.name or "")
    if name != Path(name).name or "\\" in name or any(ord(c) < 32 for c in name):
        raise DomainError("INVALID_ATTACHMENT_NAME")
    extension = Path(name).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg", ".txt", ".log", ".md"}:
        raise DomainError("UNSUPPORTED_ATTACHMENT")
    content = bytearray()
    for chunk in uploaded.chunks():
        content.extend(chunk)
        if len(content) > 5 * 1024 * 1024:
            raise DomainError("ATTACHMENT_TOO_LARGE")
    if not content:
        raise DomainError("EMPTY_ATTACHMENT")
    if extension in {".txt", ".log", ".md"}:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("INVALID_ATTACHMENT_TEXT") from exc
        if any(ord(c) < 32 and c not in "\n\r\t" for c in decoded):
            raise DomainError("INVALID_ATTACHMENT_TEXT")
        return bytes(content), "text/plain", name
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(content)) as picture:
            expected = "PNG" if extension == ".png" else "JPEG"
            if picture.format != expected or picture.width * picture.height > 20_000_000:
                raise DomainError("INVALID_ATTACHMENT_IMAGE")
            picture.verify()
        with Image.open(io.BytesIO(content)) as picture:
            safe = picture.convert("RGBA" if expected == "PNG" else "RGB")
            safe.info.clear()
            output = io.BytesIO()
            safe.save(output, format=expected)
            normalized = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise DomainError("INVALID_ATTACHMENT_IMAGE") from exc
    if len(normalized) > 5 * 1024 * 1024:
        raise DomainError("ATTACHMENT_TOO_LARGE")
    return normalized, "image/png" if expected == "PNG" else "image/jpeg", name
