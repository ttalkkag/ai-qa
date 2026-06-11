from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from html import escape
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from devauto.core.config import Settings, load_settings
from devauto.core.db import Database
from devauto.core.doctor import run_project_doctor
from devauto.core.models import ProjectConfig, RunStatus, TERMINAL_STATUSES
from devauto.core.network import format_url_host, lan_access_status, lan_candidate_hosts, qa_access_host
from devauto.core.project_config import parse_project_config
from devauto.core.state_machine import can_cancel
from devauto.runner.artifacts import read_artifact_text, safe_artifact_path
from devauto.runner.pipeline import Pipeline
from devauto.runner.workspace import workspace_display_path


class ProjectCreate(BaseModel):
    id: str
    name: str
    repo_url: str
    default_branch: str = "main"
    branch_prefix: str = "aiqa/"
    commands: dict[str, str] = Field(default_factory=dict)
    ai: dict[str, dict[str, Any]] = Field(default_factory=dict)
    docker: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    publish: dict[str, Any] = Field(default_factory=dict)
    workspace: dict[str, Any] = Field(default_factory=dict)

    def to_project(self) -> ProjectConfig:
        return ProjectConfig.from_mapping(
            {
                "id": self.id,
                "name": self.name,
                "repo": {
                    "url": self.repo_url,
                    "default_branch": self.default_branch,
                    "branch_prefix": self.branch_prefix,
                },
                "commands": self.commands,
                "ai": self.ai,
                "docker": self.docker,
                "policy": self.policy,
                "publish": self.publish,
                "workspace": self.workspace,
            }
        )


class ProjectYamlCreate(BaseModel):
    yaml_text: str


class RunCreate(BaseModel):
    project_id: str
    title: str
    description: str = ""
    target_branch: str | None = None
    mode: str | None = None


class Decision(BaseModel):
    comment: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_path)
    db.initialize()
    pipeline = Pipeline(settings, db)
    app = FastAPI(title="devauto", version="0.1.0")
    app.state.settings = settings
    app.state.db = db
    app.state.pipeline = pipeline

    def require_token(request: Request, x_devauto_token: str | None = Header(default=None)) -> None:
        if settings.shared_token and not token_matches(request, settings.shared_token, x_devauto_token):
            raise HTTPException(status_code=401, detail="devauto token이 올바르지 않습니다")

    def get_db() -> Database:
        return app.state.db

    def get_pipeline() -> Pipeline:
        return app.state.pipeline

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> RedirectResponse:
        require_ui_token(request, settings)
        return RedirectResponse(f"/runs{token_suffix(request, settings)}")

    @app.post("/api/projects", dependencies=[Depends(require_token)])
    def create_project(payload: ProjectCreate, db: Database = Depends(get_db)) -> dict[str, Any]:
        try:
            project = payload.to_project()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.upsert_project(project)
        return {"project": project.to_mapping()}

    @app.post("/api/projects/yaml", dependencies=[Depends(require_token)])
    def create_project_from_yaml(payload: ProjectYamlCreate, db: Database = Depends(get_db)) -> dict[str, Any]:
        try:
            project = parse_project_config(payload.yaml_text)
            project = persist_project_yaml(settings, project, payload.yaml_text)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.upsert_project(project)
        return {"project": project.to_mapping()}

    @app.get("/api/projects", dependencies=[Depends(require_token)])
    def list_projects(db: Database = Depends(get_db)) -> dict[str, Any]:
        return {"projects": [project.to_mapping() for project in db.list_projects()]}

    @app.get("/api/settings", dependencies=[Depends(require_token)])
    def get_settings(db: Database = Depends(get_db)) -> dict[str, Any]:
        return serialize_settings(settings, db.list_projects())

    @app.get("/api/projects/{project_id}/doctor", dependencies=[Depends(require_token)])
    def project_doctor(project_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
        try:
            project = db.get_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"project_id": project_id, "doctor": run_project_doctor(project).to_mapping()}

    @app.post("/api/runs", dependencies=[Depends(require_token)])
    def create_run(
        payload: RunCreate,
        background_tasks: BackgroundTasks,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> dict[str, Any]:
        try:
            project = db.get_project(payload.project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        request = payload.model_dump(exclude_none=True)
        status = RunStatus.QUEUED if db.has_active_run() else RunStatus.RECEIVED
        try:
            run = db.create_run(request, project, status=status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if run.status == RunStatus.RECEIVED:
            background_tasks.add_task(pipeline.prepare_run_safely, run.id)
        return {"run": serialize_run(run, settings)}

    @app.get("/api/runs", dependencies=[Depends(require_token)])
    def list_runs(db: Database = Depends(get_db)) -> dict[str, Any]:
        return {"runs": [serialize_run(run, settings) for run in db.list_runs()]}

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_token)])
    def get_run(run_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
        try:
            run = db.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "run": serialize_run(run, settings),
            "artifacts": [serialize_artifact(artifact, settings.data_root) for artifact in db.list_artifacts(run_id)],
            "approvals": [serialize_approval(approval) for approval in db.list_approvals(run_id)],
            "gates": [serialize_gate(result, settings.data_root, run_id) for result in db.list_gate_results(run_id)],
        }

    @app.post("/api/runs/{run_id}/approve-plan", dependencies=[Depends(require_token)])
    def approve_plan(
        run_id: str,
        decision: Decision,
        background_tasks: BackgroundTasks,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> dict[str, Any]:
        require_action_status(db, run_id, RunStatus.AWAITING_PLAN_APPROVAL, "plan 승인")
        background_tasks.add_task(pipeline.approve_plan_and_execute_safely, run_id, decision.comment)
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/reject-plan", dependencies=[Depends(require_token)])
    def reject_plan(run_id: str, decision: Decision, pipeline: Pipeline = Depends(get_pipeline)) -> dict[str, Any]:
        run = run_pipeline_action(lambda: pipeline.reject_plan(run_id, decision.comment))
        return {"run": serialize_run(run, settings)}

    @app.post("/api/runs/{run_id}/approve-publish", dependencies=[Depends(require_token)])
    def approve_publish(run_id: str, decision: Decision, pipeline: Pipeline = Depends(get_pipeline)) -> dict[str, Any]:
        run = run_pipeline_action(lambda: pipeline.approve_publish_safely(run_id, decision.comment))
        return {"run": serialize_run(run, settings)}

    @app.post("/api/runs/{run_id}/reject-publish", dependencies=[Depends(require_token)])
    def reject_publish(run_id: str, decision: Decision, pipeline: Pipeline = Depends(get_pipeline)) -> dict[str, Any]:
        run = run_pipeline_action(lambda: pipeline.reject_publish(run_id, decision.comment))
        return {"run": serialize_run(run, settings)}

    @app.post("/api/runs/{run_id}/approve-qa-preview", dependencies=[Depends(require_token)])
    def approve_qa_preview(
        run_id: str,
        decision: Decision,
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> dict[str, Any]:
        run = run_pipeline_action(lambda: pipeline.approve_qa_preview_safely(run_id, decision.comment))
        return {"run": serialize_run(run, settings)}

    @app.post("/api/runs/{run_id}/reject-qa-preview", dependencies=[Depends(require_token)])
    def reject_qa_preview(
        run_id: str,
        decision: Decision,
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> dict[str, Any]:
        run = run_pipeline_action(lambda: pipeline.reject_qa_preview(run_id, decision.comment))
        return {"run": serialize_run(run, settings)}

    @app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(require_token)])
    def cancel_run(run_id: str, pipeline: Pipeline = Depends(get_pipeline)) -> dict[str, Any]:
        run = run_pipeline_action(lambda: pipeline.cancel_run(run_id))
        return {"run": serialize_run(run, settings)}

    @app.get("/api/runs/{run_id}/events", dependencies=[Depends(require_token)])
    async def run_events(run_id: str, db: Database = Depends(get_db)) -> StreamingResponse:
        get_run_or_404(db, run_id)

        async def stream() -> Any:
            for _ in range(120):
                run = db.get_run(run_id)
                payload = serialize_run_event(run, db, settings)
                yield f"data: {json.dumps(payload)}\n\n"
                if run.status in TERMINAL_STATUSES:
                    break
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/artifacts/{artifact_name}", dependencies=[Depends(require_token)])
    def get_artifact(run_id: str, artifact_name: str, db: Database = Depends(get_db)) -> PlainTextResponse:
        try:
            artifact = db.get_artifact(run_id, artifact_name)
            text = read_artifact_text(settings.data_root, artifact)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="아티팩트를 사용할 수 없습니다") from exc
        return PlainTextResponse(text)

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request, db: Database = Depends(get_db)) -> str:
        require_ui_token(request, settings)
        suffix = token_suffix(request, settings)
        hidden = token_hidden_input(request, settings)
        projects = db.list_projects()
        rows = "".join(
            f"<tr><td>{escape(project.id)}</td><td>{escape(project.name)}</td>"
            f"<td>{escape(project.repo_url)}</td><td>{escape(project.default_branch)}</td>"
            f"<td>{escape(project.config_path)}</td>"
            f"<td><a href=\"/projects/{escape(project.id)}/doctor{suffix}\">진단</a></td></tr>"
            for project in projects
        )
        return page(
            "프로젝트",
            f"""
            <nav><a href="/runs{suffix}">실행 목록</a> <a href="/settings{suffix}">설정</a></nav>
            <form method="post" action="/projects{suffix}">
              {hidden}
              <label>ID <input name="id" required></label>
              <label>이름 <input name="name" required></label>
              <label>저장소 URL <input name="repo_url" required></label>
              <label>브랜치 <input name="default_branch" value="main"></label>
              <button type="submit">저장</button>
            </form>
            <h2>YAML 가져오기</h2>
            <form method="post" action="/projects/yaml{suffix}">
              {hidden}
              <label>프로젝트 YAML <textarea name="yaml_text" required placeholder="id: my-service&#10;name: My Service&#10;repo:&#10;  url: /path/to/repo&#10;  default_branch: main"></textarea></label>
              <button type="submit">YAML 가져오기</button>
            </form>
            <table><thead><tr><th>ID</th><th>이름</th><th>저장소</th><th>브랜치</th><th>설정 파일</th><th>진단</th></tr></thead><tbody>{rows}</tbody></table>
            """,
        )

    @app.get("/projects/{project_id}/doctor", response_class=HTMLResponse)
    def project_doctor_page(project_id: str, request: Request, db: Database = Depends(get_db)) -> str:
        require_ui_token(request, settings)
        suffix = token_suffix(request, settings)
        try:
            project = db.get_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        report = run_project_doctor(project)
        rows = "".join(
            f"<tr><td>{escape(check.name)}</td><td><span class=\"status\">{escape(check.status)}</span></td>"
            f"<td>{escape(check.message)}</td></tr>"
            for check in report.checks
        )
        return page(
            f"진단: {project_id}",
            f"""
            <nav><a href="/projects{suffix}">프로젝트</a> <a href="/runs{suffix}">실행 목록</a> <a href="/settings{suffix}">설정</a></nav>
            <dl>
              <dt>프로젝트</dt><dd>{escape(project_id)}</dd>
              <dt>상태</dt><dd><span class="status">{escape(report.status)}</span></dd>
            </dl>
            <table><thead><tr><th>검사</th><th>상태</th><th>메시지</th></tr></thead><tbody>{rows}</tbody></table>
            """,
        )

    @app.post("/projects")
    async def projects_form(request: Request, db: Database = Depends(get_db)) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        payload = ProjectCreate(
            id=str(form["id"]),
            name=str(form["name"]),
            repo_url=str(form["repo_url"]),
            default_branch=str(form.get("default_branch") or "main"),
            docker={"enabled": False},
        )
        try:
            project = payload.to_project()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.upsert_project(project)
        return RedirectResponse(f"/projects{token_suffix(request, settings, form)}", status_code=303)

    @app.post("/projects/yaml")
    async def projects_yaml_form(request: Request, db: Database = Depends(get_db)) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        try:
            project = parse_project_config(str(form["yaml_text"]))
            project = persist_project_yaml(settings, project, str(form["yaml_text"]))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.upsert_project(project)
        return RedirectResponse(f"/projects{token_suffix(request, settings, form)}", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, db: Database = Depends(get_db)) -> str:
        require_ui_token(request, settings)
        suffix = token_suffix(request, settings)
        hidden = token_hidden_input(request, settings)
        projects = db.list_projects()
        runs = db.list_runs()
        project_options = "".join(
            f"<option value=\"{escape(project.id)}\">{escape(project.name)}</option>" for project in projects
        )
        rows = "".join(
            f"<tr><td><a href=\"/runs/{escape(run.id)}{suffix}\">{escape(run.id)}</a></td>"
            f"<td>{escape(run.project_id)}</td><td>{escape(run.title)}</td>"
            f"<td>{escape(run.target_branch)}</td><td>{escape(run.mode)}</td>"
            f"<td><span class=\"status\">{escape(run.status.value)}</span></td>"
            f"<td>{render_preview_links(run.preview_url, preview_lan_urls(run.preview_url, settings))}</td></tr>"
            for run in runs
        )
        return page(
            "실행 목록",
            f"""
            <nav><a href="/projects{suffix}">프로젝트</a> <a href="/settings{suffix}">설정</a></nav>
            <form method="post" action="/runs{suffix}">
              {hidden}
              <label>프로젝트 <select name="project_id" required>{project_options}</select></label>
              <label>제목 <input name="title" required></label>
              <label>대상 브랜치 <input name="target_branch" placeholder="프로젝트 기본값"></label>
              <label>모드 <select name="mode"><option value="">프로젝트 기본값</option><option value="human-reviewed">human-reviewed</option><option value="auto">auto</option></select></label>
              <label>설명 <textarea name="description"></textarea></label>
              <button type="submit">실행 생성</button>
            </form>
            <table><thead><tr><th>실행</th><th>프로젝트</th><th>제목</th><th>브랜치</th><th>모드</th><th>상태</th><th>미리보기</th></tr></thead><tbody>{rows}</tbody></table>
            """,
        )

    @app.post("/runs")
    async def runs_form(
        request: Request,
        background_tasks: BackgroundTasks,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        try:
            project = db.get_project(str(form["project_id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        status = RunStatus.QUEUED if db.has_active_run() else RunStatus.RECEIVED
        try:
            run = db.create_run(
                {
                    "project_id": str(form["project_id"]),
                    "title": str(form["title"]),
                    "description": str(form.get("description") or ""),
                    **optional_form_value(form, "target_branch"),
                    **optional_form_value(form, "mode"),
                },
                project,
                status=status,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if run.status == RunStatus.RECEIVED:
            background_tasks.add_task(pipeline.prepare_run_safely, run.id)
        return RedirectResponse(f"/runs/{run.id}{token_suffix(request, settings, form)}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: str, request: Request, db: Database = Depends(get_db)) -> str:
        require_ui_token(request, settings)
        suffix = token_suffix(request, settings)
        hidden = token_hidden_input(request, settings)
        run = get_run_or_404(db, run_id)
        artifacts = db.list_artifacts(run_id)
        approvals = db.list_approvals(run_id)
        gates = db.list_gate_results(run_id)
        decision_context = render_decision_context(db, run, suffix, settings.data_root)
        run_events_script = render_run_events_script(run.id, suffix)
        artifact_links = "".join(
            f"<li><a href=\"/api/runs/{escape(run_id)}/artifacts/{escape(artifact.name)}{suffix}\">"
            f"{escape(artifact.name)}</a> <small>{escape(artifact.kind)}</small></li>"
            for artifact in artifacts
        )
        gates_rows = "".join(
            f"<tr><td>{escape(result.gate_name)}</td><td>{result.exit_code}</td>"
            f"<td>{result.duration_ms}</td><td>{escape(str(result.log_path.name))}</td></tr>"
            for result in gates
        )
        approvals_rows = "".join(
            f"<tr><td>{escape(approval.approval_type)}</td><td>{escape(approval.decision)}</td>"
            f"<td>{escape(approval.comment)}</td><td>{escape(approval.created_at)}</td></tr>"
            for approval in approvals
        )
        plan_actions = ""
        if run.status == RunStatus.AWAITING_PLAN_APPROVAL:
            plan_actions = f"""
            <form method="post" action="/runs/{escape(run.id)}/approve-plan{suffix}">{hidden}<label>코멘트 <input name="comment"></label><button type="submit">Plan 승인</button></form>
            <form method="post" action="/runs/{escape(run.id)}/reject-plan{suffix}">{hidden}<label>코멘트 <input name="comment"></label><button type="submit">Plan 반려</button></form>
            """
        publish_actions = ""
        if run.status == RunStatus.AWAITING_PUBLISH_APPROVAL:
            publish_actions = f"""
            <form method="post" action="/runs/{escape(run.id)}/approve-publish{suffix}">{hidden}<label>코멘트 <input name="comment"></label><button type="submit">Publish 승인</button></form>
            <form method="post" action="/runs/{escape(run.id)}/reject-publish{suffix}">{hidden}<label>코멘트 <input name="comment"></label><button type="submit">Publish 반려</button></form>
            """
        qa_actions = ""
        if run.status == RunStatus.AWAITING_QA_APPROVAL:
            qa_actions = f"""
            <form method="post" action="/runs/{escape(run.id)}/approve-qa-preview{suffix}">{hidden}<label>코멘트 <input name="comment"></label><button type="submit">QA Preview 승인</button></form>
            <form method="post" action="/runs/{escape(run.id)}/reject-qa-preview{suffix}">{hidden}<label>코멘트 <input name="comment"></label><button type="submit">QA Preview 반려</button></form>
            """
        cancel_action = ""
        if can_cancel(run.status):
            cancel_action = (
                f"<form method=\"post\" action=\"/runs/{escape(run.id)}/cancel{suffix}\">"
                f"{hidden}<button type=\"submit\">취소</button></form>"
            )
        return page(
            run.id,
            f"""
            <nav><a href="/runs{suffix}">실행 목록</a> <a href="/projects{suffix}">프로젝트</a> <a href="/settings{suffix}">설정</a></nav>
            <dl>
              <dt>상태</dt><dd><span id="run-status" class="status">{escape(run.status.value)}</span></dd>
              <dt>세션</dt><dd>{escape(run.session_id)}</dd>
              <dt>프로젝트</dt><dd>{escape(run.project_id)}</dd>
              <dt>제목</dt><dd>{escape(run.title)}</dd>
              <dt>설명</dt><dd>{escape(run.description or '')}</dd>
              <dt>대상 브랜치</dt><dd>{escape(run.target_branch)}</dd>
              <dt>모드</dt><dd>{escape(run.mode)}</dd>
              <dt>변경 등급</dt><dd>{escape(run.change_class or '')}</dd>
              <dt>리뷰 깊이</dt><dd>{run.review_depth}</dd>
              <dt>재시도</dt><dd>{run.current_retry}/{run.max_retries}</dd>
              <dt>미리보기</dt><dd id="run-preview">{render_preview_links(run.preview_url, preview_lan_urls(run.preview_url, settings))}</dd>
              <dt>산출물</dt><dd><span id="artifact-count">{len(artifacts)}</span><span id="latest-artifact">{format_latest_value(artifacts[-1].name if artifacts else None)}</span></dd>
              <dt>게이트</dt><dd><span id="gate-count">{len(gates)}</span><span id="latest-gate">{format_latest_value(gates[-1].gate_name if gates else None)}</span></dd>
              <dt>승인 이력</dt><dd><span id="approval-count">{len(approvals)}</span><span id="latest-approval">{format_latest_value(approvals[-1].approval_type if approvals else None)}</span></dd>
              <dt>작업공간</dt><dd>{escape(serialize_workspace_path(settings, run) or '')}</dd>
              <dt>생성 시각</dt><dd>{escape(run.created_at)}</dd>
              <dt>갱신 시각</dt><dd id="run-updated">{escape(run.updated_at)}</dd>
            </dl>
            <section class="actions">{plan_actions}{qa_actions}{publish_actions}{cancel_action}</section>
            {decision_context}
            <h2>산출물</h2><p><a href="/runs/{escape(run.id)}/artifacts{suffix}">산출물 브라우저 열기</a></p><ul>{artifact_links}</ul>
            <h2>승인 이력</h2><table><thead><tr><th>유형</th><th>결정</th><th>코멘트</th><th>시각</th></tr></thead><tbody>{approvals_rows}</tbody></table>
            <h2>게이트</h2><table><thead><tr><th>이름</th><th>종료 코드</th><th>ms</th><th>로그</th></tr></thead><tbody>{gates_rows}</tbody></table>
            {run_events_script}
            """,
        )

    @app.get("/runs/{run_id}/artifacts", response_class=HTMLResponse)
    def run_artifacts_page(run_id: str, request: Request, db: Database = Depends(get_db)) -> str:
        require_ui_token(request, settings)
        suffix = token_suffix(request, settings)
        run = get_run_or_404(db, run_id)
        artifacts = db.list_artifacts(run_id)
        rows = "".join(
            f"<tr><td>{escape(artifact.name)}</td><td>{escape(artifact.kind)}</td>"
            f"<td>{escape(artifact.created_at)}</td>"
            f"<td><a href=\"/api/runs/{escape(run_id)}/artifacts/{escape(artifact.name)}{suffix}\">열기</a></td></tr>"
            for artifact in artifacts
        )
        return page(
            f"산출물: {run_id}",
            f"""
            <nav><a href="/runs/{escape(run.id)}{suffix}">실행</a> <a href="/runs{suffix}">실행 목록</a> <a href="/projects{suffix}">프로젝트</a> <a href="/settings{suffix}">설정</a></nav>
            <dl>
              <dt>상태</dt><dd><span class="status">{escape(run.status.value)}</span></dd>
              <dt>제목</dt><dd>{escape(run.title)}</dd>
              <dt>작업공간</dt><dd>{escape(serialize_workspace_path(settings, run) or '')}</dd>
            </dl>
            <table><thead><tr><th>이름</th><th>종류</th><th>생성 시각</th><th>열기</th></tr></thead><tbody>{rows}</tbody></table>
            """,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, db: Database = Depends(get_db)) -> str:
        require_ui_token(request, settings)
        suffix = token_suffix(request, settings)
        payload = serialize_settings(settings, db.list_projects())
        system = payload["settings"]
        project_rows = "".join(
            f"<tr><td>{escape(project['id'])}</td><td>{escape(project['name'])}</td>"
            f"<td>{escape(project['repo_url'])}</td><td>{escape(project['docker_env_file'] or '')}</td>"
            f"<td>{escape(project['config_path'])}</td>"
            f"<td>{project['keep_success_runs']}/{project['keep_failed_runs']}</td>"
            f"<td>{escape(format_project_ai_roles(project))}</td></tr>"
            for project in payload["projects"]
        )
        qa_lan_urls = ", ".join(system["qa_lan_urls"])
        preview_lan_templates = ", ".join(system["preview_lan_url_templates"])
        return page(
            "설정",
            f"""
            <nav><a href="/runs{suffix}">실행 목록</a> <a href="/projects{suffix}">프로젝트</a></nav>
            <dl>
              <dt>데이터 루트</dt><dd>{escape(system['data_root'])}</dd>
              <dt>데이터베이스</dt><dd>{escape(system['database_path'])}</dd>
              <dt>바인드</dt><dd>{escape(system['bind_host'])}:{system['bind_port']}</dd>
              <dt>QA 웹 URL</dt><dd>{escape(system['qa_web_url'])}</dd>
              <dt>QA LAN URL</dt><dd>{escape(qa_lan_urls)}</dd>
              <dt>미리보기 호스트</dt><dd>{escape(system['preview_host'])}</dd>
              <dt>미리보기 포트</dt><dd>{system['preview_port_start']}-{system['preview_port_end']}</dd>
              <dt>미리보기 URL 템플릿</dt><dd>{escape(system['preview_url_template'])}</dd>
              <dt>미리보기 LAN URL 템플릿</dt><dd>{escape(preview_lan_templates)}</dd>
              <dt>LAN 접근</dt><dd>{escape(system['lan_access'])}</dd>
              <dt>공유 토큰</dt><dd>{'설정됨' if system['shared_token_configured'] else '설정되지 않음'}</dd>
            </dl>
            <h2>프로젝트 런타임 설정</h2>
            <table><thead><tr><th>프로젝트</th><th>이름</th><th>저장소</th><th>Secret 경로</th><th>설정 파일</th><th>작업공간 보존</th><th>AI CLI 역할</th></tr></thead><tbody>{project_rows}</tbody></table>
            """,
        )

    @app.post("/runs/{run_id}/approve-plan")
    async def approve_plan_form(
        run_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        get_run_or_404(db, run_id)
        if action_status_matches(db, run_id, RunStatus.AWAITING_PLAN_APPROVAL):
            background_tasks.add_task(pipeline.approve_plan_and_execute_safely, run_id, form_comment(form))
        return RedirectResponse(f"/runs/{run_id}{token_suffix(request, settings, form)}", status_code=303)

    @app.post("/runs/{run_id}/reject-plan")
    async def reject_plan_form(
        run_id: str,
        request: Request,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        get_run_or_404(db, run_id)
        run_pipeline_action_or_ignore(lambda: pipeline.reject_plan(run_id, form_comment(form)))
        return RedirectResponse(f"/runs/{run_id}{token_suffix(request, settings, form)}", status_code=303)

    @app.post("/runs/{run_id}/approve-publish")
    async def approve_publish_form(
        run_id: str,
        request: Request,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        get_run_or_404(db, run_id)
        run_pipeline_action_or_ignore(lambda: pipeline.approve_publish_safely(run_id, form_comment(form)))
        return RedirectResponse(f"/runs/{run_id}{token_suffix(request, settings, form)}", status_code=303)

    @app.post("/runs/{run_id}/reject-publish")
    async def reject_publish_form(
        run_id: str,
        request: Request,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        get_run_or_404(db, run_id)
        run_pipeline_action_or_ignore(lambda: pipeline.reject_publish(run_id, form_comment(form)))
        return RedirectResponse(f"/runs/{run_id}{token_suffix(request, settings, form)}", status_code=303)

    @app.post("/runs/{run_id}/approve-qa-preview")
    async def approve_qa_preview_form(
        run_id: str,
        request: Request,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        get_run_or_404(db, run_id)
        run_pipeline_action_or_ignore(lambda: pipeline.approve_qa_preview_safely(run_id, form_comment(form)))
        return RedirectResponse(f"/runs/{run_id}{token_suffix(request, settings, form)}", status_code=303)

    @app.post("/runs/{run_id}/reject-qa-preview")
    async def reject_qa_preview_form(
        run_id: str,
        request: Request,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        get_run_or_404(db, run_id)
        run_pipeline_action_or_ignore(lambda: pipeline.reject_qa_preview(run_id, form_comment(form)))
        return RedirectResponse(f"/runs/{run_id}{token_suffix(request, settings, form)}", status_code=303)

    @app.post("/runs/{run_id}/cancel")
    async def cancel_form(
        run_id: str,
        request: Request,
        db: Database = Depends(get_db),
        pipeline: Pipeline = Depends(get_pipeline),
    ) -> RedirectResponse:
        form = await request.form()
        require_ui_token(request, settings, form)
        get_run_or_404(db, run_id)
        run_pipeline_action_or_ignore(lambda: pipeline.cancel_run(run_id))
        return RedirectResponse(f"/runs/{run_id}{token_suffix(request, settings, form)}", status_code=303)

    return app


def require_action_status(db: Database, run_id: str, expected: RunStatus, action: str) -> None:
    run = get_run_or_404(db, run_id)
    if run.status != expected:
        raise HTTPException(
            status_code=409,
            detail=f"{action}에는 {expected.value} 상태가 필요합니다. 현재 상태: {run.status.value}",
        )


def get_run_or_404(db: Database, run_id: str) -> Any:
    try:
        return db.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def action_status_matches(db: Database, run_id: str, expected: RunStatus) -> bool:
    try:
        return db.get_run(run_id).status == expected
    except KeyError:
        return False


def run_pipeline_action(action: Any) -> Any:
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def run_pipeline_action_or_ignore(action: Any) -> None:
    try:
        action()
    except (KeyError, ValueError):
        return


def form_comment(form: Any) -> str:
    return str(form.get("comment") or "")


def optional_form_value(form: Any, name: str) -> dict[str, str]:
    value = str(form.get(name) or "").strip()
    return {name: value} if value else {}


def serialize_run(run: Any, settings: Settings | None = None) -> dict[str, Any]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "project_id": run.project_id,
        "title": run.title,
        "description": run.description,
        "target_branch": run.target_branch,
        "mode": run.mode,
        "status": run.status.value,
        "change_class": run.change_class,
        "review_depth": run.review_depth,
        "workspace_path": serialize_workspace_path(settings, run) if settings else (str(run.workspace_path) if run.workspace_path else None),
        "preview_url": run.preview_url,
        "preview_lan_urls": preview_lan_urls(run.preview_url, settings),
        "current_retry": run.current_retry,
        "max_retries": run.max_retries,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def serialize_artifact(artifact: Any, data_root: Any) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "run_id": artifact.run_id,
        "kind": artifact.kind,
        "name": artifact.name,
        "path": serialize_confined_path(data_root, artifact.run_id, artifact.path),
        "created_at": artifact.created_at,
    }


def serialize_approval(approval: Any) -> dict[str, Any]:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "approval_type": approval.approval_type,
        "decision": approval.decision,
        "comment": approval.comment,
        "created_at": approval.created_at,
    }


def serialize_run_event(run: Any, db: Database, settings: Settings | None = None) -> dict[str, Any]:
    artifacts = db.list_artifacts(run.id)
    gates = db.list_gate_results(run.id)
    approvals = db.list_approvals(run.id)
    return {
        "id": run.id,
        "session_id": run.session_id,
        "status": run.status.value,
        "updated_at": run.updated_at,
        "preview_url": run.preview_url,
        "preview_lan_urls": preview_lan_urls(run.preview_url, settings),
        "artifact_count": len(artifacts),
        "gate_count": len(gates),
        "approval_count": len(approvals),
        "latest_artifact": artifacts[-1].name if artifacts else None,
        "latest_gate": gates[-1].gate_name if gates else None,
        "latest_approval": approvals[-1].approval_type if approvals else None,
    }


def serialize_gate(result: Any, data_root: Any, run_id: str) -> dict[str, Any]:
    return {
        "gate_name": result.gate_name,
        "command": result.command,
        "exit_code": result.exit_code,
        "log_path": serialize_confined_path(data_root, run_id, result.log_path),
        "duration_ms": result.duration_ms,
    }


def serialize_confined_path(data_root: Any, run_id: str, path: Any) -> str:
    try:
        return str(safe_artifact_path(data_root, run_id, path))
    except ValueError:
        return "사용 불가"


def serialize_workspace_path(settings: Settings, run: Any) -> str | None:
    return workspace_display_path(settings.data_root, run.id, run.workspace_path)


def render_decision_context(db: Database, run: Any, suffix: str, data_root: Any) -> str:
    names = decision_context_artifacts(run.status)
    previews = [render_artifact_preview(db, run.id, name, suffix, data_root) for name in names]
    content = "".join(item for item in previews if item)
    if not content:
        return ""
    return f"<h2>판단 컨텍스트</h2>{content}"


def decision_context_artifacts(status: RunStatus) -> list[str]:
    if status == RunStatus.AWAITING_PLAN_APPROVAL:
        return ["02-auto-policy.md", "02-plan-review.md", "01-plan.md"]
    if status == RunStatus.AWAITING_QA_APPROVAL:
        return ["11-final-report.md", "docker-compose-health.log", "10-final.patch"]
    if status == RunStatus.AWAITING_PUBLISH_APPROVAL:
        return ["11-final-report.md", "10-final.patch"]
    if status in {RunStatus.PUBLISHED, RunStatus.READY_TO_PUBLISH, RunStatus.PUBLISHING}:
        return ["11-final-report.md", "12-publish.md", "10-final.patch"]
    if status in {RunStatus.ESCALATED, RunStatus.FAILED, RunStatus.CANCELLED}:
        return ["11-final-report.md", "02-scope-violation.md", "08-reviewer-write-violation.md", "01-planner-write-violation.md"]
    return []


def render_artifact_preview(db: Database, run_id: str, name: str, suffix: str, data_root: Any, limit: int = 12000) -> str:
    try:
        artifact = db.get_artifact(run_id, name)
        text = read_artifact_text(data_root, artifact)
    except (KeyError, OSError, UnicodeDecodeError, ValueError):
        return ""
    truncated = len(text) > limit
    snippet = text[:limit]
    if truncated:
        snippet = f"{snippet}\n\n[잘림; 전체 산출물을 열어 확인하세요]"
    link = f"/api/runs/{escape(run_id)}/artifacts/{escape(name)}{suffix}"
    return (
        f"<section class=\"artifact-preview\"><h3>{escape(name)}</h3>"
        f"<p><a href=\"{link}\">전체 산출물 열기</a></p>"
        f"<pre>{escape(snippet)}</pre></section>"
    )


def render_preview_anchor(preview_url: str) -> str:
    url = escape(preview_url, quote=True)
    return f"<a href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\">{escape(preview_url)}</a>"


def render_preview_links(preview_url: str | None, lan_urls: list[str] | None = None) -> str:
    parts: list[str] = []
    if preview_url:
        parts.append(render_preview_anchor(preview_url))
    unique_lan_urls = [url for url in dict.fromkeys(lan_urls or []) if url != preview_url]
    if unique_lan_urls:
        lan_links = ", ".join(render_preview_anchor(url) for url in unique_lan_urls)
        parts.append(f" <small>LAN: {lan_links}</small>")
    return "".join(parts)


def format_latest_value(value: str | None) -> str:
    if not value:
        return ""
    return f" <small>최신: {escape(value)}</small>"


def render_run_events_script(run_id: str, suffix: str) -> str:
    event_url = json.dumps(f"/api/runs/{run_id}/events{suffix}")
    return f"""<script>
(function () {{
  if (!window.EventSource) {{
    return;
  }}
  var statusElement = document.getElementById("run-status");
  var currentStatus = statusElement ? statusElement.textContent : "";
  var reloadStatuses = ["AWAITING_PLAN_APPROVAL", "AWAITING_QA_APPROVAL", "AWAITING_PUBLISH_APPROVAL", "PUBLISHED", "ESCALATED", "FAILED", "CANCELLED"];
  var source = new EventSource({event_url});
  function setText(id, value) {{
    var element = document.getElementById(id);
    if (element) {{
      element.textContent = value == null ? "" : String(value);
    }}
  }}
  function setLatest(id, value) {{
    var element = document.getElementById(id);
    if (element) {{
      element.textContent = value ? " 최신: " + value : "";
    }}
  }}
  function appendPreviewLink(element, url) {{
    var link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = url;
    element.appendChild(link);
  }}
  function setPreview(url, lanUrls) {{
    var element = document.getElementById("run-preview");
    if (!element) {{
      return;
    }}
    element.textContent = "";
    if (url) {{
      appendPreviewLink(element, url);
    }}
    if (lanUrls && lanUrls.length) {{
      var small = document.createElement("small");
      small.appendChild(document.createTextNode(" LAN: "));
      lanUrls.forEach(function (lanUrl, index) {{
        if (index > 0) {{
          small.appendChild(document.createTextNode(", "));
        }}
        appendPreviewLink(small, lanUrl);
      }});
      element.appendChild(small);
    }}
  }}
  source.onmessage = function (event) {{
    var payload = JSON.parse(event.data);
    setText("run-status", payload.status);
    setText("run-updated", payload.updated_at);
    setText("artifact-count", payload.artifact_count);
    setText("gate-count", payload.gate_count);
    setText("approval-count", payload.approval_count);
    setLatest("latest-artifact", payload.latest_artifact);
    setLatest("latest-gate", payload.latest_gate);
    setLatest("latest-approval", payload.latest_approval);
    setPreview(payload.preview_url, payload.preview_lan_urls || []);
    if (payload.status !== currentStatus && reloadStatuses.indexOf(payload.status) !== -1) {{
      window.location.reload();
    }}
    currentStatus = payload.status;
  }};
}}());
</script>"""


def preview_lan_urls(preview_url: str | None, settings: Settings | None) -> list[str]:
    if not preview_url or settings is None:
        return []
    try:
        parsed = urlsplit(preview_url)
        port = parsed.port
    except ValueError:
        return []
    if port is None:
        return []
    scheme = parsed.scheme or "http"
    path = parsed.path or ""
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if parsed.fragment:
        path = f"{path}#{parsed.fragment}"
    return [f"{scheme}://{format_url_host(host)}:{port}{path}" for host in lan_candidate_hosts(settings)]


def serialize_settings(settings: Settings, projects: list[ProjectConfig]) -> dict[str, Any]:
    qa_host = qa_access_host(settings)
    lan_hosts = lan_candidate_hosts(settings)
    return {
        "settings": {
            "data_root": str(settings.data_root),
            "database_path": str(settings.database_path),
            "bind_host": settings.bind_host,
            "bind_port": settings.bind_port,
            "qa_web_url": f"http://{qa_host}:{settings.bind_port}",
            "qa_lan_urls": [f"http://{format_url_host(host)}:{settings.bind_port}" for host in lan_hosts],
            "preview_host": settings.preview_host,
            "preview_base_port": settings.preview_base_port,
            "preview_port_count": settings.preview_port_count,
            "preview_port_start": settings.preview_base_port,
            "preview_port_end": settings.preview_base_port + settings.preview_port_count - 1,
            "preview_url_template": f"http://{qa_host}:{{preview_port}}",
            "preview_lan_url_templates": [
                f"http://{format_url_host(host)}:{{preview_port}}" for host in lan_hosts
            ],
            "lan_access": lan_access_status(settings),
            "shared_token_configured": settings.shared_token is not None,
        },
        "projects": [serialize_project_runtime_settings(project) for project in projects],
    }


def serialize_project_runtime_settings(project: ProjectConfig) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "config_path": project.config_path,
        "repo_url": project.repo_url,
        "docker_env_file": project.docker.env_file,
        "keep_success_runs": project.workspace.keep_success_runs,
        "keep_failed_runs": project.workspace.keep_failed_runs,
        "ai_roles": {
            role: {
                "command": config.command,
                "timeout_sec": config.timeout_sec,
                "configured": bool(config.command),
            }
            for role, config in sorted(project.ai.items())
        },
    }


def format_project_ai_roles(project: dict[str, Any]) -> str:
    roles = project["ai_roles"]
    if not roles:
        return "설정 없음"
    return ", ".join(
        f"{role}: {' '.join(config['command']) if config['configured'] else '설정 없음'}"
        for role, config in roles.items()
    )


def persist_project_yaml(settings: Settings, project: ProjectConfig, yaml_text: str) -> ProjectConfig:
    path = safe_project_yaml_path(settings, project.id)
    text = yaml_text if yaml_text.endswith("\n") else f"{yaml_text}\n"
    path.write_text(text, encoding="utf-8")
    return replace(project, config_path=str(path))


def safe_project_yaml_path(settings: Settings, project_id: str) -> Path:
    root = settings.data_root.resolve()
    projects_dir = root / "projects"
    if projects_dir.is_symlink():
        raise ValueError("프로젝트 설정 디렉터리는 symlink일 수 없습니다")
    projects_dir.mkdir(parents=True, exist_ok=True)
    projects_dir = projects_dir.resolve()
    try:
        projects_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError("프로젝트 설정 디렉터리가 data root 밖으로 벗어났습니다") from exc

    filename = safe_project_filename(project_id)
    path = projects_dir / f"{filename}.yaml"
    if path.is_symlink():
        raise ValueError("프로젝트 설정 경로는 symlink일 수 없습니다")
    resolved = path.resolve()
    try:
        resolved.relative_to(projects_dir)
    except ValueError as exc:
        raise ValueError("프로젝트 설정 경로가 설정 디렉터리 밖으로 벗어났습니다") from exc
    return resolved


def safe_project_filename(project_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_id).strip("._")
    return value or "project"


def token_matches(request: Request, expected: str, header_token: str | None = None, form: Any | None = None) -> bool:
    return header_token == expected or request.query_params.get("token") == expected or (form is not None and form.get("token") == expected)


def require_ui_token(request: Request, settings: Settings, form: Any | None = None) -> None:
    if settings.shared_token and not token_matches(request, settings.shared_token, form=form):
        raise HTTPException(status_code=401, detail="devauto token이 올바르지 않습니다")


def token_suffix(request: Request, settings: Settings, form: Any | None = None) -> str:
    if not settings.shared_token:
        return ""
    token = request.query_params.get("token") or (form.get("token") if form is not None else None)
    return f"?{urlencode({'token': str(token)})}" if token == settings.shared_token else ""


def token_hidden_input(request: Request, settings: Settings) -> str:
    if not settings.shared_token:
        return ""
    token = request.query_params.get("token")
    return f'<input type="hidden" name="token" value="{escape(str(token), quote=True)}">' if token == settings.shared_token else ""


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} - devauto</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; color: #17202a; background: #f7f8fa; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    nav {{ display: flex; gap: 12px; margin-bottom: 18px; }}
    form {{ display: grid; gap: 10px; margin: 14px 0 24px; padding: 16px; background: #fff; border: 1px solid #d9dee7; border-radius: 8px; }}
    label {{ display: grid; gap: 6px; font-size: 14px; font-weight: 600; }}
    input, textarea, select {{ font: inherit; padding: 8px 10px; border: 1px solid #b9c0cc; border-radius: 6px; background: #fff; }}
    textarea {{ min-height: 80px; }}
    button {{ justify-self: start; padding: 8px 12px; border: 1px solid #2563eb; border-radius: 6px; background: #2563eb; color: #fff; font-weight: 700; cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d9dee7; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e6e9ef; text-align: left; vertical-align: top; }}
    th {{ background: #eef2f7; font-size: 13px; }}
    dl {{ display: grid; grid-template-columns: 160px 1fr; gap: 8px 16px; background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 16px; }}
    dt {{ font-weight: 700; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    .status {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .actions form {{ margin: 0; padding: 0; border: 0; background: transparent; }}
    .artifact-preview {{ margin: 16px 0; padding: 14px; background: #fff; border: 1px solid #d9dee7; border-radius: 8px; }}
    .artifact-preview h3 {{ margin: 0 0 8px; font-size: 16px; }}
    .artifact-preview pre {{ max-height: 520px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: #0f172a; color: #f8fafc; padding: 12px; border-radius: 6px; }}
  </style>
</head>
<body><main><h1>{escape(title)}</h1>{body}</main></body>
</html>"""


app = create_app()
