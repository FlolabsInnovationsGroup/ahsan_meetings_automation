"""Is there a meeting in this recording at all? (owner, 2026-11-01)

Some Zoom recordings are an hour of an empty room. Right after the
transcript, before any judging or rendering, a job checks it: fewer than
CONTENT_MIN_WORDS words in the whole recording is empty with no AI call;
otherwise one short question to the AI, on a sample from the start, middle
and end. An empty recording is kept but never cut or posted.

When in doubt the meeting goes ahead: an unclear answer or a failed call
never drops a recording, it only costs the normal processing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from core.logging import get_logger
from core.models import Segment

logger = get_logger(__name__)

SAMPLE_WORDS = 2400  # what the AI reads, split over the start, middle and end

CONTENT_PROMPT = """You check the transcript of a recorded company meeting before it is edited and posted
to the company's YouTube channel. Decide whether it holds a real meeting: people discussing work,
presenting, explaining, planning or deciding something.

Answer EMPTY when there is nothing worth posting: nobody really speaks, only greetings, "can you hear
me" or waiting for others to join, people talking to someone off the call, a sound or recording test,
or the recording kept running with nobody there. A short meeting with real content is a MEETING.
When in doubt, answer MEETING.

Answer with exactly one line: MEETING or EMPTY, then "|", then the reason in at most 15 words."""


@dataclass
class ContentCheck:
    has_meeting: bool
    reason: str
    words: int
    asked_ai: bool = False


def _fmt(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def transcript_sample(segments: list[Segment], max_words: int = SAMPLE_WORDS) -> str:
    """Whole lines from the start, the middle and the end, each part about a
    third of `max_words`, as "[mm:ss] Speaker: text"."""
    lines = [f"[{_fmt(s.start)}] {s.speaker or 'Speaker'}: {s.text.strip()}" for s in segments if s.text.strip()]
    sizes = [len(line.split()) for line in lines]
    if sum(sizes) <= max_words:
        return "\n".join(lines)
    third = max_words // 3

    def take(start: int, step: int) -> list[int]:
        picked, used, i = [], 0, start
        while 0 <= i < len(lines) and used < third:
            picked.append(i)
            used += sizes[i]
            i += step
        return picked

    middle = len(lines) // 2
    chosen = sorted(set(take(0, 1) + take(middle, 1) + take(len(lines) - 1, -1)))
    out, prev = [], -1
    for i in chosen:
        if prev >= 0 and i != prev + 1:
            out.append("[…]")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


def check_content(segments: list[Segment], duration_s: float, caller, min_words: int,
                  saved: Path | None = None) -> ContentCheck:
    """The verdict for this transcript, reused from `saved` when it exists
    (a re-render asks nothing again)."""
    if saved is not None and saved.exists():
        try:
            return ContentCheck(**json.loads(saved.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
    words = sum(len(s.text.split()) for s in segments)
    if words < min_words:
        result = ContentCheck(False, f"only {words} words are spoken in {_fmt(duration_s)} of recording", words)
    else:
        result = _ask(segments, duration_s, words, caller)
    if saved is not None:
        saved.write_text(json.dumps(asdict(result)), encoding="utf-8")
    return result


def _ask(segments: list[Segment], duration_s: float, words: int, caller) -> ContentCheck:
    contents = (f"Recording: {_fmt(duration_s)} long, {words} words spoken in total.\n"
                f"Transcript (parts):\n{transcript_sample(segments)}")
    try:
        answer = (caller.generate(CONTENT_PROMPT, contents).text or "").strip()
    except Exception as exc:  # noqa: BLE001 — a failed check never drops a meeting
        logger.warning("content check failed, processing the meeting anyway: %s", exc)
        return ContentCheck(True, f"the AI check failed ({type(exc).__name__}), processed anyway", words, True)
    verdict, _, reason = answer.splitlines()[0].partition("|") if answer else ("", "", "")
    empty = verdict.strip().strip("*").upper() == "EMPTY"
    return ContentCheck(not empty, reason.strip()[:200] or answer[:200], words, True)
