from django import template
from django.urls import reverse


register = template.Library()


def _item(label, route, *args):
    return {"label": label, "url": reverse(route, args=args)}


@register.inclusion_tag("includes/navigation.html", takes_context=True)
def page_navigation(context):
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    view = match.view_name if match else ""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"items": [_item("Login", "accounts:login")]}
    admin = user.role == "ADMIN"
    if view == "projects:list":
        return {
            "items": [_item("Project", "projects:list")],
            "add_project": admin and context.get("can_manage") and bool(context.get("projects")),
        }
    roots = {
        "work_requests:list": ("Request", "work_requests:list"),
        "accounts:me": ("Account", "accounts:me"),
        "portal:docs": ("Docs", "portal:docs"),
    }
    if admin:
        roots.update({
            "ai:settings": ("AI", "ai:settings"),
            "portal:admin": ("Admin", "portal:admin"),
        })
    if view in roots:
        return {"items": [_item(*roots[view])]}
    admin_pages = {
        "accounts:manage": "Users",
        "operations:dashboard": "Operations",
    }
    if admin and view in admin_pages:
        return {"items": [_item("Admin", "portal:admin"), _item(admin_pages[view], view)]}
    if view == "accounts:password":
        return {"items": [_item("Account", "accounts:me"), _item("Password", "accounts:password")]}
    if match and match.namespace == "approvals" and view != "approvals:policy":
        items = [_item("Admin", "portal:admin"), _item("Approvals", "approvals:list")] if admin else [_item("Request", "work_requests:list")]
        publication = context.get("publication")
        job = context.get("job")
        if view == "approvals:list":
            if not admin:
                items.append(_item("Publication", "approvals:list"))
        elif publication:
            if not admin:
                work_request = publication.run.request_revision.request
                items.append(_item(work_request.title, "work_requests:detail", work_request.pk))
            items.append(_item("Review Publication", "approvals:detail", publication.pk))
        elif admin and job and job.kind == "publication.prepare":
            items.append(_item("Prepare Publication", "approvals:preparation-status", job.pk))
        else:
            items.append({"label": "Notice", "url": None})
        return {"items": items}
    if match and match.namespace in {"work_requests", "runs", "artifacts"}:
        items = [_item("Request", "work_requests:list")]
        bundle = context.get("bundle")
        work_request = context.get("work_request") or (bundle.request_revision.request if bundle else None)
        if work_request:
            items.append(_item(work_request.title, "work_requests:detail", work_request.pk))
        run = context.get("run")
        if bundle:
            items.append(_item("Run", "runs:detail", bundle.run_id))
            items.append(_item("Result", "artifacts:detail", bundle.pk))
        elif run:
            items.append(_item("Run", "runs:detail", run["id"]))
        elif not work_request:
            items.append({"label": "Notice", "url": None})
        return {"items": items}
    items = [_item("Project", "projects:list")]
    conversation = context.get("conversation")
    project = context.get("project") or (conversation.project if conversation else None)
    if project:
        items.append(_item(project.name, "projects:detail", project.pk))
    project_pages = {
        "projects:create": ("Add Project", "projects:create"),
        "projects:edit": ("Edit Project", "projects:edit"),
        "projects:configure": ("Configuration", "projects:configure"),
        "projects:dependencies": ("Dependencies", "projects:dependencies"),
        "approvals:policy": ("Publication Settings", "approvals:policy"),
    }
    if view == "projects:create" and admin:
        items.append(_item(*project_pages[view]))
    elif project and view in project_pages:
        items.append(_item(*project_pages[view], project.pk))
    elif project and match and match.namespace == "conversations":
        items.append(_item("Conversations", "conversations:list", project.pk))
        if conversation:
            items.append(_item(conversation.title, "conversations:detail", conversation.pk))
    elif not project and view != "projects:list":
        items.append({"label": "Notice", "url": None})
    return {"items": items}
