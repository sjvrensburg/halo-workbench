"""PLAN.md phase 4 acceptance, the parts that need no GPU. Run via tests/phase4_acceptance.sh.

The pattern layer, the verifier and the PDF path are all deterministic and offline, so
they are checked here directly rather than through the browser. The model layer is
exercised separately (it needs :8092) -- but note that the rule that makes the model
safe, "a proposal that does not occur verbatim is discarded", is pure and is tested
here without a model at all.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from app import pdf_redaction, redaction  # noqa: E402

FAILURES = []


def check(condition: bool, message: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        FAILURES.append(message)


INVOICE = """INVOICE 2026-0042            Acme Holdings (Pty) Ltd
From:  Anna Meyer <anna.meyer@example.co.za>   tel +27 82 123 4567
Bill to: Johannes van der Merwe, 42 Kloof Street, Cape Town 8001
ID number 8801015009087.  Account 6234567890123456.
Card 4111 1111 1111 1111.  IBAN GB82 WEST 1234 5698 7654 32.
Due 2026-09-15.  Queries: 021-555-0123 or https://billing.example.co.za/inv/42
Anna Meyer signs off; Anna Meyer is also the contact for Acme Holdings.
"""


def pattern_layer() -> None:
    """A real invoice's identifiers are caught by the deterministic layer alone."""
    spans = {s.text: s.category for s in redaction.find_pattern_spans(INVOICE)}
    for wanted, category in [
        ("anna.meyer@example.co.za", "email"),
        ("+27 82 123 4567", "phone"),
        ("8801015009087", "id_number"),
        ("6234567890123456", "account_number"),
        ("4111 1111 1111 1111", "card_number"),
        ("GB82 WEST 1234 5698 7654 32", "iban"),
        ("42 Kloof Street", "address"),
        ("021-555-0123", "phone"),
        ("2026-09-15", "date"),
        ("https://billing.example.co.za/inv/42", "url"),
    ]:
        check(spans.get(wanted) == category,
              f"{category:15} {wanted!r}" + ("" if spans.get(wanted) == category
                                             else f" (got {spans.get(wanted)!r})"))
    check(all(not s.startswith("Anna") for s in spans),
          "names are NOT claimed by the pattern layer (that is the model's and your list's job)")


def model_proposals() -> None:
    """A proposal that does not occur verbatim is dropped, and the drop is visible."""
    accepted, dropped = redaction.accept_proposals(
        INVOICE, ["Anna Meyer", "Acme Holdings", "Sipho Ndlovu", "anna meyer"]
    )
    check([s.text for s in accepted] == ["Anna Meyer", "Acme Holdings"],
          "verbatim proposals are accepted")
    check(dropped == ["Sipho Ndlovu", "anna meyer"],
          f"non-verbatim proposals are dropped, case included (got {dropped})")


def verifier_catches_broken_redactor() -> None:
    """The verifier must catch a redactor that replaces only the first occurrence."""
    spans = ["Anna Meyer", "Acme Holdings"]
    honest, _ = redaction.redact_text(INVOICE, spans)
    check(redaction.survivors(honest, spans) == [], "an honest redaction verifies")

    broken = INVOICE
    for span in spans:
        broken = broken.replace(span, redaction.PLACEHOLDER, 1)   # <- the bug
    check(redaction.survivors(broken, spans) == spans,
          "replacing only the first occurrence is caught, for every span")

    # And the variants the plan names explicitly: casing and whitespace.
    check(redaction.survivors("... ANNA   MEYER ...", ["Anna Meyer"]) == ["Anna Meyer"],
          "a span surviving in different casing and spacing is caught")


def _write_pdf(path: Path, lines: list[str], author: str | None = None) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 18
    if author:
        doc.set_metadata({"author": author, "title": f"Invoice for {author}"})
    doc.save(path)
    doc.close()


def pdf_path(tmp: Path) -> None:
    """pdftotext on the produced PDF finds none of the redacted strings."""
    source = tmp / "invoice.pdf"
    _write_pdf(source, [
        "INVOICE 2026-0042 for Anna Meyer",
        "Email anna.meyer@example.co.za, tel +27 82 123 4567",
        "Anna Meyer is also the contact person.",
    ], author="Anna Meyer")

    spans = ["Anna Meyer", "anna.meyer@example.co.za", "+27 82 123 4567"]
    output = tmp / "invoice.redaction-candidate.pdf"
    counts = pdf_redaction.redact_pdf(source, output, spans)
    check(counts["Anna Meyer"] == 2, f"both occurrences located (got {counts['Anna Meyer']})")

    text = pdf_redaction.pdftotext(output)
    check(text != "", "pdftotext ran on the output")
    check(redaction.survivors(text, spans) == [],
          "pdftotext on the OUTPUT finds none of the redacted strings")
    check(redaction.survivors(pdf_redaction.extract_text(output), spans) == [],
          "pymupdf's own extraction agrees")
    check(redaction.survivors(pdf_redaction.metadata_values(output), spans) == [],
          "output metadata (/Info, XMP) contains none of them")

    # And the control: prove the test would have failed for a covering rectangle.
    covered = tmp / "covered.pdf"
    doc = pymupdf.open(source)
    for page in doc:
        for rect in page.search_for("Anna Meyer"):
            page.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(covered)
    doc.close()
    check("Anna Meyer" in pdf_redaction.pdftotext(covered),
          "control: a black rectangle drawn over the text leaves it recoverable")


def pdf_unlocatable(tmp: Path) -> None:
    """A span that cannot be turned into rectangles fails the job instead of being skipped.

    PROBED 2026-08-29 on PyMuPDF 1.28.2: no PDF this test can synthesise actually
    reaches that branch. Text split across a line, hyphenated, rendered with ligatures,
    rotated 90 degrees, and drawn inside a form XObject are all located correctly --
    which is good news about the library and bad news for testing the safety net
    through it. So the decision function is tested on its own, and the *observable*
    guarantee (a partially redacted file is never returned) is tested on a real file
    below.
    """
    check(pdf_redaction.shortfall_messages({"Anna Meyer": (2, 2)}) == [],
          "two occurrences, two rectangles: no shortfall")
    check(pdf_redaction.shortfall_messages({"Anna Meyer": (1, 2)}) == [],
          "one occurrence split across two lines gives two rectangles: not a shortfall")
    messages = pdf_redaction.shortfall_messages({"Anna Meyer": (2, 1), "ok": (1, 1)})
    check(len(messages) == 1 and "Anna Meyer" in messages[0],
          f"two occurrences, one rectangle: reported ({messages})")


def pdf_partial_is_caught(tmp: Path) -> None:
    """The PDF analogue of the broken redactor: only the first occurrence removed."""
    source = tmp / "twice.pdf"
    _write_pdf(source, ["Anna Meyer signs.", "Countersigned by Anna Meyer."])

    partial = tmp / "partial.pdf"
    doc = pymupdf.open(source)
    page = doc[0]
    page.add_redact_annot(page.search_for("Anna Meyer")[0], fill=(0, 0, 0))  # <- only [0]
    page.apply_redactions()
    doc.save(partial)
    doc.close()

    left = redaction.survivors(pdf_redaction.pdftotext(partial), ["Anna Meyer"])
    check(left == ["Anna Meyer"],
          "a PDF with one of two occurrences removed is caught by the output verifier")

    # And the same document redacted properly passes, so the check is not vacuous.
    good = tmp / "good.pdf"
    pdf_redaction.redact_pdf(source, good, ["Anna Meyer"])
    check(redaction.survivors(pdf_redaction.pdftotext(good), ["Anna Meyer"]) == [],
          "the same document redacted properly verifies")


def pdf_scanned(tmp: Path) -> None:
    """A scanned image-only PDF is refused, never silently returned unchanged."""
    source = tmp / "scan.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 200))
    pixmap.set_rect(pixmap.irect, (200, 200, 200))
    page.insert_image(pymupdf.Rect(50, 50, 450, 250), pixmap=pixmap)
    doc.save(source)
    doc.close()

    usable, explanation = pdf_redaction.text_layer_report(source)
    check(not usable, "a scan is reported as having no usable text layer")
    check("marker" in explanation, "and the message routes the user to OCR rather than refusing flatly")
    try:
        pdf_redaction.redact_pdf(source, tmp / "scan-out.pdf", ["anything"])
        check(False, "redacting a scan raised nothing")
    except pdf_redaction.NoTextLayer:
        check(True, "redacting a scan raises NoTextLayer")


def tagger() -> None:
    """The recall layer: openai/privacy-filter, on the CPU, proposing what patterns miss.

    Skipped with a message rather than failed when the model is not installed -- the
    tool degrades to patterns plus the fallback and says so on the page, and a checkout
    without ~1 GB of weights should still be able to run this suite.
    """
    from app import pii_tagger

    try:
        proposals, _ = pii_tagger.propose(INVOICE)
    except pii_tagger.TaggerUnavailable as exc:
        print(f"  skip  {exc}")
        print("        (scripts/fetch-privacy-filter.sh installs it)")
        return

    found = {value: category for value, category in proposals}
    check(any("Anna Meyer" in v for v in found),
          f"proposes the person the pattern layer cannot see (got {sorted(found)[:6]})")
    check(all("\n" not in v for v in found),
          "no proposal spans a line break — those are split, or the PDF path could not "
          "locate them and would fail the job")
    accepted, dropped = redaction.accept_proposals(INVOICE, proposals)
    check(dropped == [],
          f"every proposal is a verbatim substring of the source (dropped: {dropped})")
    check(all(s.source == "model" for s in accepted),
          "and they are labelled as model proposals, not pattern hits")


def decoder() -> None:
    """BIOES decoding, on hand-built tags, so a bad span never depends on a live model."""
    from app.pii_tagger import _decode

    text = "Call Anna Meyer at Acme today"
    spans = _decode(
        ["O", "B-private_person", "E-private_person", "O", "S-org"],
        [0.99, 0.98, 0.97, 0.99, 0.93],
        [(0, 4), (5, 9), (10, 15), (16, 18), (19, 23)],
        text,
    )
    check(spans == [("Anna Meyer", "private_person", 0.97), ("Acme", "org", 0.93)],
          f"B/E and S spans decode with the minimum token score (got {spans})")

    # An I- with no B- before it still opens a span: a tagger that drops the opening tag
    # must not silently produce nothing, because "nothing" here means "not offered".
    spans = _decode(["I-private_person", "E-private_person"], [0.9, 0.9],
                    [(5, 9), (10, 15)], text)
    check(spans == [("Anna Meyer", "private_person", 0.9)],
          f"a span opened by I- is still decoded (got {spans})")


if __name__ == "__main__":
    tmp = Path(sys.argv[1])
    print(">> the deterministic layer catches an invoice's identifiers")
    pattern_layer()
    print("\n>> model proposals are accepted only if they occur verbatim")
    model_proposals()
    print("\n>> the verifier catches a broken redactor")
    verifier_catches_broken_redactor()
    print("\n>> PDF in, PDF out: content deleted, not covered")
    pdf_path(tmp)
    print("\n>> an unlocatable span fails the job")
    pdf_unlocatable(tmp)
    print("\n>> a partially redacted PDF is caught by the output verifier")
    pdf_partial_is_caught(tmp)
    print("\n>> a scanned PDF is refused")
    pdf_scanned(tmp)
    print("\n>> the BIOES decoder")
    decoder()
    print("\n>> the recall layer (openai/privacy-filter)")
    tagger()

    print()
    if FAILURES:
        print(f">> phase 4 acceptance: {len(FAILURES)} FAILED")
        sys.exit(1)
    print(">> phase 4 acceptance (no-GPU parts): PASS")
