import sqlite3

from django.core.management.base import BaseCommand, CommandError

from apps.errors import DomainError
from apps.operations.backup import restore_backup


class Command(BaseCommand):
    help = "백업을 새 빈 데이터 디렉토리에 복원하고 이전 실행·승인·세션을 무효화합니다."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("backup_path")
        parser.add_argument("--destination", required=True)

    def handle(self, *args, **options):
        try:
            result = restore_backup(options["backup_path"], options["destination"])
        except DomainError as error:
            raise CommandError(f"{error.code}: {error.message}") from None
        except (OSError, RuntimeError, sqlite3.Error):
            raise CommandError("복원 실패: 원본은 보존되었습니다. 중지 상태·백업·새 대상 경로를 확인하세요. 불완전 복원본은 실행하지 마세요.") from None
        self.stdout.write(f"복원 완료: {result}\n이 경로로 AIQA_DATA_DIR를 지정하고 로그인·실행 보호·모델·프로젝트를 다시 점검하세요. 과거 작업은 자동 재개되지 않습니다.")
