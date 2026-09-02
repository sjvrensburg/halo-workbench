"""The hint tool's pure half: prompts, one request to a VLM, and the spread of readings.

No FastAPI, no camera, no disk. Everything here runs on the authoring laptop, which is
why the acceptance driver can exercise the parts that decide what the operator sees.

Three things in this file are load-bearing and none of them is obvious:

* **An empty `content` is a result, not a non-answer.** halo-prep measured Chandra-OCR-2
  returning HTTP 200, `finish_reason: stop`, 568 completion tokens — all of them in
  `reasoning_content` — and `content: ''` (docs/04 "Chandra-OCR-2 tested too"). In an
  unattended batch that page came back missing with nothing in the logs. Here it must
  announce itself, so `read_once` reports `silent=True` with the reasoning length rather
  than an empty string that looks like "the model read nothing there".
* **Readings are grouped, never merged.** `group()` counts identical readings so the UI
  can show "3 of 5 said X"; it does not vote, average or rank across models. Collapsing
  the distribution throws away the only signal that distinguishes ink that is hard to
  read from ink that is easy to read and was read wrong (PLAN.md 7d).
* **Markup survives.** A member that writes `<del>fit</del>` has *read* the strikethrough,
  which is more than the others managed; halo-prep kept that text in its scorer for the
  same reason. Normalisation for grouping folds whitespace and case only, and the text
  shown is always the model's own.
"""

from __future__ import annotations

import base64
import codecs
import json
import math
import re
import time
import urllib.error
import urllib.request

# The operator's own prompt for a whole page, verbatim from halo-prep's
# scripts/bench-handwriting.py. Every measurement behind the model choice was taken with
# these words; changing them invalidates the comparison, and the "do not correct" clause
# is the clause under test (arXiv 2604.22774: every model tested ignores it).
PAGE_PROMPT = (
    "Transcribe the handwritten text in the attached image of a student's answer "
    "to a test question. Provide only the word for word transcription and do not "
    "correct or infer meaning."
)

# The crop prompt. Shorter on purpose: the operator has already said where to look by
# drawing the box, so the model is not asked to find anything. "Exactly what is written"
# rather than "the word" because the crop may hold a symbol, a fragment or a struck-out
# scribble, and asking for a word invites one.
CROP_PROMPT = (
    "This is a crop from a handwritten student's answer. Write out exactly what is "
    "written in it, character for character. Do not correct spelling, do not complete "
    "an abbreviation, and do not explain. If part of it is struck out, show that. "
    "If you cannot read it, say UNREADABLE."
)

PROMPTS = {"crop": CROP_PROMPT, "page": PAGE_PROMPT}

# Default generation ceiling, overridden per member by config.HINT_MEMBERS["max_tokens"].
# It has to be per member because the members are not the same kind of model: olmOCR-2
# answers directly in 19 tokens on a crop, while Muse and Chandra reason first and reach
# the answer only afterwards. Measured 2026-08-30: at 256 tokens Muse returned an empty
# `content` three times out of three on a one-line crop -- the tool called that silence
# correctly, and the silence was this number's fault, not the model's.
MAX_TOKENS = {"crop": 256, "page": 1024}

REQUEST_TIMEOUT_SECONDS = 300


class HintError(Exception):
    """Something the page should say plainly. Never carries image or reading text."""


# ── the image, without an image library ──────────────────────────────────────
# The crop is made in the browser with a canvas, so nothing here has to decode pixels --
# only to check that what arrived is an image of a plausible size. That is the whole
# reason this repo still has no Pillow: adding a decoder to look at two integers in a
# header would be a dependency bought with nothing.

MAX_IMAGE_BYTES = 12 * 1024 * 1024

_DATA_URL = re.compile(r"^data:image/(png|jpeg|jpg|webp);base64,", re.I)


def decode_data_url(value: str) -> tuple[bytes, str]:
    """(bytes, mime) from a `data:image/...;base64,` URL, or raise HintError."""
    match = _DATA_URL.match(value or "")
    if not match:
        raise HintError("expected a PNG, JPEG or WebP data URL from the capture")
    payload = value[match.end():]
    # base64 is 4/3 of the payload, so this refuses the oversize case before allocating.
    if len(payload) > MAX_IMAGE_BYTES // 3 * 4 + 16:
        raise HintError(f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001 -- any decode failure is the same message
        raise HintError("capture was not valid base64") from exc
    if not raw:
        raise HintError("capture was empty")
    kind = match.group(1).lower()
    return raw, "image/jpeg" if kind in ("jpeg", "jpg") else f"image/{kind}"


def image_size(raw: bytes) -> tuple[int, int] | None:
    """(width, height) for PNG and baseline/progressive JPEG. None if not recognised.

    Server-side and deliberately independent of what the browser claimed: PLAN.md 7b
    calls resolution "the whole ballgame", and a UI that reports the dimensions it asked
    for rather than the ones that arrived would reproduce halo-prep's MiniCPM failure
    (5/15 on symbols, purely on effective resolution) while looking correct.
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if raw[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(raw):
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        # SOF0..SOF15, minus the four that are not frame headers (DHT, JPG, DAC, RSTn).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (int.from_bytes(raw[index + 7:index + 9], "big"),
                    int.from_bytes(raw[index + 5:index + 7], "big"))
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(raw[index + 2:index + 4], "big")
        if length < 2:
            return None
        index += 2 + length
    return None


# ── one request ──────────────────────────────────────────────────────────────


def read_once(endpoint: str, raw: bytes, mime: str, mode: str = "crop",
              temperature: float = 0.0, seed: int | None = None,
              max_tokens: int | None = None,
              timeout: int = REQUEST_TIMEOUT_SECONDS) -> dict:
    """Ask one model to read one image once.

    `temperature` is passed per request on purpose. `llama-olmocr.service` runs
    `--temp 0` and every halo-prep measurement behind it was greedy — that is a *batch*
    decision, and sampling for spread is this tool's whole 7d. Override the request, never
    the unit.
    """
    if mode not in PROMPTS:
        raise HintError(f"unknown mode {mode!r}")
    body = {
        "model": "local",
        "temperature": temperature,
        "max_tokens": max_tokens or MAX_TOKENS[mode],
        # Per-token hesitation for the page's default reading view (7f). Harmless to
        # request against an endpoint that ignores it -- parsing below is all `.get`.
        "logprobs": True,
        "top_logprobs": 3,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64," + base64.b64encode(raw).decode()}},
            {"type": "text", "text": PROMPTS[mode]},
        ]}],
    }
    if seed is not None:
        body["seed"] = seed

    request = urllib.request.Request(
        f"{endpoint}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise HintError(f"{endpoint} answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HintError(f"{endpoint} did not answer: {exc}") from exc
    elapsed = time.time() - started

    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise HintError(f"{endpoint} returned a body with no choices") from exc

    text = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or "")
    usage = payload.get("usage") or {}
    logprobs = choice.get("logprobs")
    tokens = token_confidences(logprobs.get("content") if logprobs else None)
    if tokens is not None and "".join(span["text"] for span in tokens).strip() != text:
        # Measured on Muse Glimmer, 2026-09-02: a reasoning model's `logprobs.content`
        # covers its WHOLE generation -- channel markers, the reasoning itself, even an
        # echo of the prompt -- not just the parsed `content` this reading shows. Tokens
        # that don't reconstruct to the displayed text would tint and caption the WRONG
        # text as if it were the reading, which is worse than the feature being absent.
        tokens = None
    return {
        "text": text,
        # The Chandra failure, made loud. See this module's docstring.
        "silent": not text,
        "reasoning_chars": len(reasoning),
        "finish_reason": choice.get("finish_reason"),
        "truncated": choice.get("finish_reason") == "length",
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        # None when the endpoint didn't return logprobs, or when they don't line up
        # with `text` (see above) -- the page falls back to plain text for that
        # reading rather than showing something misleading.
        "tokens": tokens,
        # Repeat samples of one crop reuse the prefill almost completely (measured on
        # :8097, 4080 of 4081 cached). Surfaced so a slow second sample is explicable.
        "cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens")
        if isinstance(usage.get("prompt_tokens_details"), dict) else None,
        "elapsed": round(elapsed, 2),
        "temperature": temperature,
        "max_tokens": max_tokens or MAX_TOKENS[mode],
    }


def token_confidences(logprobs_content: list[dict] | None) -> list[dict] | None:
    """[{text, prob, alternates}, ...] for one generation, or None if there is none.

    This is a different thing from `group()`/`agreement()` below and does not touch
    PLAN.md 7d's "not a verifier" rule: 7d is about collapsing the SPREAD across
    samples and members into one voted answer, which this never does. This is a
    single generation's own hesitation, read off `logprob` and shown as-is -- the
    page's default view for a reading, the same way showing disagreement already is.

    `bytes`, not the `token` string field: llama-server's `token` can be a lossy
    decode of one BPE token that only holds *part* of a multi-byte UTF-8 character,
    and this tool has no reason to assume ASCII. An incremental decoder reassembles
    the character across however many tokens it actually took, so text still comes
    out right even when a character's bytes are split; a token that only contributed
    partial bytes correctly produces no span of its own, and its logprob is folded
    into whichever later token completes the character.

    `alternates` (from the request's `top_logprobs`) are decoded independently and
    best-effort (`errors="replace"`) rather than through the same incremental
    decoder: they are runner-up single tokens the model did NOT choose, not a
    continuation of the output stream, so there is nothing to reassemble them
    against. They are for a tap-to-reveal detail, never the displayed text.
    """
    if not logprobs_content:
        return None
    decoder = codecs.getincrementaldecoder("utf-8")()
    spans: list[dict] = []
    try:
        for item in logprobs_content:
            text = decoder.decode(bytes(item.get("bytes") or []))
            if text:
                alternates = [
                    {"text": bytes(alt.get("bytes") or []).decode("utf-8", "replace"),
                     "prob": math.exp(alt["logprob"])}
                    for alt in (item.get("top_logprobs") or [])
                ]
                spans.append({"text": text, "prob": math.exp(item["logprob"]),
                             "alternates": alternates})
        tail = decoder.decode(b"", final=True)
    except (UnicodeDecodeError, TypeError, KeyError, ValueError):
        return None
    if tail and spans:
        spans[-1]["text"] += tail
    return spans or None


# ── the spread ───────────────────────────────────────────────────────────────


def normalise(text: str) -> str:
    """The key readings are grouped by. Whitespace and case only, and nothing else.

    Deliberately *not* a fuzzy match. Two readings that differ by a character differ, and
    that difference is the finding — the operator is looking at the ink and wants to know
    where the models parted company. Folding `<del>`, punctuation or spelling here would
    hide exactly the disagreements this tool exists to show.
    """
    return " ".join(text.split()).casefold()


def group(samples: list[dict]) -> list[dict]:
    """Distinct readings with their counts, most-supported first, ties in first-seen order.

    `count` is support within ONE member's samples: "how sure is this model". It is never
    compared across members and never turned into a score — see PLAN.md 7d, "Not a
    verifier".
    """
    order: list[str] = []
    seen: dict[str, dict] = {}
    for sample in samples:
        if sample.get("silent") or not sample.get("text"):
            continue
        key = normalise(sample["text"])
        if key not in seen:
            seen[key] = {"text": sample["text"], "count": 0,
                         "truncated": bool(sample.get("truncated"))}
            order.append(key)
        seen[key]["count"] += 1
        seen[key]["truncated"] |= bool(sample.get("truncated"))
    return sorted((seen[key] for key in order), key=lambda r: -r["count"])


def agreement(members: list[dict]) -> dict:
    """What to say across members, in words, without picking a winner.

    Compares each member's *best-supported* reading. The three outcomes are
    `agree` / `disagree` / `insufficient`, and `disagree` is a finding rather than an
    error: on a struck-out or genuinely ambiguous squiggle it is the most useful thing
    this page can say.
    """
    tops: dict[str, str] = {}
    for member in members:
        readings = member.get("readings") or []
        if readings:
            tops[member["id"]] = normalise(readings[0]["text"])
    if len(tops) < 2:
        return {"state": "insufficient", "answered": len(tops),
                "note": "one model answered — a second voice is what makes disagreement "
                        "visible, and disagreement is the signal."}
    distinct = set(tops.values())
    if len(distinct) == 1:
        return {"state": "agree", "answered": len(tops),
                "note": "independent models read the same thing. That is evidence about "
                        "the ink, not a guarantee."}
    return {"state": "disagree", "answered": len(tops), "distinct": len(distinct),
            "note": "the models read this differently. Look at the ink: this is where "
                    "your eye is needed, and no reading here should be taken on trust."}
