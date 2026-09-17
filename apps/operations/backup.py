import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.utils import timezone

from apps.errors import DomainError
from apps.runs.processes import host_boot_id, process_table
from config.runtime import SOURCE_ROOT, private_directory, runtime_lock


FORMAT_VERSION = 1
DATA_DIRECTORIES = ("uploads", "dependencies", "artifacts", "logs")
DATA_FILES = ("settings.json", "secret-key", "instance-id", "proxy-token", ".aiqa-runtime")
EXCLUDED = ("provider credentials", "SSH", "Keychain", "workspaces", "baseline-checks", "after-checks",
            "conversations", "locks", "publishing", "staging", "backups", "codex-config", "static")
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 100000


def _fail(code, message):
    raise DomainError(code, message, 409)


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("BACKUP_PATH_INVALID", "절대 경로와 일반 디렉토리를 사용하세요.")
    for part in (path, *path.parents):
        if part.is_symlink():
            _fail("BACKUP_PATH_INVALID", "백업·복원 경로에는 심볼릭 링크를 사용할 수 없습니다.")
    return path


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail("BACKUP_INVALID", "잘못된 백업 파일 경로입니다.")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in (".", "..") for part in path.parts):
        _fail("BACKUP_INVALID", "백업 파일이 허용된 경로 밖을 가리킵니다.")
    if value not in (*DATA_FILES, "db.sqlite3") and (len(path.parts) < 2 or path.parts[0] not in DATA_DIRECTORIES):
        _fail("BACKUP_INVALID", "백업에 허용되지 않은 파일이 있습니다.")
    return path


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail("BACKUP_PATH_INVALID", "백업에는 링크나 특수 파일을 포함할 수 없습니다.")
    return info


def _files(directory):
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        _fail("BACKUP_PATH_INVALID", "백업 대상 디렉토리를 확인하세요.")
    found = []
    for parent, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs:
            child = Path(parent) / name
            if child.is_symlink():
                _fail("BACKUP_PATH_INVALID", "백업에는 심볼릭 링크를 포함할 수 없습니다.")
        for name in names:
            child = Path(parent) / name
            _regular(child)
            found.append(child)
            if len(found) > MAX_FILES:
                _fail("BACKUP_TOO_LARGE", "백업 파일 개수 제한을 초과했습니다.")
    return sorted(found)


def _transfer(source, destination=None):
    before = _regular(source)
    digest = hashlib.sha256()
    size = 0
    output = None
    try:
        if destination is not None:
            private_directory(destination.parent)
            output = os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                _fail("BACKUP_SOURCE_CHANGED", "파일이 변경되었습니다. 모든 쓰기 작업을 중지하세요.")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                if output:
                    output.write(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            _fail("BACKUP_SOURCE_CHANGED", "백업 중 파일이 변경되었습니다.")
        if output:
            output.flush()
            os.fsync(output.fileno())
    finally:
        if output:
            output.close()
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _write_json(path, value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path):
    if _regular(path).st_size > MAX_MANIFEST_BYTES:
        _fail("BACKUP_INVALID", "백업 manifest가 너무 큽니다.")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (ValueError, UnicodeError):
        _fail("BACKUP_INVALID", "백업 manifest를 읽을 수 없습니다.")


def _identity_text(path):
    if _regular(path).st_size > 128:
        _fail("BACKUP_INVALID", "인스턴스 표식 파일이 올바르지 않습니다.")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as stream:
            return stream.read(128).strip()
    except UnicodeError:
        _fail("BACKUP_INVALID", "인스턴스 표식 파일을 읽을 수 없습니다.")


def _database(path, *, immutable=False):
    _regular(path)
    options = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    database = sqlite3.connect(path.as_uri() + options, uri=True, timeout=5)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    return database


def _integrity(database):
    if database.execute("PRAGMA quick_check").fetchall()[0][0] != "ok" or database.execute("PRAGMA foreign_key_check").fetchone():
        _fail("BACKUP_INVALID", "SQLite 무결성 검사에 실패했습니다.")


def _quiescent(database):
    records = database.execute("SELECT * FROM runs_processrecord WHERE ended_at IS NULL").fetchall()
    if not records:
        return
    try:
        boot = host_boot_id()
        table = process_table()
    except Exception as error:
        raise DomainError("PROCESS_TERMINATION_UNCONFIRMED", "이전 프로세스의 종료를 확인할 수 없습니다.", 409) from error
    for record in records:
        if not record["host_boot_id"]:
            _fail("PROCESS_TERMINATION_UNCONFIRMED", "이전 프로세스의 부팅 식별자가 없습니다.")
        if record["host_boot_id"] != boot:
            continue
        if not record["pid"] or not record["process_start_identity"] or not record["process_group_id"]:
            _fail("PROCESS_TERMINATION_UNCONFIRMED", "이전 실행 프로세스의 식별 정보가 불완전합니다.")
        try:
            descendants = json.loads(record["descendants"])
            tracked = {int(pid): identity for pid, identity in descendants.items()}
        except (ValueError, TypeError, AttributeError):
            _fail("PROCESS_TERMINATION_UNCONFIRMED", "자손 프로세스의 식별 정보를 확인할 수 없습니다.")
        tracked[record["pid"]] = record["process_start_identity"]
        if any(pid in table for pid in tracked) or any(row[1] == record["process_group_id"] for row in table.values()):
            _fail("PROCESS_TERMINATION_UNCONFIRMED", "이전 프로세스가 살아 있거나 PID가 재사용되었습니다. 종료 상태를 확인하세요.")


def _references(database, entries, source_root):
    for row in database.execute("SELECT storage_key, sha256, size_bytes FROM artifacts_artifact WHERE expired_at IS NULL"):
        entry = entries.get(row["storage_key"])
        if not entry or entry["sha256"] != row["sha256"] or entry["size_bytes"] != row["size_bytes"]:
            _fail("BACKUP_REFERENCE_MISSING", "DB에서 참조하는 결과물·첨부 파일이 없거나 변경되었습니다.")
    for row in database.execute("SELECT registered_path, manifest FROM projects_dependencybundle"):
        try:
            dependency_root = Path(row["registered_path"]).relative_to(source_root)
            if not dependency_root.parts or dependency_root.parts[0] != "dependencies":
                raise ValueError()
            files = json.loads(row["manifest"])
            for item in files:
                key = (dependency_root / item["path"]).as_posix()
                _relative(key)
                entry = entries.get(key)
                if not entry or entry["sha256"] != item["sha256"] or entry["size_bytes"] != item["size"]:
                    raise ValueError()
        except (ValueError, KeyError, TypeError):
            _fail("BACKUP_REFERENCE_MISSING", "등록한 의존성 자료가 앱 데이터 안에 없거나 변경되었습니다.")


def verify_backup(path):
    root = _absolute(path)
    manifest = _read_json(root / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("version") != FORMAT_VERSION or not isinstance(manifest.get("files"), list):
        _fail("BACKUP_INVALID", "지원하지 않는 백업 형식입니다.")
    if not isinstance(manifest.get("source_data_dir"), str) or not isinstance(manifest.get("instance_id"), str):
        _fail("BACKUP_INVALID", "백업 원본 인스턴스 정보가 없습니다.")
    try:
        uuid.UUID(manifest["instance_id"])
    except ValueError:
        _fail("BACKUP_INVALID", "잘못된 원본 인스턴스 식별자입니다.")
    if len(manifest["files"]) > MAX_FILES:
        _fail("BACKUP_TOO_LARGE", "백업 파일 개수 제한을 초과했습니다.")
    entries = {}
    for row in manifest["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size_bytes"}:
            _fail("BACKUP_INVALID", "잘못된 백업 파일 명세입니다.")
        key = str(_relative(row["path"]))
        if key in entries or not isinstance(row["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", row["sha256"]):
            _fail("BACKUP_INVALID", "백업 파일이 중복되었거나 해시가 올바르지 않습니다.")
        if type(row["size_bytes"]) is not int or row["size_bytes"] < 0:
            _fail("BACKUP_INVALID", "잘못된 백업 파일 크기입니다.")
        entries[key] = row
    if not {"db.sqlite3", "secret-key", "instance-id", ".aiqa-runtime"}.issubset(entries):
        _fail("BACKUP_INVALID", "필수 백업 파일이 없습니다.")
    actual = {file.relative_to(root).as_posix() for file in _files(root)}
    if actual != {"manifest.json", *("files/" + key for key in entries)}:
        _fail("BACKUP_INVALID", "백업 manifest와 실제 파일 목록이 다릅니다.")
    for key, row in entries.items():
        if _transfer(root / "files" / key) != {"sha256": row["sha256"], "size_bytes": row["size_bytes"]}:
            _fail("BACKUP_CHECKSUM_MISMATCH", "백업 파일의 체크섬이 일치하지 않습니다.")
    if _identity_text(root / "files" / "instance-id") != manifest["instance_id"] or _identity_text(root / "files" / ".aiqa-runtime") != "aiqa-local-v1":
        _fail("BACKUP_INVALID", "백업의 인스턴스 식별 정보가 일치하지 않습니다.")
    with closing(_database(root / "files" / "db.sqlite3", immutable=True)) as database:
        _integrity(database)
        _references(database, entries, Path(manifest["source_data_dir"]))
    return manifest


def create_backup(destination=None):
    source = _absolute(settings.AIQA_DATA_DIR)
    target = _absolute(destination or source / "backups" / ("backup-" + str(uuid.uuid4())))
    if target == source or target in source.parents or SOURCE_ROOT == target or SOURCE_ROOT in target.parents:
        _fail("BACKUP_PATH_INVALID", "운영 데이터와 앱 소스를 덮어쓰는 경로는 사용할 수 없습니다.")
    if source in target.parents and not target.is_relative_to(source / "backups"):
        _fail("BACKUP_PATH_INVALID", "운영 데이터 안에서는 backups 하위 경로만 허용합니다.")
    if target.exists():
        _fail("BACKUP_DESTINATION_EXISTS", "백업에는 존재하지 않는 새 디렉토리를 지정하세요.")
    with runtime_lock(source, exclusive=True), closing(_database(source / "db.sqlite3")) as database:
        if _identity_text(source / "instance-id") != settings.AIQA_INSTANCE_ID:
            _fail("RESTORE_SOURCE_UNCONFIRMED", "현재 프로세스와 데이터 인스턴스가 다릅니다.")
        _integrity(database)
        _quiescent(database)
        source_files = [source / name for name in DATA_FILES if (source / name).exists() or (source / name).is_symlink()]
        for name in DATA_DIRECTORIES:
            source_files.extend(_files(source / name))
        if not target.parent.exists():
            private_directory(target.parent)
        target.mkdir(mode=0o700)
        private_directory(target / "files")
        database_path = target / "files" / "db.sqlite3"
        os.close(os.open(database_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        with closing(sqlite3.connect(database_path)) as backup:
            database.backup(backup)
            if backup.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                _fail("BACKUP_INVALID", "백업 DB를 단일 파일로 확정하지 못했습니다.")
        database_path.chmod(0o600)
        entries = [{"path": "db.sqlite3", **_transfer(database_path)}]
        for file in source_files:
            key = file.relative_to(source).as_posix()
            _relative(key)
            entries.append({"path": key, **_transfer(file, target / "files" / key)})
        _references(database, {row["path"]: row for row in entries}, source)
        _write_json(target / "manifest.json", {
            "version": FORMAT_VERSION, "created_at": timezone.now().isoformat(),
            "instance_id": settings.AIQA_INSTANCE_ID, "source_data_dir": str(source),
            "excluded": list(EXCLUDED), "files": sorted(entries, key=lambda row: row["path"]),
        })
        verify_backup(target)
    return target


def _invalidate_restored(database, source, destination, new_instance):
    now = timezone.now().isoformat()
    database.execute("DELETE FROM django_session")
    database.execute("UPDATE accounts_user SET authz_version=authz_version+1")
    database.execute("UPDATE work_requests_workrequest SET state='DRAFT',draft_version=draft_version+1,ready_revision_id=NULL,updated_at=?", (now,))
    database.execute("UPDATE approvals_approval SET valid=0, invalid_reason='RESTORED_SNAPSHOT'")
    database.execute("UPDATE approvals_publication SET state='UNKNOWN', state_version=state_version+1, error_code='RESTORED_SNAPSHOT' WHERE state NOT IN ('PUBLISHED','REJECTED','INVALIDATED') AND abandoned_at IS NULL")
    database.execute("UPDATE runs_job SET state='CANCELLED',error_code='RESTORED_SNAPSHOT',finished_at=?,lease_owner='',lease_epoch=0,lease_expires_at=NULL,heartbeat_at=NULL WHERE state NOT IN ('DONE','FAILED','CANCELLED')", (now,))
    database.execute("UPDATE runs_run SET status=CASE WHEN status='QUEUED' THEN 'CANCELLED' ELSE 'INTERRUPTED' END,state_version=state_version+1,error_code='RESTORED_SNAPSHOT',stop_reason='RESTORED_SNAPSHOT',current_stage='RESTORED_SNAPSHOT',finished_at=?,lease_owner='',lease_epoch=0,lease_expires_at=NULL,heartbeat_at=NULL,deadline_at=NULL WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')", (now,))
    database.execute("UPDATE runs_workerslot SET job_id=NULL,owner='',epoch=epoch+1,lease_expires_at=NULL,heartbeat_at=NULL,blocked_reason=''")
    database.execute("UPDATE runs_processrecord SET state='RESTORED',ended_at=? WHERE ended_at IS NULL", (now,))
    database.execute("UPDATE ai_aiinvocation SET state='INTERRUPTED',error_code='RESTORED_SNAPSHOT',ended_at=? WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')", (now,))
    database.execute("UPDATE ai_sandboxcheck SET native_passed=0,file_tools_passed=0,error_code='RESTORED_SNAPSHOT'")
    database.execute("UPDATE ai_modelprofile SET validation_status='UNVERIFIED',validated_cli_version='',validated_binary_hash='',validated_at=NULL,validation_error='RESTORED_SNAPSHOT'")
    database.execute("UPDATE projects_projecthealth SET readiness='SETUP_REQUIRED',source_ready=0,modification_ready=0,blocking_reasons='[\"RESTORED_SNAPSHOT\"]'")
    for row in database.execute("SELECT id, registered_path FROM projects_dependencybundle").fetchall():
        relative = Path(row[1]).relative_to(source)
        database.execute("UPDATE projects_dependencybundle SET registered_path=? WHERE id=?", (str(destination / relative), row[0]))
    database.execute("INSERT INTO accounts_accountaudit (bootstrap_identity,action,before,after,reason,created_at,actor_id,subject_id,project_id) VALUES (?,?,?,?,?,?,NULL,NULL,NULL)",
                     ("local-restore", "operations.restore", "{}", json.dumps({"instance_id": new_instance}), "백업 복원: 이전 실행·승인·세션 무효화", now))


def restore_backup(backup_path, destination):
    source = _absolute(settings.AIQA_DATA_DIR)
    backup_root = _absolute(backup_path)
    target = _absolute(destination)
    if target == source or target in source.parents or source in target.parents or target == SOURCE_ROOT or target in SOURCE_ROOT.parents or SOURCE_ROOT in target.parents:
        _fail("RESTORE_DESTINATION_INVALID", "운영 데이터·앱 소스와 겹치지 않는 새 디렉토리를 지정하세요.")
    if target == backup_root or target in backup_root.parents or backup_root in target.parents:
        _fail("RESTORE_DESTINATION_INVALID", "백업과 복원 경로는 분리해야 합니다.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        _fail("RESTORE_DESTINATION_EXISTS", "복원 대상은 새 빈 디렉토리여야 합니다.")
    with runtime_lock(source, exclusive=True), closing(_database(source / "db.sqlite3")) as original:
        if _identity_text(source / "instance-id") != settings.AIQA_INSTANCE_ID:
            _fail("RESTORE_SOURCE_UNCONFIRMED", "현재 프로세스와 데이터 인스턴스가 다릅니다.")
        _quiescent(original)
        manifest = verify_backup(backup_root)
        if manifest["source_data_dir"] != str(source) or manifest["instance_id"] != settings.AIQA_INSTANCE_ID:
            _fail("RESTORE_SOURCE_UNCONFIRMED", "백업 원본 인스턴스의 중지를 현재 호스트에서 확인해야 합니다.")
        private_directory(target)
        with runtime_lock(target, exclusive=True):
            if set(child.name for child in target.iterdir()) - {"locks", ".aiqa-runtime"}:
                _fail("RESTORE_DESTINATION_EXISTS", "복원 대상에 다른 파일이 생겼습니다.")
            _write_json(target / "restore-incomplete.json", {"backup": backup_root.name})
            for row in manifest["files"]:
                if row["path"] == "instance-id":
                    continue
                if row["path"] == ".aiqa-runtime" and (target / row["path"]).exists():
                    if _transfer(target / row["path"]) != {"sha256": row["sha256"], "size_bytes": row["size_bytes"]}:
                        _fail("BACKUP_INVALID", "앱 데이터 디렉토리 표식이 일치하지 않습니다.")
                    continue
                actual = _transfer(backup_root / "files" / row["path"], target / row["path"])
                if actual != {"sha256": row["sha256"], "size_bytes": row["size_bytes"]}:
                    _fail("BACKUP_CHECKSUM_MISMATCH", "복원 도중 백업 파일이 변경되었습니다.")
            new_instance = str(uuid.uuid4())
            with os.fdopen(os.open(target / "instance-id", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as stream:
                stream.write(new_instance + "\n")
            with closing(sqlite3.connect(target / "db.sqlite3")) as database:
                database.execute("PRAGMA foreign_keys=ON")
                with database:
                    _invalidate_restored(database, source, target, new_instance)
                _integrity(database)
            for name in DATA_DIRECTORIES:
                private_directory(target / name)
            _write_json(target / "restore-report.json", {
                "version": FORMAT_VERSION, "restored_at": timezone.now().isoformat(),
                "source_instance_id": manifest["instance_id"], "instance_id": new_instance,
                "required_actions": ["login", "native_sandbox_check", "model_validation", "project_inspection", "owner_reconfirmation"],
            })
            (target / "restore-incomplete.json").unlink()
    return target
