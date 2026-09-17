import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("확정된 프로젝트 정책 이력은 변경할 수 없습니다.")

    def delete(self):
        raise ValidationError("확정된 프로젝트 정책 이력은 삭제할 수 없습니다.")


class ImmutableRecord(models.Model):
    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding or kwargs.get("force_update"):
            raise ValidationError("확정된 프로젝트 정책 이력은 변경할 수 없습니다.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("확정된 프로젝트 정책 이력은 삭제할 수 없습니다.")


class Project(models.Model):
    class State(models.TextChoices):
        ACTIVE = "ACTIVE", "활성"
        ARCHIVED = "ARCHIVED", "아카이브"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    canonical_repo_path = models.TextField(unique=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    project_config_version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    @property
    def current_revision(self):
        return self.config_revisions.get(project_config_version=self.project_config_version)

    def __str__(self):
        return self.name


class ProjectConfigRevision(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="config_revisions")
    project_config_version = models.PositiveIntegerField()
    canonical_repo_path = models.TextField()
    source_ref = models.CharField(max_length=255)
    read_paths = models.JSONField(default=list)
    write_paths = models.JSONField(default=list)
    excluded_paths = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "project_config_version"], name="project_config_version_unique")]
        ordering = ["project_config_version"]


class DependencyBundle(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="dependency_bundles")
    lock_file_hash = models.CharField(max_length=64)
    platform = models.CharField(max_length=80)
    runtime = models.CharField(max_length=80)
    manifest_sha256 = models.CharField(max_length=64)
    registered_path = models.TextField()
    manifest = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class VerificationCommand(ImmutableRecord):
    class Kind(models.TextChoices):
        TEST = "TEST", "테스트"
        COMMAND = "COMMAND", "명령"

    config_revision = models.ForeignKey(ProjectConfigRevision, on_delete=models.PROTECT, related_name="verification_commands")
    command_id = models.CharField(max_length=64)
    display_name = models.CharField(max_length=120)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    parser_id = models.CharField(max_length=32, blank=True, default="")
    parser_version = models.PositiveIntegerField(null=True, blank=True)
    report_path = models.TextField(blank=True, default="")
    argv = models.JSONField()
    cwd = models.TextField(default=".")
    timeout_sec = models.PositiveIntegerField(default=300)
    required = models.BooleanField(default=True)
    generated_paths = models.JSONField(default=list)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["config_revision", "command_id"], name="verification_command_id_unique")]
        ordering = ["order", "id"]


class PreparationCommand(ImmutableRecord):
    config_revision = models.ForeignKey(ProjectConfigRevision, on_delete=models.PROTECT, related_name="preparation_commands")
    command_id = models.CharField(max_length=64)
    argv = models.JSONField()
    cwd = models.TextField(default=".")
    timeout_sec = models.PositiveIntegerField(default=300)
    dependency_bundle = models.ForeignKey(DependencyBundle, on_delete=models.PROTECT, null=True, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["config_revision", "command_id"], name="preparation_command_id_unique")]
        ordering = ["order", "id"]


class ProjectHealth(models.Model):
    class Readiness(models.TextChoices):
        READY = "READY", "준비됨"
        SETUP_REQUIRED = "SETUP_REQUIRED", "준비 필요"
        UNAVAILABLE = "UNAVAILABLE", "접근 불가"

    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="health")
    checked_at = models.DateTimeField(auto_now=True)
    actual_repo_path = models.TextField(blank=True, default="")
    base_commit_sha = models.CharField(max_length=64, blank=True, default="")
    is_dirty = models.BooleanField(default=False)
    dirty_count = models.PositiveIntegerField(default=0)
    readiness = models.CharField(max_length=16, choices=Readiness.choices, default=Readiness.SETUP_REQUIRED)
    source_ready = models.BooleanField(default=False)
    modification_ready = models.BooleanField(default=False)
    blocking_reasons = models.JSONField(default=list)


class ProjectAudit(models.Model):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    reason = models.CharField(max_length=500, blank=True, default="")
    from_version = models.PositiveIntegerField(null=True)
    to_version = models.PositiveIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
