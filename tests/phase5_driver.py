"""PLAN.md phase 5 acceptance: ten cases, and the backtracking case.

    python tests/phase5_driver.py cases      # needs :8092 -- runs the real loop
    python tests/phase5_driver.py offline    # no model: the runner, timeout and parsing

The cases are the acceptance criterion: the loop must find a passing pattern for at
least eight, and for the rest it must SAY it found nothing rather than return something
untested. Note what is being measured -- not whether the model writes good regexes, but
whether the loop only ever returns an executed one.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import regex_synth  # noqa: E402
from app.tools.regex import ask_model  # noqa: E402

CASES = [
    ("ISO dates", ["2026-08-29", "1999-01-01"], ["29/08/2026", "2026-8-9", "hello"], "fullmatch"),
    ("email addresses", ["a@b.co", "anna.meyer@example.co.za"], ["a@b", "@b.co", "a b@c.co"], "fullmatch"),
    ("South African ID", ["8801015009087"], ["880101500908", "88010150090871", "abc"], "fullmatch"),
    ("hex colours", ["#fff", "#A1B2C3"], ["#ffff", "fff", "#gggggg"], "fullmatch"),
    ("integers, optionally signed", ["42", "-7", "+0"], ["4.2", "--7", "seven"], "fullmatch"),
    ("words ending in -ing", ["running", "sing"], ["ringed", "ing "], "fullmatch"),
    ("a line starting with a tab", ["\tindented"], ["    spaces", "no indent"], "search"),
    ("IPv4 dotted quad", ["192.168.0.1", "8.8.8.8"], ["999.1.1.1", "1.2.3", "1.2.3.4.5"], "fullmatch"),
    ("markdown h2 heading", ["## Title", "##  Spaced"], ["# Title", "###Title", "Title"], "search"),
    ("quoted string", ['"hello"', '"a b"'], ['"unclosed', 'plain'], "fullmatch"),
]


def run_cases() -> int:
    passed, honest, wrong = 0, 0, 0
    for name, positives, negatives, mode in CASES:
        started = time.time()
        result = regex_synth.synthesise(positives, negatives, name, mode,
                                        ask_model=ask_model)
        elapsed = time.time() - started
        winner = result["winner"]

        if winner:
            # Re-run the winner independently: the loop says it passed, and this asserts
            # that claim rather than trusting the record it wrote.
            check = regex_synth.test_pattern(winner.pattern, positives, negatives, mode)
            if check.passes:
                passed += 1
                print(f"  ok    {name:32} {winner.pattern!r} "
                      f"({result['rounds_used']} round(s), {elapsed:.0f}s)")
            else:
                wrong += 1
                print(f"  WRONG {name:32} returned a pattern that does not pass: "
                      f"{winner.pattern!r}")
        else:
            honest += 1
            print(f"  none  {name:32} no pattern found in {result['rounds_used']} rounds, "
                  f"{len(result['tried'])} tried ({elapsed:.0f}s)")

    print(f"\n  {passed}/{len(CASES)} solved, {honest} honestly unsolved, {wrong} wrong")
    # The wrong count is the one that must be zero: a returned pattern that does not
    # pass the user's own examples is the single failure this design exists to prevent.
    if wrong:
        print("  FAIL: a returned pattern did not pass the examples")
        return 1
    if passed < 8:
        print(f"  FAIL: {passed} of 10 solved, acceptance asks for at least 8")
        return 1
    print("  PASS")
    return 0


def run_offline() -> int:
    failures = []

    def check(condition, message):
        print(("  ok   " if condition else "  FAIL ") + message)
        if not condition:
            failures.append(message)

    verdict = regex_synth.test_pattern(r"^\d{4}-\d{2}-\d{2}$", ["2026-08-29"], ["29/08/2026"],
                                       "fullmatch")
    check(verdict.passes, "a correct pattern passes and is marked as such")

    # The acceptance case: a prompt-engineered backtracker against a long non-match.
    started = time.time()
    verdict = regex_synth.test_pattern(r"(a+)+$", ["a" * 40 + "b"], [])
    elapsed = time.time() - started
    check(not verdict.passes and "backtracking" in verdict.error,
          f"(a+)+$ on a long non-match fails that candidate on timeout ({elapsed:.1f}s)")
    check(elapsed < regex_synth.TIMEOUT_SECONDS + 2,
          f"...and the request is not held hostage by it ({elapsed:.1f}s)")

    verdict = regex_synth.test_pattern("[unclosed", ["x"], [])
    check(not verdict.ok and "does not compile" in verdict.error,
          "an uncompilable pattern is reported, not raised")

    check(regex_synth.parse_candidates("1. ```\n^a+$\n```\n- /b+/g\n^a+$\n") == ["^a+$", "b+"],
          "fences, numbering, /.../ delimiters and duplicates are stripped")

    # The loop must never return a pattern it has not executed -- proved with a model
    # stub that answers with a pattern that fails, then one that works.
    answers = iter(["^\\d+$\nnope(", "^[0-9]+$"])
    result = regex_synth.synthesise(["42"], ["4.2"], "", "fullmatch",
                                    ask_model=lambda _: next(answers))
    check(result["winner"] is not None and result["winner"].passes,
          "the loop returns a candidate only after it passes")
    check(result["rounds_used"] == 1,
          f"and stops at the first passing round (used {result['rounds_used']})")

    answers = iter(["nope(" for _ in range(regex_synth.MAX_ROUNDS)])
    result = regex_synth.synthesise(["42"], [], "", "fullmatch",
                                    ask_model=lambda _: next(answers))
    check(result["winner"] is None and result["rounds_used"] == regex_synth.MAX_ROUNDS,
          "a model that never produces a valid pattern yields no winner, not a guess")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_offline() if sys.argv[1] == "offline" else run_cases())
