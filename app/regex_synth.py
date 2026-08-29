"""Generate-and-test for regular expressions. The user's examples are the verifier.

That is the entire design (PLAN.md phase 5): a pattern is never returned unless it has
been executed against every example given, and the page shows the execution. A regex
that was not run is not an answer, it is a guess with syntax highlighting.

The model's job is only to propose. It gets five tries per round and is told exactly
which examples its last attempt got wrong, which is the part a chat interface cannot do
for you and the reason this is a loop rather than a prompt.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Per candidate. A pattern that needs longer than this on the user's own examples is
# not a pattern anyone wants: the examples are short, so a second is already three
# orders of magnitude of slack.
TIMEOUT_SECONDS = 1.0

CANDIDATES_PER_ROUND = 5
MAX_ROUNDS = 6


@dataclass
class Verdict:
    """What executing one candidate against the examples actually did."""

    pattern: str
    ok: bool = False                     # compiled and ran
    error: str = ""
    positives: list[bool] = field(default_factory=list)
    negatives: list[bool] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        """Every positive matched and no negative did. The only definition of success."""
        return self.ok and all(self.positives) and not any(self.negatives)

    @property
    def score(self) -> int:
        """How many examples it got right, for ranking the near misses."""
        if not self.ok:
            return -1
        return sum(self.positives) + sum(not hit for hit in self.negatives)

    def failures(self, positives: list[str], negatives: list[str]) -> list[str]:
        """The examples it got wrong, phrased for the next prompt and for the page."""
        if not self.ok:
            return [self.error]
        wrong = [f"should match but does not: {example!r}"
                 for example, hit in zip(positives, self.positives) if not hit]
        wrong += [f"should NOT match but does: {example!r}"
                  for example, hit in zip(negatives, self.negatives) if hit]
        return wrong


def test_pattern(pattern: str, positives: list[str], negatives: list[str],
                 mode: str = "search") -> Verdict:
    """Run one candidate in its own process, and treat a timeout as that candidate's fault.

    A hung candidate must fail the candidate, never the request (PLAN.md phase 5): the
    loop goes on to the next of the five, and the user still gets an answer.
    """
    request = json.dumps({"pattern": pattern, "positives": positives,
                          "negatives": negatives, "mode": mode})
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.regex_runner"],
            input=request,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return Verdict(
            pattern=pattern,
            error=f"took longer than {TIMEOUT_SECONDS:g}s on these examples — "
                  f"catastrophic backtracking, rejected",
        )

    if result.returncode != 0:
        return Verdict(pattern=pattern, error=f"runner failed: {result.stderr.strip()[:200]}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Verdict(pattern=pattern, error="runner produced no verdict")

    if not payload.get("ok"):
        return Verdict(pattern=pattern, error=payload.get("error", "unknown"))

    return Verdict(pattern=pattern, ok=True,
                   positives=payload["positives"], negatives=payload["negatives"])


# ── talking to the model ─────────────────────────────────────────────────────

_FENCE = re.compile(r"^\s*(?:```[a-z]*|`)|(?:```|`)\s*$")
_NUMBERING = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+")


def parse_candidates(text: str, limit: int = CANDIDATES_PER_ROUND) -> list[str]:
    """One pattern per line, with the wrappers a model adds anyway stripped off.

    Deliberately forgiving about formatting and completely unforgiving about content:
    whatever survives here is executed before it is shown to anyone, so a mangled line
    costs one wasted candidate rather than a wrong answer.
    """
    seen: list[str] = []
    for line in (text or "").splitlines():
        line = _NUMBERING.sub("", line.strip())
        line = _FENCE.sub("", line).strip()
        if line.startswith("/") and line.rstrip("gimsuy").endswith("/"):
            line = line.rstrip("gimsuy")[1:-1]     # /.../ from a JS-flavoured answer
        if line and line not in seen:
            seen.append(line)
        if len(seen) >= limit:
            break
    return seen


def first_prompt(positives: list[str], negatives: list[str], description: str,
                 mode: str) -> str:
    verb = "fully match" if mode == "fullmatch" else "be found in"
    return (
        f"Write {CANDIDATES_PER_ROUND} different Python `re` regular expressions.\n\n"
        f"Each one must {verb} every string in MATCH, and must not {verb} any string in "
        f"REJECT.\n\n"
        f"MATCH:\n" + "\n".join(f"  {example!r}" for example in positives) + "\n\n"
        f"REJECT:\n" + ("\n".join(f"  {example!r}" for example in negatives) or "  (none)")
        + (f"\n\nThe user describes it as: {description}\n" if description else "\n")
        + f"\nOutput exactly {CANDIDATES_PER_ROUND} lines, one regular expression per "
        f"line. No numbering, no code fences, no explanation, no delimiters."
    )


def retry_prompt(positives: list[str], negatives: list[str], description: str, mode: str,
                 attempts: list[Verdict]) -> str:
    """The same request, plus exactly what the last round got wrong.

    This is the whole value of the loop over a chat window: the feedback is executed
    fact, not the user's description of what went wrong.
    """
    report = []
    for verdict in attempts[:CANDIDATES_PER_ROUND]:
        problems = verdict.failures(positives, negatives)
        report.append(f"  {verdict.pattern}\n    " + "\n    ".join(problems[:4]))

    return (
        first_prompt(positives, negatives, description, mode)
        + "\n\nThese attempts were executed against the examples and failed:\n"
        + "\n".join(report)
        + "\n\nDo not repeat them. Fix the specific failures listed."
    )


def overfit_warnings(pattern: str, positives: list[str], negatives: list[str]) -> list[str]:
    """Reasons to distrust a pattern that legitimately passed every example.

    OBSERVED 2026-08-29 in the acceptance run: given one positive example, the model
    answered `^8801015009087$` -- the example itself, anchored. It passes, honestly, and
    it generalises to nothing. The verifier cannot catch this, because the pattern does
    exactly what the user asked and the user asked for too little.

    So the page says it. This is the same rule as the redaction tool's "candidate"
    wording: where the check cannot cover a property the user assumes, say so where they
    will read it, rather than letting a green tick imply it.
    """
    warnings = []

    embedded = [example for example in positives
                if len(example) > 3 and example in pattern]
    if embedded:
        warnings.append(
            "the pattern contains " + ", ".join(repr(e) for e in embedded)
            + " literally, so it may match little beyond the example(s) you gave"
        )
    if len(positives) < 2:
        warnings.append(
            "only one example had to match, which almost any pattern can satisfy — "
            "add a second that is different in the way that matters"
        )
    if not negatives:
        warnings.append(
            "nothing had to be rejected, so `.*` would have passed too — add a string "
            "that must NOT match"
        )
    return warnings


def synthesise(positives: list[str], negatives: list[str], description: str, mode: str,
               ask_model, on_round=None) -> dict:
    """Run the loop. `ask_model(prompt) -> str` is injected so this stays testable.

    Returns the winner (or None), the near misses, and every round's verdicts -- the
    page shows what was tried, because "no pattern found" is only trustworthy if you
    can see the attempts.
    """
    tried: list[Verdict] = []
    rounds: list[list[Verdict]] = []

    for round_number in range(1, MAX_ROUNDS + 1):
        prompt = (first_prompt(positives, negatives, description, mode)
                  if not tried else
                  retry_prompt(positives, negatives, description, mode, sorted(
                      tried, key=lambda v: -v.score)))

        candidates = parse_candidates(ask_model(prompt))
        verdicts = [test_pattern(candidate, positives, negatives, mode)
                    for candidate in candidates
                    if candidate not in {v.pattern for v in tried}]
        rounds.append(verdicts)
        tried.extend(verdicts)

        if on_round:
            on_round(round_number, verdicts)

        winner = next((v for v in verdicts if v.passes), None)
        if winner:
            return {"winner": winner, "rounds": rounds, "tried": tried,
                    "rounds_used": round_number}

    return {"winner": None, "rounds": rounds, "tried": tried, "rounds_used": MAX_ROUNDS}
