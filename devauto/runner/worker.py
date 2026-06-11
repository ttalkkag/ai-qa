from __future__ import annotations

import time
from dataclasses import dataclass, field

from devauto.core.config import Settings
from devauto.core.db import Database
from devauto.core.models import Run, RunStatus
from devauto.runner.pipeline import Pipeline


@dataclass(frozen=True)
class WorkerTick:
    prepared: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.prepared or self.errors or self.recovered)


def run_pending_once(
    settings: Settings,
    db: Database | None = None,
    recover_stale_after_sec: float | None = None,
) -> WorkerTick:
    db = db or Database(settings.database_path)
    db.initialize()
    pipeline = Pipeline(settings, db)
    prepared: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    recovered: list[str] = []

    if recover_stale_after_sec is not None:
        try:
            recovered = [run.id for run in pipeline.recover_stale_runs(recover_stale_after_sec)]
        except Exception as exc:
            errors.append(f"stale recovery 오류: {exc}")

    for run in sorted(db.list_runs(), key=lambda item: (item.created_at, item.id)):
        if run.status != RunStatus.RECEIVED:
            continue
        if db.has_active_run(exclude_run_id=run.id):
            skipped.append(run.id)
            continue
        _prepare_run(pipeline, run, prepared, skipped, errors)

    try:
        next_run = pipeline.start_next_queued()
    except Exception as exc:
        errors.append(f"queued run 오류: {exc}")
    else:
        if next_run is not None:
            prepared.append(next_run.id)

    return WorkerTick(prepared=prepared, skipped=skipped, errors=errors, recovered=recovered)


def run_worker(
    settings: Settings,
    interval_sec: float = 2.0,
    once: bool = False,
    recover_stale_after_sec: float | None = None,
) -> None:
    db = Database(settings.database_path)
    db.initialize()
    while True:
        tick = run_pending_once(settings, db, recover_stale_after_sec=recover_stale_after_sec)
        if tick.recovered:
            print("stale 복구: " + ", ".join(tick.recovered), flush=True)
        if tick.prepared:
            print("준비됨: " + ", ".join(tick.prepared), flush=True)
        if tick.errors:
            print("오류: " + "; ".join(tick.errors), flush=True)
        if once:
            return
        time.sleep(interval_sec)


def _prepare_run(
    pipeline: Pipeline,
    run: Run,
    prepared: list[str],
    skipped: list[str],
    errors: list[str],
) -> None:
    try:
        completed = pipeline.prepare_run_safely(run.id)
    except Exception as exc:
        errors.append(f"{run.id}: {exc}")
        return
    if completed.status == RunStatus.PREPARING:
        skipped.append(run.id)
        return
    prepared.append(run.id)
