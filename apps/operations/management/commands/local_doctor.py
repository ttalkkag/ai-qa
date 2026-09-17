import json

from django.core.management.base import BaseCommand, CommandError

from apps.operations.doctor import inspect_local


class Command(BaseCommand):
    help = "로컬 DB·경로·worker·Codex 로그인 상태를 점검합니다. 모델 추론이나 원격 쓰기는 실행하지 않습니다."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        checks = inspect_local(include_cli=True)
        if options["as_json"]:
            self.stdout.write(json.dumps({"checks": checks}, ensure_ascii=False))
        else:
            for check in checks:
                self.stdout.write(f"{check['status']:7} {check['id']}: {check['detail']}")
        if any(check["status"] == "FAIL" for check in checks):
            raise CommandError("로컬 환경 점검에서 실패 항목이 발견되었습니다.")
