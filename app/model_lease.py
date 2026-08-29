"""Start a model server when a job needs it, and stop it again when nobody does.

**This reverses a documented decision, on the operator's instruction 2026-08-29.** The
app used to start nothing: a stopped model produced a plain "it is not running, here is
the command" message. That was honest and it was also friction — `llama-hymt2` exists
solely to serve this app's translate page, so "someone pressed Translate" is exactly the
signal to start it, and nothing else on the box wants it running the rest of the time.

Only units listed in `config.MANAGED_UNITS` are touched, which is one unit: the
translation model this repo owns. `:8088` is Ornith-35B, Open Notebook's resident worker
— this app must never start or stop that, and does not.

**Only what we started gets stopped.** If the unit was already active when a job arrived,
the operator (or another consumer) is using it and it is left alone forever. That single
rule replaces the refcounting in halo-prep's `vibevoice-lease.sh`, which needs it because
several consumers overlap there; here the job queue is one deep by design, so the only
concurrency that matters is *someone else outside this app*.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request

from app import config

log = logging.getLogger(__name__)

# How long to wait for a cold model server to answer /health. Measured on this box, the
# 7B Q6_K at --load-mode dio is up in well under a minute; the ceiling is generous
# because a first load from cold page cache is slower than a repeat.
START_TIMEOUT_SECONDS = 180
POLL_SECONDS = 1.0

_LOCK = threading.Lock()
_STARTED_BY_US: set[str] = set()
_LAST_USED: dict[str, float] = {}
_STOP = threading.Event()
_IDLE_THREAD: threading.Thread | None = None


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, check=False)


def is_active(unit: str) -> bool:
    return _systemctl("is-active", unit).stdout.strip() == "active"


def _healthy(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/health", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_running(endpoint: str, on_wait=None) -> float:
    """Make sure the model behind `endpoint` is answering. Returns seconds waited.

    `on_wait(unit)` is called once if a start is actually needed, so the job page can say
    "starting the model" rather than looking wedged for half a minute.
    """
    unit = config.MANAGED_UNITS.get(endpoint)
    if unit is None:
        return 0.0  # not ours to manage -- :8088 belongs to Open Notebook

    with _LOCK:
        _LAST_USED[unit] = time.time()

        if _healthy(endpoint):
            return 0.0

        already_active = is_active(unit)
        if on_wait:
            on_wait(unit)

        if not already_active:
            log.info("starting %s for %s", unit, endpoint)
            result = _systemctl("start", unit)
            if result.returncode != 0:
                raise RuntimeError(
                    f"could not start {unit}: {result.stderr.strip() or 'systemctl failed'}"
                )
            _STARTED_BY_US.add(unit)

        started = time.time()
        while time.time() - started < START_TIMEOUT_SECONDS:
            if _healthy(endpoint):
                waited = time.time() - started
                log.info("%s answered after %.1fs", unit, waited)
                _LAST_USED[unit] = time.time()
                return waited
            time.sleep(POLL_SECONDS)

        raise RuntimeError(
            f"{unit} did not answer on {endpoint} within {START_TIMEOUT_SECONDS}s — "
            f"`journalctl --user -u {unit} -n 30`"
        )


def touch(endpoint: str) -> None:
    """Mark the model as in use, so the idle timer measures idleness and not runtime."""
    unit = config.MANAGED_UNITS.get(endpoint)
    if unit:
        _LAST_USED[unit] = time.time()


def release_idle(now: float | None = None) -> list[str]:
    """Stop units we started that nothing has used lately. Returns what was stopped."""
    now = now or time.time()
    stopped = []
    with _LOCK:
        for unit in sorted(_STARTED_BY_US):
            if now - _LAST_USED.get(unit, 0) < config.MODEL_IDLE_SECONDS:
                continue
            log.info("stopping %s after %d s idle", unit, config.MODEL_IDLE_SECONDS)
            _systemctl("stop", unit)
            _STARTED_BY_US.discard(unit)
            _LAST_USED.pop(unit, None)
            stopped.append(unit)
    return stopped


def _idle_loop() -> None:
    while not _STOP.is_set():
        # Checked often, acted on rarely: the wait is short so shutdown is prompt, and
        # release_idle does nothing until the model has actually been idle long enough.
        if _STOP.wait(30):
            return
        try:
            release_idle()
        except Exception:  # a failed stop must never take the service down
            log.exception("idle release failed")


def start() -> None:
    global _IDLE_THREAD
    _STOP.clear()
    _IDLE_THREAD = threading.Thread(target=_idle_loop, name="model-lease", daemon=True)
    _IDLE_THREAD.start()


def stop() -> None:
    """Shut down the idle thread and hand back anything we started.

    Releasing on shutdown is deliberate: a restart of this app should not leave ~6 GiB of
    GTT held by a model whose only consumer just went away.
    """
    _STOP.set()
    if _IDLE_THREAD is not None:
        _IDLE_THREAD.join(timeout=5)
    with _LOCK:
        for unit in sorted(_STARTED_BY_US):
            log.info("stopping %s on shutdown", unit)
            _systemctl("stop", unit)
        _STARTED_BY_US.clear()
