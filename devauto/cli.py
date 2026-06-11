from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn

from devauto.core.config import load_settings
from devauto.core.doctor import run_project_doctor
from devauto.core.project_config import load_project_config
from devauto.core.db import Database
from devauto.runner.pipeline import Pipeline
from devauto.runner.worker import run_worker


def main() -> None:
    parser = argparse.ArgumentParser(prog="devauto")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("project_config", type=Path)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--once", action="store_true", help="대기 중인 run을 한 번 처리하고 종료합니다")
    worker.add_argument("--interval", type=float, default=2.0, help="poll 간격(초)")
    worker.add_argument(
        "--recover-stale-minutes",
        type=float,
        default=None,
        help="poll 전에 지정 분 동안 갱신이 없는 active run을 실패 처리합니다",
    )
    recover = subparsers.add_parser("recover-stale")
    recover.add_argument(
        "--older-than-minutes",
        type=float,
        required=True,
        help="지정 분 동안 갱신이 없는 active run을 실패 처리합니다",
    )
    args = parser.parse_args()
    if args.command == "doctor":
        project = load_project_config(args.project_config)
        print(json.dumps(run_project_doctor(project).to_mapping(), indent=2))
        return
    if args.command is None:
        apply_server_overrides(args.host, args.port)
    settings = load_settings()
    if args.command == "worker":
        recover_after = args.recover_stale_minutes * 60 if args.recover_stale_minutes is not None else None
        run_worker(settings, interval_sec=args.interval, once=args.once, recover_stale_after_sec=recover_after)
        return
    if args.command == "recover-stale":
        db = Database(settings.database_path)
        db.initialize()
        recovered = Pipeline(settings, db).recover_stale_runs(args.older_than_minutes * 60)
        print(json.dumps({"recovered": [run.id for run in recovered]}, indent=2))
        return
    uvicorn.run(
        "devauto.api.main:app",
        host=args.host or settings.bind_host,
        port=args.port or settings.bind_port,
        reload=False,
    )


def apply_server_overrides(host: str | None, port: int | None) -> None:
    if host is not None:
        os.environ["DEVAUTO_BIND_HOST"] = host
    if port is not None:
        os.environ["DEVAUTO_BIND_PORT"] = str(port)


if __name__ == "__main__":
    main()
