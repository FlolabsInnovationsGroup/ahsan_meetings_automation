"""The design of a thumbnail card: which template, illustration, palette and
words, for a video that has no usable real picture.

ONE call to the app's configured AI (decide.gemini_client.make_caller: the
default is Anthropic Claude) is made per card. It gets the video's title, its
short description or hook, the team and a short excerpt of what was said, and
answers with JSON. Nothing the AI writes is used as it comes: every id is
checked against the fixed lists (an unknown one falls back to a default), and
every word of the title lines and the subtitle must pass the same checks the
app uses for titles (said in the video, numbers said, no person's name, no
never-post word). A failure of any kind gives None, and the caller makes a
deterministic card from the video's own topic title instead.
"""

from __future__ import annotations

import itertools
import json
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from core.logging import get_logger
from publish import thumbart

logger = get_logger(__name__)

LINE_MAX_CHARS = 24  # asked for about 14-22
TITLE_MAX_CHARS = 56
SUBTITLE_MAX_CHARS = 28
EXCERPT_CHARS = 1500
TIMEOUT_S = 60
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

SYSTEM = (
    "You design the thumbnail card of a YouTube video of a company's team meeting. Answer with one JSON object "
    "and nothing else, with these keys: "
    '"template" (one of ' + ", ".join(thumbart.TEMPLATES) + "), "
    '"art" (the illustration, one of ' + ", ".join(thumbart.ART_IDS) + "), "
    '"title_lines" (1 to 3 short lines, about 14 to 22 characters each, that together say what the video is '
    'about, for example ["Offline", "voice AI"]), '
    '"accent_line" (the index, from 0, of the one title line to show in the accent colour), '
    '"subtitle" (at most 28 characters that add detail, or an empty string), '
    '"palette" (one of ' + ", ".join(thumbart.PALETTES) + "). "
    "Rules: use only words that are in the title, the description or what was said; never add a number that was "
    "not said; never name a person; no < or > characters; no emoji. Choose the illustration that best matches the "
    "topic: network = maps, graphs, platforms; doc_to_code = specs and code; chip_mic = hardware and voice; "
    "robot_loop = AI that improves; robot_arm = robotics; rocket and satellite = space; brain = AI models and "
    "learning; heart = health or mood; gear = process and automation; database = data and storage; chart = "
    "metrics; camera = vision; wifi = sensors and IoT; cloud = hosting; globe = global or maps. Choose the "
    "template that suits it: diagram, hardware, concept or statement."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "template": {"type": "string"},
        "art": {"type": "string"},
        "title_lines": {"type": "array", "items": {"type": "string"}},
        "accent_line": {"type": "integer"},
        "subtitle": {"type": "string"},
        "palette": {"type": "string"},
    },
    "required": ["template", "art", "title_lines", "accent_line", "subtitle", "palette"],
    "additionalProperties": False,
}


def _usage_now(caller) -> dict:
    usage = getattr(caller, "usage", None)
    return dict(usage.as_dict()) if hasattr(usage, "as_dict") else {}


def _usage_since(before: dict, after: dict) -> dict:
    return {k: (after.get(k) or 0) - (before.get(k) or 0)
            for k in ("calls", "prompt_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}


def _palette_for(seed: str, template: str) -> str:
    return thumbart.TEMPLATE_DEFAULTS[template][1]


def split_lines(label: str, per_line: int = 16, max_lines: int = 3) -> list[str]:
    """The words of a label in up to `max_lines` lines of about `per_line`
    characters each (the card wraps them again when they do not fit)."""
    words = label.split()
    if not words:
        return []
    count = min(max_lines, max(1, -(-len(label) // per_line)))
    best = None
    for cuts in itertools.combinations(range(1, len(words)), min(count, len(words)) - 1):
        edges = (0, *cuts, len(words))
        lines = [" ".join(words[a:b]) for a, b in itertools.pairwise(edges)]
        widest = max(len(line) for line in lines)
        if best is None or widest < best[0]:
            best = (widest, lines)
    return best[1] if best else [label]


def default_design(label: str, team: str = "", seed: str = "") -> dict | None:
    """A card made only from the video's own topic words: an illustration that
    fits them, its template and palette, the words in up to three lines. None
    when the words cannot be drawn."""
    label = " ".join((label or "").split())
    if not label or not thumbart.renderable(label):
        return None
    art = thumbart.pick_art(label, seed or label)
    template = thumbart.template_for(art)
    return {"template": template, "art": art, "palette": _palette_for(seed or label, template),
            "lines": split_lines(label), "accent_line": None, "subtitle": ""}


def check_design(raw, *, corpus: str, job, team: str = "", seed: str = "") -> dict | None:
    """The AI's answer, checked and cleaned; None when the title words are not
    acceptable. Unknown template, art or palette ids fall back to defaults; a
    subtitle that fails a check is dropped, the title never is."""
    from publish import describe, run

    if not isinstance(raw, dict):
        return None
    lines_raw = raw.get("title_lines")
    if not isinstance(lines_raw, list) or not 1 <= len(lines_raw) <= 3:
        return None
    lines = []
    for item in lines_raw:
        if not isinstance(item, str):
            return None
        line = " ".join(item.split())
        if not line or len(line) > LINE_MAX_CHARS:
            return None
        lines.append(line)
    title = " ".join(lines)
    if len(title) > TITLE_MAX_CHARS or "<" in title or ">" in title or not thumbart.renderable(title):
        return None
    names = describe._speaker_names(job)
    if not describe._grounded(title, corpus) or describe._names_someone(title, names):
        return None
    if run.listed_words(title, "") is not None:
        return None
    subtitle = raw.get("subtitle")
    subtitle = " ".join(subtitle.split()) if isinstance(subtitle, str) else ""
    if subtitle and (len(subtitle) > SUBTITLE_MAX_CHARS or "<" in subtitle or ">" in subtitle
                     or not thumbart.renderable(subtitle) or not describe._grounded(subtitle, corpus)
                     or describe._names_someone(subtitle, names) or run.listed_words(subtitle, "") is not None):
        subtitle = ""
    art = raw.get("art") if raw.get("art") in thumbart.ART_IDS else thumbart.pick_art(title, seed or title)
    template = raw.get("template") if raw.get("template") in thumbart.TEMPLATES else thumbart.template_for(art)
    palette = raw.get("palette") if raw.get("palette") in thumbart.PALETTES else _palette_for(seed or title, template)
    accent = raw.get("accent_line")
    accent = accent if isinstance(accent, int) and not isinstance(accent, bool) and 0 <= accent < len(lines) else None
    return {"template": template, "art": art, "palette": palette, "lines": lines, "accent_line": accent,
            "subtitle": subtitle}


def ask(job, *, title: str, hook: str, team: str, excerpt: str, corpus: str, cost_key: str = "", job_id: str = "",
        meeting: str = "", caller=None, timeout_s: float = TIMEOUT_S) -> dict | None:
    """The design call. Never raises: any error, refusal, bad JSON or timeout
    gives None. The AI cost is recorded under "ai:thumbnail:<cost_key>" in the
    cost meter, also when the answer is unusable."""
    try:
        from decide.gemini_client import make_caller

        caller = caller or make_caller()
        payload = json.dumps({"title": title, "description": hook, "team": team, "said": excerpt[:EXCERPT_CHARS]},
                             ensure_ascii=False)

        def work():
            before = _usage_now(caller)
            try:
                response = caller.generate(SYSTEM, payload, SCHEMA)
                text = _FENCE.sub("", (getattr(response, "text", "") or "").strip())
                return json.loads(text)
            finally:
                used = _usage_since(before, _usage_now(caller))
                if cost_key and used.get("calls"):
                    try:
                        from costs.record import record_call

                        record_call(f"ai:thumbnail:{cost_key}", used, job_id=job_id, meeting=meeting)
                    except Exception as exc:  # noqa: BLE001 - the meter must not break a thumbnail
                        logger.warning("thumbnail design: cost not recorded (%s)", type(exc).__name__)

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            answer = pool.submit(work).result(timeout=timeout_s)
        finally:
            pool.shutdown(wait=False)
        design = check_design(answer, corpus=corpus, job=job, team=team, seed=cost_key or title)
        if design is None:
            logger.info("thumbnail design: the AI's answer failed the checks; using the topic title")
        return design
    except FutureTimeout:
        logger.warning("thumbnail design: the AI call took longer than %.0fs", timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001 - a card is always made, from the title, when this fails
        logger.warning("thumbnail design: no AI design (%s: %s)", type(exc).__name__, exc)
        return None
