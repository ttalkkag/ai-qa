import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

from apps.ai.models import ModelProfile, SandboxCheck
from apps.errors import DomainError
from apps.runs.models import Job, ProcessRecord, WorkerSlot


def inspect_local(*, include_cli=False):
    checks = []

    def add(key, status, detail):
        checks.append({"id": key, "status": status, "detail": detail})

    root = Path(settings.AIQA_DATA_DIR)
    try:
        info = root.lstat()
        private = stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o077 and info.st_uid == os.getuid()
        for name in ("secret-key", "instance-id", "db.sqlite3", "settings.json", "proxy-token"):
            file = root / name
            if not file.exists() and name not in ("secret-key", "instance-id", "db.sqlite3"):
                continue
            metadata = file.lstat()
            private = private and stat.S_ISREG(metadata.st_mode) and not metadata.st_mode & 0o077 and metadata.st_uid == os.getuid()
        add("private_data", "PASS" if private else "FAIL", "데이터·비밀 파일의 소유자와 비공개 권한" if private else "데이터 디렉토리 0700·비밀/DB 0600 권한을 확인하세요.")
        free = shutil.disk_usage(root).free
        add("free_space", "PASS" if free >= 2 * 1024 ** 3 else "FAIL", f"디스크 여유 {free // (1024 ** 2)} MiB; 신규 실행 최소 2 GiB")
    except OSError:
        add("private_data", "FAIL", "데이터 경로·필수 파일에 접근할 수 없습니다.")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        add("database", "PASS", "DB 읽기 연결 확인")
        slots = list(WorkerSlot.objects.all())
        if any(slot.blocked_reason for slot in slots):
            add("worker", "FAIL", "실행 종료 확인 또는 운영 복구가 필요합니다.")
        elif slots and any(slot.heartbeat_at and slot.heartbeat_at > timezone.now() - timedelta(seconds=45) for slot in slots):
            add("worker", "PASS", "최근 worker heartbeat 확인")
        else:
            add("worker", "WARN", "worker가 없거나 heartbeat가 45초 이상 지났습니다. process_jobs를 확인하세요.")
        unresolved = ProcessRecord.objects.filter(ended_at__isnull=True).count()
        add("process_records", "WARN" if unresolved else "PASS", f"종료 미확인 프로세스 기록 {unresolved}건; 백업은 실제 종료 확인 뒤 가능합니다.")
        profiles = {}
        for profile in ModelProfile.objects.order_by("key", "-version"):
            profiles.setdefault(profile.key, profile)
        ready = all(key in profiles and profiles[key].enabled and profiles[key].validation_status == "VALIDATED" for key in ("conversation", "implementation"))
        add("model_profiles", "PASS" if ready else "WARN", "필수 모델·effort의 호출 확인 기록 있음" if ready else "AI 연결 화면에서 요구 분석·수정 프로필을 확인하세요. 자동 모델 대체는 하지 않습니다.")
        latest = SandboxCheck.objects.filter(checks__0__id="docker_runtime").first()
        if include_cli:
            from apps.ai.services import require_sandbox_ready

            try:
                require_sandbox_ready()
                add("sandbox", "PASS", "Docker 연결·실행 이미지 확인; 모델 미호출")
            except DomainError:
                add("sandbox", "FAIL", "Docker Desktop과 실행 이미지를 확인하세요")
        else:
            recorded = latest is not None and latest.native_passed
            add("sandbox", "WARN" if recorded else "NOT_RUN", "Docker 점검 기록 있음; 현재 연결은 AI에서 확인하세요" if recorded else "Docker 환경 점검 기록이 없습니다")
        queued = Job.objects.filter(state="QUEUED").count()
        add("queue", "PASS", f"대기 작업 {queued}건; 조회는 작업을 실행하지 않습니다.")
    except DatabaseError:
        add("database", "FAIL", "DB 테이블에 접근할 수 없습니다. migrate와 데이터 경로를 확인하세요.")
    if include_cli:
        from integrations.codex.container import check_connection

        if (root / ".codex").exists() or (root / "config.toml").exists():
            add("codex_login", "FAIL", "앱 데이터 루트의 예상하지 못한 Codex 구성 파일을 확인하세요.")
        else:
            result = check_connection()
            connected = result.get("connected") and result.get("supported_cli")
            add("codex_login", "PASS" if connected else "FAIL", "Codex 버전·기존 로그인 확인; 추론 미실행" if connected else "Codex 설치 버전·호스트 로그인을 확인하세요.")
    else:
        add("codex_login", "NOT_RUN", "이 화면은 CLI를 실행하지 않습니다. 터미널 local_doctor에서 로그인 상태를 확인하세요.")
    if (root / "restore-incomplete.json").exists():
        add("restore", "FAIL", "복원이 완료되지 않은 데이터 디렉토리입니다. 실행하지 마세요.")
    elif (root / "restore-report.json").exists():
        add("restore", "WARN", "복원본입니다. 환경 재점검과 요청 소유자의 새 확인·실행 제출이 필요합니다.")
    return checks
