import json
import hashlib
import io
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.ai import services
from apps.ai.models import AIInvocation, ModelProfile, SandboxCheck
from apps.errors import DomainError
from integrations.codex import adapter
from integrations.codex.permissions import PermissionScope


@pytest.fixture(autouse=True)
def initial_profiles(db):
    services.seed_profiles()


@pytest.fixture(autouse=True)
def isolated_container_control(monkeypatch, tmp_path):
    from integrations.codex import container
    auth = tmp_path / "fake-auth.json"
    auth.write_text("synthetic")
    monkeypatch.setattr(container, "ready", lambda: "sha256:fixture")
    monkeypatch.setattr(container, "docker_binary", lambda: "/fixture/docker")
    monkeypatch.setattr(container, "login_file", lambda: auth)
    monkeypatch.setattr(container, "remove", lambda name: None)


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


def success(text="확인했습니다.", **kwargs):
    return stream(
        {"type": "thread.started", "thread_id": "fixture", **kwargs},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}},
    )


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_user(username="ai-admin", display_name="관리자", role="ADMIN", password="Ai-Local-Test-12904")


@pytest.fixture
def manager(db):
    return get_user_model().objects.create_user(username="ai-manager", display_name="매니저", role="MANAGER", password="Ai-Local-Test-12904")


def assert_error(code, operation, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code


def test_jsonl_requires_real_final_response_and_turn_completion():
    result = adapter.parse_jsonl(success())
    assert result.error_code == ""
    assert result.final_response == "확인했습니다."
    assert result.reported_tokens == {"input_tokens": 12, "output_tokens": 4}
    assert result.effective_model == result.effective_effort == "not_reported"
    assert_error("MODEL_UNAVAILABLE", adapter.validate_model_values, "", "low")


def test_jsonl_records_command_completion_not_just_success_claims():
    output = stream(
        {"type": "item.started", "item": {"id": "cmd-1", "type": "command_execution", "command": "pytest", "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "cmd-1", "type": "command_execution", "command": "pytest", "status": "completed", "exit_code": 1, "aggregated_output": "private output"}},
    ) + "\n" + success("Everything passed")
    result = adapter.parse_jsonl(output)
    assert [event["event"] for event in result.execution_events] == ["item.started", "item.completed"]
    assert result.execution_events[-1]["exit_code"] == 1
    assert "private output" not in str(result.execution_events)
    assert result.trace_complete


@pytest.mark.parametrize("output", [
    "", "not-json", "[]", '{"type":1}', '{"type":"item.completed"}',
    stream({"type": "turn.completed"}),
    stream({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
    success() + '\n{"type":"turn.completed"}',
    success() + '\n{"type":"item.completed","item":{"type":"agent_message","text":"late"}}',
    stream({"type": "turn.completed", "usage": {"input_tokens": -1}}),
    b"\xff", "x" * (1024 * 1024 + 1),
])
def test_incomplete_or_malformed_stream_is_not_success(output):
    assert adapter.parse_jsonl(output).error_code == "CLI_OUTPUT_INVALID"


@pytest.mark.parametrize("flags,code", [
    ({"timed_out": True}, "CLI_TIMEOUT"),
    ({"returncode": 2}, "AI_FAILED"),
    ({"output_complete": False}, "CLI_OUTPUT_INVALID"),
])
def test_exit_timeout_and_output_loss_override_model_claims(flags, code):
    assert adapter.parse_jsonl(success("All tests passed."), **flags).error_code == code


def test_reported_model_mismatch_never_silently_substitutes():
    output = success(model="different-model", reasoning_effort="ultra")
    result = adapter.parse_jsonl(output, requested_model="gpt-6-astra", requested_effort="xhigh")
    assert result.error_code == "MODEL_MISMATCH"
    assert result.final_response == ""


@pytest.mark.parametrize("error,code", [
    ("429 usage limit", "RATE_LIMITED"), ("unsupported model", "MODEL_UNAVAILABLE"), ("internal failure", "AI_FAILED"),
])
def test_error_events_cannot_be_hidden_by_successful_final(error, code):
    output = stream({"type": "error", "message": error}) + "\n" + success()
    assert adapter.parse_jsonl(output).error_code == code


def test_final_and_error_outputs_do_not_expose_tokens():
    token = "sk-fixtureNotARealKey999999999"
    assert token not in adapter.parse_jsonl(success("API_KEY=" + token)).final_response
    assert adapter.parse_jsonl(success("Bearer " + token)).final_response == "Bearer [REDACTED]"
    assert adapter.parse_jsonl(success("x" * (200 * 1024 + 1))).error_code == "CLI_OUTPUT_INVALID"


def test_disabled_external_tool_event_is_rejected():
    output = stream({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "mcp"}}) + "\n" + success()
    assert adapter.parse_jsonl(output).error_code == "CONFIG_UNSUPPORTED"


def test_cli_argv_is_explicit_without_bypass_or_shell_prompt(tmp_path):
    scope = PermissionScope(tmp_path / "workspace", (tmp_path / "workspace" / "src",))
    profile = {"model_id": "gpt-6-astra", "effort": "xhigh"}
    argv = adapter.exec_argv("/opt/codex", profile, scope)
    assert argv[:2] == ["/opt/codex", "exec"]
    assert argv[-1] == "-"
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="xhigh"' in argv
    assert all(flag in argv for flag in ("--ignore-user-config", "--strict-config", "--ephemeral", "--json"))
    assert not any(flag in argv for flag in ("--ignore-rules", "--sandbox", "--dangerously-bypass-approvals-and-sandbox", "--full-auto"))
    assert 'approval_policy="never"' in argv
    assert 'web_search="disabled"' in argv
    assert "features.skip_host_skill_discovery=true" in argv
    assert {argv[index + 1] for index, part in enumerate(argv) if part == "--disable"}.issuperset({"hooks", "apps", "multi_agent", "computer_use", "plugins"})


def test_parent_environment_does_not_copy_arbitrary_credentials(monkeypatch, tmp_path):
    for key in ("GH_TOKEN", "SSH_AUTH_SOCK", "OPENAI_API_KEY", "DATABASE_URL", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "not-real-sensitive-fixture")
    env = adapter.parent_environment(tmp_path)
    assert not any(key in env for key in ("GH_TOKEN", "SSH_AUTH_SOCK", "OPENAI_API_KEY", "DATABASE_URL", "AWS_SECRET_ACCESS_KEY"))
    assert env["TMPDIR"] == str(tmp_path)


def test_private_uv_environment_never_inherits_host_cache_or_hardlinks(monkeypatch, tmp_path):
    monkeypatch.setenv("UV_CACHE_DIR", "/host/shared-cache")
    monkeypatch.setenv("UV_LINK_MODE", "hardlink")
    first = adapter.parent_environment(tmp_path / "first")
    second = adapter.parent_environment(tmp_path / "second")
    assert first["UV_CACHE_DIR"] == str(tmp_path / "first" / "uv-cache")
    assert second["UV_CACHE_DIR"] == str(tmp_path / "second" / "uv-cache")
    assert first["UV_CACHE_DIR"] != second["UV_CACHE_DIR"]
    assert first["UV_LINK_MODE"] == second["UV_LINK_MODE"] == "copy"
    assert "/host/shared-cache" not in first.values()
    assert "UV_CACHE_DIR" not in adapter.parent_environment()


def test_login_diagnostics_only_use_status_and_never_return_raw_output(monkeypatch, tmp_path):
    calls = []
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture")
    binary.chmod(0o700)
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=b"codex-cli 0.154.0\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"Logged in using ChatGPT sk-DO_NOT_COPY_RAW_FIXTURE")
    monkeypatch.setattr(subprocess, "run", run)
    result = adapter.check_connection(str(binary), cwd=tmp_path)
    assert result["connected"] and result["supported_cli"]
    assert calls == [[str(binary), "--version"], [str(binary), "login", "status"]]
    assert "DO_NOT_COPY" not in json.dumps(result)


def test_unknown_cli_version_does_not_mean_supported(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture")
    binary.chmod(0o700)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout=b"codex-cli 99.1.0" if argv[-1] == "--version" else b"Logged in using ChatGPT", stderr=b""))
    result = adapter.check_connection(str(binary))
    assert result["connected"]
    assert result["supported_cli"] is False


@pytest.mark.django_db
def test_defaults_are_versioned_but_not_misrepresented_as_verified():
    conversation = services.profile_snapshot("conversation")
    implementation = services.profile_snapshot("implementation")
    deep = services.profile_snapshot("implementation_deep")
    assert (conversation["model_id"], conversation["effort"]) == ("gpt-5.6-luna", "low")
    assert (implementation["model_id"], implementation["effort"]) == ("gpt-6-astra", "xhigh")
    assert deep["effort"] == "ultra" and not deep["enabled"]
    assert conversation["validation_status"] == "UNVERIFIED"
    assert_error("MODEL_UNAVAILABLE", services.profile_snapshot, "conversation", require_validated=True)
    assert ModelProfile.objects.count() == 3


def test_profile_changes_create_new_version_preserving_snapshots(admin):
    snapshot = services.profile_snapshot("conversation")
    new = services.save_profile(admin, "conversation", "gpt-5.6-luna", "medium")
    assert new.version == 2
    assert ModelProfile.objects.get(pk=snapshot["id"]).effort == "low"
    assert snapshot["effort"] == "low"
    new.effort = "high"
    with pytest.raises(ValidationError):
        new.save()


def test_managers_cannot_change_models_or_request_model_validation(manager):
    assert_error("ACCESS_DENIED", services.save_profile, manager, "conversation", "gpt-5.6-luna", "low")
    snapshot = services.profile_snapshot("conversation")
    assert_error("ACCESS_DENIED", services.validate_profile, manager, snapshot["id"], True, "unique")
    assert not AIInvocation.objects.exists()


def test_profile_validation_is_explicit_and_idempotent_job(admin):
    from apps.runs.models import Job
    snapshot = services.profile_snapshot("conversation")
    assert_error("CONSENT_REQUIRED", services.validate_profile, admin, snapshot["id"], False, "unique")
    assert Job.objects.count() == 0
    first = services.validate_profile(admin, snapshot["id"], True, "unique")
    assert services.validate_profile(admin, snapshot["id"], True, "unique").pk == first.pk
    assert Job.objects.count() == AIInvocation.objects.count() == 1
    assert Job.objects.get().kind == "MODEL_VALIDATION"
    assert first.state == "QUEUED"


def test_missing_docker_blocks_model_call_even_after_queue(admin, monkeypatch):
    from apps.runs.models import Job
    snapshot = services.profile_snapshot("conversation")
    invocation = services.validate_profile(admin, snapshot["id"], True, "validate")
    Job.objects.filter(subject_id=invocation.id).update(state="RUNNING", lease_owner="test-worker", lease_epoch=1, lease_expires_at=timezone.now() + timedelta(seconds=45))
    monkeypatch.setattr(services, "_environment_fingerprint", lambda: {"cli_binary_hash": "a" * 64, "cli_version": "codex-cli 0.154.0", "policy_hash": "b" * 64, "host_os_version": "fixture"})
    def never_launch(**kwargs):
        raise AssertionError("Docker availability must be checked before model invocation")
    monkeypatch.setattr("apps.runs.processes.launch_process", never_launch)
    def unavailable():
        raise DomainError("DOCKER_UNAVAILABLE")
    monkeypatch.setattr(services.container, "ready", unavailable)
    assert_error("DOCKER_UNAVAILABLE", services.process_job, Job.objects.get(subject_id=invocation.id))
    invocation.refresh_from_db()
    assert invocation.state == "FAILED" and invocation.error_code == "DOCKER_UNAVAILABLE"


@pytest.mark.django_db
def test_old_native_checks_do_not_block_docker_execution(monkeypatch):
    fingerprint = {"cli_binary_hash": "a" * 64, "cli_version": "codex-cli 0.154.0", "policy_hash": "b" * 64, "host_os_version": "fixture"}
    monkeypatch.setattr(services, "_environment_fingerprint", lambda: fingerprint)
    check = SandboxCheck.objects.create(**fingerprint, native_passed=True, file_tools_passed=True, checks=[])
    assert services.require_sandbox_ready() == "sha256:fixture"
    check.checks = [{"id": name, "status": "PASS"} for name in services.REQUIRED_NATIVE_CHECKS | {"native_file_tools"}]
    check.save(update_fields=["checks"])
    assert services.require_sandbox_ready() == "sha256:fixture"
    fingerprint["policy_hash"] = "c" * 64
    assert services.require_sandbox_ready() == "sha256:fixture"


@pytest.mark.django_db
def test_model_free_probe_does_not_certify_model_file_tools(monkeypatch):
    fingerprint = {"cli_binary_hash": "a" * 64, "cli_version": "codex-cli 0.154.0", "policy_hash": "b" * 64, "host_os_version": "fixture"}
    monkeypatch.setattr(services, "_environment_fingerprint", lambda: fingerprint)
    SandboxCheck.objects.create(**fingerprint, native_passed=True, file_tools_passed=False, checks=[{"id": name, "status": "PASS"} for name in services.REQUIRED_NATIVE_CHECKS])
    assert services.require_sandbox_ready(require_file_tools=False)
    assert services.require_sandbox_ready() == "sha256:fixture"


def test_status_page_does_not_call_model_or_login_probe(admin, client, monkeypatch):
    client.force_login(admin)
    session = client.session
    session["authz_version"] = admin.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()
    monkeypatch.setattr(services, "check_connection", lambda: pytest.fail("GET must not repeat connection probes"))
    response = client.get("/ai/")
    assert response.status_code == 200
    assert "gpt-5.6-luna" in response.content.decode()


def test_invocation_rejects_large_context_before_subprocess(tmp_path):
    scope = PermissionScope(tmp_path / "workspace", ())
    assert_error("CONTEXT_TOO_LARGE", services._invoke_checked, "x" * 100001, {"model_id": "gpt-5.6-luna", "effort": "low"}, scope, timezone.now() + timedelta(seconds=3), job=None, process_launcher=lambda **kwargs: pytest.fail("must not launch"))


def test_get_ai_settings_does_not_recreate_missing_profiles(admin, client):
    ModelProfile.objects.all().delete()
    client.force_login(admin)
    session = client.session
    session.update({"authz_version": admin.authz_version, "issued_at": timezone.now().timestamp()})
    session.save()
    assert client.get("/ai/").status_code == 200
    assert ModelProfile.objects.count() == 0


def test_text_and_image_inputs_use_verified_snapshots_and_readonly_copy(monkeypatch, tmp_path):
    from apps.artifacts import services as artifacts

    contents = {"text": b"A harmless UTF8 attachment", "image": b"sanitized image fixture"}
    snapshots = [{"id": key, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content), "mime_type": "text/plain" if key == "text" else "image/png"} for key, content in contents.items()]
    def open_artifact(actor, artifact_id):
        snapshot = next(item for item in snapshots if item["id"] == artifact_id)
        stream = io.BytesIO(contents[artifact_id])
        stream.artifact = SimpleNamespace(**snapshot)
        return stream
    monkeypatch.setattr(artifacts, "open_artifact", open_artifact)
    context, images = services._prepare_attachments("Question", snapshots, None, tmp_path)
    assert "A harmless UTF8 attachment" in context
    assert len(images) == 1
    assert images[0].parent == tmp_path / ".aiqa-inputs"
    assert images[0].read_bytes() == contents["image"]
    assert images[0].stat().st_mode & 0o777 == 0o400
    argv = adapter.exec_argv("/opt/codex", {"model_id": "gpt-5.6-luna", "effort": "low"}, PermissionScope(tmp_path, ()), images=images)
    assert argv[argv.index("--image") + 1] == str(images[0])


def test_changed_attachment_hash_blocks_injection(monkeypatch, tmp_path):
    from apps.artifacts import services as artifacts

    content = b"changed"
    snapshot = {"id": "fixture", "sha256": "0" * 64, "size_bytes": len(content), "mime_type": "text/plain"}
    def open_artifact(actor, artifact_id):
        stream = io.BytesIO(content)
        stream.artifact = SimpleNamespace(**snapshot)
        return stream
    monkeypatch.setattr(artifacts, "open_artifact", open_artifact)
    assert_error("ATTACHMENT_CHANGED", services._prepare_attachments, "Question", [snapshot], None, tmp_path)


def test_check_argv_uses_container_and_registered_cwd(monkeypatch, tmp_path, settings):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    (workspace / ".env").write_text("FAKE_ONLY")
    (source / "main.py").write_text("pass")
    settings.AIQA_DATA_DIR = tmp_path / "state"
    monkeypatch.setattr(services, "require_sandbox_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "resolve_binary", lambda value: "/opt/codex")
    captured = {}
    def launcher(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="", timed_out=False, output_complete=True)
    (workspace / ".aiqa-output" / "tests").mkdir(parents=True)
    command = {"argv": ["python3", "main.py"], "cwd": "src", "generated_paths": [".aiqa-output/tests"],
               "config_revision": SimpleNamespace(read_paths=["."], write_paths=["src"], excluded_paths=[".env", ".aiqa-output"])}
    services.execute_check(SimpleNamespace(project=None), workspace, command, launcher)
    assert captured["cwd"] == settings.AIQA_DATA_DIR
    assert captured["argv"][-2:] == ["python3", "main.py"]
    assert captured["argv"][captured["argv"].index("--workdir") + 1] == str(source)
    assert "bridge" in captured["argv"]
    assert "UV_LINK_MODE=copy" in captured["argv"]
    assert not any("auth.json" in arg for arg in captured["argv"])


def test_check_failure_guard_never_runs_host_fallback(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise DomainError("SANDBOX_UNAVAILABLE")
    monkeypatch.setattr(services, "require_sandbox_ready", unavailable)
    assert_error("SANDBOX_UNAVAILABLE", services.execute_check, SimpleNamespace(project=None), tmp_path, {}, lambda **kwargs: pytest.fail("host fallback forbidden"))


def test_read_only_contract_audit_does_not_claim_full_configuration(monkeypatch, tmp_path):
    def run(argv, **kwargs):
        if "features" in argv:
            stdout = "\n".join(name + " stable false" for name in adapter.DISABLED_FEATURES)
        elif "sandbox" in argv:
            stdout = "--permission-profile --include-managed-config"
        else:
            stdout = "--ignore-user-config --strict-config --ephemeral --json"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    checks = adapter.inspect_configuration_contract("/opt/codex", tmp_path)
    assert all(item["status"] == "PASS" for item in checks)
    assert "effective_config" not in {item["id"] for item in checks}


def test_configuration_marker_changes_without_reading_config_or_credentials(tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    codex.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    config = codex / "config.toml"
    config.write_text('model="fixture"')
    marker = adapter.configuration_marker(tmp_path)
    config.write_text('model="changed-fixture"')
    assert adapter.configuration_marker(tmp_path) != marker
    marker = adapter.configuration_marker(tmp_path)
    (codex / "auth.json").write_text("DO_NOT_READ_AUTH")
    assert adapter.configuration_marker(tmp_path) == marker
    monkeypatch.setattr(Path, "read_bytes", lambda path: pytest.fail("configuration audit must not read authentication or raw config"))
    assert adapter.configuration_marker(tmp_path) == marker


def test_scope_uses_same_explicit_python_runtime_as_canary(tmp_path):
    scope = services.make_scope(tmp_path / "workspace", temporary_dir=tmp_path / "temporary")
    assert set(adapter.runtime_read_paths()).issubset(scope.readable_paths)
    argv = adapter.exec_argv("/opt/codex", {"model_id": "gpt-5.6-luna", "effort": "low"}, scope)
    assert "shell_environment_policy.set.TMPDIR=" + json.dumps(str(scope.temporary_dir)) in argv
    assert "shell_environment_policy.set.UV_CACHE_DIR=" + json.dumps(str(scope.temporary_dir / "uv-cache")) in argv
    assert 'shell_environment_policy.set.UV_LINK_MODE="copy"' in argv


def test_stale_worker_cannot_commit_response(admin):
    from apps.runs.models import Job

    snapshot = services.profile_snapshot("conversation")
    invocation = services.validate_profile(admin, snapshot["id"], True, "stale")
    job = Job.objects.get(subject_id=invocation.id)
    assert_error("STALE_WORKER", services._commit_response, job, invocation, adapter.InvocationResult(final_response="AIQA_MODEL_OK"))
    invocation.refresh_from_db()
    assert invocation.state == "QUEUED" and invocation.final_response == ""


def test_input_filesystem_failure_finishes_invocation_without_model_call(admin, monkeypatch):
    from apps.runs.models import Job

    snapshot = services.profile_snapshot("conversation")
    invocation = services.validate_profile(admin, snapshot["id"], True, "io-failure")
    Job.objects.filter(subject_id=invocation.id).update(state="RUNNING", lease_owner="fixture", lease_epoch=1, lease_expires_at=timezone.now() + timedelta(seconds=45))
    monkeypatch.setattr(services, "require_sandbox_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture disk full")))
    monkeypatch.setattr("apps.runs.processes.launch_process", lambda **kwargs: pytest.fail("No model call before input preparation"))
    assert_error("AI_CONTEXT_UNAVAILABLE", services.process_job, Job.objects.get(subject_id=invocation.id))
    invocation.refresh_from_db()
    assert invocation.state == "FAILED" and invocation.ended_at is not None


def test_conversation_worker_prepares_readonly_source_and_persists_fake_response(admin, monkeypatch, tmp_path, settings):
    from apps.conversations.services import start_conversation
    from apps.projects.models import Project
    from apps.runs.models import Job

    settings.AIQA_DATA_DIR = tmp_path / "state"
    settings.AIQA_DATA_DIR.mkdir()
    project = Project.objects.create(name="AI source fixture", canonical_repo_path=str(tmp_path / "repo"), created_by=admin)
    conversation = start_conversation(admin, project.id, "코드 구조를 설명해 주세요.", "source-message", "source-chat")
    invocation = AIInvocation.objects.get(conversation=conversation)
    Job.objects.filter(subject_id=invocation.id).update(state="RUNNING", lease_owner="fixture", lease_epoch=1, lease_expires_at=timezone.now() + timedelta(seconds=45))
    monkeypatch.setattr(services, "require_sandbox_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(services, "_require_profile", lambda snapshot: None)
    execution = SimpleNamespace(project=project, base_commit_sha="a" * 40, policy={"read_paths": ["src"]}, config_revision=SimpleNamespace(project_config_version=1))
    monkeypatch.setattr("apps.projects.services.resolve_execution_input", lambda *args, **kwargs: execution)
    prepared = []
    def source_copy(repo, sha, invocation_id, policy, data):
        assert repo == project.canonical_repo_path and sha == "a" * 40 and policy["read_paths"] == ["src"]
        path = data / "conversations-tmp" / str(invocation_id)
        path.mkdir(parents=True)
        (path / "readable.txt").write_text("non-sensitive source fixture")
        prepared.append(path)
        return path
    monkeypatch.setattr("apps.runs.workspace.create_readonly_workspace", source_copy)
    monkeypatch.setattr(adapter, "resolve_binary", lambda value: "/opt/codex")
    def launcher(**kwargs):
        assert kwargs["argv"][kwargs["argv"].index("--cd") + 1] == str(prepared[0])
        assert kwargs["stdout_line_limit"] == 1024 * 1024
        assert kwargs["output_limit"] == 8 * 1024 * 1024
        mounts = [kwargs["argv"][i + 1] for i, part in enumerate(kwargs["argv"]) if part == "--mount"]
        assert f"type=bind,src={prepared[0]},dst={prepared[0]},readonly" in mounts
        assert "committed" in kwargs["stdin_text"]
        trace = stream({"type": "item.completed", "item": {"id": "read-1", "type": "command_execution", "command": "ls", "status": "completed", "exit_code": 0}})
        return SimpleNamespace(returncode=0, stdout=trace + "\n" + success("읽기 전용 코드 설명 fixture"), stderr="", output_complete=True, timed_out=False)
    monkeypatch.setattr("apps.runs.processes.launch_process", launcher)
    services.process_job(Job.objects.get(subject_id=invocation.id))
    invocation.refresh_from_db()
    assert invocation.state == "SUCCEEDED"
    assert invocation.trace_complete
    assert invocation.execution_events[0]["command"] == "ls"
    assert invocation.execution_events[0]["exit_code"] == 0
    assert invocation.context_manifest["source_base_commit_sha"] == "a" * 40
    assert conversation.messages.filter(kind="ASSISTANT", ai_invocation=invocation).get().body == "읽기 전용 코드 설명 fixture"
