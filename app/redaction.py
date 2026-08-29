"""Finding spans, removing them from text, and proving they are gone.

Pure functions: no FastAPI, no job store, no network. Everything the redaction tool
decides about *what* to remove and *whether it worked* lives here, so it can be
exercised without a GPU or a running app.

Two rules shape this whole module, both from PLAN.md phase 4:

1. **The deterministic layer is the primary detector.** Patterns are exact, offline
   and testable. The model is a recall booster on top and can only ever *add*
   proposals, each of which must occur verbatim in the source or be discarded.
2. **The verifier proves that everything found was removed. It cannot prove nothing
   was missed.** Recall is unverifiable here, which is why the output is a
   *redaction candidate* and every label the user sees says so. A tool that quietly
   under-redacts one invoice is worse than no tool, because it replaces the user's
   own care with false confidence.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# The replacement written in place of a removed span. Fixed width regardless of what it
# replaces: a placeholder that tracked the original length would leak it, and "J████"
# tells a reader how long the name was.
PLACEHOLDER = "[REDACTED]"


@dataclass(frozen=True)
class Span:
    """One string to remove, with where it came from and how often it occurs."""

    text: str
    category: str
    source: str  # "pattern" or "model"
    count: int = 0
    contexts: tuple[str, ...] = field(default=())

    @property
    def key(self) -> str:
        """Identity for the confirm form. The text itself, so the form cannot desync."""
        return self.text


# ── the deterministic layer ──────────────────────────────────────────────────
#
# Every pattern here is deliberately *broad*: a false positive costs the user one
# click to untick, a false negative costs them the thing the tool exists to prevent.
# Ordering matters only for which category a span is reported under, since overlapping
# hits collapse to whichever matched first.

PATTERNS: list[tuple[str, str]] = [
    # RFC-ish rather than RFC-exact. The strict grammar accepts quoted local parts with
    # spaces, which in prose swallows the preceding words.
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # URLs before phone numbers: a URL with a port looks like a number run otherwise.
    ("url", r"\bhttps?://[^\s<>\"')\]]+"),
    # IBAN: two letters, two check digits, then up to 30 alphanumerics, allowing the
    # spaced-in-fours form people actually type. The trailing 1-3 group is not optional
    # decoration: without it `GB82 WEST 1234 5698 7654 32` matches up to `7654` and the
    # last two digits survive into the output, which is a partial account number left
    # in a document the user believes is clean.
    ("iban", r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Za-z0-9]{4})+(?:[ ]?[A-Za-z0-9]{1,3})?\b"),
    # South African ID: 13 digits, often written 000000 0000 000 (the box's operator is
    # in ZA; this is the local identifier a generic pattern misses).
    ("id_number", r"\b\d{6}[ ]?\d{4}[ ]?\d{3}\b"),
    # Card-shaped: 13-19 digits in groups. Luhn is checked separately, see below.
    ("card_number", r"\b(?:\d[ -]?){12,18}\d\b"),
    # Dates BEFORE phones: the phone pattern is three groups of digits with separators,
    # which `2026-09-15` satisfies, and a due date reported as a phone number is a
    # category the user will distrust the whole list over.
    ("date", r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"),
    # Phone: +27 82 123 4567, (021) 555-0123, 021-555-0123. Requires either a leading +
    # or at least 9 digits, or every four-digit year in a document becomes a phone.
    ("phone", r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]\d{2,4}){2,4}\b"),
    # Account-ish: a bare run of 8-17 digits that no pattern above claimed.
    ("account_number", r"\b\d{8,17}\b"),
    # A street line: number, words, and a street-type word. Deliberately narrow -- a
    # general address matcher matches most sentences.
    (
        "address",
        r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Z][A-Za-z'-]+\s+){0,3}"
        r"(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Lane|Ln\.?|Drive|Dr\.?|Boulevard|"
        r"Blvd\.?|Way|Close|Crescent|Court|Terrace|Laan|Straat|Weg|Rylaan)\b",
    ),
]

_COMPILED = [(name, re.compile(pattern)) for name, pattern in PATTERNS]


def _luhn_ok(digits: str) -> bool:
    """Standard card checksum. Used to sort card-shaped numbers from other digit runs.

    A failing checksum does not drop the hit -- it recategorises it as an account
    number, which is still a thing to offer for removal. Dropping it would be the
    pattern layer deciding a 16-digit number in an invoice is nothing.
    """
    total, alternate = 0, False
    for char in reversed(digits):
        value = int(char)
        if alternate:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alternate = not alternate
    return total % 10 == 0


def find_pattern_spans(text: str) -> list[Span]:
    """Every deterministic hit, longest first so overlaps resolve to the bigger span."""
    hits: dict[str, str] = {}  # text -> category, first category wins
    taken: list[tuple[int, int]] = []

    for category, pattern in _COMPILED:
        for match in pattern.finditer(text):
            start, end = match.span()
            value = match.group().strip()
            if not value:
                continue
            # Skip anything already inside a longer, earlier-category hit: an email's
            # domain must not also surface as a URL.
            if any(start >= s and end <= e for s, e in taken):
                continue
            # Bound to this match, not to `category`: assigning the loop variable let
            # one failed Luhn check recategorise every later hit of the same pattern,
            # so a real card number after a non-card digit run was reported as an
            # account number (observed while writing this, 2026-08-29).
            found_as = category
            if category == "card_number":
                digits = re.sub(r"\D", "", value)
                found_as = "card_number" if _luhn_ok(digits) else "account_number"
            hits.setdefault(value, found_as)
            taken.append((start, end))

    return [
        Span(text=value, category=category, source="pattern", count=text.count(value),
             contexts=contexts_for(text, value))
        for value, category in sorted(hits.items(), key=lambda kv: -len(kv[0]))
    ]


def contexts_for(text: str, value: str, limit: int = 2, width: int = 40) -> tuple[str, ...]:
    """A few surrounding snippets, so the user can judge a candidate before ticking it."""
    out = []
    for match in re.finditer(re.escape(value), text):
        start = max(0, match.start() - width)
        end = min(len(text), match.end() + width)
        out.append(("…" if start else "") + text[start:end].replace("\n", " ") + ("…" if end < len(text) else ""))
        if len(out) >= limit:
            break
    return tuple(out)


# ── the model layer ──────────────────────────────────────────────────────────


def accept_proposals(
    text: str, proposals: list[str] | list[tuple[str, str]]
) -> tuple[list[Span], list[str]]:
    """Keep model proposals that occur verbatim in the source; return the rest as drops.

    Exact string match, never fuzzy. This is the single rule that removes hallucinated
    spans entirely: a name the model invented cannot be located, so it cannot be
    offered, so the user cannot confirm it. Fuzzy-matching here would reintroduce every
    failure mode it prevents, in exchange for catching typos that the pattern layer and
    the user's own literal list already cover.
    """
    accepted: list[Span] = []
    dropped: list[str] = []
    seen: set[str] = set()

    for raw in proposals:
        # Either a bare string (the fallback chat model) or (text, category) from the
        # tagger, whose categories are worth keeping: the review page groups by them.
        raw, category = raw if isinstance(raw, tuple) else (raw, "model")
        value = (raw or "").strip().strip('"“”')
        if not value or value in seen:
            continue
        seen.add(value)
        if value in text:
            accepted.append(
                Span(text=value, category=category, source="model",
                     count=text.count(value), contexts=contexts_for(text, value))
            )
        else:
            dropped.append(value)

    return accepted, dropped


# ── removal and verification ─────────────────────────────────────────────────


def redact_text(text: str, spans: list[str]) -> tuple[str, dict[str, int]]:
    """Replace every occurrence of every span. Longest first, so nesting cannot orphan.

    Counts are returned per span for the job log: the user is told how many hits each
    confirmed span had, which is also how a "replaced only the first occurrence" bug
    becomes visible rather than invisible.
    """
    counts: dict[str, int] = {}
    for span in sorted(spans, key=len, reverse=True):
        if not span:
            continue
        counts[span] = text.count(span)
        text = text.replace(span, PLACEHOLDER)
    return text, counts


def normalise(text: str) -> str:
    """Casefold, strip accents, collapse whitespace -- the form the verifier compares in.

    "in any casing or whitespace variant" (PLAN.md). A span that survives as `John  Smith`
    across a line break, or as `JOHN SMITH`, has survived; a verifier that only did an
    exact match would pass the document that most needs failing.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).casefold()


def survivors(output_text: str, spans: list[str]) -> list[str]:
    """Which confirmed spans still occur in the output. Empty list means it verified.

    This is the whole verifier for the text path, and half of it for the PDF path. It
    runs on the *produced output*, re-read, never on the plan that produced it.
    """
    haystack = normalise(output_text)
    return [span for span in spans if span.strip() and normalise(span) in haystack]
