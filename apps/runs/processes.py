import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
from django.conf import settings
from django.utils import timezone

from apps.errors import DomainError
from .models import Job, ProcessRecord, Run, WorkerSlot


OUTPUT_LIMIT = 10 * 1024 * 1024
_GATE = "import os,sys; fd=int(sys.argv[1]); permit=os.read(fd,1); os.close(fd); (os.execvpe(sys.argv[2],sys.argv[2:],os.environ) if permit==b'1' else sys.exit(125))"


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    output_complete: bool
    timed_out: bool


def host_boot_id():
    if sys.platform == "linux":
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=3)
    if result.returncode or not result.stdout.strip():
        raise DomainError("PROCESS_IDENTITY_UNAVAILABLE")
    return result.stdout.strip()


def process_table():
    table = {}
    for process in psutil.process_iter(["pid", "ppid", "create_time"]):
        try:
            info = process.info
            group = os.getpgid(info["pid"])
            identity = f"{info['create_time']:.9f}"
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied, TypeError):
            raise DomainError("PROCESS_IDENTITY_UNAVAILABLE")
        table[info["pid"]] = (info["ppid"], group, identity)
    return table


def _blocked(record):
    WorkerSlot.objects.filter(name=record.job.slot, job=record.job).update(blocked_reason="EXECUTION_BLOCKED")
    record.state = "UNKNOWN"
    record.save(update_fields=["state"])
    raise DomainError("PROCESS_TERMINATION_UNCONFIRMED", "실행 프로세스의 종료를 확인할 수 없습니다. 관리자가 호스트를 확인해야 합니다.")


def _observe(record):
    table = process_table()
    if record.host_boot_id != host_boot_id():
        return {}
    tracked = {int(pid): identity for pid, identity in record.descendants.items()}
    if record.pid:
        tracked[record.pid] = record.process_start_identity
    for pid, identity in tracked.items():
        if pid in table and table[pid][2] != identity:
            _blocked(record)
    changed = True
    while changed:
        changed = False
        for pid, (parent, group, identity) in table.items():
            if pid in tracked:
                continue
            if parent in tracked or group == record.process_group_id:
                tracked[pid] = identity
                changed = True
    live = {pid: table[pid] for pid in tracked if pid in table}
    if any(row[1] != record.process_group_id for row in live.values()):
        _blocked(record)
    record.descendants = {str(pid): identity for pid, identity in tracked.items() if pid != record.pid}
    record.save(update_fields=["descendants"])
    return live


def confirm_stopped(record):
    if record.ended_at:
        return True
    if record.pid is None or not record.process_start_identity:
        return False
    try:
        live = _observe(record)
    except (OSError, subprocess.SubprocessError, DomainError):
        return False
    if live:
        return False
    record.ended_at = timezone.now()
    record.state = "STOPPED"
    record.save(update_fields=["ended_at", "state"])
    return True


def terminate_record(record, child=None):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            live = _observe(record)
            if not live:
                break
            os.killpg(record.process_group_id, sig)
        except ProcessLookupError:
            pass
        except (OSError, subprocess.SubprocessError):
            _blocked(record)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child:
                child.poll()
            if confirm_stopped(record):
                return True
            time.sleep(0.1)
    if child:
        child.poll()
    if not confirm_stopped(record):
        _blocked(record)
    return True


def _cancelled(job):
    current = Job.objects.filter(pk=job.pk).values("state", "lease_epoch", "lease_owner", "lease_expires_at", "cancel_requested_at").first()
    if not current or current["state"] != "RUNNING" or current["lease_epoch"] != job.lease_epoch or current["lease_owner"] != job.lease_owner:
        return True
    if current["cancel_requested_at"]:
        return True
    if not current["lease_expires_at"] or current["lease_expires_at"] <= timezone.now():
        return True
    if job.kind == "RUN":
        return Run.objects.filter(pk=job.subject_id, status="CANCEL_REQUESTED").exists()
    return False


def _request_stop(job, reason):
    if job.kind != "RUN":
        return
    from .services import TERMINAL, transition_run

    try:
        run = Run.objects.get(pk=job.subject_id)
        if run.status not in TERMINAL and run.status != "CANCEL_REQUESTED":
            transition_run(run.pk, "CANCEL_REQUESTED", lease_epoch=job.lease_epoch, stop_reason=reason,
                           message="실행을 중단하고 전체 프로세스 종료를 확인합니다.")
    except Exception:
        return


def launch_process(*, job, argv, cwd, env, timeout, stdin_text=None, stdout_line_limit=None, output_limit=OUTPUT_LIMIT, container_name=""):
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise DomainError("INVALID_COMMAND")
    if timeout <= 0 or timeout > 1800 or _cancelled(job):
        raise DomainError("JOB_NOT_RUNNING")
    if type(output_limit) is not int or not 0 < output_limit <= OUTPUT_LIMIT:
        raise DomainError("INVALID_OUTPUT_LIMIT")
    if stdout_line_limit is not None and (type(stdout_line_limit) is not int or not 0 < stdout_line_limit <= OUTPUT_LIMIT):
        raise DomainError("INVALID_OUTPUT_LIMIT")
    record = ProcessRecord.objects.create(
        job=job, run_id=job.subject_id if job.kind == "RUN" else None, stage=job.kind,
        worker_identity=job.lease_owner, instance_id=settings.AIQA_INSTANCE_ID, host_boot_id=host_boot_id(), container_name=container_name,
    )
    read_gate, write_gate = os.pipe()
    child = None
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", _GATE, str(read_gate), *argv], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(read_gate,), start_new_session=True,
        )
        os.close(read_gate)
        read_gate = None
        identity = process_table().get(child.pid)
        if not identity or identity[1] != child.pid:
            raise DomainError("PROCESS_IDENTITY_UNAVAILABLE")
        record.pid, record.process_group_id, record.process_start_identity = child.pid, identity[1], identity[2]
        record.state = "REGISTERED"
        record.save(update_fields=["pid", "process_group_id", "process_start_identity", "state"])
        if _cancelled(job):
            raise DomainError("JOB_CANCELLED")
        os.write(write_gate, b"1")
        os.close(write_gate)
        write_gate = None
        record.state = "RUNNING"
        record.save(update_fields=["state"])
        return _collect(child, record, job, timeout, stdin_text, stdout_line_limit, output_limit)
    except BaseException:
        if write_gate is not None:
            os.close(write_gate)
            write_gate = None
        if child is not None:
            if record.pid:
                _request_stop(job, "PROCESS_FAILED")
                terminate_record(record, child)
            else:
                child.wait(timeout=5)
                record.state = "STOPPED"
                record.ended_at = timezone.now()
                record.save(update_fields=["state", "ended_at"])
        else:
            record.state = "NOT_STARTED"
            record.ended_at = timezone.now()
            record.save(update_fields=["state", "ended_at"])
        raise
    finally:
        if read_gate is not None:
            os.close(read_gate)
        if write_gate is not None:
            os.close(write_gate)


def _collect(child, record, job, timeout, stdin_text, stdout_line_limit, output_limit):
    def feed():
        try:
            if stdin_text:
                child.stdin.write(stdin_text.encode("utf-8"))
            child.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    next_observation = 0
    timed_out = False
    output_complete = True
    stdout_line_size = 0
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map() or child.poll() is None:
            now = time.monotonic()
            if now >= next_observation:
                _observe(record)
                next_observation = now + 0.25
                if _cancelled(job) or now >= deadline:
                    timed_out = now >= deadline
                    _request_stop(job, "CLI_TIMEOUT" if timed_out else "WORKER_LEASE_LOST")
                    terminate_record(record, child)
                    break
            for key, _ in selector.select(0.1):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = streams[key.data]
                available = output_limit - sum(len(part) for part in streams.values())
                buffer.extend(chunk[:max(0, available)])
                event_too_large = False
                if key.data == "stdout" and stdout_line_limit is not None:
                    pieces = chunk.split(b"\n")
                    for position, piece in enumerate(pieces):
                        stdout_line_size += len(piece)
                        if stdout_line_size > stdout_line_limit:
                            event_too_large = True
                            break
                        if position < len(pieces) - 1:
                            stdout_line_size = 0
                if len(chunk) > available or event_too_large:
                    output_complete = False
                    _request_stop(job, "EVENT_LIMIT_EXCEEDED" if event_too_large else "OUTPUT_LIMIT_EXCEEDED")
                    terminate_record(record, child)
                    break
            if not output_complete:
                break
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        terminate_record(record, child)
        child.wait(timeout=1)
    if not confirm_stopped(record):
        _request_stop(job, "DESCENDANTS_STILL_RUNNING")
        terminate_record(record, child)
    record.exit_code = child.returncode
    record.save(update_fields=["exit_code"])
    writer.join(timeout=1)
    child.stdout.close()
    child.stderr.close()
    decoded = {}
    for name, content in streams.items():
        try:
            decoded[name] = content.decode("utf-8")
        except UnicodeDecodeError:
            decoded[name] = content.decode("utf-8", errors="replace")
            output_complete = False
    return ProcessResult(child.returncode, decoded["stdout"], decoded["stderr"], output_complete, timed_out)
