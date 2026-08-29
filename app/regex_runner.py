"""Execute one candidate pattern against one set of examples, in a throwaway process.

    echo '{"pattern": "...", "positives": [...], "negatives": [...], "mode": "search"}' \
        | python -m app.regex_runner

Prints one JSON verdict on stdout. Never imports anything from the rest of the app: it
is spawned once per candidate and must start fast, and it is the process that gets
killed when a pattern hangs.

**Why a subprocess at all.** Catastrophic backtracking is not hypothetical for generated
patterns -- `(a+)+$` against a long non-matching string is a classic, and the loop here
asks a model for five patterns at a time. Python's `re` has no timeout and no way to
interrupt a match in progress: not a signal, not a thread, nothing. The only thing that
reliably stops it is killing the process running it, so each candidate gets its own.
"""

import json
import re
import sys


def main() -> None:
    request = json.load(sys.stdin)
    pattern = request["pattern"]
    mode = request.get("mode", "search")

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        print(json.dumps({"ok": False, "error": f"does not compile: {exc}"}))
        return

    run = compiled.fullmatch if mode == "fullmatch" else compiled.search

    results = {"positives": [], "negatives": []}
    for example in request.get("positives", []):
        results["positives"].append(bool(run(example)))
    for example in request.get("negatives", []):
        results["negatives"].append(bool(run(example)))

    print(json.dumps({"ok": True, **results}))


if __name__ == "__main__":
    main()
