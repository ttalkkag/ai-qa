from getpass import getpass

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from apps.accounts.models import AccountAudit, User
from apps.accounts.services import _validate_password
from apps.errors import DomainError


class Command(BaseCommand):
    help = "최초 앱 관리자를 생성합니다. 비밀번호는 숨김 입력만 받습니다."

    def add_arguments(self, parser):
        parser.add_argument("--username")
        parser.add_argument("--email", default="")

    def handle(self, *args, **options):
        username = options["username"] or input("사용자 이름: ").strip()
        if User.objects.filter(username=username).exists():
            raise CommandError("이미 존재하는 사용자 이름입니다. 기존 계정을 변경하지 않았습니다.")
        if User.objects.exists():
            raise CommandError("최초 계정이 이미 있습니다. 추가 계정은 관리자 계정 관리 화면에서 생성하세요.")
        password = getpass("비밀번호: ")
        if password != getpass("비밀번호 확인: "):
            raise CommandError("비밀번호가 일치하지 않습니다.")
        user = User(username=username, display_name=username, email=options["email"], role=User.Role.ADMIN, must_change_password=False)
        try:
            _validate_password(password, user)
            user.set_password(password)
            user.full_clean(exclude=["password"])
            with transaction.atomic():
                if User.objects.exists():
                    raise CommandError("최초 계정이 이미 있습니다. 추가 계정은 관리자 계정 관리 화면에서 생성하세요.")
                user.save()
                AccountAudit.objects.create(subject=user, bootstrap_identity="local-terminal", action="account.bootstrap", after={"username": username, "role": User.Role.ADMIN})
        except (ValidationError, DomainError):
            raise CommandError("사용자 이름, 이메일 또는 비밀번호가 안전성 기준을 충족하지 않습니다.") from None
        except IntegrityError:
            raise CommandError("이미 존재하는 사용자 이름입니다.") from None
        self.stdout.write(self.style.SUCCESS(f"관리자 생성: {user.pk} {user.username}"))
