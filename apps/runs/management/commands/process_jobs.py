import fcntl
import os
import signal
import stat
import threading
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.runs.worker import worker_loop
from config.runtime import runtime_lock


@contextmanager
def worker_lock(path):
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise CommandError("안전한 작업자 잠금 파일을 열 수 없습니다.") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise CommandError("작업자 잠금 파일은 현재 사용자의 비공개 일반 파일이어야 합니다.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("이 데이터 디렉터리의 작업자가 이미 실행 중입니다.") from None
        yield
    finally:
        os.close(descriptor)


class Command(BaseCommand):
    help = "같은 Django 프로젝트의 대화 및 실행 작업을 처리합니다."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="각 슬롯의 작업 하나씩 처리한 뒤 종료합니다.")

    def handle(self, *args, **options):
        lock_path = settings.AIQA_DATA_DIR / "worker.lock"
        with runtime_lock(settings.AIQA_DATA_DIR, config_hash=settings.AIQA_CONFIG_HASH), worker_lock(lock_path):
            stop = threading.Event()
            previous = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            try:
                worker_loop(str(uuid.uuid4()), stop, once=options["once"])
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
