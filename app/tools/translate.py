"""Translate and rephrase, in one window, with the spans you protect surviving both.

One tool, two modes, decided by the language pair (PLAN.md phase 6b plus the operator's
UX ask, 2026-08-29):

* source ≠ target → **translate**, on :8094 (Hy-MT2-7B, chosen by the 6a bake-off)
* source = target → **rephrase**, on :8088 (Ornith-35B; an MT model cannot rewrite
  English into better English)

Protected spans are ZnZ sentinels, vendored with their measurements in app/protect.py,
and NOT the model's own do-not-translate instruction: on the very model picked for that
instruction, sentinels scored 10/10 against the instruction's 8/10 and were faster.

Every request runs the checks in app/translation.py, and a chunk whose checks fail is
never presented as a finished result. That is not caution about a rare case -- two
identical `temperature: 0` runs in the bake-off differed on 7 of 10 paragraphs, and span
loss came and went between them. The request in front of the user is a fresh sample.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import config, jobs, model_lease, protect, redaction, translation
from app.views import templates

log = logging.getLogger(__name__)
router = APIRouter()

TIMEOUT_SECONDS = 300
LANGUAGE_NAME = {"en": "English", "zh": "Chinese"}

# Style presets for a rewrite. Short, concrete, and in-repo: a preset that needs a
# paragraph of explanation belongs in a guide file instead.
PRESETS = {
    "": "",
    "plainer": "Prefer short sentences and plain words. Break up long sentences. "
               "Replace nominalisations with verbs. Do not add or remove information.",
    "formal": "Raise the register: no contractions, no colloquialisms, precise word "
              "choice. Do not add or remove information.",
    "shorter": "Say the same thing in about two thirds of the words. Cut redundancy and "
               "filler, never content.",
}


# ── style guides (a SKILL.md works as one) ───────────────────────────────────


def _summarise(text: str) -> str:
    """The guide's own description line, including YAML block scalars.

    `description: |` followed by indented lines is the common shape in a SKILL.md, and
    reading only the line the key is on yields a literal "|" -- which is what the picker
    showed the first time this ran.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value not in ("|", "|-", ">", ">-"):
            return value.strip("\"'")
        collected = []
        for following in lines[index + 1:]:
            if following.strip() and not following.startswith((" ", "\t")):
                break
            collected.append(following.strip())
        return " ".join(part for part in collected if part)

    for line in lines:
        line = line.strip()
        if line and not line.startswith(("---", "#", "name:", "version:", "license:")):
            return line
    return ""


def available_guides() -> list[dict]:
    """Markdown guides found in config.STYLE_GUIDE_DIRS, newest convention first.

    A guide is just a markdown file. `academic-humanizer/SKILL.md` is one, which is the
    point: the editorial rules already written for an agent are reusable as instructions
    for a rewrite, without copying them into this repo.
    """
    found: list[dict] = []
    for directory in config.STYLE_GUIDE_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")) + sorted(directory.glob("*/SKILL.md")):
            text = path.read_text(errors="replace")
            name = path.stem if path.stem != "SKILL" else path.parent.name
            summary = _summarise(text)
            found.append({"id": f"{directory.name}/{name}", "name": name,
                          "path": str(path), "summary": summary[:160],
                          "chars": len(text)})
    return found


def load_guide(guide_id: str) -> tuple[str, str]:
    """(name, text) for a guide id, or raise. Path is resolved from the listing only.

    Never from the request: the id is matched against what `available_guides` found, so a
    crafted id cannot read an arbitrary file.
    """
    for guide in available_guides():
        if guide["id"] == guide_id:
            return guide["name"], Path(guide["path"]).read_text(errors="replace")
    raise jobs.JobError(f"no style guide called {guide_id!r} is installed")


# ── talking to the models ────────────────────────────────────────────────────


def _ask(endpoint: str, prompt: str, max_tokens: int = 2048) -> str:
    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        # Translation is not a task with a creative temperature. Note that this does NOT
        # buy determinism on llama.cpp -- the bake-off measured 7 of 10 paragraphs
        # changing between two runs at 0 -- which is why the checks run per request.
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
        # Thinking off where the server offers it: measured 2026-08-29, this family
        # spends a whole budget in reasoning_content and returns empty content.
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    request = urllib.request.Request(f"{endpoint}/v1/chat/completions", data=payload,
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = json.load(response)
    message = (body.get("choices") or [{}])[0].get("message", {})
    return (message.get("content") or "").strip()


def _translate_prompt(masked: str, target: str) -> str:
    """Hy-MT2's own default-translation shape, plus what the sentinels are."""
    return (
        f"Translate the following text into {LANGUAGE_NAME[target]}. Markers such as Z0Z "
        f"are placeholders: reproduce each one exactly as written, in the right place, "
        f"and never translate or renumber them. Note that you should only output the "
        f"translated result without any additional explanation:\n\n{masked}"
    )


def _rephrase_prompt(masked: str, language: str, instruction: str) -> str:
    return (
        f"Rewrite the following {LANGUAGE_NAME[language]} text. {instruction}\n\n"
        f"Markers such as Z0Z are placeholders: reproduce each one exactly as written, "
        f"in the right place. Keep every number and citation exactly as it is. Output "
        f"only the rewritten text, with no commentary.\n\n{masked}"
    )


def _audit_prompt(text: str, guide_name: str, guide: str) -> str:
    """First of two passes when a guide is used.

    A SKILL.md is a *procedure* -- academic-humanizer's is read, audit, propose, edit,
    with tools -- not a rewrite instruction. Collapsing it into one call gets a shallow
    imitation of it. Two calls reproduce its actual shape: name the problems first, then
    fix exactly those. It also gives the user something to read: the audit is shown.
    """
    return (
        f"You are applying the editorial guide below to a piece of writing.\n\n"
        f"=== GUIDE: {guide_name} ===\n{guide}\n=== END GUIDE ===\n\n"
        f"Do not rewrite anything yet. List the specific problems this guide identifies "
        f"in the text, one per line, each quoting the phrase at fault. If there are "
        f"none, output NONE.\n\nTEXT:\n{text}"
    )


# ── the job ──────────────────────────────────────────────────────────────────


@jobs.register("translate")
def run(job: jobs.Job) -> None:
    text = job.params["text"]
    source = job.params["source"]
    target = job.params["target"]
    mode = "rephrase" if source == target else "translate"
    endpoint = config.REPHRASE_ENDPOINT if mode == "rephrase" else config.TRANSLATE_ENDPOINT

    # Start the model if it is ours and it is not up. The page shows the wait rather
    # than looking wedged: a cold 7B is tens of seconds, which is indistinguishable from
    # a hang if nothing says so.
    def announce(unit: str) -> None:
        jobs.record(job.id, stage=f"starting {unit} — the model is not resident")

    try:
        waited = model_lease.ensure_running(endpoint, on_wait=announce)
    except RuntimeError as exc:
        raise jobs.JobError(str(exc)) from None
    if waited:
        jobs.record(job.id, model_start_seconds=round(waited, 1))
    jobs.record(job.id, stage="")

    instruction = PRESETS.get(job.params.get("preset", ""), "")
    guide_id = job.params.get("guide", "")
    audit = ""

    if guide_id and mode == "rephrase":
        guide_name, guide_text = load_guide(guide_id)
        try:
            audit = _ask(endpoint, _audit_prompt(text, guide_name, guide_text))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise jobs.JobError(_offline(endpoint, exc)) from None
        instruction = (
            f"Apply this audit, and nothing beyond it:\n{audit}\n\n"
            f"{instruction}" if audit and not audit.upper().startswith("NONE") else instruction
        )
        jobs.record(job.id, audit=audit, guide_name=guide_name)

    chunks: list[translation.Chunk] = []
    extra = [term.strip() for term in job.params.get("extra_spans", []) if term.strip()]

    for source_chunk in translation.split_chunks(text):
        # Whole contact details, protected BEFORE the vendored masker sees them.
        #
        # MEASURED the first time this ran, 2026-08-29: `finance@example.co.za` came back
        # as `financeZ3Z`. app/protect.py was written for LaTeX, where `@name` is a
        # citation key, so its `key` rule matched `@example.co.za` and masked half the
        # address -- leaving a sentinel glued to `finance`, which its own restoration
        # rule then refuses to touch (a one-sided match would turn any capital Z beside a
        # digit into a restored span). The verifier caught it; this stops it happening.
        #
        # Reusing the redaction tool's pattern layer rather than writing a third one: it
        # already knows what an email, a URL, an IBAN and a phone number look like, and
        # those are exactly the spans a translation must not touch.
        auto = [
            span.text
            for span in redaction.find_pattern_spans(source_chunk)
            if span.category in ("email", "url", "iban", "account_number", "phone",
                                 "id_number", "card_number")
        ]

        masked_text, spans = translation.mask_all(source_chunk, extra + auto)

        prompt = (_translate_prompt(masked_text, target) if mode == "translate"
                  else _rephrase_prompt(masked_text, target, instruction))
        try:
            raw = _ask(endpoint, prompt)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise jobs.JobError(_offline(endpoint, exc)) from None

        restored, _ = protect.unmask(raw, spans)
        chunk = translation.Chunk(source=source_chunk, output=restored)
        chunk.checks = translation.run_checks(source_chunk, restored, spans, target, mode)
        chunks.append(chunk)

    # Keep the model alive while a person is working through a document, and let the
    # idle timer measure idleness rather than uptime.
    model_lease.touch(endpoint)

    jobs.record(
        job.id,
        mode=mode,
        endpoint=endpoint,
        audit=audit,
        output="\n\n".join(chunk.output for chunk in chunks),
        chunks=[{"source": c.source, "output": c.output,
                 "checks": [{"name": k.name, "ok": k.ok, "detail": k.detail}
                            for k in c.checks],
                 "ok": c.ok}
                for c in chunks],
        failed_chunks=[i + 1 for i, c in enumerate(chunks) if not c.ok],
    )

    bad = [i + 1 for i, chunk in enumerate(chunks) if not chunk.ok]
    if bad:
        # Recorded first, then failed: the page shows exactly which paragraph broke and
        # which check caught it. Never silently patch a mangled sentinel, and never
        # present the whole thing as a finished translation.
        raise jobs.JobError(
            f"paragraph {', '.join(map(str, bad))} of {len(chunks)} did not pass its "
            f"checks — see below. Nothing here is a finished translation."
        )


def _offline(endpoint: str, exc: Exception) -> str:
    unit = "llama-hymt2" if endpoint.endswith("8094") else "llama-ornith35"
    return (f"{endpoint} did not answer ({exc.__class__.__name__}). That model is "
            f"on-demand and this app never starts it: `systemctl --user start {unit}`")


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/translate", response_class=HTMLResponse)
def index(request: Request):
    recent = [j for j in jobs.all_jobs() if j.tool == "translate"][:10]
    return templates.TemplateResponse(
        request, "translate_index.html",
        {"jobs": recent, "guides": available_guides(), "presets": PRESETS,
         "queued": jobs.queue_depth()},
        headers={"cache-control": "no-store"},
    )


@router.post("/translate")
def submit(
    text: str = Form(""),
    source: str = Form("auto"),
    target: str = Form("en"),
    preset: str = Form(""),
    guide: str = Form(""),
    extra_spans: str = Form(""),
):
    text = text.strip()
    if not text:
        raise HTTPException(400, "nothing to translate")
    if source == "auto":
        source = translation.detect_language(text)
    if source not in LANGUAGE_NAME or target not in LANGUAGE_NAME:
        raise HTTPException(400, "languages are en or zh")

    job_id = jobs.submit("translate", params={
        "text": text, "source": source, "target": target,
        "preset": preset, "guide": guide,
        "extra_spans": [line for line in extra_spans.splitlines() if line.strip()],
    })
    return RedirectResponse(f"/translate/{job_id}", status_code=303)


@router.get("/translate/{job_id}.json")
def status_json(job_id: str):
    """What the page polls. Small on purpose: the page redraws from it every 400 ms."""
    job = jobs.get(job_id)
    if job is None or job.tool != "translate":
        raise HTTPException(404, "no such translate job")
    return JSONResponse({
        "status": job.status,
        "error": job.error,
        "elapsed": job.elapsed_seconds(),
        "output": job.result.get("output", ""),
        "chunks": job.result.get("chunks", []),
        "audit": job.result.get("audit", ""),
        "mode": job.result.get("mode", ""),
        "stage": job.result.get("stage", ""),
    })


@router.get("/translate/{job_id}", response_class=HTMLResponse)
def status(request: Request, job_id: str):
    job = jobs.get(job_id)
    if job is None or job.tool != "translate":
        raise HTTPException(404, "no such translate job")
    return templates.TemplateResponse(
        request, "translate_index.html",
        {"job": job, "jobs": [j for j in jobs.all_jobs() if j.tool == "translate"][:10],
         "guides": available_guides(), "presets": PRESETS, "queued": jobs.queue_depth()},
        headers={"cache-control": "no-store"},
    )
