"""PLAN.md phase 6b acceptance, plus the drift guard on the vendored masker.

    python tests/phase6_driver.py offline    # masking, the checks, sentinel-hostile input
    python tests/phase6_driver.py live       # a real round trip through :8094 and :8088
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import protect, translation  # noqa: E402

FAILURES = []


def check(condition, message):
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        FAILURES.append(message)


def vendored_masker_unchanged():
    """The copy in app/protect.py is still the file it was copied from.

    Vendoring buys the measurements in that module without a cross-repo import; the price
    is drift, and this is the receipt. When it fails, read the diff and decide -- do not
    just bump the constant.
    """
    source = Path(protect.VENDORED_FROM)
    if not source.is_absolute():
        source = Path(__file__).resolve().parent.parent / source
    if not source.exists():
        print(f"  skip  {source} is not present on this machine")
        return
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    check(actual == protect.VENDORED_FROM_SHA256,
          "app/protect.py matches the sibling it was vendored from"
          + ("" if actual == protect.VENDORED_FROM_SHA256
             else f"\n         upstream is now {actual[:16]}…, we vendored "
                  f"{protect.VENDORED_FROM_SHA256[:16]}…"))


def five_spans_round_trip():
    """A paragraph with 5 protected spans round-trips with all 5 byte-identical."""
    text = ("See \\cite{dang2022} and $x^2$: growth of 12.4% was reported by "
            "finance@example.co.za on 2026-09-15.")
    literal = ["finance@example.co.za"]
    masked, mapping = translation.mask_all(text, literal)

    check(len(mapping) >= 5, f"at least 5 spans protected (got {len(mapping)})")
    check("finance@example.co.za" not in masked, "the email is masked whole, not split")
    check("@example" not in masked,
          "...and its domain is not left behind by the bibtex-key rule")

    restored, lost = protect.unmask(masked, mapping)
    check(restored == text, "the paragraph round-trips byte-identically")
    check(lost == [], f"no span was lost (lost: {lost})")


def sentinel_hostile():
    """Sentinels against CJK punctuation, at the very start, and at the very end."""
    text = "《Hy-MT2》，见 finance@example.co.za。"
    masked, mapping = translation.mask_all(text, ["Hy-MT2", "finance@example.co.za"])
    restored, lost = protect.unmask(masked, mapping)
    check(restored == text and not lost,
          f"a span between CJK brackets and one before a full stop round-trip ({masked})")

    text = "12.4% is the figure, and the reference is \\cite{x}"
    masked, mapping = translation.mask_all(text, [])
    check(masked.startswith("Z"), f"a span at position 0 masks ({masked[:12]}…)")
    check(masked.rstrip().endswith("Z"), "a span at the very end masks")
    restored, lost = protect.unmask(masked, mapping)
    check(restored == text and not lost, "both round-trip")

    # And the failure that must stay visible rather than being patched: a model that
    # mangles a sentinel loses the span, and the check has to say so.
    mangled = masked.replace("Z0Z", "Z 0 Z", 1)          # whitespace drift: recoverable
    recovered, _ = protect.unmask(mangled, mapping)
    check("12.4" in recovered, "whitespace inside a sentinel is tolerated on restore")

    broken = masked.replace("Z0Z", "0", 1)               # flank eaten: not recoverable
    recovered, _ = protect.unmask(broken, mapping)
    verdict = translation.check_spans(text, recovered, mapping)
    check(not verdict.ok, f"a destroyed sentinel fails the span check ({verdict.detail[:60]})")


def half_translation():
    """A paragraph the model half-translates is caught by the script check."""
    source = "我们使用 LSTM-GARCH 规范估计条件方差模型，并在日频序列上验证。"
    half = "We use the LSTM-GARCH specification，并在日频序列上验证。"
    verdict = translation.check_script("en", half, {})
    check(not verdict.ok, f"half-translated output is caught ({verdict.detail})")

    whole = "We use the LSTM-GARCH specification and validate it on a daily series."
    check(translation.check_script("en", whole, {}).ok, "fully translated output passes")

    # The inverse direction, and the protected-span exemption that makes it usable.
    check(translation.check_script("zh", "我们在 S&P 500 上验证。", {"0": "S&P 500"}).ok,
          "Latin inside a protected span does not fail an en→zh translation")


def other_checks():
    check(not translation.check_passthrough("hello world", "hello world").ok,
          "an echoed input fails the passthrough check")
    check(translation.check_passthrough("hello world", "bonjour").ok,
          "a real translation passes it")

    before = "Growth was 12.4% (Dang and Ullah, 2022)."
    check(translation.check_untouched(before, "Growth reached 12.4% (Dang and Ullah, 2022).").ok,
          "a rewrite that preserves the number and citation passes")
    check(not translation.check_untouched(before, "Growth reached 12.5% (Dang and Ullah, 2022).").ok,
          "a rewrite that edits a number fails — the one thing a style guide promises")
    check(not translation.check_untouched(before, "Growth was 12.4% (Smith, 2019).").ok,
          "...and so does one that invents a citation")

    check(len(translation.split_chunks("a\n\nb\n\nc")) == 3, "paragraphs split on blank lines")
    check(translation.detect_language("我们使用模型") == "zh"
          and translation.detect_language("We use a model") == "en",
          "language detection tells the two scripts apart")


def live():
    """A real round trip, both modes. Needs :8094 and :8088 up."""
    import urllib.error

    from app.tools.translate import _ask, _translate_prompt
    from app import config

    text = ("请在 2026-09-15 前将 12.4% 的余额付至 finance@example.co.za，"
            "并参阅 \\cite{dang2022}。")
    masked, mapping = translation.mask_all(text, ["finance@example.co.za"])
    try:
        raw = _ask(config.TRANSLATE_ENDPOINT, _translate_prompt(masked, "en"))
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  skip  {config.TRANSLATE_ENDPOINT} not answering ({exc}); "
              f"`systemctl --user start llama-hymt2`")
        return

    restored, _ = protect.unmask(raw, mapping)
    print(f"        {restored}")
    for verdict in translation.run_checks(text, restored, mapping, "en", "translate"):
        check(verdict.ok, f"{verdict.name}: {verdict.detail}")


if __name__ == "__main__":
    if sys.argv[1] == "live":
        print(">> a real round trip through the translation model")
        live()
    else:
        print(">> the vendored masker has not drifted")
        vendored_masker_unchanged()
        print("\n>> five protected spans round-trip byte-identically")
        five_spans_round_trip()
        print("\n>> sentinel-hostile input round-trips or fails visibly")
        sentinel_hostile()
        print("\n>> a half-translated paragraph is caught")
        half_translation()
        print("\n>> passthrough, numbers and citations, chunking, detection")
        other_checks()

    print()
    if FAILURES:
        print(f">> phase 6b acceptance: {len(FAILURES)} FAILED")
        sys.exit(1)
    print(">> phase 6b acceptance: PASS")
