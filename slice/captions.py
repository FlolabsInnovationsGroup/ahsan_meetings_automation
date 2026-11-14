"""Captions for the vertical shorts (plan D7).

Zoom transcripts are sentence-level (one "word" per sentence here), far too
long for a phone screen, so each sentence is split into words whose times are
spread over the sentence in proportion to their length, then regrouped into
short cues (a few words, never across a sentence end or a pause). Cue times
are on the clip's own timeline (source time − clip start).

Two files come out of one cue list: an .srt shipped next to the short (for
platforms that take a caption upload) and an .ass burned into the video with
libass — ASS gives the exact style (size, outline, position, wrapping) without
fragile force_style strings, plus the moment's title at the top.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.models import Word

SENTENCE_END = (".", "!", "?", "…")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def caption_cues(
    items: list[Word],
    clip_start: float,
    clip_end: float,
    *,
    max_chars: int = 30,
    max_words: int = 6,
    max_pause_s: float = 0.8,
    bridge_s: float = 0.5,
) -> list[Cue]:
    """Short caption cues for [clip_start, clip_end) (source seconds) on the
    clip timeline (0 = clip start). A word belongs to the clip if its
    estimated midpoint is inside it."""
    tokens: list[tuple[float, float, str]] = []
    cursor = float("-inf")
    for item in sorted(items, key=lambda w: w.start):
        if item.end <= clip_start or item.start >= clip_end:
            continue
        words = item.word.split()
        if not words:
            continue
        # a line never starts before the previous one ends: rolling/overlapping
        # transcript cues otherwise interleave their words and put two
        # captions on screen at once
        begin = max(item.start, cursor)
        end = max(item.end, begin + 0.25 * len(words))
        cursor = end
        weight = sum(len(w) + 1 for w in words)
        t = begin
        for w in words:
            d = (end - begin) * (len(w) + 1) / weight
            if clip_start <= t + d / 2 < clip_end:
                tokens.append((max(t, clip_start), min(t + d, clip_end), w))
            t += d

    groups: list[list[tuple[float, float, str]]] = []
    current: list[tuple[float, float, str]] = []
    for tok in tokens:
        if current:
            text = " ".join(t for _, _, t in current)
            if (len(text) + 1 + len(tok[2]) > max_chars or len(current) >= max_words
                    or tok[0] - current[-1][1] > max_pause_s):
                groups.append(current)
                current = []
        current.append(tok)
        if tok[2].endswith(SENTENCE_END):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    cues = [Cue(g[0][0] - clip_start, g[-1][1] - clip_start, " ".join(t for _, _, t in g)) for g in groups]
    # close small gaps so captions don't flicker off between cues
    out: list[Cue] = []
    for i, cue in enumerate(cues):
        end = cue.end
        if i + 1 < len(cues) and 0 <= cues[i + 1].start - end <= bridge_s:
            end = cues[i + 1].start
        if end > cue.start:
            out.append(Cue(round(cue.start, 3), round(end, 3), cue.text))
    return out


def _srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_text(cues: list[Cue]) -> str:
    blocks = [f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    return "\n".join(blocks)


def _ass_time(t: float) -> str:
    cs = max(0, math.floor(t * 100 + 0.5))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    # "{...}" starts an override block and a backslash starts a tag
    return " ".join(text.replace("\\", "/").replace("{", "(").replace("}", ")").split())


def ass_document(cues: list[Cue], *, title: str, duration_s: float, font_family: str, width: int,
                 height: int) -> str:
    """Captions near the bottom, the title at the top (whole clip), sized for
    width x height (1080x1920 by default)."""
    k = height / 1920
    caption_size, title_size = round(76 * k), round(62 * k)
    margin_x = round(90 * width / 1080)
    fields = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
              "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
              "Alignment, MarginL, MarginR, MarginV, Encoding")
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        fields,
        (f"Style: Caption,{font_family},{caption_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,"
         f"100,0,0,1,{max(1, round(5 * k))},0,2,{margin_x},{margin_x},{round(430 * k)},1"),
        (f"Style: Title,{font_family},{title_size},&H0000E5FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,"
         f"0,0,1,{max(1, round(4 * k))},0,8,{margin_x},{margin_x},{round(300 * k)},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title.strip():
        lines.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration_s)},Title,,0,0,0,,{_ass_escape(title)}")
    for cue in cues:
        lines.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,{_ass_escape(cue.text)}")
    return "\n".join(lines) + "\n"


# --- new-style Shorts subtitle document (hook card, safe-band captions) -------------------------------------

END_LINE_S = 2.5
HOOK_MAX_CHARS = 42


def _break_near_middle(words: list[str]) -> int:
    """Index (1..len-1) at which splitting the words gives the most even two halves by characters."""
    total = sum(len(w) for w in words) + len(words) - 1
    best, best_gap, run = 1, float("inf"), 0
    for i in range(1, len(words)):
        run += len(words[i - 1]) + (1 if i > 1 else 0)
        gap = abs(run - (total - run - 1))
        if gap < best_gap:
            best, best_gap = i, gap
    return best


def _row_len(text: str) -> float:
    """Row width in average-character units: capitals are wider than the average glyph, so they count more."""
    return sum(1.25 if ch.isupper() else 1.0 for ch in text)


def wrap_two_lines(text: str, chars_per_line: int) -> str | None:
    """`text` with one \\N at the word break nearest the middle when it is longer than one line;
    None when it would still need three or more lines."""
    text = " ".join(text.split())
    if _row_len(text) <= chars_per_line:
        return text
    words = text.split(" ")
    if len(words) < 2:
        return None
    i = _break_near_middle(words)
    first, second = " ".join(words[:i]), " ".join(words[i:])
    if _row_len(first) > chars_per_line or _row_len(second) > chars_per_line:
        return None
    return f"{first}\\N{second}"


def _chars_per_line(width: int, size_px: float, margin_px: float, char_em: float) -> int:
    # 0.88: libass wraps again on its own when a row is wider than the estimate, so keep a safety margin
    return max(1, int(0.88 * (width - 2 * margin_px) / (char_em * size_px)))


def cap_two_lines(cues: list[Cue], *, width: int, size_px: float, margin_px: float, char_em: float) -> list[Cue]:
    """Wrap every cue to at most two rows; a cue that cannot fit is split in two at the word nearest its
    middle, the time divided in proportion to characters (contiguous, not overlapping)."""
    per_line = _chars_per_line(width, size_px, margin_px, char_em)
    out: list[Cue] = []

    def add(cue: Cue, depth: int = 0) -> None:
        wrapped = wrap_two_lines(cue.text, per_line)
        words = cue.text.split()
        if wrapped is not None or len(words) < 2 or depth > 6:
            # an unbreakable single long word (or the depth guard) is kept as is
            out.append(Cue(cue.start, cue.end, wrapped if wrapped is not None else " ".join(words)))
            return
        i = _break_near_middle(words)
        a, b = " ".join(words[:i]), " ".join(words[i:])
        cut = round(cue.start + (cue.end - cue.start) * len(a) / (len(a) + len(b)), 3)
        add(Cue(cue.start, cut, a), depth + 1)
        add(Cue(cut, cue.end, b), depth + 1)

    for cue in cues:
        add(cue)
    return out


def choose_hook(screen_hook: str | None, title: str | None) -> str:
    """The screen hook when there is one, else the title if it is short enough, else no card."""
    hook = " ".join((screen_hook or "").split())
    if hook:
        return hook
    title = " ".join((title or "").split())
    return title if 0 < len(title) <= HOOK_MAX_CHARS else ""


def _fit_text(text: str, *, width: int, size_px: float, margin_px: float, char_em: float,
              min_size_px: float) -> tuple[str, int]:
    """(escaped text wrapped to at most two rows, font size). The size shrinks (not below min_size_px) so
    two rows hold the text; past that the text is cut with an ellipsis."""
    text = _ass_escape(text)
    size = round(size_px)
    while True:
        per_line = _chars_per_line(width, size, margin_px, char_em)
        wrapped = wrap_two_lines(text, per_line)
        if wrapped is not None:
            return wrapped, size
        if size <= min_size_px:
            break
        size = max(round(min_size_px), size - 2)
    words = text.split(" ")
    while len(words) > 1:
        words.pop()
        wrapped = wrap_two_lines(" ".join(words) + "…", per_line)
        if wrapped is not None:
            return wrapped, size
    return text[: per_line * 2 - 1] + "…", size


def ass_document_new(cues: list[Cue], *, hook: str, title: str, end_line: str, duration_s: float, look,
                     font_family: str, width: int, height: int) -> str:
    """New-style Shorts subtitles, designed at 1080x1920 and scaled: captions with their bottom edge at
    y=1330, a large hook card from the first frame, a small title for the rest, an end line over the
    last 2.5 s (the title stops when it starts). Nothing above y=192 or below y=1330."""
    k = height / 1920
    kx = width / 1080
    char_em = 0.6 if "dejavu" in font_family.lower() else 0.56
    margin_x = round(60 * kx)
    caption_px = round(look.caption_px * k)
    hook_px, title_px = round(86 * k), round(46 * k)
    top_margin = round(250 * k)
    box_pad = max(1, round(12 * k))
    fields = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
              "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
              "Alignment, MarginL, MarginR, MarginV, Encoding")
    yellow = "&H0000D7FF"
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        fields,
        (f"Style: Caption,{font_family},{caption_px},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,"
         f"100,0,0,1,{max(1, round(7 * k))},{max(1, round(3 * k))},2,{margin_x},{margin_x},"
         f"{round((1920 - 1330) * k)},1"),
        (f"Style: Hook,{font_family},{hook_px},&H00101010,&H000000FF,{yellow},{yellow},-1,0,0,0,100,100,0,0,3,"
         f"{box_pad},0,8,{margin_x},{margin_x},{top_margin},1"),
        (f"Style: Title,{font_family},{title_px},&H00FFFFFF,&H000000FF,&H66000000,&H66000000,-1,0,0,0,100,100,"
         f"0,0,3,{box_pad},0,8,{margin_x},{margin_x},{top_margin},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def card(text: str, px: int) -> str:
        # the box padding eats into the side margins, so the text area is a little narrower
        wrapped, size = _fit_text(text, width=width, size_px=px, margin_px=margin_x + box_pad, char_em=char_em,
                                  min_size_px=round(px * 0.7))
        return f"{{\\fs{size}}}{wrapped}" if size != px else wrapped

    hook_s = min(look.hook_seconds, duration_s) if hook.strip() else 0.0
    if hook_s > 0:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(hook_s)},Hook,,0,0,0,,"
                     f"{{\\fad(0,250)}}{card(hook, hook_px)}")
    end_start = max(0.0, duration_s - END_LINE_S) if end_line.strip() else duration_s
    if look.top_text != "none" and title.strip() and end_start > hook_s:
        lines.append(f"Dialogue: 1,{_ass_time(hook_s)},{_ass_time(end_start)},Title,,0,0,0,,"
                     f"{card(title, title_px)}")
    if end_line.strip():
        lines.append(f"Dialogue: 1,{_ass_time(end_start)},{_ass_time(duration_s)},Hook,,0,0,0,,"
                     f"{card(end_line, hook_px)}")
    # escape first, then wrap: the \N row break is the only backslash in the document
    safe = [Cue(c.start, c.end, _ass_escape(c.text)) for c in cues]
    for cue in cap_two_lines(safe, width=width, size_px=caption_px, margin_px=margin_x, char_em=char_em):
        lines.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,{cue.text}")
    return "\n".join(lines) + "\n"
