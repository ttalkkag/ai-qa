from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.errors import DomainError

from . import services
from .forms import ChangeAccountForm, CreateAccountForm, LoginForm, MembershipForm, PasswordForm, ProfileForm, ResetPasswordForm
from .models import User


def _error(request, error):
    if "application/json" in request.headers.get("Accept", "") or request.content_type == "application/json":
        return JsonResponse({"error": error.code, "message": error.message}, status=error.status)
    if error.status == 401:
        return redirect("accounts:login")
    return render(request, "accounts/error.html", {"error": error}, status=error.status)


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("accounts:password" if request.user.must_change_password else "accounts:me")
    form = LoginForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            user = services.authenticate(**form.cleaned_data, remote_addr=request.META.get("REMOTE_ADDR", ""))
        except DomainError as error:
            form.add_error(None, error.message)
            status = error.status
        else:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            request.session["issued_at"] = timezone.now().timestamp()
            request.session["authz_version"] = user.authz_version
            request.session.set_expiry(0)
            return redirect("accounts:password" if user.must_change_password else "/")
    return render(request, "accounts/login.html", {"form": form}, status=status)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


@login_required
@require_http_methods(["GET", "POST"])
def password_view(request):
    form = PasswordForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            services.change_password(request.user, form.cleaned_data["current_password"], form.cleaned_data["new_password"])
        except DomainError as error:
            form.add_error(None, error.message)
            status = error.status
        else:
            logout(request)
            messages.success(request, "비밀번호를 변경했습니다. 새 비밀번호로 로그인하세요.")
            return redirect("accounts:login")
    return render(request, "accounts/password.html", {"form": form}, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def me_view(request):
    try:
        actor = services.require_access(request.user, "account.profile")
    except DomainError as error:
        return _error(request, error)
    form = ProfileForm(
        request.POST if request.method == "POST" else None,
        initial={"display_name": actor.display_name},
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                services.update_profile(actor, **form.cleaned_data)
            except DomainError as error:
                form.add_error(None, error.message)
                status = error.status
            else:
                messages.success(request, "내 정보를 저장했습니다.")
                return redirect("accounts:me")
        else:
            status = 400
    return render(request, "accounts/me.html", {"form": form, "account_username": actor.username}, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def manage_view(request):
    try:
        actor = services.require_access(request.user, "account.manage")
        projects = list(services.list_accessible_projects(actor))
    except DomainError as error:
        return _error(request, error)
    create_form = CreateAccountForm()
    error = None
    status = 200
    if request.method == "POST":
        action = request.POST.get("action")
        classes = {"create": CreateAccountForm, "change": ChangeAccountForm, "membership": MembershipForm, "reset": ResetPasswordForm}
        form_class = classes.get(action)
        if form_class is None:
            return _error(request, DomainError("INVALID_INPUT"))
        form = form_class(request.POST, **({"projects": projects} if action == "membership" else {}))
        if form.is_valid():
            values = form.cleaned_data.copy()
            try:
                if action == "create":
                    services.create_account(actor, **values)
                elif action == "change":
                    target_id = values.pop("target_id")
                    expected_version = values.pop("expected_authz_version")
                    reason = values.pop("reason")
                    services.change_account(actor, target_id, expected_version, values, reason)
                elif action == "membership":
                    values["granted"] = values["granted"] == "yes"
                    services.set_membership(actor, **values)
                else:
                    services.reset_password(actor, **values)
            except DomainError as domain_error:
                error = domain_error.message
                status = domain_error.status
            else:
                messages.success(request, "계정 설정을 저장했습니다.")
                return redirect("accounts:manage")
        else:
            error = "입력 항목을 확인하세요."
            status = 400
        if action == "create":
            create_form = form
    rows = []
    for user in User.objects.order_by("username"):
        rows.append({
            "account": user,
            "change_form": ChangeAccountForm(
                auto_id=f"change-{user.pk}-%s",
                initial={"target_id": user.pk, "expected_authz_version": user.authz_version, "display_name": user.display_name, "role": user.role, "is_active": user.is_active},
            ),
            "reset_form": ResetPasswordForm(auto_id=f"reset-{user.pk}-%s", initial={"target_id": user.pk}),
            "membership_form": MembershipForm(auto_id=f"membership-{user.pk}-%s", projects=projects, initial={"user_id": user.pk}),
            "memberships": user.memberships.select_related("project"),
        })
    return render(request, "accounts/manage.html", {"create_form": create_form, "rows": rows, "error": error}, status=status)
