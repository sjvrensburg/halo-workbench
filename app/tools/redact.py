"""Assisted redaction: find candidates, let the user confirm, remove, then prove it.

**This tool never produces a redacted document. It produces a redaction candidate.**
The verifier proves that everything *found* was removed; nothing can prove nothing was
missed, and recall is the property a redaction tool's user assumes it has. So every
page, button, filename and download here says "candidate — review before sending", and
that wording is a rule with a real failure behind it, not a disclaimer (PLAN.md phase
4, CLAUDE.md "Redaction output is a candidate").

Two jobs, because the human review step sits between them:

    POST /redact          -> a `redact-scan` job: extract text, run patterns, ask the
                             model for extra candidates, discard any that do not occur
                             verbatim in the source.
    POST /redact/<id>/apply -> a `redact-apply` job: remove exactly the spans the user
                             ticked, then re-extract the output and prove they are gone.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import urllib.error
import urllib.request

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import config, jobs, pdf_redaction, pii_tagger, redaction
from app.views import templates

log = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# What the model is asked to read. The server reports n_ctx 131072, but a redaction
# prompt that fills it would hold the GPU for minutes on a queue of one; documents
# longer than this are scanned by patterns over the whole text and by the model over
# the head, which is stated on the page rather than hidden.
MODEL_CHARS = 12000

# The model is a recall booster. A slow or stopped :8092 must therefore degrade to
# "patterns only, and we said so", never to a failed job -- the deterministic layer is
# the primary detector and is worth having on its own.
MODEL_TIMEOUT_SECONDS = 120

SUFFIX = ".redaction-candidate"


# ── the recall layer ─────────────────────────────────────────────────────────

# Kept as a fallback for a box where the tagger is not installed. Thinking is turned
# OFF explicitly: measured 2026-08-29 on :8092, this model with thinking on spent a
# 4000-token budget entirely inside `reasoning_content`, returned empty `content` after
# 138.9 s and proposed NOTHING; with `enable_thinking: false` it answered in 2.4 s with
# 41 tokens and named all four people and organisations in the test letter. That is a
# measured counter-example to halo-prep pitfall #25 ("turning thinking off makes it
# quietly wrong") for extraction tasks -- and it is safe here for a structural reason:
# every proposal is checked against the source verbatim, so a worse model proposes
# less, never something false.
_PROMPT = """You are helping redact a document. List the personal identifiers in it \
that a regular expression would miss: people's names, organisation names, job titles \
tied to a person, and oblique identifiers such as "my daughter's school".

Rules:
- Copy each item EXACTLY as it appears in the document, character for character.
- One item per line. No numbering, no commentary, no explanation.
- If there are none, output NONE.

Document:
---
{document}
---"""

MODEL_CHARS = 12000
MODEL_TIMEOUT_SECONDS = 120


def _ask_ornith(text: str) -> tuple[list[str], str]:
    """The fallback recall layer: :8092, thinking off. Returns (proposals, note)."""
    payload = json.dumps(
        {
            "messages": [{"role": "user", "content": _PROMPT.format(document=text[:MODEL_CHARS])}],
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0,
            "max_tokens": 800,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        f"{config.REGEX_ENDPOINT}/v1/chat/completions",
        data=payload,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return [], (
            f"no recall layer ran ({exc.__class__.__name__}) — these candidates are from "
            f"the pattern layer and your own list alone"
        )

    content = ((body.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    if not content or content.upper().startswith("NONE"):
        return [], "the fallback model proposed nothing beyond the pattern hits"
    return [line.strip(" -•\t") for line in content.splitlines() if line.strip()], (
        "openai/privacy-filter is not installed, so the fallback chat model on :8092 "
        "proposed these instead"
    )


def propose_spans(text: str) -> tuple[list[tuple[str, str]], str]:
    """Extra candidates as (text, category), plus a note naming which layer ran.

    The user is always told which layers contributed. A recall layer that silently did
    not run would let someone believe a document was examined more thoroughly than it
    was, and this is a tool whose whole risk is false confidence.
    """
    try:
        proposals, note = pii_tagger.propose(text)
        return proposals, note
    except pii_tagger.TaggerUnavailable as exc:
        log.warning("privacy-filter unavailable (%s) — falling back to :8092", exc)
    except Exception:
        log.exception("privacy-filter failed — falling back to :8092")

    proposals, note = _ask_ornith(text)
    return [(value, "model") for value in proposals], note


# ── job: scan ────────────────────────────────────────────────────────────────


def _document_text(job: jobs.Job) -> str:
    source = job.input_path
    if source is None or not source.is_file():
        raise jobs.JobError("no uploaded file found in the job directory")

    if source.suffix.lower() == ".pdf":
        usable, explanation = pdf_redaction.text_layer_report(source)
        if not usable:
            raise jobs.JobError(explanation)
        return pdf_redaction.extract_text(source)

    return source.read_text(errors="replace")


@jobs.register("redact-scan")
def scan(job: jobs.Job) -> None:
    text = _document_text(job)
    spans = redaction.find_pattern_spans(text)

    # The user's own literal list -- names, employers, case numbers. These are
    # deterministic too, and they are the only layer that knows what this particular
    # document is about.
    unmatched: list[str] = []
    for term in job.params.get("terms", []):
        if term and term in text:
            spans.append(
                redaction.Span(
                    text=term,
                    category="your list",
                    source="pattern",
                    count=text.count(term),
                    contexts=redaction.contexts_for(text, term),
                )
            )
        elif term:
            # Reported on the page, not just logged. The user asked for this string to
            # go and believes it will; silence here is the tool letting them think a
            # document is clean of a name it never contained in that spelling.
            unmatched.append(term)
            log.info("job %s: your-list term %r does not occur in the document", job.id, term)

    proposals, note = propose_spans(text)
    accepted, dropped = redaction.accept_proposals(text, proposals)

    # Do not offer the same string twice because two layers found it.
    known = {span.text for span in spans}
    spans.extend(span for span in accepted if span.text not in known)

    if dropped:
        # Visible in the job log rather than silent (PLAN.md acceptance).
        log.info("job %s: dropped %d model proposal(s) not present verbatim: %s",
                 job.id, len(dropped), ", ".join(repr(d) for d in dropped[:10]))

    jobs.record(
        job.id,
        kind="pdf" if (job.input_path.suffix.lower() == ".pdf") else "text",
        chars=len(text),
        truncated_for_model=len(text) > MODEL_CHARS,
        model_note=note,
        dropped_proposals=dropped,
        unmatched_terms=unmatched,
        spans=[
            {
                "text": span.text,
                "category": span.category,
                "source": span.source,
                "count": span.count,
                "contexts": list(span.contexts),
            }
            for span in spans
        ],
    )


# ── job: apply ───────────────────────────────────────────────────────────────


@jobs.register("redact-apply")
def apply(job: jobs.Job) -> None:
    spans: list[str] = job.params["spans"]
    if not spans:
        raise jobs.JobError("no spans were confirmed, so there is nothing to remove")

    source = job.input_path
    if source is None or not source.is_file():
        raise jobs.JobError("no source document found in the job directory")

    if source.suffix.lower() == ".pdf":
        output = job.path(f"{source.stem}{SUFFIX}.pdf")
        try:
            counts = pdf_redaction.redact_pdf(source, output, spans)
        except pdf_redaction.NoTextLayer as exc:
            raise jobs.JobError(str(exc)) from None
        except pdf_redaction.Unlocatable as exc:
            raise jobs.JobError(str(exc)) from None
        except pdf_redaction.VerificationFailed as exc:
            raise jobs.JobError(f"VERIFIER FAILED — {exc}") from None

        # THE verifier for this path: re-extract the produced file with both extractors
        # and assert nothing confirmed survives. Not the plan -- the output.
        survivors = redaction.survivors(pdf_redaction.extract_text(output), spans)
        survivors += [s for s in redaction.survivors(pdf_redaction.pdftotext(output), spans)
                      if s not in survivors]
        metadata_survivors = redaction.survivors(pdf_redaction.metadata_values(output), spans)
    else:
        text = source.read_text(errors="replace")
        redacted, counts = redaction.redact_text(text, spans)
        output = job.path(f"{source.stem}{SUFFIX}{source.suffix or '.md'}")
        output.write_text(redacted)
        survivors = redaction.survivors(output.read_text(), spans)
        metadata_survivors = []

    if survivors or metadata_survivors:
        # Delete the output. A file that failed the verifier must not exist to be
        # downloaded by a user who did not read the error.
        output.unlink(missing_ok=True)
        where = "the document" if survivors else "the file's metadata"
        raise jobs.JobError(
            f"VERIFIER FAILED — {len(survivors or metadata_survivors)} confirmed span(s) "
            f"still present in {where} of the output: "
            f"{', '.join(repr(s) for s in (survivors or metadata_survivors)[:5])}. "
            f"No file has been produced."
        )

    jobs.record(
        job.id,
        output_name=output.name,
        output_rel=str(output.relative_to(job.dir)),
        output_bytes=output.stat().st_size,
        removed=counts,
        removed_total=sum(counts.values()),
        verified_with=["pymupdf", "pdftotext"] if source.suffix.lower() == ".pdf" else ["text"],
    )


# ── routes ───────────────────────────────────────────────────────────────────


def _job_or_404(job_id: str, tools: tuple[str, ...]) -> jobs.Job:
    job = jobs.get(job_id)
    if job is None or job.tool not in tools:
        raise HTTPException(404, "no such redaction job")
    return job


@router.get("/redact", response_class=HTMLResponse)
def index(request: Request):
    recent = [j for j in jobs.all_jobs() if j.tool in ("redact-scan", "redact-apply")][:20]
    return templates.TemplateResponse(
        request,
        "redact_index.html",
        {"jobs": recent, "queued": jobs.queue_depth()},
        headers={"cache-control": "no-store"},
    )


@router.post("/redact")
async def upload(file: UploadFile, terms: str = Form("")):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    job_id = jobs.submit(
        "redact-scan",
        params={"terms": [t.strip() for t in terms.splitlines() if t.strip()]},
        upload=(file.filename or "document.txt", data),
    )
    return RedirectResponse(f"/redact/{job_id}", status_code=303)


@router.get("/redact/{job_id}", response_class=HTMLResponse)
def status(request: Request, job_id: str):
    job = _job_or_404(job_id, ("redact-scan", "redact-apply"))
    applied = None
    if job.tool == "redact-scan":
        applied = next(
            (j for j in jobs.all_jobs()
             if j.tool == "redact-apply" and j.params.get("source_job") == job.id),
            None,
        )
    return templates.TemplateResponse(
        request,
        "redact_job.html",
        {"job": job, "applied": applied},
        headers={"cache-control": "no-store"},
    )


@router.post("/redact/{job_id}/apply")
async def apply_route(job_id: str, request: Request):
    scan_job = _job_or_404(job_id, ("redact-scan",))
    if scan_job.status != jobs.DONE:
        raise HTTPException(409, "that scan has not finished")

    form = await request.form()
    confirmed = form.getlist("span")
    # Only spans this scan actually offered. The form is the user's, so it is input:
    # a hand-edited one must not be able to name a string the scan never located.
    offered = {span["text"] for span in scan_job.result.get("spans", [])}
    spans = [s for s in confirmed if s in offered]
    if not spans:
        raise HTTPException(400, "no candidates were ticked, so there is nothing to remove")

    source = scan_job.input_path
    apply_id = jobs.submit(
        "redact-apply",
        params={"spans": spans, "source_job": scan_job.id},
        upload=(source.name, source.read_bytes()),
    )
    return RedirectResponse(f"/redact/{apply_id}", status_code=303)


@router.get("/redact/{job_id}/download")
def download(job_id: str):
    job = _job_or_404(job_id, ("redact-apply",))
    rel = job.result.get("output_rel")
    if job.status != jobs.DONE or not rel:
        raise HTTPException(409, "this job produced no verified output")

    output = job.dir / rel
    # Re-run the verifier at serve time, exactly as the marker tool re-checks its
    # sidecar: the file could have been edited since, and this is the last place a
    # surviving span can be caught before it leaves the box.
    survivors = redaction.survivors(
        pdf_redaction.extract_text(output) if output.suffix.lower() == ".pdf"
        else output.read_text(errors="replace"),
        job.params["spans"],
    )
    if survivors:
        raise HTTPException(
            409,
            f"refusing to serve this file — {len(survivors)} confirmed span(s) are "
            f"present in it, so it is not the output this job verified",
        )

    media = "application/pdf" if output.suffix.lower() == ".pdf" else "text/markdown"
    return FileResponse(output, media_type=media, filename=output.name)
