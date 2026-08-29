"""Driver for the PLAN.md phase 1 acceptance tests. Run it via tests/phase1_acceptance.sh.

Not a unit-test suite -- this repo's checks are per-request verifiers, not a suite
that runs at build time (CLAUDE.md). This exists because the two phase 1 acceptance
criteria are about a *process*: what the queue does when three jobs arrive at once,
and what it does when the process is killed mid-job. Both need a real one to kill.

    python tests/phase1_driver.py serial     # three jobs at once, one at a time, in order
    python tests/phase1_driver.py crash      # submits 3, SIGKILLs itself mid-job
    python tests/phase1_driver.py recover    # asserts what the next start() makes of that
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, jobs  # noqa: E402  (after sys.path)

RUN_LOG = config.STATE_DIR / "run.log"


@jobs.register("sleepy")
def sleepy(job: jobs.Job) -> None:
    """Occupies the worker for `seconds`, logging when it entered and left.

    The log is what proves serialisation: with one worker no two intervals overlap,
    and the order of entries is submission order.
    """
    with RUN_LOG.open("a") as fh:
        fh.write(f"{time.time():.3f} enter {job.id}\n")
    time.sleep(job.params["seconds"])
    job.path("output.txt").write_text(f"slept {job.params['seconds']}s\n")
    with RUN_LOG.open("a") as fh:
        fh.write(f"{time.time():.3f} leave {job.id}\n")


def _wait(predicate, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    sys.exit(f"FAIL: timed out after {timeout}s waiting for {what}")


def _check(condition: bool, message: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        sys.exit(1)


def serial() -> None:
    """Acceptance: submit three jobs at once; they run one at a time, in order."""
    RUN_LOG.unlink(missing_ok=True)
    jobs.start()
    ids = [jobs.submit("sleepy", params={"seconds": 1}) for _ in range(3)]
    print(f"submitted {len(ids)} jobs")

    _wait(
        lambda: all(jobs.get(i).status in (jobs.DONE, jobs.FAILED) for i in ids),
        timeout=30,
        what="all three jobs to finish",
    )
    jobs.stop()

    _check(all(jobs.get(i).status == jobs.DONE for i in ids), "all three finished `done`")

    events = [line.split() for line in RUN_LOG.read_text().splitlines()]
    order = [e[2] for e in events if e[1] == "enter"]
    _check(order == ids, f"ran in submission order ({len(order)} of {len(ids)} started)")

    # One worker means the log must read enter/leave/enter/leave..., never two enters
    # in a row. That is the whole of "concurrency 1" as a user can observe it.
    depth, overlapped = 0, False
    for _, kind, _job in events:
        depth += 1 if kind == "enter" else -1
        overlapped |= depth > 1
    _check(not overlapped, "never more than one job running at a time")

    for i in ids:
        _check(jobs.get(i).path("output.txt").exists(), f"{i} produced its output")


def crash() -> None:
    """Submit one long job and two short ones, then die uncleanly while the first runs."""
    RUN_LOG.unlink(missing_ok=True)
    jobs.start()
    long_id = jobs.submit("sleepy", params={"seconds": 600})
    queued = [jobs.submit("sleepy", params={"seconds": 1}) for _ in range(2)]
    (config.STATE_DIR / "crash-ids.json").write_text(
        json.dumps({"running": long_id, "queued": queued})
    )

    _wait(lambda: jobs.get(long_id).status == jobs.RUNNING, timeout=30, what="the first job to start")
    print(f"job {long_id} is running; SIGKILLing pid {os.getpid()}")
    sys.stdout.flush()
    # SIGKILL, not sys.exit: the point is that no shutdown hook, no `finally`, and no
    # atexit gets to run. Whatever the next start() finds on disk is all it has.
    os.kill(os.getpid(), signal.SIGKILL)


def recover() -> None:
    """Acceptance: the killed job reads failed/interrupted; the queued ones still run."""
    ids = json.loads((config.STATE_DIR / "crash-ids.json").read_text())

    before = jobs.get(ids["running"])
    _check(before.status == jobs.RUNNING, "the killed job was left `running` on disk")

    jobs.start()

    killed = jobs.get(ids["running"])
    _check(killed.status == jobs.FAILED, "killed job is now `failed`")
    _check(killed.error == "interrupted", f"...with error `interrupted` (got {killed.error!r})")

    _wait(
        lambda: all(jobs.get(i).status == jobs.DONE for i in ids["queued"]),
        timeout=30,
        what="the queued jobs to run after the restart",
    )
    jobs.stop()
    _check(True, "the two queued jobs ran after the restart")


if __name__ == "__main__":
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    {"serial": serial, "crash": crash, "recover": recover}[sys.argv[1]]()
    print("PASS")
