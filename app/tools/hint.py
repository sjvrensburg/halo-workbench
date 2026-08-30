"""The handwriting hint tool: a second opinion on one squiggle, while you look at it.

PLAN.md phase 7. The operator is at a document camera over a student's script, hits a
word they cannot read, drags a box around it and asks. It is **not** a transcription
service, not a marking aid and not a batch pipeline, and that distinction settles most of
the design rather than being a caveat on it.

**This tool's verifier is the human, per request, by construction.** Every other tool here
ships a machine check because its user is not looking at the source; here the operator is
looking at the same ink at the same moment, which is a stronger gate than anything
automatic in this repo, not a weaker one. What the page must not do is *invite trust* — so
it shows a distribution of readings and never a winner, it says when members disagree
rather than reconciling them, and it says when a member answered with nothing rather than
letting silence read as "there was nothing there".

Three ways this differs from the other four tools, each deliberate:

* **Not through the job queue.** The queue exists so a 20-minute conversion survives a
  reload; a hint that takes 20 minutes is a failed hint. Requests here are interactive and
  serialised among themselves by `_GPU` — one lock, because the GPU is one GPU. A hint can
  still queue behind a marker job; the page says so instead of pretending otherwise.
* **Nothing is written to disk.** Captures are student scripts and carry names. The image
  lives in this process for the life of the request and in the browser tab until it is
  closed. No job directory, no 7-day retention, and nothing here logs a reading.
* **Members are asked one at a time, by the operator.** olmOCR-2 answers first because it
  is fast and free of the others' failure modes; a second voice is a second press, which
  is also what keeps ~30 GiB of models off a box with no swap (PLAN.md 7e).
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import config, handwriting, model_lease
from app.views import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/hint")

# One read at a time. Not the job queue (see the module docstring) but the same reason:
# every member contends for the one GPU, and two browser tabs racing would only make both
# slower. The wait is generous because a legitimate queue here is one other read.
_GPU = threading.Lock()
_LOCK_WAIT_SECONDS = 180

MEMBERS = {member["id"]: member for member in config.HINT_MEMBERS}
MAX_SAMPLES = 8


def _member_status(member: dict) -> dict:
    """What the page needs to offer a member as a button, without starting anything."""
    return {
        "id": member["id"],
        "name": member["name"],
        "unit": member["unit"],
        "endpoint": member["endpoint"],
        "approx_gib": member["approx_gib"],
        "primary": member["primary"],
        "note": member["note"],
        "installed": model_lease.unit_exists(member["unit"]),
        "active": model_lease.is_active(member["unit"]),
    }


@router.get("", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "hint_index.html",
        {"page": "hint",
         "members": [_member_status(member) for member in config.HINT_MEMBERS],
         "samples": config.HINT_SAMPLES,
         "temperature": config.HINT_TEMPERATURE,
         "min_long_edge": config.HINT_MIN_LONG_EDGE_PX,
         # Written down rather than derived: see config.HINT_HTTPS_ORIGINS for the two
         # ways deriving them from the current URL produces a link that cannot work.
         "https_origins": config.HINT_HTTPS_ORIGINS},
        headers={"cache-control": "no-store"})


@router.get("/members")
def members_status():
    """Polled by the page so a model started elsewhere shows as available immediately."""
    return JSONResponse(
        {"members": [_member_status(member) for member in config.HINT_MEMBERS],
         "available_gib": round(model_lease.available_gib(), 1)},
        headers={"cache-control": "no-store"})


@router.post("/read")
def read(payload: dict = Body(...)):
    """Ask one member to read one crop, `samples` times. Returns its distinct readings.

    One member per request on purpose: the operator sees olmOCR-2's answer while deciding
    whether the ink is ambiguous enough to be worth a second model's ~20 GiB, and the page
    stays responsive without any streaming machinery.
    """
    member = MEMBERS.get(payload.get("member") or "")
    if member is None:
        return JSONResponse({"error": "unknown model"}, status_code=400)

    mode = payload.get("mode") or "crop"
    if mode not in handwriting.PROMPTS:
        return JSONResponse({"error": f"unknown mode {mode!r}"}, status_code=400)

    # Three samples from the primary answers "how sure is this model". A second voice is
    # asked a different question -- "is the ink ambiguous" -- which one answer settles, and
    # three of Muse's 8-12 s samples would put the page past the operator's patience on
    # their own (measured 2026-08-30).
    default_samples = config.HINT_SAMPLES if member["primary"] else config.HINT_SECOND_VOICE_SAMPLES
    samples = max(1, min(MAX_SAMPLES, int(payload.get("samples") or default_samples)))
    # One sample is a greedy read -- the condition every halo-prep measurement behind these
    # models was taken under. Temperature only enters when spread is what was asked for.
    temperature = (0.0 if samples == 1
                   else float(payload.get("temperature") or config.HINT_TEMPERATURE))

    try:
        raw, mime = handwriting.decode_data_url(payload.get("image") or "")
    except handwriting.HintError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    size = handwriting.image_size(raw)
    # The capture the crop came out of, as the browser measured it. The crop itself is one
    # word and is *supposed* to be small; it is the CAPTURE that must not be 640x480.
    capture = payload.get("capture_px") or {}
    capture_long_edge = max(int(capture.get("width") or 0), int(capture.get("height") or 0))

    if not _GPU.acquire(timeout=_LOCK_WAIT_SECONDS):
        return JSONResponse(
            {"error": "another read is still running. Everything here shares one GPU."},
            status_code=503)
    try:
        try:
            waited = model_lease.ensure_running(
                member["endpoint"],
                needs_gib=member["approx_gib"] + config.HINT_MEMORY_HEADROOM_GIB)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc), "member": member["id"]},
                                status_code=503)

        results = []
        for index in range(samples):
            try:
                results.append(handwriting.read_once(
                    member["endpoint"], raw, mime, mode=mode, temperature=temperature,
                    max_tokens=member["max_tokens"][mode]))
            except handwriting.HintError as exc:
                # A member that dies mid-sample still has something to show: the samples
                # it did answer are a real reading, and hiding them to report one error
                # would be the same mistake as hiding a silent answer.
                return JSONResponse(
                    {"error": str(exc), "member": member["id"],
                     "readings": handwriting.group(results), "samples": results,
                     "partial": True}, status_code=502)
        model_lease.touch(member["endpoint"])
    finally:
        _GPU.release()

    silent = [sample for sample in results if sample["silent"]]
    # Sizes and timings only. Never a reading -- these are student scripts, and the
    # journal is not where a name should turn up (CLAUDE.md "Privacy").
    log.info("hint: %s x%d, %s, %d bytes, %.1fs",
             member["id"], samples, mode, len(raw),
             sum(sample["elapsed"] for sample in results))

    return JSONResponse({
        "member": member["id"],
        "name": member["name"],
        "mode": mode,
        "temperature": temperature,
        "started_wait": round(waited, 1),
        "readings": handwriting.group(results),
        "samples": results,
        "silent_count": len(silent),
        # Reported rather than asserted: an empty answer from Chandra is a known
        # behaviour, and in an attended UI it is information, not a failure.
        "silent_note": (
            f"{len(silent)} of {samples} answers came back empty — the model spent its "
            f"tokens reasoning and wrote nothing. halo-prep measured this on Chandra-OCR-2; "
            f"press again, or ask another model." if silent else None),
        "crop_px": {"width": size[0], "height": size[1]} if size else None,
        "capture_px": {"width": capture.get("width"), "height": capture.get("height")}
                      if capture_long_edge else None,
        "low_resolution": bool(capture_long_edge)
                          and capture_long_edge < config.HINT_MIN_LONG_EDGE_PX,
        "min_long_edge": config.HINT_MIN_LONG_EDGE_PX,
    }, headers={"cache-control": "no-store"})


@router.post("/agreement")
def agreement(payload: dict = Body(...)):
    """Say, in words, whether the members that answered agree. No image, no model.

    Separate from `/read` because the page collects members one press at a time and the
    sentence has to be recomputed each time one arrives. It never picks a winner: see
    app/handwriting.agreement and PLAN.md 7d, "Do not average, vote, or pick a winner".
    """
    members = payload.get("members")
    if not isinstance(members, list):
        return JSONResponse({"error": "expected a list of members"}, status_code=400)
    return JSONResponse(handwriting.agreement(members),
                        headers={"cache-control": "no-store"})
