from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.services import require_access


@login_required
@require_GET
def admin(request):
    require_access(request.user, "account.manage")
    return render(request, "portal/admin.html")


@login_required
@require_GET
def docs(request):
    actor = require_access(request.user, "account.password")
    return render(request, "portal/docs.html", {"can_manage": actor.role == "ADMIN"})
