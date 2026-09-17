import sqlite3

from django.core.management.base import BaseCommand, CommandError

from apps.errors import DomainError
from apps.operations.backup import create_backup


class Command(BaseCommand):
    help = "웹·worker·자손을 중지한 로컬 인스턴스를 비공개 디렉토리에 백업합니다."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--destination")

    def handle(self, *args, **options):
        try:
            result = create_backup(options["destination"])
        except DomainError as error:
            raise CommandError(f"{error.code}: {error.message}") from None
        except (OSError, RuntimeError, sqlite3.Error):
            raise CommandError("백업 실패: 경로·DB·maintenance lock과 모든 실행 프로세스의 중지 상태를 확인하세요.") from None
        self.stdout.write(f"백업 완료: {result}")
