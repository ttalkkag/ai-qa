import io
import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import StopUpload
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import ProjectMembership
from apps.artifacts.models import Artifact, ArtifactExpiry, ResultBundle
from apps.artifacts.services import expire_results, get_result, materialize_baseline, open_artifact, pin_attachments, pin_bundle, read_bundle_file, seal_result, snapshot_attachments, store_attachment, unpin_bundle, verify_bundle
from apps.artifacts.storage import data_root, read_private, redact_text, write_private
from apps.artifacts.trees import collect_tree, make_patch, tree_digest
from apps.artifacts.uploads import LimitedAttachmentUploadHandler
from apps.conversations.models import Conversation
from apps.errors import DomainError
from apps.projects.models import Project
from apps.runs.models import Run
from apps.work_requests.models import RequestRevision, WorkRequest


pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(settings, tmp_path):
    settings.AIQA_DATA_DIR = tmp_path / "private"
    User = get_user_model()
    admin = User.objects.create_user(username="artifact-admin", role="ADMIN", password="test")
    manager = User.objects.create_user(username="artifact-manager", role="MANAGER", password="test")
    other = User.objects.create_user(username="artifact-other", role="MANAGER", password="test")
    project = Project.objects.create(name="Artifact", canonical_repo_path=str(tmp_path / "source"), created_by=admin)
    for user in (manager, other):
        ProjectMembership.objects.create(user=user, project=project, granted_by=admin)
    conversation = Conversation.objects.create(project=project, owner=manager, title="Request", creation_key="one", creation_hash="a" * 64)
    request = WorkRequest.objects.create(project=project, owner=manager, conversation=conversation)
    revision = RequestRevision.objects.create(request=request, number=1, source_draft_version=1, snapshot={}, content_hash="a" * 64, confirmed_by=manager, project_config_version=1, base_commit_sha="b" * 40)
    run = Run.objects.create(project=project, request_revision=revision, requested_by=manager, baseline_commit_sha="b" * 40, project_config_version=1, model_profile_version=1, idempotency_key="run", status="SUCCEEDED")
    return {"admin": admin, "manager": manager, "other": other, "project": project, "conversation": conversation, "run": run, "tmp": tmp_path}


def create_bundle(setup, logs=None):
    before = setup["tmp"] / "before"
    after = setup["tmp"] / "after"
    before.mkdir(exist_ok=True)
    after.mkdir(exist_ok=True)
    (before / "app.py").write_text("value = 1\n")
    (after / "app.py").write_text("value = 2\n")
    after_hash = tree_digest(after)
    return seal_result(setup["run"].id, {"baseline_dir": before, "candidate_dir": after, "checked_source_tree_sha256": after_hash, "processes_stopped": True, "checks": [{"phase": "AFTER", "checked_source_tree_sha256": after_hash, "status": "PASSED"}], "summary": "값 수정", "complete": True, "logs": logs or {}})


def test_owner_completes_result_then_admin_approves_without_remote_job(setup, client):
    from apps.runs.models import Job

    bundle = create_bundle(setup)
    def login(user):
        client.force_login(user)
        session = client.session
        session["authz_version"] = user.authz_version
        session["issued_at"] = timezone.now().timestamp()
        session.save()
    login(setup["manager"])
    url = reverse("artifacts:detail", args=[bundle.id])
    assert "작업 완료 · 관리자에게 전달" in client.get(url).content.decode()
    decision = f"/results/{bundle.id}/review/"
    data = {"action": "accept", "manifest_sha256": bundle.manifest_sha256}
    assert client.post(decision, data).status_code == 302
    assert "관리자 승인 대기" in client.get(url).content.decode()
    login(setup["admin"])
    assert client.post(decision, {**data, "action": "approve"}).status_code == 302
    assert "관리자 승인 완료" in client.get(url).content.decode()
    assert not Job.objects.filter(kind__startswith="publication.").exists()


def test_local_git_result_is_visible_only_to_admin(setup, client):
    run = setup["run"]
    run.result_branch = "temp/aiqa-fixture"
    run.result_commit_sha = "f" * 40
    run.save()
    bundle = create_bundle(setup)
    url = reverse("artifacts:detail", args=[bundle.id])
    for role, visible in (("manager", False), ("admin", True)):
        user = setup[role]
        client.force_login(user)
        session = client.session
        session["authz_version"] = user.authz_version
        session["issued_at"] = timezone.now().timestamp()
        session.save()
        response = client.get(url)
        assert response.status_code == 200
        assert ("temp/aiqa-fixture" in response.content.decode()) == visible


def test_attachment_snapshot_hash_and_acl(setup):
    artifact = store_attachment(setup["manager"], setup["conversation"].id, SimpleUploadedFile("steps.txt", b"reproduce"))
    snapshot = snapshot_attachments(setup["manager"], setup["conversation"].id, [artifact.id])
    assert snapshot[0]["sha256"] == artifact.sha256
    assert open_artifact(setup["manager"], artifact.id).read() == b"reproduce"
    with pytest.raises(DomainError) as caught:
        open_artifact(setup["other"], artifact.id)
    assert caught.value.code == "NOT_FOUND"
    ProjectMembership.objects.filter(user=setup["manager"]).delete()
    with pytest.raises(DomainError):
        open_artifact(setup["manager"], artifact.id)


def test_result_review_permissions_rework_and_stale_result(setup):
    from apps.artifacts.reviews import decide_result

    bundle = create_bundle(setup)
    args = (bundle.id, "accept", bundle.manifest_sha256)
    for actor in (setup["admin"], setup["other"]):
        with pytest.raises(DomainError):
            decide_result(actor, *args)
    with pytest.raises(DomainError):
        decide_result(setup["manager"], bundle.id, "approve", bundle.manifest_sha256)
    with pytest.raises(DomainError, match="현재 결과"):
        decide_result(setup["admin"], bundle.id, "approve", bundle.manifest_sha256)
    with pytest.raises(DomainError):
        decide_result(setup["manager"], bundle.id, "accept", "0" * 64)
    original = dict(bundle.request_revision.snapshot)
    review = decide_result(setup["manager"], bundle.id, "rework", bundle.manifest_sha256, "호환성 안내 추가")
    request = bundle.request_revision.request
    request.refresh_from_db()
    assert request.draft_version == 2 and request.state == "DRAFT" and request.ready_revision is None
    assert "호환성 안내 추가" in request.draft["expected_behavior"]
    bundle.request_revision.refresh_from_db()
    assert bundle.request_revision.snapshot == original
    assert decide_result(setup["manager"], bundle.id, "rework", bundle.manifest_sha256, "호환성 안내 추가").pk == review.pk
    request.refresh_from_db()
    assert request.draft_version == 2
    with pytest.raises(DomainError):
        decide_result(setup["manager"], *args)


def test_result_review_new_draft_invalidates_pending_admin_approval(setup):
    from apps.artifacts.reviews import decide_result
    from apps.work_requests.services import save_draft

    bundle = create_bundle(setup)
    decide_result(setup["manager"], bundle.id, "accept", bundle.manifest_sha256)
    request = bundle.request_revision.request
    save_draft(setup["manager"], request.id, 1, {"title": "다른 요청"})
    with pytest.raises(DomainError) as caught:
        decide_result(setup["admin"], bundle.id, "approve", bundle.manifest_sha256)
    assert caught.value.code == "RESULT_CHANGED"


@pytest.mark.parametrize("name,content", [("unsafe.html", b"<script>bad</script>"), ("fake.png", b"not an image"), ("binary.txt", b"\xff\0"), ("huge.txt", b"x" * (5 * 1024 * 1024 + 1))])
def test_invalid_attachments_rejected_before_storage(setup, name, content):
    with pytest.raises(DomainError):
        store_attachment(setup["manager"], setup["conversation"].id, SimpleUploadedFile(name, content))
    assert not Artifact.objects.exists()


def test_image_reencoded_and_exif_removed(setup):
    output = io.BytesIO()
    exif = Image.Exif()
    exif[0x010E] = "private camera metadata"
    Image.new("RGB", (3, 3)).save(output, format="JPEG", exif=exif)
    artifact = store_attachment(setup["manager"], setup["conversation"].id, SimpleUploadedFile("screen.jpg", output.getvalue()))
    with Image.open(open_artifact(setup["manager"], artifact.id)) as picture:
        assert not picture.getexif()
    assert artifact.sha256 != __import__("hashlib").sha256(output.getvalue()).hexdigest()


def test_secret_attachment_not_forwarded(setup):
    artifact = store_attachment(setup["manager"], setup["conversation"].id, SimpleUploadedFile("trace.log", b'"api_key": "do-not-store-in-ai"'))
    assert artifact.visibility == "RESTRICTED"
    with pytest.raises(DomainError):
        snapshot_attachments(setup["manager"], setup["conversation"].id, [artifact.id])


def test_patch_reconstructs_binary_delete_modes_and_existing_link(setup):
    before, after = setup["tmp"] / "a", setup["tmp"] / "b"
    before.mkdir()
    after.mkdir()
    (before / "old.txt").write_text("delete")
    for root in (before, after):
        (root / "script").write_text("exit 0\n")
        (root / "link").symlink_to("script")
    (after / "script").chmod(0o755)
    (after / "new.bin").write_bytes(b"\0\xff\1")
    baseline, bc = collect_tree(before)
    candidate, cc = collect_tree(after)
    patch = make_patch(baseline, bc, candidate, cc)
    assert b"GIT binary patch" in patch
    assert b"old mode 100644" in patch
    assert b"deleted file mode" in patch


def test_after_hash_mismatch_blocks_seal(setup):
    before = setup["tmp"] / "a"
    before.mkdir()
    with pytest.raises(DomainError) as caught:
        seal_result(setup["run"].id, {"baseline_dir": before, "candidate_dir": before, "checked_source_tree_sha256": "0" * 64, "processes_stopped": True})
    assert caught.value.code == "SOURCE_CHANGED_DURING_CHECK"
    assert not ResultBundle.objects.exists()


def test_bundle_verification_baseline_and_tamper(setup):
    bundle = create_bundle(setup)
    assert verify_bundle(bundle.id, bundle.manifest_sha256).pk == bundle.pk
    baseline = materialize_baseline(bundle.id, setup["tmp"] / "restored")
    assert (baseline / "app.py").read_text() == "value = 1\n"
    checks = json.loads(read_bundle_file(bundle.id, "checks.json"))
    assert checks[0]["checked_source_tree_sha256"] == bundle.result_tree_sha256
    artifact = bundle.artifacts.get(kind="CHECK_REPORT")
    (data_root() / artifact.storage_key).write_text("tamper")
    with pytest.raises(DomainError):
        open_artifact(setup["manager"], bundle.artifacts.get(kind="CANONICAL_PATCH").id)
    assert get_result(setup["manager"], bundle.id).corruption_reason


def test_versions_are_immutable_paths(setup):
    first = create_bundle(setup)
    second = create_bundle(setup)
    assert (first.version, second.version) == (1, 2)
    assert verify_bundle(first.id, first.manifest_sha256)
    assert verify_bundle(second.id, second.manifest_sha256)


def test_summary_display_keeps_text_and_does_not_make_model_links_clickable():
    from apps.artifacts.views import _summary_sections
    sections = _summary_sections("**변경 내용**\n\n[README.md](/private/workspace/README.md)만 수정\n\n<script>alert(1)</script>")
    assert sections[0]["heading"] == "변경 내용"
    assert sections[1]["text"] == "README.md만 수정"
    assert sections[2]["text"] == "<script>alert(1)</script>"


def test_normal_log_expiry_keeps_manifest_valid(setup):
    bundle = create_bundle(setup, {"worker": ["Author", "ization: Bearer private-key\n\x1b[31mend"]})
    log = bundle.artifacts.get(kind="REDACTED_LOG")
    assert b"private-key" not in read_bundle_file(bundle.id, "logs/0.txt")
    assert b"\x1b" not in read_bundle_file(bundle.id, "logs/0.txt")
    before_hash = bundle.manifest_sha256
    assert expire_results(bundle.sealed_at + timedelta(days=8)) == 1
    assert ArtifactExpiry.objects.filter(artifact=log).exists()
    assert verify_bundle(bundle.id, before_hash)


def test_early_log_loss_is_corruption(setup):
    bundle = create_bundle(setup, {"worker": "normal log"})
    log = bundle.artifacts.get(kind="REDACTED_LOG")
    (data_root() / log.storage_key).unlink()
    with pytest.raises(DomainError) as caught:
        verify_bundle(bundle.id, bundle.manifest_sha256)
    assert caught.value.code == "ARTIFACT_CORRUPTED"


def test_pin_prevents_expiry_then_releases(setup):
    bundle = create_bundle(setup, {"worker": "log"})
    pin_bundle(bundle.id, "publication:test")
    assert expire_results(bundle.sealed_at + timedelta(days=31)) == 0
    unpin_bundle(bundle.id, "publication:test")
    assert expire_results(bundle.sealed_at + timedelta(days=31)) > 0
    bundle.refresh_from_db()
    assert bundle.expired_at


def test_live_run_evidence_not_deleted(setup):
    bundle = create_bundle(setup)
    Run.objects.filter(pk=setup["run"].pk).update(status="RUNNING")
    assert expire_results(bundle.sealed_at + timedelta(days=31)) == 0


def test_attachment_pin_keeps_content(setup):
    artifact = store_attachment(setup["manager"], setup["conversation"].id, SimpleUploadedFile("steps.md", b"steps"))
    snapshot = snapshot_attachments(setup["manager"], setup["conversation"].id, [artifact.id])
    pin_attachments(snapshot, "revision:test")
    assert expire_results(artifact.expires_at + timedelta(days=1)) == 0


@pytest.mark.parametrize("key", ["../outside", "/tmp/outside", "logs/../outside", "logs\\outside", "logs/\x00x"])
def test_storage_path_escape_rejected(setup, key):
    with pytest.raises(DomainError):
        write_private(key, b"denied")


def test_storage_parent_symlink_and_leaf_swap_rejected(setup):
    outside = setup["tmp"] / "outside"
    outside.mkdir()
    data_root().joinpath("uploads").symlink_to(outside)
    with pytest.raises(DomainError):
        write_private("uploads/secret", b"denied")
    assert not list(outside.iterdir())


def test_log_redaction_handles_complete_chunk_record():
    value = redact_text('Authorization: Bearer token-value\nCookie: private-cookie\n{"api_key":"secret"}\nhttps://user:password@example.test\n\x1b[31mhello')
    for secret in ("token-value", "private-cookie", '"secret"', "user:password", "\x1b"):
        assert secret not in value


def test_upload_handler_stops_before_temporary_file_spooling():
    request = RequestFactory().post("/conversations/example/attachments/")
    handler = LimitedAttachmentUploadHandler(request)
    handler.new_file("file", "steps.txt", "text/plain", 6 * 1024 * 1024)
    with pytest.raises(StopUpload):
        handler.receive_data_chunk(b"x" * (5 * 1024 * 1024 + 1), 0)
    assert request.attachment_upload_rejected


def test_readonly_verification_does_not_mark_database(setup):
    bundle = create_bundle(setup)
    patch = bundle.artifacts.get(kind="CANONICAL_PATCH")
    (data_root() / patch.storage_key).write_bytes(b"changed")
    with pytest.raises(DomainError):
        verify_bundle(bundle.id, bundle.manifest_sha256, readonly=True)
    bundle.refresh_from_db()
    assert bundle.corruption_reason == ""


def test_collector_matches_workspace_hash(setup):
    from apps.runs.workspace import tree_sha256
    root = setup["tmp"] / "tree"
    root.mkdir()
    (root / "file").write_text("data")
    (root / "file").chmod(0o750)
    (root / "alias").symlink_to("file")
    assert tree_digest(root) == tree_sha256(root)


def test_result_and_download_http_security(setup, client):
    bundle = create_bundle(setup)
    user = setup["manager"]
    client.force_login(user)
    session = client.session
    session["authz_version"] = user.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()
    response = client.get(reverse("artifacts:detail", args=[bundle.id]))
    assert response.status_code == 200
    assert "변경 결과" in response.content.decode()
    assert "관리자 발행 검토 준비" not in response.content.decode()
    artifact = bundle.artifacts.get(kind="CANONICAL_PATCH")
    download = client.get(reverse("artifacts:download", args=[artifact.id]))
    assert download.status_code == 200
    assert "no-store" in download["Cache-Control"]
    assert download["X-Content-Type-Options"] == "nosniff"
    assert download["Content-Disposition"].startswith("attachment;")
    assert b"value = 2" in b"".join(download.streaming_content)


def test_inactive_known_url_denied(setup):
    artifact = store_attachment(setup["manager"], setup["conversation"].id, SimpleUploadedFile("steps.txt", b"notes"))
    get_user_model().objects.filter(pk=setup["manager"].pk).update(is_active=False)
    with pytest.raises(DomainError) as caught:
        open_artifact(setup["manager"], artifact.id)
    assert caught.value.code == "AUTH_REQUIRED"


def test_publication_prepare_form_has_fresh_key_per_page(setup, client):
    bundle = create_bundle(setup)
    from apps.artifacts.reviews import decide_result
    decide_result(setup["manager"], bundle.id, "accept", bundle.manifest_sha256)
    decide_result(setup["admin"], bundle.id, "approve", bundle.manifest_sha256)
    user = setup["admin"]
    client.force_login(user)
    session = client.session
    session["authz_version"] = user.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()
    url = reverse("artifacts:detail", args=[bundle.id])
    first, second = client.get(url), client.get(url)
    assert first.status_code == second.status_code == 200
    assert first.context["publication_key"] != second.context["publication_key"]
    assert f'value="{first.context["publication_key"]}"' in first.content.decode()


def test_result_directory_symlink_is_not_followed(setup):
    outside = setup["tmp"] / "outside"
    outside.mkdir()
    (data_root() / "artifacts").symlink_to(outside)
    with pytest.raises(DomainError):
        create_bundle(setup)
    assert not ResultBundle.objects.exists()
    assert not list(outside.iterdir())


def test_revision_may_reference_multiple_messages_attachments(setup):
    attachments = [store_attachment(setup["manager"], setup["conversation"].id, SimpleUploadedFile(f"steps-{i}.txt", b"notes")) for i in range(4)]
    ids = [artifact.id for artifact in attachments]
    with pytest.raises(DomainError):
        snapshot_attachments(setup["manager"], setup["conversation"].id, ids)
    assert len(snapshot_attachments(setup["manager"], setup["conversation"].id, ids, max_count=None)) == 4


def test_malformed_attachment_id_is_not_found(setup):
    with pytest.raises(DomainError) as caught:
        snapshot_attachments(setup["manager"], setup["conversation"].id, ["not-a-uuid"])
    assert caught.value.code == "NOT_FOUND"
