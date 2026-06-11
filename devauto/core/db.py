from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from devauto.core.models import (
    Approval,
    Artifact,
    GateResult,
    ProjectConfig,
    Run,
    RunStatus,
    TERMINAL_STATUSES,
    normalize_run_description,
    validate_git_branch,
    validate_run_mode,
    validate_run_title,
)
from devauto.core.state_machine import assert_transition


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def new_session_id() -> str:
    return f"session-{secrets.token_hex(8)}"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  config_path TEXT NOT NULL DEFAULT '',
                  config_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL DEFAULT '',
                  project_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL,
                  target_branch TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  request_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  change_class TEXT,
                  review_depth INTEGER DEFAULT 1,
                  workspace_path TEXT,
                  preview_url TEXT,
                  current_retry INTEGER DEFAULT 0,
                  max_retries INTEGER DEFAULT 2,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  name TEXT NOT NULL,
                  path TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  approval_type TEXT NOT NULL,
                  decision TEXT NOT NULL,
                  comment TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gate_results (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  gate_name TEXT NOT NULL,
                  command TEXT NOT NULL,
                  exit_code INTEGER NOT NULL,
                  log_path TEXT NOT NULL,
                  duration_ms INTEGER NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "projects", "config_path", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "runs", "session_id", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_project(self, project: ProjectConfig) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, config_path, config_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name = excluded.name,
                  config_path = excluded.config_path,
                  config_json = excluded.config_json
                """,
                (project.id, project.name, project.config_path, json.dumps(project.to_mapping()), now),
            )

    def list_projects(self) -> list[ProjectConfig]:
        with self.connect() as conn:
            rows = conn.execute("SELECT config_json, config_path FROM projects ORDER BY id").fetchall()
        return [self._project_from_row(row) for row in rows]

    def get_project(self, project_id: str) -> ProjectConfig:
        with self.connect() as conn:
            row = conn.execute("SELECT config_json, config_path FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"프로젝트를 찾을 수 없습니다: {project_id}")
        return self._project_from_row(row)

    def create_run(
        self,
        request: dict[str, Any],
        project: ProjectConfig,
        status: RunStatus = RunStatus.RECEIVED,
    ) -> Run:
        now = utc_now()
        run_id = new_run_id()
        session_id = new_session_id()
        title = validate_run_title(request.get("title"))
        description = normalize_run_description(request.get("description"))
        target_branch = validate_git_branch(request.get("target_branch") or project.default_branch, "target_branch")
        mode = validate_run_mode(request.get("mode") or project.policy.default_mode)
        normalized_request = {
            **request,
            "project_id": project.id,
            "title": title,
            "description": description,
            "target_branch": target_branch,
            "mode": mode,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                  id, session_id, project_id, title, description, target_branch, mode,
                  request_json, status, max_retries, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    project.id,
                    title,
                    description,
                    target_branch,
                    mode,
                    json.dumps(normalized_request),
                    status.value,
                    project.policy.max_inner_gate_fixes + project.policy.max_outer_ai_fixes,
                    now,
                    now,
                ),
            )
        return self.get_run(run_id)

    def list_runs(self) -> list[Run]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC").fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_project_runs(self, project_id: str) -> list[Run]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC, id DESC",
                (project_id,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def project_has_active_run(self, project_id: str, exclude_run_id: str | None = None) -> bool:
        return self.has_active_run(project_id=project_id, exclude_run_id=exclude_run_id)

    def has_active_run(self, project_id: str | None = None, exclude_run_id: str | None = None) -> bool:
        terminal = [status.value for status in TERMINAL_STATUSES]
        placeholders = ", ".join("?" for _ in terminal)
        project_clause = "AND project_id = ?" if project_id else ""
        exclude_clause = "AND id != ?" if exclude_run_id else ""
        values = [RunStatus.QUEUED.value, *terminal]
        if project_id:
            values.append(project_id)
        if exclude_run_id:
            values.append(exclude_run_id)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1 FROM runs
                WHERE status != ?
                  AND status NOT IN ({placeholders})
                  {project_clause}
                  {exclude_clause}
                LIMIT 1
                """,
                values,
            ).fetchone()
        return row is not None

    def next_queued_run(self, project_id: str | None = None) -> Run | None:
        project_clause = "AND project_id = ?" if project_id else ""
        values = [RunStatus.QUEUED.value]
        if project_id:
            values.append(project_id)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM runs
                WHERE status = ?
                  {project_clause}
                ORDER BY created_at ASC
                LIMIT 1
                """,
                values,
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def get_run(self, run_id: str) -> Run:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"실행을 찾을 수 없습니다: {run_id}")
        return self._run_from_row(row)

    def transition_run(self, run_id: str, status: RunStatus) -> Run:
        run = self.get_run(run_id)
        assert_transition(run.status, status)
        return self.update_run(run_id, status=status)

    def try_transition_run(self, run_id: str, expected: list[RunStatus], status: RunStatus) -> Run | None:
        for current in expected:
            assert_transition(current, status)
        if not expected:
            return None
        now = utc_now()
        placeholders = ", ".join("?" for _ in expected)
        values = [status.value, now, run_id, *[item.value for item in expected]]
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE runs
                SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                values,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_run(run_id)

    def update_run(self, run_id: str, **fields: Any) -> Run:
        if not fields:
            return self.get_run(run_id)
        fields["updated_at"] = utc_now()
        columns = []
        values = []
        for key, value in fields.items():
            if key == "status" and isinstance(value, RunStatus):
                value = value.value
            elif key in {"workspace_path"} and isinstance(value, Path):
                value = str(value)
            columns.append(f"{key} = ?")
            values.append(value)
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(columns)} WHERE id = ?", values)
        return self.get_run(run_id)

    def add_artifact(self, run_id: str, kind: str, name: str, path: Path) -> Artifact:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO artifacts (run_id, kind, name, path, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, kind, name, str(path), now),
            )
            artifact_id = int(cursor.lastrowid)
        return Artifact(artifact_id, run_id, kind, name, path, now)

    def list_artifacts(self, run_id: str) -> list[Artifact]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            Artifact(
                id=int(row["id"]),
                run_id=str(row["run_id"]),
                kind=str(row["kind"]),
                name=str(row["name"]),
                path=Path(row["path"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def get_artifact(self, run_id: str, name: str) -> Artifact:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? AND name = ? ORDER BY id DESC LIMIT 1",
                (run_id, name),
            ).fetchone()
        if row is None:
            raise KeyError(f"산출물을 찾을 수 없습니다: {run_id}/{name}")
        return Artifact(
            id=int(row["id"]),
            run_id=str(row["run_id"]),
            kind=str(row["kind"]),
            name=str(row["name"]),
            path=Path(row["path"]),
            created_at=str(row["created_at"]),
        )

    def add_approval(self, run_id: str, approval_type: str, decision: str, comment: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (run_id, approval_type, decision, comment, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, approval_type, decision, comment, utc_now()),
            )

    def list_approvals(self, run_id: str) -> list[Approval]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            Approval(
                id=int(row["id"]),
                run_id=str(row["run_id"]),
                approval_type=str(row["approval_type"]),
                decision=str(row["decision"]),
                comment=str(row["comment"] or ""),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def add_gate_result(self, run_id: str, result: GateResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO gate_results (run_id, gate_name, command, exit_code, log_path, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.gate_name,
                    result.command,
                    result.exit_code,
                    str(result.log_path),
                    result.duration_ms,
                    utc_now(),
                ),
            )

    def list_gate_results(self, run_id: str) -> list[GateResult]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gate_results WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            GateResult(
                gate_name=str(row["gate_name"]),
                command=str(row["command"]),
                exit_code=int(row["exit_code"]),
                log_path=Path(row["log_path"]),
                duration_ms=int(row["duration_ms"]),
            )
            for row in rows
        ]

    def _run_from_row(self, row: sqlite3.Row) -> Run:
        workspace_path = Path(row["workspace_path"]) if row["workspace_path"] else None
        return Run(
            id=str(row["id"]),
            session_id=str(row["session_id"] or row["id"]),
            project_id=str(row["project_id"]),
            title=str(row["title"]),
            description=str(row["description"]),
            target_branch=str(row["target_branch"]),
            mode=str(row["mode"]),
            status=RunStatus(str(row["status"])),
            request=json.loads(str(row["request_json"])),
            change_class=row["change_class"],
            review_depth=int(row["review_depth"]),
            workspace_path=workspace_path,
            preview_url=row["preview_url"],
            current_retry=int(row["current_retry"]),
            max_retries=int(row["max_retries"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _project_from_row(self, row: sqlite3.Row) -> ProjectConfig:
        data = json.loads(row["config_json"])
        config_path = str(row["config_path"] or "")
        if config_path:
            data = {**data, "config_path": config_path}
        return ProjectConfig.from_mapping(data, inline_secret_action="redact")
