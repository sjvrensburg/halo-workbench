"""Chunking, the three checks, and the two modes. No FastAPI, no network.

Phase 6b. Two things share this module because they share everything except the model:

* **translate** — source language ≠ target language.
* **rephrase** — source language = target language. Same window, same protected spans,
  same checks; a different endpoint, because an MT model cannot rewrite English into
  better English (MiLMMT is a pure completion MT model, Hy-MT2 is MT-only).

Protected spans use ZnZ sentinels, not the model's own do-not-translate instruction.
That is measured, not assumed: on Hy-MT2-7B — the model chosen *for* its trained
instruction support — sentinels scored 10/10 against the instruction's 8/10 on the same
paragraphs, and were faster (docs/translation-bakeoff.md, 2026-08-29). An instruction is
a request a model may decline; a sentinel is a substitution that either round-trips or
is caught.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app import protect

# MiLMMT's unit comment notes ~1500 characters for its stage, and check 2 (script ratio)
# starts firing on whole-document calls, so paragraphs are translated separately and
# joined. Blank lines are the split, because they are also what the user sees.
MAX_CHUNK_CHARS = 1500

HAN = re.compile(r"[㐀-鿿豈-﫿]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")

# Everything a style rewrite promises not to touch. Numbers and citation keys are the
# academic-humanizer skill's own contract ("never changes a number, result, or
# citation"), which makes that contract mechanically checkable — the only part of an
# editorial rewrite that is.
NUMBER = re.compile(r"\d+(?:[.,]\d+)*%?")
CITATION = re.compile(r"@[A-Za-z][A-Za-z0-9_:.-]*|\\cite[a-zA-Z]*\*?\{[^}]*\}"
                      r"|\([A-Z][A-Za-z'’-]+(?:\s+(?:and|&|et\s+al\.?)\s+[A-Z][A-Za-z'’-]+)*,?\s+\d{4}[a-z]?\)")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Chunk:
    source: str
    output: str = ""
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def split_chunks(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Paragraphs, then oversized paragraphs by sentence, never mid-sentence."""
    chunks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            chunks.append(paragraph)
            continue
        current = ""
        # Split after sentence-enders in either script; keep the ender with its sentence.
        for sentence in re.split(r"(?<=[.!?。！？])\s*", paragraph):
            if not sentence:
                continue
            if len(current) + len(sentence) > limit and current:
                chunks.append(current.strip())
                current = ""
            current += sentence
        if current.strip():
            chunks.append(current.strip())
    return chunks


def detect_language(text: str) -> str:
    """`zh` or `en`, by script. Enough for the two languages this tool supports.

    Deliberately not a language-ID model: the tool offers Chinese and English, the two
    scripts do not overlap, and a wrong answer here is visible immediately in the output
    rather than silent.
    """
    han = len(HAN.findall(text))
    latin = len(LATIN_WORD.findall(text))
    return "zh" if han > max(2, latin * 0.15) else "en"


def mask_all(text: str, literal: list[str]) -> tuple[str, dict[int, str]]:
    """Mask the caller's literal spans and the pattern-found ones, in that order.

    The order is forced, and getting it wrong is a bug this cost twice on 2026-08-29:

    1. **Literal spans first**, or app/protect.py splits them. Its `key` rule is a bibtex
       citation key (`@name`), which also matches the domain half of an email address, so
       `finance@example.co.za` masks as `finance` + a sentinel — and a sentinel glued to a
       letter is one its own restoration deliberately refuses to touch.
    2. **In a digit-free form**, or protect.mask masks the digits *inside* our sentinels:
       `Z900Z` contains `900`, its `number` rule matches, and what reaches the model is
       neither a sentinel nor a number. The model dropped them entirely.

    So literal spans become `ZQ<letters>Z` first, protect.mask runs over the result, and
    the letter tokens are renumbered afterwards into the same ZnZ space with a single
    merged mapping. app/protect.py itself is never edited — it is vendored, and its
    sha256 is checked.
    """
    staged = text
    letters: dict[str, str] = {}
    for index, span in enumerate(sorted(set(literal), key=len, reverse=True)):
        if span and span in staged:
            token = f"ZQ{chr(ord('A') + index // 26)}{chr(ord('A') + index % 26)}Z"
            staged = staged.replace(span, token)
            letters[token] = span

    masked = protect.mask(staged)
    mapping = dict(masked.mapping)
    text_out = masked.text

    next_index = max(mapping, default=-1) + 1
    for token, span in letters.items():
        mapping[next_index] = span
        text_out = text_out.replace(token, f"Z{next_index}Z")
        next_index += 1

    return text_out, mapping


# ── the checks, run on every request ─────────────────────────────────────────


def check_spans(source: str, output: str, spans: dict[int, str]) -> Check:
    """Every protected span present as often as the source has it.

    NOT "exactly once", and NOT "in order" — both were in PLAN.md and both were falsified
    by the phase 6a bake-off. `--parallel` occurs twice in a source paragraph and a
    faithful translation contains it twice; and Chinese reorders, so requiring order
    fails good output ("Upgrade to b10582 before enabling draft-mtp" comes back with the
    two spans swapped). Order is reported by the caller for the curious, and decides
    nothing.
    """
    wrong = []
    for value in spans.values():
        want, got = source.count(value), output.count(value)
        if got != want:
            wrong.append(f"{value!r}: {got}× in the output, {want}× in the source")

    leftover = protect._ORPHAN_SENTINEL.findall(output) if hasattr(protect, "_ORPHAN_SENTINEL") else []
    if leftover:
        wrong.append(f"{len(leftover)} sentinel(s) never restored")

    return Check("spans", not wrong,
                 "all protected spans survived" if not wrong else "; ".join(wrong))


def check_script(target_language: str, output: str, spans: dict[int, str]) -> Check:
    """Residual-script check: did the model translate everything, or copy part of it?

    The characteristic failure of a small MT model on a long paragraph, and invisible to
    anyone who does not read both languages — which is exactly the user this tool has.
    """
    protected = "".join(spans.values())
    han = len(HAN.findall(output)) - len(HAN.findall(protected))
    latin = len(LATIN_WORD.findall(output)) - len(LATIN_WORD.findall(protected))

    if target_language == "zh":
        ok = han > 0 and latin <= 3
        detail = f"{han} Han characters, {latin} Latin words outside protected spans"
    else:
        ok = han <= 0
        detail = f"{max(han, 0)} Han characters outside protected spans"
    return Check("script", ok, detail)


def check_passthrough(source: str, output: str) -> Check:
    """Output ≈ input means the model declined and echoed. Fail it."""
    def squash(text: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()

    same = squash(source) == squash(output)
    return Check("passthrough", not same,
                 "the model returned its input unchanged" if same
                 else "the output differs from the input")


def check_untouched(source: str, output: str) -> Check:
    """Numbers and citations identical, for a style rewrite. The only real check it has.

    A rewrite cannot be verified for quality -- that is judgement, and this box is bad at
    judgement. What it *can* be held to is the promise the guides themselves make: the
    academic-humanizer skill says it "never changes a number, result, or citation". So
    the multiset of numbers and citation keys must come back identical, and a rewrite
    that edits one fails the job rather than reaching the user.
    """
    def multiset(pattern: re.Pattern, text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for match in pattern.findall(text):
            counts[match] = counts.get(match, 0) + 1
        return counts

    problems = []
    for label, pattern in (("number", NUMBER), ("citation", CITATION)):
        before, after = multiset(pattern, source), multiset(pattern, output)
        for value, count in before.items():
            if after.get(value, 0) != count:
                problems.append(f"{label} {value!r}: {count}× before, {after.get(value, 0)}× after")
        for value, count in after.items():
            if value not in before:
                problems.append(f"{label} {value!r} appeared, and was not in the source")

    return Check("numbers and citations", not problems,
                 "every number and citation is unchanged" if not problems
                 else "; ".join(problems[:5]))


def run_checks(source: str, output: str, spans: dict[int, str], target_language: str,
               mode: str) -> list[Check]:
    checks = [
        check_spans(source, output, spans),
        check_script(target_language, output, spans),
        check_passthrough(source, output),
    ]
    if mode == "rephrase":
        # A rephrase stays in one language, so the script check above is asking a
        # different question (did it stay?), and the numbers/citations check is the one
        # that carries the weight.
        checks.append(check_untouched(source, output))
    return checks
