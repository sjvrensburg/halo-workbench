"""The recall layer: openai/privacy-filter, as ONNX, on the CPU.

A token classifier over eight privacy categories that labels a whole sequence in one
forward pass and decodes BIOES spans. It replaced a general chat model on :8092, which
was measured on 2026-08-29 doing this job badly: Ornith-1.5-9B is a thinking model, so
with an 800-token budget it spent the lot inside `reasoning_content` and returned an
EMPTY `content` -- zero proposals, silently, in 139 s. (With `enable_thinking: false`
it does the job in 2.4 s and found 4/4 names, which is worth knowing but is still a
9B model queueing for a GPU that three other units want.)

This runs on the CPU in its own process. It therefore does not queue behind Hermes or
Open Notebook at all, which is the first thing in this repo that can honestly say so.

**It proposes. It never decides.** Every span it returns is checked against the source
by exact string match, offered to the user with a tick box, and removed only if they
confirm it -- and then the verifier re-reads the output. Its own recall is no more
provable than the previous layer's, which is why the output is still a candidate.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app import config

log = logging.getLogger(__name__)

# The model reads 128k tokens, but this runs on the CPU beside three GPU model servers,
# and a job here holds the single worker. 20k characters is a few seconds; the pattern
# layer still covers the whole document, and the page says the model saw the head.
MAX_CHARS = 20000

# Banded attention, band size 128 (257 tokens including self), so the model does not
# need the document in one piece -- it needs each window intact. Chunks overlap by more
# than the band so a span on a boundary is seen whole by at least one chunk.
CHUNK_TOKENS = 2048
OVERLAP_TOKENS = 256

# Below this the span is not offered. The card exposes precision/recall operating
# points; this is the recall-leaning end, because a false positive costs one click and
# a false negative costs the thing the tool exists to prevent.
MIN_SCORE = 0.5

_LOCK = threading.Lock()
_SESSION = None
_TOKENIZER = None
_LABELS: dict[int, str] = {}


class TaggerUnavailable(Exception):
    """The model is not installed here. The caller degrades to patterns and says so."""


def _load():
    """Load once, on first use. Held for the process: ~1 GB of q4 weights, mmapped."""
    global _SESSION, _TOKENIZER, _LABELS
    if _SESSION is not None:
        return _SESSION, _TOKENIZER, _LABELS

    model = config.PII_MODEL_DIR / "onnx" / "model_q4.onnx"
    tokenizer_json = config.PII_MODEL_DIR / "tokenizer.json"
    if not model.is_file() or not tokenizer_json.is_file():
        raise TaggerUnavailable(
            f"openai/privacy-filter is not in {config.PII_MODEL_DIR} — see "
            f"scripts/fetch-privacy-filter.sh"
        )

    import onnxruntime
    from tokenizers import Tokenizer

    with _LOCK:
        if _SESSION is None:
            options = onnxruntime.SessionOptions()
            # One job at a time already (CLAUDE.md "Concurrency 1"), and the GPU units
            # want the cores that a thread-per-core ONNX session would take.
            options.intra_op_num_threads = 4
            _SESSION = onnxruntime.InferenceSession(
                str(model), options, providers=["CPUExecutionProvider"]
            )
            _TOKENIZER = Tokenizer.from_file(str(tokenizer_json))
            labels = json.loads((config.PII_MODEL_DIR / "config.json").read_text())["id2label"]
            _LABELS = {int(k): v for k, v in labels.items()}
    return _SESSION, _TOKENIZER, _LABELS


def _softmax_max(row: list[float]) -> tuple[int, float]:
    top = max(range(len(row)), key=row.__getitem__)
    biggest = row[top]
    total = sum(pow(2.718281828459045, value - biggest) for value in row)
    return top, 1.0 / total


def _decode(labels: list[str], scores: list[float], offsets: list[tuple[int, int]],
            text: str) -> list[tuple[str, str, float]]:
    """BIOES tags -> (text, category, score) spans, using each token's own offsets.

    Tolerant of imperfect tag sequences: an I- or E- with no B- before it still opens a
    span, and a category change closes the previous one. A constrained Viterbi decode is
    what the model card describes; this is the same idea done plainly, and anything it
    gets wrong is caught downstream -- a span that is not a verbatim substring is
    discarded, and everything else still has to survive the user's eye and the verifier.

    A span's score is the MINIMUM over its tokens, not the mean: one uncertain token in
    the middle of a name is a reason to score the whole span low.
    """
    spans: list[tuple[str, str, float]] = []
    start: int | None = None
    category = ""
    score = 1.0
    last_end = 0

    def emit(end: int) -> None:
        nonlocal start, category, score
        if start is not None and end > start:
            value = text[start:end].strip()
            if value:
                spans.append((value, category, score))
        start, category, score = None, "", 1.0

    for label, confidence, (begin, end) in zip(labels, scores, offsets):
        if end <= begin:  # special tokens carry an empty offset range
            continue
        if label == "O":
            emit(last_end)
            continue

        prefix, _, kind = label.partition("-")
        if start is None or prefix in ("B", "S") or kind != category:
            emit(last_end)
            start, category, score = begin, kind, confidence

        score = min(score, confidence)
        last_end = end
        if prefix in ("E", "S"):
            emit(end)

    emit(last_end)
    return spans


def propose(text: str) -> tuple[list[tuple[str, str]], str]:
    """Return [(span text, category)] and a note for the page. Raises TaggerUnavailable."""
    session, tokenizer, id2label = _load()

    encoding = tokenizer.encode(text[:MAX_CHARS])
    ids, offsets = encoding.ids, encoding.offsets
    proposals: dict[str, str] = {}

    step = CHUNK_TOKENS - OVERLAP_TOKENS
    for begin in range(0, max(1, len(ids)), step):
        window = ids[begin : begin + CHUNK_TOKENS]
        if not window:
            break
        outputs = session.run(None, {
            "input_ids": [window],
            "attention_mask": [[1] * len(window)],
        })
        logits = outputs[0][0]

        labels, scores = [], []
        for row in logits:
            index, confidence = _softmax_max(list(map(float, row)))
            labels.append(id2label.get(index, "O"))
            scores.append(confidence)

        for value, category, score in _decode(
            labels, scores, offsets[begin : begin + CHUNK_TOKENS], text
        ):
            if score < MIN_SCORE:
                continue
            # Split proposals that cross a line break into one span per line. The
            # tagger happily returns "42 Kloof Street,\nCape Town" as one address, which
            # is correct and unusable: in the PDF path a span that spans a line is the
            # case that cannot be located as a rectangle, so it would fail the whole job
            # (by design -- an unlocatable span is never silently skipped). Per-line
            # spans are still verbatim substrings, still get removed, and still verify.
            for part in (p.strip() for p in value.splitlines()):
                if len(part) > 1 and part not in proposals:
                    proposals[part] = category

        if begin + CHUNK_TOKENS >= len(ids):
            break

    note = ""
    if len(text) > MAX_CHARS:
        note = (f"the model read the first {MAX_CHARS:,} characters; the pattern layer "
                f"read all {len(text):,}")
    return [(value, category) for value, category in proposals.items()], note
