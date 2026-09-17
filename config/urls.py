from django.urls import include, path
from django.views.generic import RedirectView


urlpatterns = [
    path("", RedirectView.as_view(pattern_name="projects:list", permanent=False)),
    path("", include("apps.portal.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("projects/", include("apps.projects.urls")),
    path("", include("apps.conversations.urls")),
    path("", include("apps.work_requests.urls")),
    path("runs/", include("apps.runs.urls")),
    path("ai/", include("apps.ai.urls")),
    path("", include("apps.artifacts.urls")),
    path("", include("apps.approvals.urls")),
    path("operations/", include("apps.operations.urls")),
]
