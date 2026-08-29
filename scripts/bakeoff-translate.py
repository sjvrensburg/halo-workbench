#!/usr/bin/env python3
"""PLAN.md phase 6a: run the translation bake-off and write the raw results.

    scripts/bakeoff-translate.py <name>=<url> [<name>=<url> ...] [--out FILE]

Each candidate gets the same ten paragraphs (tests/bakeoff/cases.json), both
directions, each with 3-5 protected spans, and the same prompt. What is measured is
what 6b will check on every request anyway:

  * every protected span present, once, and in the original order
  * a residual-script check -- Han characters left in an English translation, or an
    English translation that is mostly Han, means the model answered in the wrong
    language or echoed the source
  * wall-clock per paragraph

Adequacy is judged by eye afterwards, from the transcript this writes. That part is not
automatable and the report says which parts were and were not.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "tests/bakeoff/cases.json").read_text())

HAN = re.compile(r"[一-鿿]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")

LANGUAGE = {"en->zh": ("English", "Chinese", "中文"), "zh->en": ("Chinese", "English", "English")}


# ── protecting spans: two mechanisms, because the models support different ones ──
#
# INSTRUCT: list the spans as "X translates to X" and tell the model to keep them. This
# is Hy-MT2's own documented "Terminology" shape and the native lever this phase exists
# to evaluate. It requires a model that follows instructions at all.
#
# SENTINEL: substitute each span for a marker, translate, restore. MiLMMT-46 has no
# chat template (`{% for message in messages %}{{ message.content }}{% endfor %}`) and
# no instruction training -- it is a completion model with a fixed template -- so this
# is the only mechanism it can use.
#
# The ZnZ form is not a guess: ../anti-watermark-mcp measured seven sentinel forms over
# a round trip through Chinese and found `[[0]]` scored 0/21 on this very 1B (it
# collapses the doubled brackets) while `Z0Z` scored 21/21. Reused rather than
# re-derived.
SENTINEL = "Z{}Z"
SENTINEL_FIND = re.compile(r"(?<![0-9A-Ya-y])[Zz]\s*(\d+)\s*[Zz](?![0-9A-Ya-y])")

MILMMT_TEMPLATE = "Translate this from {src} to {tgt}:\n{src}: {text}\n{tgt}:"
# MiLMMT's trained language names are load-bearing: "Chinese (Simplified)", not
# "Chinese" (../anti-watermark-mcp translation.py).
MILMMT_NAME = {"English": "English", "Chinese": "Chinese (Simplified)"}


def mask(case: dict) -> tuple[str, dict[int, str]]:
    """Replace protected spans with sentinels, longest first so nesting cannot orphan."""
    text = case["text"]
    mapping = {}
    for index, span in enumerate(sorted(case["protect"], key=len, reverse=True)):
        if span in text:
            text = text.replace(span, SENTINEL.format(index))
            mapping[index] = span
    return text, mapping


def unmask(text: str, mapping: dict[int, str]) -> tuple[str, int]:
    """Restore sentinels. Returns the text and the number of orphans left behind."""
    restored = SENTINEL_FIND.sub(
        lambda m: mapping.get(int(m.group(1)), m.group(0)), text)
    orphans = len(SENTINEL_FIND.findall(restored))
    return restored, orphans


def build_prompt(case: dict) -> str:
    """Hy-MT2's documented Terminology + Structured-Data instructions, used for everyone.

    The same prompt goes to every candidate, because the question is which MODEL is
    better at this, not which prompt is. It is Hy-MT2's own documented shape (its card's
    "Terminology" and "Structured Data 1" rows) since that is the native lever the phase
    is here to evaluate; a model without trained support for it simply does worse, which
    is the finding.
    """
    _, target, _ = LANGUAGE[case["direction"]]
    protected = "\n".join(f"{span} translates to {span}" for span in case["protect"])
    return (
        "Reference the following translations:\n"
        f"{protected}\n\n"
        f"Translate the following text into {target}. Keep every term above exactly as "
        f"written, in the same order. Note that you must ONLY output the translated "
        f"result without any additional explanation:\n\n"
        f"{case['text']}"
    )


def chat(url: str, prompt: str, timeout: int = 300) -> tuple[str, float]:
    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 1024,
        "stream": False,
    }).encode()
    request = urllib.request.Request(f"{url}/v1/chat/completions", data=payload,
                                     headers={"content-type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    elapsed = time.time() - started
    message = (body.get("choices") or [{}])[0].get("message", {})
    # Thinking models put everything in reasoning_content and leave content empty
    # (measured 2026-08-29 on :8092) -- record that rather than scoring it as a refusal.
    return (message.get("content") or "").strip(), elapsed


def completion(url: str, prompt: str, timeout: int = 300) -> tuple[str, float]:
    """/v1/completions, for a base-style model with no chat template."""
    payload = json.dumps({
        "prompt": prompt,
        "temperature": 0,
        "max_tokens": 1024,
        "stream": False,
        # The template puts the answer on one line and the model will happily start the
        # next example if not stopped.
        "stop": ["\nTranslate this from", "\nEnglish:", "\nChinese"],
    }).encode()
    request = urllib.request.Request(f"{url}/v1/completions", data=payload,
                                     headers={"content-type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    return (body.get("choices") or [{}])[0].get("text", "").strip(), time.time() - started


def run_case(url: str, style: str, case: dict) -> tuple[str, float, int]:
    """Translate one case in the given style. Returns (output, seconds, orphan sentinels)."""
    source, target, _ = LANGUAGE[case["direction"]]

    if style == "completion-sentinel":
        masked, mapping = mask(case)
        prompt = MILMMT_TEMPLATE.format(src=MILMMT_NAME[source], tgt=MILMMT_NAME[target],
                                        text=masked)
        raw, elapsed = completion(url, prompt)
        restored, orphans = unmask(raw, mapping)
        return restored, elapsed, orphans

    if style == "chat-sentinel":
        masked, mapping = mask(case)
        prompt = (f"Translate the following text into {target}. The markers such as Z0Z "
                  f"are placeholders: reproduce each one exactly as written, in the "
                  f"right place. Output only the translation:\n\n{masked}")
        raw, elapsed = chat(url, prompt)
        restored, orphans = unmask(raw, mapping)
        return restored, elapsed, orphans

    output, elapsed = chat(url, build_prompt(case))
    return output, elapsed, 0


def check(case: dict, output: str) -> dict:
    """Spans present the RIGHT NUMBER OF TIMES, plus residual script. Order is advisory.

    Two corrections to PLAN.md 6b's wording, both forced by the first run of this
    bake-off on 2026-08-29:

    * "present once" is wrong. `--parallel` occurs twice in the release-note case, and a
      faithful translation contains it twice. The check is count-in-output equals
      count-in-source.
    * "in order" is wrong. Chinese reorders: "Upgrade to b10582 before enabling
      draft-mtp" comes back as "在启用 draft-mtp 之前，请升级到 b10582", which is a correct
      translation with the two spans swapped. Requiring order would fail good output,
      which is worse than not checking it -- so order is recorded and reported, and does
      not decide a pass.
    """
    positions = []
    missing, wrong_count = [], []
    for span in case["protect"]:
        want = case["text"].count(span)
        got = output.count(span)
        if got == 0:
            missing.append(span)
        elif got != want:
            wrong_count.append(f"{span!r} {got}x, source has {want}x")
        if got:
            positions.append(output.index(span))

    in_order = positions == sorted(positions)

    han = len(HAN.findall(output))
    latin = len(LATIN_WORD.findall(output))
    protected_latin = sum(len(LATIN_WORD.findall(span)) for span in case["protect"])

    if case["direction"] == "en->zh":
        # Chinese out: Han must dominate. Latin beyond the protected spans means English
        # survived untranslated.
        script_ok = han > 0 and latin <= protected_latin + 3
        script_note = f"{han} Han, {latin} latin words ({protected_latin} protected)"
    else:
        # English out: any Han beyond what the protected spans themselves contain is
        # source text that was never translated.
        protected_han = sum(len(HAN.findall(span)) for span in case["protect"])
        script_ok = han <= protected_han
        script_note = f"{han} Han ({protected_han} protected), {latin} latin words"

    return {
        "missing": missing,
        "wrong_count": wrong_count,
        "in_order": in_order,          # reported, deliberately not part of `passes`
        "script_ok": script_ok,
        "script_note": script_note,
        "empty": not output,
        "passes": not missing and not wrong_count and script_ok and bool(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates", nargs="+",
                        help="name=url[:style] where style is chat-instruct (default), "
                             "chat-sentinel, completion-sentinel")
    parser.add_argument("--out", default="tests/bakeoff/results.json")
    args = parser.parse_args()

    results = {}
    for candidate in args.candidates:
        name, _, spec = candidate.partition("=")
        url, _, style = spec.partition("|")
        style = style or "chat-instruct"
        print(f"\n=== {name} ({url}, {style}) ===", flush=True)
        rows = []
        for case in CASES:
            try:
                output, elapsed, orphans = run_case(url, style, case)
            except Exception as exc:                     # noqa: BLE001 - report, continue
                print(f"  {case['id']:16} ERROR {exc}", flush=True)
                rows.append({"id": case["id"], "direction": case["direction"],
                             "error": str(exc), "passes": False})
                continue

            verdict = check(case, output)
            if orphans:
                verdict["passes"] = False
            rows.append({"id": case["id"], "direction": case["direction"],
                         "output": output, "seconds": round(elapsed, 1),
                         "orphan_sentinels": orphans, **verdict})
            flag = "ok  " if verdict["passes"] else "FAIL"
            detail = []
            if verdict["empty"]:
                detail.append("empty reply")
            if verdict["missing"]:
                detail.append(f"lost {verdict['missing']}")
            if verdict["wrong_count"]:
                detail.append("; ".join(verdict["wrong_count"]))
            if orphans:
                detail.append(f"{orphans} sentinel(s) never restored")
            if not verdict["in_order"]:
                detail.append("(reordered — not a failure)")
            if not verdict["script_ok"]:
                detail.append(f"script: {verdict['script_note']}")
            print(f"  {flag} {case['id']:16} {elapsed:5.1f}s  {'; '.join(detail)}", flush=True)

        passed = sum(1 for row in rows if row.get("passes"))
        total_time = sum(row.get("seconds", 0) for row in rows)
        print(f"  -> {passed}/{len(CASES)} mechanically clean, {total_time:.0f}s total",
              flush=True)
        results[name] = {"url": url, "style": style, "rows": rows, "passed": passed,
                         "seconds": round(total_time, 1)}

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
