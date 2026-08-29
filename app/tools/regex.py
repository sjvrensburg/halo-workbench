"""Regex from examples: the model proposes, the user's examples decide.

Not a chatbot with a regex flavour. The loop is the product (PLAN.md phase 5): five
candidates a round, every one executed against every example, failures fed back, up to
six rounds. **Nothing is returned that has not been run against the examples**, and when
nothing passes the page says so and shows what was tried, rather than offering the
closest-looking string.

The model is `:8092`, Ornith-1.5-9B -- short prompts, so it is a good fit -- with
thinking off. That is measured, not assumed: on an extraction-shaped task this model
spent an entire 4000-token budget in `reasoning_content` and returned empty `content`
(2026-08-29, halo-prep pitfall #25's counter-example). The same shape applies here: five
one-line answers do not need a reasoning budget, and an empty reply costs a whole round.

That port is `--parallel 1` and shared with Hermes, so a long Hermes turn queues this and
vice versa (halo-prep pitfall #34). Accepted at this scale -- do not add slots to fix it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config, jobs, regex_synth
from app.views import templates

log = logging.getLogger(__name__)
router = APIRouter()

# Five one-line patterns. Generous enough for long alternations, small enough that a
# model which starts explaining itself is cut off rather than left running.
MAX_TOKENS = 600
TIMEOUT_SECONDS = 120


def ask_model(prompt: str) -> str:
    payload = json.dumps(
        {
            "messages": [{"role": "user", "content": prompt}],
            # See the module docstring: thinking on returns nothing useful for this
            # shape of task on this server, and costs the round.
            "chat_template_kwargs": {"enable_thinking": False},
            # 0 would make every round of the loop repeat the previous round's answer,
            # which is the one thing the loop must not do. Low, but not zero.
            "temperature": 0.4,
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        f"{config.REGEX_ENDPOINT}/v1/chat/completions",
        data=payload,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = json.load(response)
    return (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""


@jobs.register("regex")
def synthesise(job: jobs.Job) -> None:
    positives = job.params["positives"]
    negatives = job.params["negatives"]
    mode = job.params.get("mode", "search")

    def note_round(number: int, verdicts: list[regex_synth.Verdict]) -> None:
        log.info("job %s: round %d tried %d candidate(s), %d passed",
                 job.id, number, len(verdicts), sum(v.passes for v in verdicts))

    try:
        result = regex_synth.synthesise(
            positives, negatives, job.params.get("description", ""), mode,
            ask_model=ask_model, on_round=note_round,
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        raise jobs.JobError(
            f"the model on {config.REGEX_ENDPOINT} did not answer ({exc.__class__.__name__}). "
            f"`systemctl --user is-active llama-ornith`"
        ) from None

    def as_dict(verdict: regex_synth.Verdict) -> dict:
        return {
            "pattern": verdict.pattern,
            "ok": verdict.ok,
            "error": verdict.error,
            "passes": verdict.passes,
            "score": verdict.score,
            "positives": verdict.positives,
            "negatives": verdict.negatives,
            "failures": verdict.failures(positives, negatives),
        }

    jobs.record(
        job.id,
        winner=as_dict(result["winner"]) if result["winner"] else None,
        warnings=(regex_synth.overfit_warnings(result["winner"].pattern, positives, negatives)
                  if result["winner"] else []),
        rounds_used=result["rounds_used"],
        tried=[as_dict(v) for v in sorted(result["tried"], key=lambda v: -v.score)],
        examples_run=len(positives) + len(negatives),
    )

    # A job that found nothing is DONE, not failed: the loop worked, the answer is "no
    # pattern passed your examples", and that is a result the page can show honestly.
    # Failing it would hide the attempts, which are the evidence for the answer.


@router.get("/regex", response_class=HTMLResponse)
def index(request: Request):
    recent = [j for j in jobs.all_jobs() if j.tool == "regex"][:20]
    return templates.TemplateResponse(
        request, "regex_index.html", {"jobs": recent, "queued": jobs.queue_depth()},
        headers={"cache-control": "no-store"},
    )


@router.post("/regex")
def submit(
    positives: str = Form(""),
    negatives: str = Form(""),
    description: str = Form(""),
    mode: str = Form("search"),
):
    match = [line for line in positives.splitlines() if line.strip()]
    reject = [line for line in negatives.splitlines() if line.strip()]
    if not match:
        raise HTTPException(400, "give at least one string the pattern must match")
    if mode not in ("search", "fullmatch"):
        raise HTTPException(400, "mode must be search or fullmatch")

    job_id = jobs.submit("regex", params={
        "positives": match, "negatives": reject,
        "description": description.strip(), "mode": mode,
    })
    return RedirectResponse(f"/regex/{job_id}", status_code=303)


@router.get("/regex/{job_id}", response_class=HTMLResponse)
def status(request: Request, job_id: str):
    job = jobs.get(job_id)
    if job is None or job.tool != "regex":
        raise HTTPException(404, "no such regex job")
    return templates.TemplateResponse(
        request, "regex_job.html", {"job": job},
        headers={"cache-control": "no-store"},
    )
