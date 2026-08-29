"""Protected spans and ZnZ sentinels — VENDORED, not written here.

Copied 2026-08-29 from ../anti-watermark-mcp/src/anti_watermark_mcp/masking.py, whose
docstrings are kept intact below because they are the evidence, not decoration: the
sentinel form and the restoration rules were *measured* over round trips through
Chinese, and re-deriving them here would cost the same days a second time.

Why vendored rather than imported: that module lives in another repo's package and
another venv, and this app installs neither. The `marker_run.sh` precedent -- never
reimplement a sibling's scar tissue -- is honoured by copying it verbatim rather than
paraphrasing it.

The risk of a copy is silent drift, so it is checked mechanically. The source file's
sha256 when copied was:

    VENDORED_FROM_SHA256 = '38112c224065430e751aa329a9f4ab3ecb63c941ada48342a83dff59b701ba28'

`tests/phase6_acceptance.sh` re-hashes the sibling and fails when it no longer matches,
which turns "someone improved masking.py and we never noticed" into a test failure with
a diff to read. Update the constant deliberately, after reading that diff.
"""

from __future__ import annotations

VENDORED_FROM = "../anti-watermark-mcp/src/anti_watermark_mcp/masking.py"
VENDORED_FROM_SHA256 = "38112c224065430e751aa329a9f4ab3ecb63c941ada48342a83dff59b701ba28"

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Priority-ordered alternation. Order matters: math spans and citation keys contain
#: digits, so they must match before the bare-number rule can split them apart.
_PROTECTED = re.compile(
    r"""
    (?P<env>    \\begin\{(?P<envname>[^}]*)\}.*?\\end\{(?P=envname)\} )
  | (?P<math>   \$\$.+?\$\$ | \$[^$\n]+?\$ | \\\(.+?\\\) | \\\[.+?\\\] )
  | (?P<cite>   \\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{[^}]*\} )
  | (?P<ref>    \\(?:eq|page|auto|name|c)?ref\{[^}]*\} )
  | (?P<label>  \\(?:label|url|href|footnote|caption|input|include)
                (?:\[[^\]]*\])*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\} )
  | (?P<fmt>    \\(?:emph|textbf|textit|texttt|textsc|textsf|underline|uline|mbox|text)
                \{[^{}]*(?:\{[^{}]*\}[^{}]*)*\} )
  | (?P<key>    @[A-Za-z][A-Za-z0-9_:.-]*[A-Za-z0-9] | @[A-Za-z] )
  | (?P<escword> [A-Za-z0-9]+ (?: \\[&%$#_] [A-Za-z0-9]+ )+ )
  | (?P<esc>    \\[&%$#_{}~^\\] )
  | (?P<range>  \d+(?:[.,]\d+)*\s*-{2,3}\s*\d+(?:[.,]\d+)* )
  | (?P<cmd>    \\[a-zA-Z@]+\*? (?:\[[^\]]*\])* (?:\{[^{}]*\})* )
  | (?P<dash>   -{2,3} )
  | (?P<number> \d+(?:[.,]\d+)* )
    """,
    re.VERBOSE | re.DOTALL,
)

#: Categories masked by default. Everything the preservation check tracks is protected,
#: so the two stay in step -- protecting less than we check for guarantees noise.
DEFAULT_CATEGORIES = (
    "env", "math", "cite", "ref", "label", "fmt", "key", "escword", "esc",
    "range", "cmd", "dash", "number",
)

_SENTINEL_OUT = "Z{}Z"

#: Restoration is deliberately tolerant, because the ways a translator perturbs a
#: sentinel are cheap to accept and expensive to reject:
#:
#: * whitespace drifts in and out around CJK boundaries (``Z 3 Z``);
#: * the flanks are sometimes lowercased (``z3z``), or one flank only (``Z3z``).
#:
#: What it will *not* accept is a missing flank. ``ZnZ`` has no bracket structure to
#: reason about, so a one-sided match would turn an ordinary capital Z next to a digit
#: into a restored citation. Both flanks are required.
#:
#: The lookarounds keep the sentinel from matching inside a longer alphanumeric run:
#: ``Z3Z`` is a sentinel, the ``z3z`` inside ``xyz3z9`` is not.
#:
#: They exclude ``A-Y``/``a-y`` and digits, but deliberately *not* ``Z`` itself.
#: Adjacent protected spans produce adjacent sentinels -- ``4.7\%`` masks to ``Z5ZZ6Z``
#: -- and a flat ``(?![0-9A-Za-z])`` reads the second sentinel's opening ``Z`` as a word
#: character and rejects the pair, losing both spans. Measured: that one rule was the
#: difference between 7/9 and 9/9 on the econometrics test paragraph.
_SENTINEL_FIND = re.compile(r"(?<![0-9A-Ya-y])[Zz]\s*(\d+)\s*[Zz](?![0-9A-Ya-y])")

#: Sentinels the model invented that map to nothing. Dropped rather than left in place:
#: leaving ``Z7Z`` in the output makes the preservation check report a fabricated
#: number 7, burying real findings under an artefact of our own masking.
_ORPHAN_SENTINEL = re.compile(r"(?<![0-9A-Ya-y])[Zz]\s*\d+\s*[Zz](?![0-9A-Ya-y])")

#: The 12B-era form. Still recognised on restoration only, never emitted: it costs one
#: alternation to keep a pivot text captured under the old sentinel restorable, and to
#: keep working if a future translator re-doubles the brackets on its own.
_LEGACY_SENTINEL_FIND = re.compile(r"[\[［]\s*[\[［]\s*(\d+)\s*[\]］)）}】]{1,2}")
_LEGACY_ORPHAN = re.compile(r"[\[［]\s*[\[［]\s*\d+\s*[\]］)）}】]{1,2}")


@dataclass
class Masked:
    """Masked text plus the mapping needed to restore it."""

    text: str
    mapping: dict[int, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.mapping)


def mask(text: str, categories: tuple[str, ...] = DEFAULT_CATEGORIES) -> Masked:
    """Replace protected spans with ``ZnZ`` sentinels."""
    mapping: dict[int, str] = {}
    counter = 0

    def substitute(match: re.Match) -> str:
        nonlocal counter
        if match.lastgroup not in categories:
            return match.group(0)
        token = _SENTINEL_OUT.format(counter)
        mapping[counter] = match.group(0)
        counter += 1
        return token

    return Masked(_PROTECTED.sub(substitute, text), mapping)


def unmask(text: str, mapping: dict[int, str]) -> tuple[str, list[int]]:
    """Restore sentinels. Returns the text and the indices that did not come back.

    A missing index means translation ate that span. The caller should surface it
    rather than silently returning text with a hole where a citation used to be.
    """
    seen: set[int] = set()

    orphans: list[str] = []
    duplicates: list[str] = []

    def restore(match: re.Match) -> str:
        index = int(match.group(1))
        if index not in mapping:
            orphans.append(match.group(0))
            return ""
        if index in seen:
            duplicates.append(match.group(0))
            return ""
        seen.add(index)
        return mapping[index]

    restored = _SENTINEL_FIND.sub(restore, text)
    restored = _LEGACY_SENTINEL_FIND.sub(restore, restored)
    # Belt and braces: anything still sentinel-shaped never mapped to content, so it is
    # our own scaffolding leaking into the result.
    restored = _ORPHAN_SENTINEL.sub("", restored)
    restored = _LEGACY_ORPHAN.sub("", restored)
    if orphans:
        logger.warning("Dropped %d invented sentinel(s): %s", len(orphans), orphans[:5])
    if duplicates:
        logger.warning("Dropped %d duplicated sentinel(s): %s", len(duplicates), duplicates[:5])

    missing = sorted(set(mapping) - seen)
    if missing:
        logger.warning(
            "%d/%d protected spans lost in translation: %s",
            len(missing),
            len(mapping),
            [mapping[i] for i in missing[:5]],
        )
    return restored, missing
