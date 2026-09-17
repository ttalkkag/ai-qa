import io
import json
import uuid
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from apps.accounts.services import require_access
from apps.errors import DomainError
from .models import Artifact, ArtifactAccessEvent, ArtifactExpiry, ArtifactPin, ChangedFile, ResultBundle
from .storage import attachment_content, canonical_json, contains_secret, digest, publish_directory, read_private, redact_text, relative_key, unlink_private, write_private
from .trees import collect_tree, make_patch, materialize_tree, tree_digest, tree_manifest


def _access(actor, artifact):
    try:
        require_access(actor, "artifact.view", project_id=artifact.project_id, obj=artifact)
    except DomainError:
        ArtifactAccessEvent.objects.create(actor=actor if getattr(actor, "is_authenticated", False) else None, project_id=artifact.project_id, artifact=artifact, action="DENIED", reason="NOT_FOUND")
        raise


def _pinned(query, now):
    return query.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).exists()


def _artifact_expired(artifact, now):
    if artifact.expired_at:
        return True
    if artifact.expires_at > now or _pinned(artifact.pins, now):
        return False
    if artifact.bundle_id and _pinned(artifact.bundle.pins, now):
        return False
    return True


def store_attachment(actor, conversation_id, uploaded_file):
    Conversation = apps.get_model("conversations", "Conversation")
    conversation = Conversation.objects.filter(pk=conversation_id).first()
    if not conversation:
        raise DomainError("NOT_FOUND", status=404)
    require_access(actor, "conversation.edit", project_id=conversation.project_id, obj=conversation)
    content, mime, name = attachment_content(uploaded_file)
    with transaction.atomic():
        conversation = Conversation.objects.select_for_update().filter(pk=conversation_id).first()
        if not conversation:
            raise DomainError("NOT_FOUND", status=404)
        require_access(actor, "conversation.edit", project_id=conversation.project_id, obj=conversation)
        used = Artifact.objects.filter(conversation=conversation, expired_at__isnull=True).aggregate(total=Sum("size_bytes"))["total"] or 0
        if used + len(content) > 30 * 1024 * 1024:
            raise DomainError("CONVERSATION_ATTACHMENT_LIMIT")
        artifact_id = uuid.uuid4()
        key = f"uploads/{artifact_id}/content"
        write_private(key, content)
        artifact = Artifact.objects.create(id=artifact_id, project_id=conversation.project_id, owner=actor, conversation=conversation, kind="INPUT_ATTACHMENT", storage_key=key, sha256=digest(content), size_bytes=len(content), mime_type=mime, display_name=name[:255], visibility="RESTRICTED" if mime == "text/plain" and contains_secret(content) else "NORMAL", expires_at=timezone.now() + timedelta(days=30))
        touch_conversation_attachments(conversation.id)
        return artifact


def touch_conversation_attachments(conversation_id, activity_at=None):
    now = activity_at or timezone.now()
    Artifact.objects.filter(conversation_id=conversation_id, kind="INPUT_ATTACHMENT", expired_at__isnull=True, expires_at__gt=now).update(expires_at=now + timedelta(days=30))


def snapshot_attachments(actor, conversation_id, artifact_ids, *, max_count=3):
    if not isinstance(artifact_ids, (list, tuple)) or (max_count is not None and len(artifact_ids) > max_count) or len({str(item) for item in artifact_ids}) != len(artifact_ids):
        raise DomainError("ATTACHMENT_LIMIT")
    snapshots = []
    for artifact_id in artifact_ids:
        try:
            artifact = Artifact.objects.filter(pk=artifact_id, conversation_id=conversation_id, kind="INPUT_ATTACHMENT").first()
        except (ValidationError, TypeError, ValueError):
            raise DomainError("NOT_FOUND", status=404) from None
        if not artifact:
            raise DomainError("NOT_FOUND", status=404)
        _access(actor, artifact)
        if artifact.visibility != "NORMAL" or _artifact_expired(artifact, timezone.now()):
            raise DomainError("ATTACHMENT_UNAVAILABLE")
        read_private(artifact.storage_key, artifact.sha256, artifact.size_bytes)
        snapshots.append({"id": str(artifact.id), "sha256": artifact.sha256, "size_bytes": artifact.size_bytes, "mime_type": artifact.mime_type})
    return snapshots


def pin_attachments(snapshot, reference, expires_at=None):
    with transaction.atomic():
        for item in snapshot:
            artifact = Artifact.objects.select_for_update().get(pk=item["id"])
            if artifact.sha256 != item["sha256"] or _artifact_expired(artifact, timezone.now()):
                raise DomainError("ATTACHMENT_UNAVAILABLE")
            read_private(artifact.storage_key, artifact.sha256, artifact.size_bytes)
            ArtifactPin.objects.update_or_create(artifact=artifact, reference=reference, defaults={"expires_at": expires_at})


def _changed_rows(baseline, candidate, contents):
    before, after = {row["path"]: row for row in baseline}, {row["path"]: row for row in candidate}
    rows = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        content = contents.get(path, b"")
        try:
            content.decode("utf-8")
            binary = b"\0" in content
        except UnicodeDecodeError:
            binary = True
        rows.append({"path": path, "change": "ADD" if not old else "DELETE" if not new else "MODIFY", "before_mode": old["mode"] if old else None, "after_mode": new["mode"] if new else None, "before_sha256": old["sha256"] if old else "", "after_sha256": new["sha256"] if new else "", "binary": binary})
    return rows


def seal_result(run_id, evidence):
    if not evidence.get("processes_stopped"):
        raise DomainError("PROCESS_STILL_ACTIVE")
    Run = apps.get_model("runs", "Run")
    run = Run.objects.select_related("request_revision").get(pk=run_id)
    if str(evidence.get("request_revision_id", run.request_revision_id)) != str(run.request_revision_id) or evidence.get("baseline_commit_sha", run.baseline_commit_sha) != run.baseline_commit_sha or evidence.get("project_config_version", run.project_config_version) != run.project_config_version:
        raise DomainError("RESULT_BASIS_MISMATCH")
    baseline, baseline_contents = collect_tree(evidence["baseline_dir"])
    candidate, candidate_contents = collect_tree(evidence["candidate_dir"])
    result_hash = digest(canonical_json(candidate))
    if evidence.get("checked_source_tree_sha256") != result_hash:
        raise DomainError("SOURCE_CHANGED_DURING_CHECK")
    checks = evidence.get("checks", [])
    for check in checks:
        if check.get("phase") == "AFTER" and check.get("checked_source_tree_sha256") != result_hash:
            raise DomainError("SOURCE_CHANGED_DURING_CHECK")
    changed = _changed_rows(baseline, candidate, candidate_contents)
    patch = make_patch(baseline, baseline_contents, candidate, candidate_contents)
    complete = bool(evidence.get("complete")) and len(patch) <= 10 * 1024 * 1024 and len(changed) <= 500
    restricted = evidence.get("visibility") == "RESTRICTED" or contains_secret(patch) or any(contains_secret(candidate_contents.get(item["path"], b"")) for item in changed)
    summary = redact_text(evidence.get("summary", ""))
    files = {
        "summary.md": (summary.encode(), "SUMMARY", "text/plain", True),
        "canonical.patch": (patch, "CANONICAL_PATCH", "text/plain", True),
        "checks.json": (canonical_json(checks), "CHECK_REPORT", "application/json", True),
        "changed-files.json": (canonical_json(changed), "CHANGED_FILES", "application/json", True),
    }
    if "evaluation" in evidence:
        from apps.ai.evaluation import redact_evaluation
        evaluation = canonical_json(redact_evaluation(evidence["evaluation"]))
        files["evaluation.json"] = (evaluation, "CHECK_REPORT", "application/json", True)
    for row in baseline:
        files[f"baseline/{row['sha256']}"] = (baseline_contents[row["path"]], "SOURCE_BASELINE", "application/octet-stream", True)
    logs_size = 0
    for number, (_name, chunks) in enumerate(evidence.get("logs", {}).items()):
        record = "".join(chunks) if isinstance(chunks, (list, tuple)) else str(chunks)
        content = redact_text(record).encode()
        logs_size += len(content)
        if len(content) > 10 * 1024 * 1024 or logs_size > 50 * 1024 * 1024:
            complete = False
            content = content[: min(10 * 1024 * 1024, max(0, 50 * 1024 * 1024 - logs_size + len(content)))]
        files[f"logs/{number}.txt"] = (content, "REDACTED_LOG", "text/plain", False)
    file_rows = [{"path": name, "sha256": digest(value[0]), "size_bytes": len(value[0]), "kind": value[1], "mime_type": value[2], "required_for_publication": value[3], "retention_days": 30 if value[3] else 7} for name, value in sorted(files.items())]
    manifest = {"schema_version": 1, "run_id": str(run.id), "request_revision_id": str(run.request_revision_id), "baseline_commit_sha": run.baseline_commit_sha, "project_config_version": run.project_config_version, "result_tree_sha256": result_hash, "baseline_tree": baseline, "result_tree": candidate, "files": file_rows, "completeness": "COMPLETE" if complete else "INCOMPLETE", "visibility": "RESTRICTED" if restricted else "NORMAL"}
    encoded = canonical_json(manifest)
    files["manifest.json"] = (encoded, "MANIFEST", "application/json", True)
    stage = f"staging/{uuid.uuid4()}"
    for name, value in files.items():
        write_private(f"{stage}/{name}", value[0])
    now = timezone.now()
    with transaction.atomic():
        Run.objects.select_for_update().get(pk=run.id)
        version = (ResultBundle.objects.filter(run=run).aggregate(value=Max("version"))["value"] or 0) + 1
        publish_directory(stage, f"artifacts/{run.id}/{version}")
        bundle = ResultBundle.objects.create(run=run, request_revision_id=run.request_revision_id, version=version, baseline_commit_sha=run.baseline_commit_sha, project_config_version=run.project_config_version, manifest_sha256=digest(encoded), result_tree_sha256=result_hash, completeness=manifest["completeness"], visibility=manifest["visibility"], summary=summary, sealed_at=now, expires_at=now + timedelta(days=30))
        owner_id = getattr(run, "requested_by_id", None) or run.request_revision.request.owner_id
        Artifact.objects.bulk_create([Artifact(bundle=bundle, project_id=run.project_id, owner_id=owner_id, kind=value[1], storage_key=f"artifacts/{run.id}/{version}/{name}", sha256=digest(value[0]), size_bytes=len(value[0]), mime_type=value[2], display_name=f"run-{str(run.id)[:8]}-v{version}.patch" if value[1] == "CANONICAL_PATCH" else Path(name).name, visibility=bundle.visibility, required_for_publication=value[3], expires_at=now + timedelta(days=30 if value[3] else 7)) for name, value in files.items()])
        ChangedFile.objects.bulk_create([ChangedFile(bundle=bundle, **row) for row in changed])
        return bundle


seal_bundle = seal_result


def _manifest(bundle):
    artifact = bundle.artifacts.get(kind="MANIFEST")
    content = read_private(artifact.storage_key, bundle.manifest_sha256, artifact.size_bytes)
    try:
        manifest = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DomainError("ARTIFACT_CORRUPTED") from exc
    if canonical_json(manifest) != content:
        raise DomainError("ARTIFACT_CORRUPTED")
    return manifest


def verify_bundle(bundle_id, expected_hash, *, readonly=False):
    bundle = ResultBundle.objects.get(pk=bundle_id)
    now = timezone.now()
    if bundle.manifest_sha256 != expected_hash or bundle.corruption_reason:
        raise DomainError("ARTIFACT_CORRUPTED")
    if bundle.expired_at or (bundle.expires_at <= now and not _pinned(bundle.pins, now)):
        raise DomainError("RESULT_EXPIRED")
    if bundle.visibility != "NORMAL":
        raise DomainError("RESULT_RESTRICTED")
    try:
        manifest = _manifest(bundle)
        expected = {"run_id": str(bundle.run_id), "request_revision_id": str(bundle.request_revision_id), "baseline_commit_sha": bundle.baseline_commit_sha, "project_config_version": bundle.project_config_version, "result_tree_sha256": bundle.result_tree_sha256, "completeness": bundle.completeness, "visibility": bundle.visibility}
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise DomainError("ARTIFACT_CORRUPTED")
        for row in manifest["files"]:
            artifact = bundle.artifacts.get(storage_key=f"artifacts/{bundle.run_id}/{bundle.version}/{row['path']}")
            if artifact.sha256 != row["sha256"] or artifact.size_bytes != row["size_bytes"] or artifact.required_for_publication != row["required_for_publication"] or artifact.kind != row["kind"] or artifact.mime_type != row["mime_type"]:
                raise DomainError("ARTIFACT_CORRUPTED")
            if artifact.expired_at and not row["required_for_publication"]:
                expiry = ArtifactExpiry.objects.filter(artifact=artifact, reason="RETENTION", sha256=row["sha256"], size_bytes=row["size_bytes"], scheduled_at=artifact.expires_at, expired_at__gte=artifact.expires_at).first()
                if expiry is None:
                    raise DomainError("ARTIFACT_CORRUPTED")
                continue
            read_private(artifact.storage_key, row["sha256"], row["size_bytes"])
    except (DomainError, Artifact.DoesNotExist, KeyError, TypeError) as exc:
        if not readonly:
            ResultBundle.objects.filter(pk=bundle.id).update(corruption_reason="ARTIFACT_CORRUPTED")
        raise DomainError("ARTIFACT_CORRUPTED") from exc
    return bundle


def read_bundle_file(bundle_id, relative_path):
    relative_key(relative_path)
    bundle = ResultBundle.objects.get(pk=bundle_id)
    verify_bundle(bundle.id, bundle.manifest_sha256)
    artifact = bundle.artifacts.get(storage_key=f"artifacts/{bundle.run_id}/{bundle.version}/{relative_path}")
    return read_private(artifact.storage_key, artifact.sha256, artifact.size_bytes)


def materialize_baseline(bundle_id, destination):
    bundle = ResultBundle.objects.get(pk=bundle_id)
    verify_bundle(bundle.id, bundle.manifest_sha256)
    entries = _manifest(bundle)["baseline_tree"]
    contents = {row["path"]: read_bundle_file(bundle.id, f"baseline/{row['sha256']}") for row in entries}
    return materialize_tree(entries, contents, destination)


def pin_bundle(bundle_id, reference, expires_at=None):
    with transaction.atomic():
        bundle = ResultBundle.objects.select_for_update().get(pk=bundle_id)
        verify_bundle(bundle.id, bundle.manifest_sha256)
        return ArtifactPin.objects.get_or_create(bundle=bundle, reference=reference, defaults={"expires_at": expires_at})[0]


def unpin_bundle(bundle_id, reference):
    ArtifactPin.objects.filter(bundle_id=bundle_id, reference=reference).delete()


def get_result(actor, bundle_id):
    bundle = ResultBundle.objects.select_related("run", "request_revision__request").filter(pk=bundle_id).first()
    if not bundle:
        raise DomainError("NOT_FOUND", status=404)
    require_access(actor, "artifact.view", project_id=bundle.run.project_id, obj=bundle.request_revision.request)
    if bundle.visibility == "NORMAL" and not bundle.expired_at:
        try:
            verify_bundle(bundle.id, bundle.manifest_sha256)
        except DomainError as error:
            bundle.refresh_from_db()
            bundle.unavailable_reason = error.code
    elif bundle.expired_at:
        bundle.unavailable_reason = "RESULT_EXPIRED"
    return bundle


def open_artifact(actor, artifact_id):
    artifact = Artifact.objects.select_related("bundle").filter(pk=artifact_id).first()
    if not artifact:
        raise DomainError("NOT_FOUND", status=404)
    _access(actor, artifact)
    if artifact.kind == "SOURCE_BASELINE" or artifact.visibility != "NORMAL" or (artifact.bundle_id and artifact.bundle.visibility != "NORMAL"):
        raise DomainError("ARTIFACT_RESTRICTED", status=403)
    if _artifact_expired(artifact, timezone.now()):
        raise DomainError("ARTIFACT_EXPIRED", status=410)
    try:
        if artifact.bundle_id:
            verify_bundle(artifact.bundle_id, artifact.bundle.manifest_sha256)
        content = read_private(artifact.storage_key, artifact.sha256, artifact.size_bytes)
    except DomainError:
        ArtifactAccessEvent.objects.create(actor=actor, project_id=artifact.project_id, artifact=artifact, action="DENIED", reason="ARTIFACT_CORRUPTED")
        raise
    ArtifactAccessEvent.objects.create(actor=actor, project_id=artifact.project_id, artifact=artifact, action="DOWNLOAD")
    stream = io.BytesIO(content)
    stream.artifact = artifact
    return stream


def expire_results(now=None):
    now = now or timezone.now()
    expired = 0
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
    for artifact_id in Artifact.objects.filter(expired_at__isnull=True, expires_at__lte=now).values_list("id", flat=True):
        with transaction.atomic():
            artifact = Artifact.objects.select_for_update().select_related("bundle__run").get(pk=artifact_id)
            if artifact.bundle_id and artifact.bundle.run.status not in terminal:
                continue
            if not _artifact_expired(artifact, now):
                continue
            read_private(artifact.storage_key, artifact.sha256, artifact.size_bytes)
            ArtifactExpiry.objects.create(artifact=artifact, sha256=artifact.sha256, size_bytes=artifact.size_bytes, scheduled_at=artifact.expires_at, expired_at=now, reason="RETENTION")
            unlink_private(artifact.storage_key)
            artifact.expired_at = now
            artifact.save(update_fields=["expired_at"])
            expired += 1
    for bundle in ResultBundle.objects.filter(expired_at__isnull=True, expires_at__lte=now):
        if bundle.run.status in terminal and not _pinned(bundle.pins, now) and not bundle.artifacts.filter(required_for_publication=True, expired_at__isnull=True).exists():
            bundle.expired_at = now
            bundle.save(update_fields=["expired_at"])
    return expired
