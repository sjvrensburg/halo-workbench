"""Where hint captures live now that they're retained (7g, operator 2026-09-02).

Phase 7 built the hint tool to touch nothing on disk, on the reasoning that a capture
is a student's script, not the operator's own document. The operator has since asked
for 7-day retention, matching the other tools' job store -- so this module exists to
do that, as narrowly as asked: only the CROP actually sent to the model is kept, never
the full capture frame it came from, and never the samples' token/logprob detail
(app/handwriting.token_confidences() output) -- just the grouped reading(s), the same
thing already shown on the page.

Directories, not rows, same as app/jobs.py: `~/.local/state/halo-workbench/hints/<id>/`
holding the crop and a meta.json. But this is NOT a job -- there is no queue, no
worker thread, and nothing here is ever read back by the app; `save()` is called once,
after a `/hint/read` response has already been decided, purely to persist it. Pruning
runs on its own thread and its own interval, deliberately not sharing app/jobs.py's
pruner: the two stores have different retention knobs (config.HINT_RETENTION_DAYS vs.
JOB_RETENTION_DAYS) and no lifecycle to keep in sync.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import threading
from datetime import datetime, timedelta, timezone

from app import config

log = logging.getLogger(__name__)

# Same cadence as app/jobs.py's pruner, for the same reason: pruning only at startup
# would mean a service that stays up for weeks never notices anything crossed the
# window, and these are a student's captures.
PRUNE_INTERVAL_SECONDS = 3600

_STOP = threading.Event()
_PRUNER: threading.Thread | None = None


def _new_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def save(*, member: str, mode: str, mime: str, image: bytes,
          readings: list[dict], capture_px: dict | None) -> str:
    """Write one crop and its reading(s). Returns the new entry's id.

    `readings` is trimmed to `text`/`count`/`truncated` -- the reading as shown, not
    the samples, tokens or alternates behind it. Those describe how confident the
    model sounded while generating, not what happened to be true of the ink; keeping
    them would retain more about the model's internals than the operator asked for.
    """
    entry_id = _new_id()
    entry_dir = config.HINTS_DIR / entry_id
    entry_dir.mkdir(parents=True, exist_ok=True)
    ext = mime.split("/")[-1] or "bin"  # handwriting.decode_data_url only ever hands
                                         # back png/jpeg/webp -- see its docstring
    (entry_dir / f"crop.{ext}").write_bytes(image)
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "member": member,
        "mode": mode,
        "capture_px": capture_px,
        "readings": [{"text": r.get("text"), "count": r.get("count"),
                      "truncated": r.get("truncated", False)} for r in readings],
    }
    (entry_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return entry_id


def prune(now: datetime | None = None) -> int:
    """Delete capture directories past the retention window. Returns how many went.

    mtime is the fallback for a directory whose meta.json is missing or corrupt --
    exactly the debris that most deserves pruning, so it must not be skipped (same
    reasoning as app/jobs.py.prune()).
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=config.HINT_RETENTION_DAYS)
    removed = 0

    if not config.HINTS_DIR.is_dir():
        return 0

    for entry in config.HINTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        age_ref = None
        meta_path = entry / "meta.json"
        if meta_path.exists():
            try:
                created = json.loads(meta_path.read_text()).get("created_at")
                if created:
                    age_ref = datetime.fromisoformat(created)
            except (json.JSONDecodeError, ValueError, OSError):
                age_ref = None
        if age_ref is None:
            age_ref = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
        if age_ref < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            log.info("pruned hint capture %s (older than %d days)",
                     entry.name, config.HINT_RETENTION_DAYS)

    return removed


def _pruner_loop() -> None:
    while not _STOP.wait(PRUNE_INTERVAL_SECONDS):
        try:
            prune()
        except Exception:  # a failed prune must never take the service down with it
            log.exception("hint capture prune failed")


def start() -> None:
    """Prune once and start the background pruner. Called from the lifespan."""
    global _PRUNER
    config.HINTS_DIR.mkdir(parents=True, exist_ok=True)
    _STOP.clear()
    prune()
    _PRUNER = threading.Thread(target=_pruner_loop, name="hint-pruner", daemon=True)
    _PRUNER.start()


def stop() -> None:
    _STOP.set()
