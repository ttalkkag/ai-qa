"""Synthetic browser fixtures: no model call, execution, or remote publication."""

import json
import os
import subprocess
from pathlib import Path

import django


django.setup()

from apps.accounts.models import ProjectMembership, User
from apps.artifacts.services import seal_result
from apps.artifacts.trees import tree_digest
from apps.conversations.models import Conversation
from apps.projects.services import register_project
from apps.runs.models import Run
from apps.work_requests.models import RequestRevision, WorkRequest
from tests.e2e.board_fixture import prepare_board, verification_command


root = Path(os.environ["AIQA_E2E_FIXTURES"])
root.mkdir(mode=0o700, parents=True)
password = "Synthetic-browser-only-9876!"
admin = User.objects.create_user(
    username="browser-admin", password=password, role="ADMIN",
    display_name="시험 관리자", must_change_password=False,
)
manager = User.objects.create_user(
    username="browser-manager", password=password, role="MANAGER",
    display_name="시험 매니저", must_change_password=False,
)
other = User.objects.create_user(
    username="browser-other", password=password, role="MANAGER",
    display_name="다른 매니저", must_change_password=False,
)
git_env = {
    "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"],
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Synthetic Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Synthetic Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}
source = root / "source"
source.mkdir()
(source / "example.txt").write_text("value = 1\n", encoding="utf-8")
subprocess.run(["git", "init", "-b", "main", str(source)], env=git_env, check=True, capture_output=True)
subprocess.run(["git", "-C", str(source), "add", "example.txt"], env=git_env, check=True)
subprocess.run(
    ["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "commit", "-m", "synthetic fixture"],
    env=git_env, check=True, capture_output=True,
)
sha = subprocess.check_output(
    ["git", "-C", str(source), "rev-parse", "HEAD"], env=git_env, text=True,
).strip()
project = register_project(
    admin, "브라우저 검증 프로젝트", str(source), "main", description="비민감 시험 자료",
)
for user in (manager, other):
    ProjectMembership.objects.create(project=project, user=user, granted_by=admin)
conversation = Conversation.objects.create(
    project=project, owner=manager, title="합성 결과 전용 대화",
    creation_key="seed-result", creation_hash="a" * 64,
)
request = WorkRequest.objects.create(
    project=project, owner=manager, conversation=conversation, draft={"title": "합성 결과 조회"},
)
revision = RequestRevision.objects.create(
    request=request, number=1, source_draft_version=1, snapshot={"title": "합성 결과 조회"},
    content_hash="a" * 64, confirmed_by=manager, project_config_version=1, base_commit_sha=sha,
)
run = Run.objects.create(
    project=project, request_revision=revision, requested_by=manager, baseline_commit_sha=sha,
    project_config_version=1, model_profile_version=1, idempotency_key="seed-result-run",
    status="SUCCEEDED",
)
before, after = root / "before", root / "after"
before.mkdir()
after.mkdir()
(before / "example.txt").write_text("value = 1\n", encoding="utf-8")
(after / "example.txt").write_text("value = 2\n", encoding="utf-8")
after_hash = tree_digest(after)
bundle = seal_result(run.id, {
    "baseline_dir": before, "candidate_dir": after,
    "checked_source_tree_sha256": after_hash, "processes_stopped": True,
    "checks": [{
        "phase": "AFTER", "check_id": "synthetic", "checked_source_tree_sha256": after_hash,
        "status": "PASSED",
    }],
    "summary": "합성 결과 — 실제 AI 작업이 아닙니다.", "complete": True, "logs": {},
})
artifact = bundle.artifacts.get(kind="CANONICAL_PATCH")
review_fixtures = {}
for browser in ("chromium", "firefox", "webkit"):
    review_fixtures[browser] = {}
    for flow in ("complete", "rework"):
        chat = Conversation.objects.create(project=project, owner=manager, title=f"{browser} {flow}", creation_key=f"review-{browser}-{flow}", creation_hash="b" * 64)
        task = WorkRequest.objects.create(project=project, owner=manager, conversation=chat, draft={"title": chat.title})
        spec = RequestRevision.objects.create(request=task, number=1, source_draft_version=1, snapshot={"title": chat.title, "expected_behavior": "값 변경"}, content_hash="b" * 64, confirmed_by=manager, project_config_version=1, base_commit_sha=sha)
        attempt = Run.objects.create(project=project, request_revision=spec, requested_by=manager, baseline_commit_sha=sha, project_config_version=1, model_profile_version=1, idempotency_key=f"review-{browser}-{flow}", status="SUCCEEDED", result_branch="temp/aiqa-browser-fixture", result_commit_sha="f" * 40)
        result = seal_result(attempt.id, {"baseline_dir": before, "candidate_dir": after, "checked_source_tree_sha256": after_hash, "processes_stopped": True, "checks": [{"phase": "AFTER", "check_id": "synthetic", "checked_source_tree_sha256": after_hash, "status": "PASSED"}], "summary": "리뷰 흐름용 합성 결과", "complete": True})
        attempt.result_bundle_id = result.id
        attempt.save(update_fields=["result_bundle_id"])
        review_fixtures[browser][flow] = {"bundle_id": str(result.id), "request_id": str(task.id)}
board_sources = {name: prepare_board(root / ("board-" + name)) for name in ("chromium", "firefox", "webkit")}
print(json.dumps({
    "project_id": str(project.id), "conversation_id": str(conversation.id),
    "request_id": str(request.id), "bundle_id": str(bundle.id),
    "artifact_id": str(artifact.id), "run_id": str(run.id),
    "review_fixtures": review_fixtures,
    "source": str(source), "source_commit": sha,
    "board_sources": board_sources, "board_check": verification_command(),
}, ensure_ascii=False))
