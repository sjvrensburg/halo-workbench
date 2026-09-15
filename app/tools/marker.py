"""PDF → markdown, by way of halo-prep's marker wrapper, with its sidecar as the verifier.

The highest-value tool here and the lowest-risk, because marker is not an LLM: its
output is deterministic and its failure mode is a non-zero exit rather than plausible
slop.

This module never imports marker and never calls `marker_single`. It shells out to
`marker_run.sh` (config.MARKER_RUN), which is ~150 lines of scar tissue from two
substitution incidents in two days -- read its header before considering a shortcut.
It sets the two mandatory env vars, runs marker in its own process group and kills
the *group* on exit (surya leaves ~2.9 GB of model servers behind otherwise), checks
the output has markdown structure, and writes the checksummed `.marker-ok` sidecar.

**The sidecar is the verifier.** A download is served only when the sidecar exists and
its `markdown_sha256` matches the bytes on disk. That is what makes it impossible to
hand a user `pdftotext` output, or an LLM's own transcription, wearing marker's
filename -- the failure that the wrapper exists to prevent, repeated here because a
download route is exactly where the substitution would surface.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app import config, jobs
from app.views import templates

log = logging.getLogger(__name__)
router = APIRouter()

# app/tools/marker.py -> app/tools -> app -> repo root. Only used to put this repo on
# PYTHONPATH for marker_single's own subprocess -- see `convert()`.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Read whole into memory (jobs.submit takes bytes), so it is capped. 200 MB is far
# above any paper and still small next to the headroom this box needs: no swap, and
# marker is about to want ~2.9 GB for its own model servers (halo-prep pitfall #17).
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Marker is allowed to be slow -- ~20 minutes on a real paper is normal and an agent
# has already once mistaken that for a stall (halo-prep, 2026-08-23). This is only a
# backstop so a wedged conversion cannot hold the single worker forever; 3x the
# longest observed run.
MARKER_TIMEOUT_SECONDS = 60 * 60

# marker_run.sh's exit codes, from its header: 1 = marker itself failed or produced
# nothing usable, 2 = the box is misconfigured (no marker, no llama-server, no file),
# 3 = output has no markdown structure at all, i.e. it looks like plain pdftotext.
_EXIT_MESSAGES = {
    1: "marker failed to convert this PDF",
    2: "marker is not installed or configured correctly on this box",
    3: "marker produced suspect output: no headings, tables, display math or images, "
    "which is what plain text extraction looks like rather than marker output",
}

STDERR_LOG = "marker.stderr.log"

# ── advanced options ─────────────────────────────────────────────────────────
# marker_single has dozens of flags (`marker_single --help`); these three are the
# ones actually worth a per-document choice from the upload form. Everything else
# stays at marker_run.sh's defaults.
MODES = ("fast", "balanced")
_PAGE_RANGE_RE = re.compile(r"^\d+(-\d+)?(,\s*\d+(-\d+)?)*$")


def _extra_args(params: dict) -> list[str]:
    """marker_single flags for this job's advanced options, already validated at
    upload time (`_validate_options`) -- this only ever emits flags it knows about,
    never a user string used as a flag name, so there is no way to smuggle an
    arbitrary marker_single option through the form.
    """
    args: list[str] = []
    if params.get("mode") == "balanced":
        # marker_run.sh hardcodes `--mode fast` ahead of anything we pass; a later
        # --mode on the command line wins (ordinary click behaviour, verified against
        # this marker install 2026-09-15), and marker_run.sh's own sidecar now records
        # whichever one actually ran rather than a hardcoded "fast" (see its header).
        args += ["--mode", "balanced"]
    if params.get("page_range"):
        args += ["--page_range", params["page_range"]]
    if params.get("force_ocr"):
        args.append("--force_ocr")
    if params.get("llm_enhance"):
        # Ornith-35B on :8088 (config.LLM_ENHANCE_ENDPOINT), OpenAI-compatible and
        # already always running -- see config.py's comment for why this needs no
        # on-demand start/stop. app.marker_services.OrnithService, not marker's stock
        # OpenAIService, because Ornith performs best as a VLM with thinking off, and
        # only honours that via chat_template_kwargs={"enable_thinking": false} on
        # every request -- marker's own service has no way to send that, hence the
        # subclass (see app/marker_services.py). Loaded by `marker_single` itself, in
        # a different venv from this app's -- `convert()` puts this repo on that
        # subprocess's PYTHONPATH so the import resolves.
        # --openai_api_key is required by marker's OpenAIService (it builds an
        # `openai` SDK client that refuses a None key) but llama-server does not check
        # it -- any non-empty string satisfies both.
        args += [
            "--use_llm",
            "--llm_service", "app.marker_services.OrnithService",
            "--openai_base_url", f"{config.LLM_ENHANCE_ENDPOINT}/v1",
            "--openai_model", config.LLM_ENHANCE_MODEL,
            "--openai_api_key", "none",
        ]
    return args


def _llm_enhance_reachable() -> bool:
    """A quick, short-timeout check -- not a guarantee, just an instant "no" instead
    of a 20-minute job failing on the last mile because :8088 happened to be down.
    marker itself still re-discovers any failure that happens after this point.
    """
    try:
        urllib.request.urlopen(f"{config.LLM_ENHANCE_ENDPOINT}/health", timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


def _validate_options(mode: str, page_range: str, force_ocr: bool, llm_enhance: bool) -> dict:
    """Raises HTTPException(400) with a message the user can act on. Never trusts the
    mode string as anything but a fixed choice, and never forwards page_range to a
    subprocess without checking its shape first -- marker_single's own parser would
    reject a bad one deep inside a 20-minute job instead of on the upload form.
    """
    if mode not in MODES:
        raise HTTPException(400, f"mode must be one of {', '.join(MODES)}")
    page_range = page_range.strip()
    if page_range and not _PAGE_RANGE_RE.fullmatch(page_range):
        raise HTTPException(
            400,
            "page range must look like '0,5-10,20' -- comma-separated page numbers "
            "or ranges, 0-indexed",
        )
    if llm_enhance and not _llm_enhance_reachable():
        raise HTTPException(
            400,
            "LLM enhancement is unreachable right now — Ornith-35B "
            f"({config.LLM_ENHANCE_ENDPOINT}) isn't answering. It's Open Notebook's "
            "own resident worker and this app cannot start it; try again once it's up, "
            "or convert without LLM enhancement.",
        )
    return {
        "mode": mode,
        "page_range": page_range,
        "force_ocr": force_ocr,
        "llm_enhance": llm_enhance,
    }


def _scope_unit(job_id: str) -> str:
    return f"workbench-marker-{job_id}.scope"


def _reap_scope(unit: str) -> None:
    """Kill anything still alive in the job's cgroup.

    MEASURED 2026-08-29, on this box, this marker: after a successful 12-page
    conversion, `surya.fast_layout.server` (1.5 GB) and `surya.ocr_error.server`
    (1.0 GB) were still resident 30 s later, reparented to pid 1. marker_run.sh does
    try to prevent exactly this -- it runs marker in its own process group and kills
    the GROUP on exit -- but these two put themselves in a process group of their own
    (PGID == their own PID), so `kill -- -PGID` never reaches them. 2.6 GB stranded on
    a box with no swap that OOM-kills under pressure (halo-prep pitfall #17).

    The wrapper now opens a cgroup scope of its own for the same reason (halo-prep,
    2026-08-29, pitfall #37), and this app tells it not to -- see MARKER_RUN_SCOPE
    below. One scope, opened by the outer caller, because only the outer caller can
    still reap it after the *service* is killed mid-job. A cgroup is the one grouping
    a child cannot leave without privileges.
    """
    subprocess.run(
        ["systemctl", "--user", "stop", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,  # already gone is the normal case, and is a success
    )


# ── the verifier ─────────────────────────────────────────────────────────────


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(markdown: Path) -> None:
    """Raise unless `markdown` is exactly what marker signed.

    Cheap (one sha256 of a text file), so it runs both when the job finishes and again
    on every download -- the second one is not redundant: the acceptance test corrupts
    the `.md` after a successful conversion, and the only thing standing between that
    and a user is this check at serve time.
    """
    sidecar = markdown.with_suffix(".marker-ok")
    if not sidecar.is_file():
        raise jobs.JobError(
            "no .marker-ok provenance sidecar beside the output — only marker_run.sh "
            "writes one, so this markdown did not come from marker"
        )

    fields = dict(
        line.split("=", 1)
        for line in sidecar.read_text().splitlines()
        if "=" in line
    )
    expected = fields.get("markdown_sha256")
    if not expected:
        raise jobs.JobError("the .marker-ok sidecar has no markdown_sha256 field")

    actual = _sha256(markdown)
    if actual != expected:
        raise jobs.JobError(
            f"checksum mismatch: the markdown on disk ({actual[:12]}…) is not the file "
            f"marker signed ({expected[:12]}…) — it has been modified or replaced"
        )


def reap_scopes_for(job_ids: list[str]) -> None:
    """Stop the scopes of jobs a previous process left mid-flight.

    A job that was `running` when the service was killed is reaped as `interrupted`
    (jobs._recover), but that only fixes the bookkeeping -- the surya servers it
    started are in a cgroup that outlived the service, and nothing else will ever
    stop them. Same 2.6 GB, same box with no swap.

    Takes the ids rather than matching `workbench-marker-*.scope`, and MEASURED
    2026-08-29 that distinction matters: a wildcard reaper run at startup killed the
    live conversion of an instance that was already running (uvicorn fires the
    lifespan before it binds the port, so even a second instance that goes on to fail
    with "address already in use" gets to run this first). Only jobs this state
    directory owns, and only ones recovery just declared nobody is running.
    """
    for job_id in job_ids:
        unit = _scope_unit(job_id)
        log.warning("stopping marker scope %s left by an interrupted job", unit)
        _reap_scope(unit)


# ── the job handler ──────────────────────────────────────────────────────────


def _stderr_tail(job: jobs.Job, lines: int = 40) -> str:
    log_path = job.path(STDERR_LOG)
    if not log_path.is_file():
        return ""
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-lines:])


@jobs.register("marker")
def convert(job: jobs.Job) -> None:
    pdf = job.input_path
    if pdf is None or not pdf.is_file():
        raise jobs.JobError("no uploaded file found in the job directory")

    # Checked here rather than at upload so it reads as a failed job with a reason,
    # the same as every other failure, instead of a form error the user has to
    # re-read. It also costs nothing next to spawning marker on a non-PDF.
    if pdf.open("rb").read(5) != b"%PDF-":
        raise jobs.JobError(f"{pdf.name} is not a PDF (it does not start with %PDF-)")

    outdir = job.path("out")
    outdir.mkdir(exist_ok=True)

    command = [str(config.MARKER_RUN), str(pdf), str(outdir), *_extra_args(job.params)]
    unit = _scope_unit(job.id)
    # MARKER_RUN_SCOPE=0 tells marker_run.sh not to open a scope of its own. Nesting
    # would put the surya servers in the inner cgroup, which our startup reaper cannot
    # see -- so a `kill -9` of this service would strand exactly what all of this is
    # here to prevent. The scope has to belong to whoever outlives the job.
    #
    # PYTHONPATH: only needed when llm_enhance passes --llm_service
    # app.marker_services.OrnithService (see _extra_args), but harmless to set always
    # -- marker_single runs under ~/venvs/marker, a different interpreter from this
    # app's, and would not otherwise find this repo's `app` package on sys.path.
    env = {
        **os.environ,
        "MARKER_RUN_SCOPE": "0",
        "PYTHONPATH": str(_REPO_ROOT),
    }

    if shutil.which("systemd-run"):
        # A transient scope so every descendant lands in one cgroup we can empty
        # afterwards -- see _reap_scope. --collect so a failed scope does not linger
        # in systemd's unit list needing a manual reset-failed.
        command = ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                   f"--unit={unit}", *command]
    else:
        # Then let the wrapper open its own after all: its cleanup is better than none.
        env.pop("MARKER_RUN_SCOPE")
        # No user systemd (not this box; the authoring laptop, where none of this
        # works anyway). Run it plainly rather than refusing to run at all.
        log.warning("systemd-run not found — marker will run without a cgroup to reap")

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=MARKER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # SIGTERM, not kill: marker_run.sh traps INT/TERM and tears down the surya
        # model servers it spawned. SIGKILLing the wrapper is precisely how ~2.9 GB
        # of them were once left resident on a box with no swap.
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        _reap_scope(unit)
        job.path(STDERR_LOG).write_text(stderr)
        raise jobs.JobError(
            f"marker exceeded {MARKER_TIMEOUT_SECONDS // 60} minutes and was stopped"
        )

    # On every exit path, including success: the leak measured here happened after a
    # conversion that SUCCEEDED.
    _reap_scope(unit)
    job.path(STDERR_LOG).write_text(stderr)
    log.info("job %s: marker_run.sh exited %d", job.id, proc.returncode)

    if proc.returncode != 0:
        # Exit 3 is deliberately not folded into "it failed": it means marker ran and
        # produced *something*, and the user should be told the output is suspect
        # rather than shown a generic failure.
        raise jobs.JobError(
            _EXIT_MESSAGES.get(proc.returncode, f"marker_run.sh exited {proc.returncode}")
        )

    # The wrapper prints exactly one line on success: the absolute path of the .md.
    # Do not go looking for the file ourselves -- assuming marker's output layout is
    # its own documented failure (halo-prep, 2026-08-22: an agent checked the wrong
    # path, concluded conversion had failed, and passed an empty document downstream).
    markdown = Path(stdout.strip().splitlines()[-1]) if stdout.strip() else None
    if markdown is None or not markdown.is_file():
        raise jobs.JobError("marker_run.sh exited 0 but named no markdown file")

    verify(markdown)

    # Images marker extracted, which it writes beside the .md.
    images = sorted(
        p.name
        for p in markdown.parent.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    )
    jobs.record(
        job.id,
        # Relative to the job directory: absolute paths in meta.json survive a rename
        # of the state directory as pointers to somebody else's file.
        markdown_rel=str(markdown.relative_to(job.dir)),
        markdown_name=markdown.name,
        markdown_bytes=markdown.stat().st_size,
        images=images,
    )


# ── routes ───────────────────────────────────────────────────────────────────


def _job_or_404(job_id: str) -> jobs.Job:
    job = jobs.get(job_id)
    if job is None or job.tool != "marker":
        raise HTTPException(404, "no such marker job")
    return job


@router.get("/marker", response_class=HTMLResponse)
def index(request: Request):
    recent = [j for j in jobs.all_jobs() if j.tool == "marker"][:20]
    return templates.TemplateResponse(
        request,
        "marker_index.html",
        {"jobs": recent, "queued": jobs.queue_depth()},
        headers={"cache-control": "no-store"},  # the recent-jobs list goes stale too
    )


@router.post("/marker")
async def upload(
    file: UploadFile,
    mode: str = Form("fast"),
    page_range: str = Form(""),
    force_ocr: bool = Form(False),
    llm_enhance: bool = Form(False),
):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    params = _validate_options(mode, page_range, force_ocr, llm_enhance)
    job_id = jobs.submit("marker", params=params, upload=(file.filename or "upload.pdf", data))
    # 303: a reload of the status page must not repost the upload.
    return RedirectResponse(f"/marker/{job_id}", status_code=303)


@router.get("/marker/{job_id}", response_class=HTMLResponse)
def status(request: Request, job_id: str):
    job = _job_or_404(job_id)
    return templates.TemplateResponse(
        request,
        "marker_job.html",
        {"job": job, "stderr": _stderr_tail(job) if job.status == jobs.FAILED else ""},
        # OBSERVED 2026-08-29 from an iPad: the page kept reading "running, 0s" for
        # minutes after the job finished, while the journal showed Safari fetching it
        # and getting 200. A page whose whole job is to change under a meta refresh
        # must say so, or a mobile browser will serve its own copy back to the user and
        # the tool looks hung -- which is the one failure mode this page exists to
        # prevent.
        headers={"cache-control": "no-store"},
    )


def _verified_markdown(job: jobs.Job) -> Path:
    """The output path, re-verified. Raises HTTPException 409 if it no longer matches."""
    rel = job.result.get("markdown_rel")
    if job.status != jobs.DONE or not rel:
        raise HTTPException(409, "this job has no verified output to download")

    markdown = job.dir / rel
    try:
        verify(markdown)
    except jobs.JobError as exc:
        # 409 rather than 500: nothing is broken, the file is simply no longer the one
        # marker signed, and the user must not be handed it.
        raise HTTPException(409, f"refusing to serve this file — {exc}")
    return markdown


@router.get("/marker/{job_id}/download")
def download(job_id: str):
    job = _job_or_404(job_id)
    markdown = _verified_markdown(job)
    return FileResponse(markdown, media_type="text/markdown", filename=markdown.name)


@router.get("/marker/{job_id}/download.zip")
def download_zip(job_id: str):
    job = _job_or_404(job_id)
    markdown = _verified_markdown(job)

    # Built in memory, not cached in the job directory: it is a few hundred KB, and a
    # cached copy is a second artefact that the sidecar does not cover.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(markdown, markdown.name)
        # The sidecar travels with it, so provenance survives leaving this box.
        sidecar = markdown.with_suffix(".marker-ok")
        if sidecar.is_file():
            zf.write(sidecar, sidecar.name)
        for name in job.result.get("images", []):
            image = markdown.parent / name
            if image.is_file():
                zf.write(image, name)

    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="{markdown.stem}.zip"'},
    )
