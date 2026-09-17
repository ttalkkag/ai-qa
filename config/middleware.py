import hmac
import ipaddress

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.db import OperationalError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.deprecation import MiddlewareMixin

from apps.errors import DomainError


class LocalAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
            if not peer.is_loopback:
                return HttpResponse("로컬 프록시를 통해 접속해 주세요.", status=403)
            request.get_host()
        except (ValueError, DisallowedHost):
            return HttpResponse("허용되지 않은 접속입니다.", status=400)
        if settings.AIQA_ACCESS_MODE == "lan_https":
            token = request.META.get("HTTP_X_AIQA_PROXY_TOKEN", "")
            if not hmac.compare_digest(token, settings.RUNTIME["proxy_token"]) or request.META.get("HTTP_X_FORWARDED_PROTO") != "https":
                return HttpResponse("HTTPS 프록시 확인이 필요합니다.", status=403)
            request.META["wsgi.url_scheme"] = "https"
        else:
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
            request.META.pop("HTTP_X_FORWARDED_HOST", None)
        response = self.get_response(request)
        if not request.path.startswith(settings.STATIC_URL):
            response["Cache-Control"] = "no-store, private"
        response["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response["Referrer-Policy"] = "same-origin"
        return response


class DomainErrorMiddleware(MiddlewareMixin):
    def process_exception(self, request, exception):
        if isinstance(exception, OperationalError) and "locked" in str(exception).lower():
            exception = DomainError("BUSY", "다른 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.", 409)
        if not isinstance(exception, DomainError):
            return None
        payload = {"error": exception.code, "message": exception.message}
        if "application/json" in request.headers.get("Accept", "") or request.path.endswith("/updates/"):
            return JsonResponse(payload, status=exception.status)
        return render(request, "error.html", payload, status=exception.status)
