"""Is there a meeting in this recording at all? (owner, 2026-11-01)

Some Zoom recordings are an hour of an empty room. Right after the
transcript, before any judging or rendering, a job counts the words spoken
in the whole recording: fewer than CONTENT_MIN_WORDS (200) is empty, kept on
the server but never cut or posted. That many words or more is a meeting,
always (owner, 2026-11-04: no AI question any more; it judged real 100-minute
meetings empty).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from core.logging import get_logger
from core.models import Segment

logger = get_logger(__name__)


@dataclass
class ContentCheck:
    has_meeting: bool
    reason: str
    words: int


def _fmt(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def check_content(segments: list[Segment], duration_s: float, min_words: int,
                  saved: Path | None = None) -> ContentCheck:
    """Count the words; the verdict is written to `saved` for the record
    (it is counted again every time: it costs nothing, and an old verdict
    from the AI must never skip a meeting)."""
    words = sum(len(s.text.split()) for s in segments)
    if words < min_words:
        result = ContentCheck(False, f"only {words} words are spoken in {_fmt(duration_s)} of recording "
                                     f"(a meeting needs at least {min_words})", words)
    else:
        result = ContentCheck(True, f"{words} words are spoken in {_fmt(duration_s)} of recording", words)
    if saved is not None:
        try:
            saved.write_text(json.dumps(asdict(result)), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not save %s: %s", saved, exc)
    return result
