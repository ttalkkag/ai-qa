from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.services import require_access

from .doctor import inspect_local


@login_required
@require_GET
def dashboard(request):
    require_access(request.user, "account.manage")
    return render(request, "operations/dashboard.html", {"checks": inspect_local()})
