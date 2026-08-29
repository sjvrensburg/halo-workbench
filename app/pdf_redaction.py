"""PDF in, PDF out, with the content actually deleted.

**Never draw a black rectangle over text.** The text sits untouched underneath and
`pdftotext` recovers all of it in one command; that is *the* classic redaction failure
and it looks identical to success on screen. PyMuPDF's `add_redact_annot` +
`apply_redactions` removes the underlying content stream, and the acceptance test is
`pdftotext` on the *output* finding none of the removed strings.

The hard part is span → coordinates, and it fails quietly. `page.search_for()` cannot
find a span the text layer splits across a line break, hyphenates, or spaces oddly --
the span is *in* the document and *not* findable as a quad. So this module counts: a
span that occurs three times in the extracted text must yield three rectangles, or the
job fails. **An unlocatable span is a failure to report, never a span to skip.** A PDF
where 9 of 10 names were removed is more dangerous than one where none were, because
it looks done.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pymupdf


# Below this many extracted characters per page on average, treat the document as
# having no text layer worth searching -- a scan. Not zero: a scanned page often
# carries a stray character or two from a stamp or a form field, and calling that a
# text layer would let a scan through the "we searched it" path.
MIN_CHARS_PER_PAGE = 32


class Unlocatable(Exception):
    """A confirmed span could not be turned into rectangles on every page it occurs on."""


class NoTextLayer(Exception):
    """The PDF is (or contains) scanned images with nothing to search."""


class VerificationFailed(Exception):
    """The produced file still contains a confirmed span. It is deleted, not returned."""


def shortfall_messages(stats: dict[str, tuple[int, int]]) -> list[str]:
    """Which spans yielded fewer rectangles than the text layer says they occur.

    Split out as a pure function because it is the branch that must not rot: on
    PyMuPDF 1.28.2 no synthetic PDF this repo can build actually triggers it -- text
    split across lines, hyphenated, ligatured, rotated and inside a form XObject are
    all located correctly (probed 2026-08-29). That makes this a safety net for real
    documents rather than a path the other tests exercise, so it is tested directly.

    Rectangles may legitimately EXCEED occurrences: one match split across two lines
    returns two rectangles. Only a shortfall is a failure.
    """
    return [
        f"{span!r}: {occurrences} occurrence(s) in the text layer, "
        f"{rectangles} locatable on the page"
        for span, (occurrences, rectangles) in stats.items()
        if rectangles < occurrences
    ]


def extract_text(path: Path) -> str:
    """All text, PyMuPDF's view of it. This is what spans are found in and counted in."""
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def pdftotext(path: Path) -> str:
    """The other extractor. Deliberately a second opinion, not a fallback.

    PyMuPDF and pdftotext disagree about ligatures, column order and soft hyphens, so
    a span can survive in one view and not the other. The verifier asserts on both;
    `pdftotext` is also the exact command a sceptical user will run on the file we
    hand them, which makes it the one that matters most.
    """
    if not shutil.which("pdftotext"):
        return ""
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def text_layer_report(path: Path) -> tuple[bool, str]:
    """(has a usable text layer, human explanation). Checked before anything is promised."""
    with pymupdf.open(path) as doc:
        pages = doc.page_count
        chars = sum(len(page.get_text().strip()) for page in doc)
        image_only = [
            i + 1
            for i, page in enumerate(doc)
            if len(page.get_text().strip()) < MIN_CHARS_PER_PAGE and page.get_images()
        ]

    if pages and chars / pages >= MIN_CHARS_PER_PAGE and not image_only:
        return True, f"{pages} page(s), {chars} characters of text"

    where = f"page(s) {', '.join(map(str, image_only))}" if image_only else "the document"
    return False, (
        f"{where} carry images with no text layer — this looks like a scan. There is "
        f"nothing to search and nothing for the verifier to check, so this tool will "
        f"not pretend to redact it. Convert it with the marker tool first and redact "
        f"the markdown, accepting that the OCR's own error rate is then inside the "
        f"blast radius."
    )


def redact_pdf(source: Path, destination: Path, spans: list[str]) -> dict[str, int]:
    """Delete every occurrence of every span, or raise. Returns hits per span.

    Raises NoTextLayer for a scan, and Unlocatable when the number of rectangles found
    is short of the number of occurrences the text layer says are there.
    """
    usable, explanation = text_layer_report(source)
    if not usable:
        raise NoTextLayer(explanation)

    doc = pymupdf.open(source)
    try:
        stats: dict[str, tuple[int, int]] = {}

        for span in spans:
            if not span.strip():
                continue
            occurrences = 0
            rectangles = 0
            for page in doc:
                # Counted PER PAGE and literally, not normalised over the whole
                # document: a confirmed span is always a literal substring of the
                # extracted text (patterns match on it, model proposals are checked
                # against it verbatim, your-list terms are checked with `in`), and a
                # per-page count is the only one that can be compared with per-page
                # rectangles at all.
                occurrences += page.get_text().count(span)
                # DEHYPHENATE so a span broken by a hyphen at a line end is still found.
                for rect in page.search_for(span, flags=pymupdf.TEXT_DEHYPHENATE):
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    rectangles += 1
            stats[span] = (occurrences, rectangles)

        shortfalls = shortfall_messages(stats)
        if shortfalls:
            # Before applying anything: a partial redaction must not reach the disk at
            # all, not even to be deleted afterwards.
            raise Unlocatable(
                "these confirmed spans could not be located on the page, so the job "
                "fails rather than removing some of them — the usual cause is a span "
                "split across a line break or rendered with ligatures:\n  "
                + "\n  ".join(shortfalls)
            )

        for page in doc:
            # This is the line that deletes content. Without it the annotations are
            # just black boxes drawn over live text.
            page.apply_redactions()

        # A body that is clean while /Info still says the author's full name has not
        # been redacted. XMP is a second, separate copy of much the same thing.
        doc.set_metadata({})
        doc.del_xml_metadata()
        doc.save(destination, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()

    # Re-read what was written, with both extractors and the metadata, before the file
    # is allowed to exist as far as the caller is concerned. The locate-count above is
    # a prediction; this is the observation, and it is the one that catches a cause of
    # survival nobody anticipated.
    from app.redaction import survivors  # here to keep this module import-light

    left = survivors(extract_text(destination), spans)
    left += [s for s in survivors(pdftotext(destination), spans) if s not in left]
    left += [s for s in survivors(metadata_values(destination), spans) if s not in left]
    if left:
        destination.unlink(missing_ok=True)
        raise VerificationFailed(
            "the produced PDF still contains "
            + ", ".join(repr(s) for s in left[:5])
            + " — the file has been deleted rather than returned"
        )

    return {span: rectangles for span, (_, rectangles) in stats.items()}


def metadata_values(path: Path) -> str:
    """Everything the file says about itself, as one string for the verifier to search."""
    with pymupdf.open(path) as doc:
        parts = [str(v) for v in (doc.metadata or {}).values() if v]
        xml = doc.get_xml_metadata()
    if xml:
        parts.append(xml)
    return "\n".join(parts)
