from django.contrib.auth import SESSION_KEY, logout
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone


class AccountSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and SESSION_KEY in request.session:
            logout(request)
        if request.user.is_authenticated:
            issued_at = request.session.get("issued_at")
            version = request.session.get("authz_version")
            now = timezone.now().timestamp()
            valid = isinstance(issued_at, (int, float)) and 0 <= now - issued_at < 12 * 60 * 60
            valid = valid and request.user.is_active and version == request.user.authz_version
            if not valid:
                logout(request)
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        public_names = {"accounts:login", "accounts:logout", "accounts:password"}
        wants_json = (
            "application/json" in request.headers.get("Accept", "")
            or request.content_type == "application/json"
            or match.view_name in {"conversations:updates", "runs:events"}
        )
        if not request.user.is_authenticated and match.view_name not in {"accounts:login", "accounts:logout"} and wants_json:
            return JsonResponse({"error": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}, status=401)
        if request.user.is_authenticated and request.user.must_change_password and match.view_name not in public_names:
            if wants_json:
                return JsonResponse({"error": "PASSWORD_CHANGE_REQUIRED", "message": "초기 비밀번호를 변경하세요."}, status=403)
            return redirect("accounts:password")
        return None
