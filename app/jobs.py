"""The job store and the single worker thread every tool runs on.

A job is a directory, not a row: `~/.local/state/halo-workbench/jobs/<id>/` holding
`meta.json`, the uploaded input, and whatever the tool produced. That survives a
restart, needs no schema migration, and can be read with `ls` and `cat` when a user
reports something odd (CLAUDE.md "Jobs are directories").

Nothing here is request/response. Marker takes up to ~20 minutes on a real paper
(halo-prep, 2026-08-23 — an agent concluded it had stalled at that point and it had
not), so every tool submits a job and the browser polls.

Concurrency is 1 and is not a config knob (CLAUDE.md "Concurrency 1"): every job
contends for the same GPU as three resident llama-server units, marker spawns ~2.9 GB
of its own surya servers on top, and this box has no swap and OOM-kills under
pressure (halo-prep pitfall #17).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import secrets
import shutil
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import config

log = logging.getLogger(__name__)

# How often the pruner wakes. Retention itself is 7 days (config.JOB_RETENTION_DAYS,
# operator 2026-08-29); this is only how promptly we notice a job crossed it. Pruning
# on startup alone would mean a service that stays up for a month never prunes at all,
# and these are the user's real documents.
PRUNE_INTERVAL_SECONDS = 3600

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# The two states a job can be resumed or reaped from after a restart. Everything else
# is terminal and is only ever read.
_LIVE = (QUEUED, RUNNING)

# One tool handler per name, registered at import time by app/tools/*.py. A handler
# takes the Job and either returns (the job is `done`) or raises (the job is `failed`
# with the exception text). There is deliberately no third outcome: a result the
# verifier did not pass must raise, never return (CLAUDE.md "the rule").
_TOOLS: dict[str, Callable[[Job], None]] = {}

_QUEUE: queue.Queue[str] = queue.Queue()
_META_LOCK = threading.Lock()  # meta.json writes come from both the worker and uvicorn
_STOP = threading.Event()
_WORKER: threading.Thread | None = None
_PRUNER: threading.Thread | None = None
_LOCK_FD: int | None = None


def _acquire_lock() -> None:
    """Take an exclusive flock on the state directory, or refuse to start.

    Concurrency 1 is the rule this whole module implements, and until now nothing
    enforced *one instance*. Two of them sharing a state directory fight in ways that
    destroy work rather than merely duplicating it: the second one's recovery marks
    the first one's in-flight job `failed / interrupted` and reaps its cgroup, killing
    a conversion that was twenty minutes in. `dev.sh` next to the installed unit is
    all it takes, and uvicorn runs the lifespan BEFORE it binds the port -- so the
    second instance does that damage on its way to exiting with "address already in
    use", which reads like a harmless mistake and is not one.

    flock rather than a pid file: the kernel drops it when the process dies, so a
    `kill -9` -- which is a supported way to stop this service, and an acceptance
    test -- cannot leave a stale lock that needs clearing by hand.
    """
    global _LOCK_FD

    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.STATE_DIR / "lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.read(fd, 64).decode(errors="replace").strip() or "unknown"
        os.close(fd)
        raise StateDirBusy(
            f"another halo-workbench (pid {holder}) is already using "
            f"{config.STATE_DIR}. Only one instance may run against a state "
            f"directory — check `systemctl --user status halo-workbench`, or point "
            f"this one somewhere else with WORKBENCH_STATE_DIR."
        ) from None

    # The pid is only for the message above; the lock is the flock, not this content.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _LOCK_FD = fd


def _release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        os.close(_LOCK_FD)  # closing drops the flock
        _LOCK_FD = None


def _now() -> str:
    """UTC, seconds, ISO-8601 -- the format meta.json stores and _prune parses."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateDirBusy(RuntimeError):
    """Another instance already holds this state directory. Startup must not continue."""


class JobError(Exception):
    """A tool's own failure, reported to the user as-is.

    Raise this for a message the user should read (`marker exited 3: suspect output`).
    Anything else that escapes a handler is a bug and gets a generic message plus a
    traceback in the job's `error.log` -- an internal error should not put a Python
    repr on a status page.
    """


@dataclass(frozen=True)
class Job:
    id: str
    meta: dict[str, Any]

    @property
    def dir(self) -> Path:
        return config.JOBS_DIR / self.id

    @property
    def status(self) -> str:
        return self.meta["status"]

    @property
    def tool(self) -> str:
        return self.meta["tool"]

    @property
    def error(self) -> str | None:
        return self.meta.get("error")

    @property
    def params(self) -> dict[str, Any]:
        return self.meta.get("params", {})

    @property
    def result(self) -> dict[str, Any]:
        """Whatever the tool recorded about its output. See `record`."""
        return self.meta.get("result", {})

    @property
    def input_path(self) -> Path | None:
        """The uploaded file, if this job had one."""
        name = self.meta.get("input_filename")
        return self.dir / name if name else None

    def path(self, name: str) -> Path:
        """A path inside the job directory, for a tool to write its output to."""
        return self.dir / name

    def elapsed_seconds(self) -> float | None:
        """Wall time since the job started -- the status page's most important field.

        The single most likely user error is concluding a 20-minute conversion has
        hung (PLAN.md phase 2), so every tool page shows this.
        """
        started = self.meta.get("started_at")
        if not started:
            return None
        end = self.meta.get("finished_at") or _now()
        return (datetime.fromisoformat(end) - datetime.fromisoformat(started)).total_seconds()


def register(name: str) -> Callable[[Callable[[Job], None]], Callable[[Job], None]]:
    """Register a tool handler under `name`. Used as a decorator by app/tools/*.py."""

    def decorate(fn: Callable[[Job], None]) -> Callable[[Job], None]:
        if name in _TOOLS and _TOOLS[name] is not fn:
            raise RuntimeError(f"tool {name!r} is already registered")
        _TOOLS[name] = fn
        return fn

    return decorate


# ── reading and writing meta.json ────────────────────────────────────────────


def _meta_path(job_id: str) -> Path:
    return config.JOBS_DIR / job_id / "meta.json"


def _write_meta(job_id: str, meta: dict[str, Any]) -> None:
    """Replace meta.json atomically.

    Write-then-rename rather than truncate-then-write: a `kill -9` (which is an
    acceptance test, not a hypothetical) between those two must never leave a job
    whose status is half a word. The reader either sees the old file or the new one.
    """
    path = _meta_path(job_id)
    tmp = path.with_suffix(".json.tmp")
    with _META_LOCK:
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)


def _read_meta(job_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_meta_path(job_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _update(job_id: str, **fields: Any) -> dict[str, Any]:
    meta = _read_meta(job_id) or {}
    meta.update(fields)
    _write_meta(job_id, meta)
    return meta


# ── the public API ───────────────────────────────────────────────────────────


def new_id() -> str:
    """Sortable, and readable in `ls`: 20260829-160142-3f9c1a.

    The timestamp prefix means directory listing order is submission order, which is
    what both the queue recovery and a human debugging by eye want. The random suffix
    is only there to keep two submissions in the same second apart.
    """
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def submit(
    tool: str,
    *,
    params: dict[str, Any] | None = None,
    upload: tuple[str, bytes] | None = None,
) -> str:
    """Create a job directory, enqueue it, and return its id.

    `upload` is (filename, bytes) and is written into the job directory before the job
    becomes visible as queued -- the worker must never start on a half-written input.
    """
    if tool not in _TOOLS:
        raise KeyError(f"no such tool: {tool!r}")

    job_id = new_id()
    job_dir = config.JOBS_DIR / job_id
    job_dir.mkdir(parents=True)

    input_filename = None
    if upload is not None:
        # Basename only. The filename comes from a browser and `../../.ssh/authorized_keys`
        # is a filename as far as multipart is concerned.
        input_filename = Path(upload[0]).name or "input"
        (job_dir / input_filename).write_bytes(upload[1])

    _write_meta(
        job_id,
        {
            "id": job_id,
            "tool": tool,
            "status": QUEUED,
            "params": params or {},
            "input_filename": input_filename,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
        },
    )
    _QUEUE.put(job_id)
    log.info("job %s queued (%s)", job_id, tool)
    return job_id


def record(job_id: str, **fields: Any) -> None:
    """Merge tool-produced facts into meta.json's `result` object.

    Paths recorded here are relative to the job directory, never absolute: the
    directory can be read after a restart, and a status page that has to resolve an
    absolute path is one rename away from serving a file from somewhere else.
    """
    meta = _read_meta(job_id) or {}
    meta["result"] = {**meta.get("result", {}), **fields}
    _write_meta(job_id, meta)


def get(job_id: str) -> Job | None:
    """Read a job from disk. Always from disk -- there is no in-memory copy to go stale."""
    # Defends the obvious traversal: `GET /marker/../../../etc` must not read a path
    # outside the jobs directory.
    if "/" in job_id or job_id in (".", ".."):
        return None
    meta = _read_meta(job_id)
    return Job(job_id, meta) if meta else None


def all_jobs() -> list[Job]:
    """Every job on disk, newest first (ids sort chronologically)."""
    if not config.JOBS_DIR.is_dir():
        return []
    jobs = (get(d.name) for d in config.JOBS_DIR.iterdir() if d.is_dir())
    return sorted((j for j in jobs if j), key=lambda j: j.id, reverse=True)


def queue_depth() -> int:
    return _QUEUE.qsize()


# ── the worker ───────────────────────────────────────────────────────────────


def _run(job_id: str) -> None:
    job = get(job_id)
    if job is None:  # pruned, or deleted by hand, between enqueue and pickup
        log.warning("job %s vanished before it ran", job_id)
        return

    meta = _update(job_id, status=RUNNING, started_at=_now())
    job = Job(job_id, meta)
    log.info("job %s running (%s)", job_id, job.tool)

    try:
        _TOOLS[job.tool](job)
    except JobError as exc:
        _update(job_id, status=FAILED, finished_at=_now(), error=str(exc))
        log.info("job %s failed: %s", job_id, exc)
    except Exception:
        # A bug, not a tool outcome. The traceback goes in the job directory rather
        # than only the journal, so it is beside the job the operator is looking at --
        # and never into the status message, which the user reads.
        job.path("error.log").write_text(traceback.format_exc())
        _update(
            job_id,
            status=FAILED,
            finished_at=_now(),
            error="internal error — see error.log in the job directory",
        )
        log.exception("job %s raised", job_id)
    else:
        _update(job_id, status=DONE, finished_at=_now())
        log.info("job %s done", job_id)


def _worker_loop() -> None:
    while not _STOP.is_set():
        try:
            job_id = _QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _run(job_id)
        finally:
            _QUEUE.task_done()


# ── startup recovery and retention ───────────────────────────────────────────


def _recover() -> list[str]:
    """Reconcile the disk with the fact that this process just started.

    Returns the ids it marked interrupted, because a tool may have left more than a
    status behind -- marker leaves a cgroup full of model servers holding GPU memory.

    Runs before the worker does, because both of these decide what the worker sees:

    - `running` means a job that was mid-flight when the process died. Nobody is doing
      it and nobody can resume it, so it is `failed / interrupted`. Leaving it running
      would hang the UI forever on a job that no longer exists.
    - `queued` means a job that was accepted and never started. Re-enqueue it in
      submission order; the user was told it was accepted.
    """
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    interrupted = []

    for job in sorted(all_jobs(), key=lambda j: j.id):  # oldest first: submission order
        if job.status == RUNNING:
            _update(job.id, status=FAILED, finished_at=_now(), error="interrupted")
            log.warning("job %s was running at shutdown — marked interrupted", job.id)
            interrupted.append(job.id)
        elif job.status == QUEUED:
            _QUEUE.put(job.id)
            log.info("job %s re-queued after restart", job.id)

    return interrupted


def prune(now: datetime | None = None) -> int:
    """Delete job directories past the retention window. Returns how many went.

    The whole directory, not just the output: the uploaded original is the sensitive
    half (CLAUDE.md "Privacy"). Live jobs are skipped whatever their age -- a queue
    that has been stuck behind a 20-minute marker run must not have its input deleted
    out from under it.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=config.JOB_RETENTION_DAYS)
    removed = 0

    if not config.JOBS_DIR.is_dir():
        return 0

    for entry in config.JOBS_DIR.iterdir():
        if not entry.is_dir():
            continue
        meta = _read_meta(entry.name)
        if meta and meta.get("status") in _LIVE:
            continue
        # mtime is the fallback for a directory whose meta.json is missing or corrupt
        # -- exactly the debris that most deserves pruning, so it must not be skipped.
        created = meta.get("created_at") if meta else None
        try:
            age_ref = datetime.fromisoformat(created) if created else None
        except ValueError:
            age_ref = None
        if age_ref is None:
            age_ref = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
        if age_ref < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            log.info("pruned job %s (older than %d days)", entry.name, config.JOB_RETENTION_DAYS)

    return removed


def _pruner_loop() -> None:
    while not _STOP.is_set():
        # Wait first: startup pruning already happened in start(), and running it twice
        # a second apart tells us nothing.
        if _STOP.wait(PRUNE_INTERVAL_SECONDS):
            return
        try:
            prune()
        except Exception:  # a failed prune must never take the service down with it
            log.exception("prune failed")


# ── lifecycle ────────────────────────────────────────────────────────────────


def start() -> list[str]:
    """Recover, prune, and start the two background threads. Called from the lifespan.

    Returns the ids of jobs that were interrupted by the previous process, for a tool
    that has cleaning up of its own to do.
    """
    global _WORKER, _PRUNER

    # First, before recovery touches a single meta.json.
    _acquire_lock()
    _STOP.clear()
    interrupted = _recover()
    prune()

    # Exactly one worker. A second would double GPU contention on a box with no swap,
    # and -- because the serialisation lives in this queue rather than in a lock a
    # second process could see -- would break it silently rather than loudly.
    _WORKER = threading.Thread(target=_worker_loop, name="job-worker", daemon=True)
    _WORKER.start()
    _PRUNER = threading.Thread(target=_pruner_loop, name="job-pruner", daemon=True)
    _PRUNER.start()

    return interrupted


def stop(timeout: float = 5.0) -> None:
    """Ask the threads to finish. A job already running is left to run out its timeout.

    Both threads are daemons, so a hard exit cannot be blocked by one; anything still
    `running` when the process goes is reaped as `interrupted` on the next start.
    """
    _STOP.set()
    for thread in (_WORKER, _PRUNER):
        if thread is not None:
            thread.join(timeout=timeout)
    _release_lock()
