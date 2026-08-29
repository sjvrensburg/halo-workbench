"""The shared Jinja environment and the two filters every tool page needs.

Server-rendered HTML, no build step and no npm: this is a handful of forms and a
status page, not a frontend (see scripts/bootstrap.sh for why jinja2 is a dependency
and nothing else is).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def duration(seconds: float | None) -> str:
    """`7m 41s`. Coarse on purpose -- this is read to answer "is it still moving"."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def filesize(num: int | None) -> str:
    if num is None:
        return "—"
    for unit in ("B", "KB", "MB"):
        if num < 1024 or unit == "MB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} MB"


templates.env.filters["duration"] = duration
templates.env.filters["filesize"] = filesize
