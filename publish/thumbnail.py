"""Custom thumbnails: a clean frame (or a team-colour card), a short label
taken from the clip, and a team chip, set on the video after it is uploaded.

Nothing here runs inside an upload. The builder (called by publish.extras
right after an upload) composes the picture and saves it in state/extras/;
the runner sets it later through optional.call, so a refusal from YouTube can
never touch the upload itself.

Under thumbnail_mode "test" every other video of a team gets ours and the
rest keep the one YouTube picks, so the difference in views can be measured
(the arm is written to state/experiments.jsonl and to the views database).
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.proc import run_checked
from ledger import files
from publish import extras, optional, series, youtube_extra
from slice.ffmpeg_wrapper import drawtext
from slice.fonts import find_font

logger = get_logger(__name__)

FEATURE = "thumbnail"
SIZE_FULL = (1280, 720)
SIZE_SHORT = (1080, 1920)
MAX_BYTES = 2 * 1024 * 1024  # YouTube's limit is 2 MB
MAX_TEXT_W = 1190
FONT_MAX, FONT_MIN = 110, 64
TWO_LINE_MAX = 90
ONE_LINE_GOOD = 80  # a single line at this size or bigger is kept on one line
MARGIN = 45
CHIP_SIZE = 40
CHIP_CHARS = 22
SHORT_DELAY = timedelta(minutes=20)
FORBIDDEN_TEXT = "YouTube refused custom thumbnails for this channel (it may need verifying in Studio)"
BLOCK_HOURS = 24

# White text stays readable on every one of these.
PALETTE = ("D62828", "1D7874", "3A5BA0", "7B2CBF", "B5560F", "2E7D32")


# ------------------------------------------------------------------ the label


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _job_id(job) -> str:
    return str(getattr(job, "job_id", None) or getattr(job, "id", "") or "")


def _job_dir(job) -> Path:
    return get_settings().jobs_dir / _job_id(job)


def _field(info, *names):
    for name in names:
        value = info.get(name) if isinstance(info, dict) else getattr(info, name, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _manifest_short(job, index) -> dict:
    data = _read_json(_job_dir(job) / "manifest.json")
    for row in (data.get("shorts") if isinstance(data, dict) else None) or []:
        if isinstance(row, dict) and str(row.get("file", "")).endswith(f"short_{int(index or 0):02d}.mp4"):
            return row
    return {}


def _full_hook(job) -> tuple[str, str]:
    """(screen hook, clip text) of the moment the full video's title is taken
    from, when a Short of that moment has one."""
    from publish import describe

    span = describe.best_moment_range(job)
    if span is None:
        return "", ""
    data = _read_json(_job_dir(job) / "manifest.json")
    for row in (data.get("shorts") if isinstance(data, dict) else None) or []:
        try:
            if abs(float(row["source_start"]) - span[0]) < 0.5 and abs(float(row["source_end"]) - span[1]) < 0.5:
                return str(row.get("screen_hook") or ""), str(row.get("text") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return "", ""


def _candidate(job, video_kind: str, info) -> tuple[str, str]:
    """(label text, the clip's own words) before any check."""
    if video_kind == "short":
        index = info.get("index") if isinstance(info, dict) else getattr(info, "index", None)
        row = _manifest_short(job, index)
        hook = str(row.get("screen_hook_shown") or row.get("screen_hook") or "") or _field(info, "screen_hook")
        return hook, str(row.get("text") or _field(info, "text"))
    if video_kind == "topic":
        return _field(info, "hook", "screen_hook"), _field(info, "text")
    hook, text = _full_hook(job)
    if hook.strip():
        return hook, text
    title = info if isinstance(info, str) else _field(info, "title")
    words = title.split(" | ")[0].split()[:5]
    return " ".join(words), ""


def label_for(job, video_kind: str, info=None, cfg=None) -> str | None:
    """The 2-5 word label for a video, or None (the picture then has a frame and
    a chip only). `info` is the video's own row: the upload title for a full
    video, the Short's row, the topic's row. Any failed check drops the label."""
    from publish import settings as publishing_settings

    cfg = cfg or publishing_settings.current()
    if not cfg.thumbnail_label:
        return None
    try:
        from publish import describe, run

        raw, text = _candidate(job, video_kind, info)
        if "<" in raw or ">" in raw:
            return None
        label = describe.one_line(raw, 60)
        words = label.split()
        if not 2 <= len(words) <= 5:
            return None
        if describe._names_someone(label, describe._speaker_names(job)):
            return None
        if not describe._grounded(label, text):
            return None
        if run.listed_words(label, "") is not None:
            return None
        return label
    except Exception:
        logger.exception("thumbnail label could not be made")
        return None


# ------------------------------------------------------------------- the chip


def team_chip(job, item=None) -> tuple[str, str]:
    """(the team's name as the registry has it, its colour as RRGGBB)."""
    from publish import describe

    topic = getattr(item, "topic", None) or getattr(item, "meeting", None) or getattr(job, "title", None) or ""
    row = series.lookup(topic)
    key = row.key if row else series.series_key(topic)
    label = (row.label if row else "") or describe.series_of(topic)
    return label, PALETTE[series.colour_index(key) % len(PALETTE)]


# ------------------------------------------------------------------ composing

_metrics: dict[str, str] = {}


def _text_width(font: Path, text: str, size: int) -> float:
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        name = _metrics.get(str(font))
        if name is None:
            name = f"hcthumb{len(_metrics)}"
            pdfmetrics.registerFont(TTFont(name, str(font)))
            _metrics[str(font)] = name
        return pdfmetrics.stringWidth(text, name, size)
    except Exception:  # noqa: BLE001 - a rough width is better than no thumbnail
        return 0.62 * size * len(text)


def _split(font: Path, words: list[str], size: int, max_w: float) -> list[str] | None:
    """The label on one line, else on two balanced ones; None when it does not fit."""
    whole = " ".join(words)
    if _text_width(font, whole, size) <= max_w:
        return [whole]
    best = None
    for cut in range(1, len(words)):
        a, b = " ".join(words[:cut]), " ".join(words[cut:])
        widest = max(_text_width(font, a, size), _text_width(font, b, size))
        if widest <= max_w and (best is None or widest < best[0]):
            best = (widest, [a, b])
    return best[1] if best else None


def fit(font: Path, label: str, max_w: float) -> tuple[int, list[str]] | None:
    """(font size, lines): 64-110 px, at most two lines, at most `max_w` wide."""
    words = label.split()
    one = two = None
    for size in range(FONT_MAX, FONT_MIN - 1, -2):
        if one is None and _text_width(font, " ".join(words), size) <= max_w:
            one = (size, [" ".join(words)])
        if two is None and size <= TWO_LINE_MAX and len(words) > 1:
            lines = _split(font, words, size, max_w)
            if lines is not None:
                two = (size, lines)
    if one and one[0] >= ONE_LINE_GOOD:
        return one
    return two or one


def _write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_bytes(text.encode("utf-8"))
    return path


def _filters(base: bool, size, label, chip, colour, font, folder: Path) -> str:
    width, height = size
    parts: list[str] = []
    if base:
        parts.append(f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}")
    else:
        parts.append("null")
    hexcolour = f"0x{colour}"
    lines: list[str] = []
    text_size = 0
    if label and font is not None:
        fitted = fit(font, label, min(MAX_TEXT_W, width - 2 * MARGIN))
        if fitted:
            text_size, lines = fitted
    line_h = round(text_size * 1.15)
    pad = 26
    chip_text = (chip or "").strip().upper()[:CHIP_CHARS] if font is not None else ""
    chip_w = round(_text_width(font, chip_text, CHIP_SIZE)) + 44 if chip_text else 0
    chip_h = round(CHIP_SIZE * 1.0) + 28
    if lines and base:
        band_h = 2 * pad + line_h * len(lines)
        top = height - band_h
        parts.append(f"drawbox=x=0:y={top}:w={width}:h={band_h}:color=black@0.85:t=fill")
        for i, line in enumerate(lines):
            path = _write(folder, f"line{i}.txt", line)
            parts.append(drawtext(font, path, size=text_size, x=str(MARGIN), y=str(top + pad + i * line_h),
                                  box=False))
        chip_x, chip_y = width - MARGIN - chip_w, 28  # top right: slide titles sit lower, YouTube's time badge is bottom right
    elif lines:  # a card: the words in the middle
        block = line_h * len(lines)
        for i, line in enumerate(lines):
            path = _write(folder, f"line{i}.txt", line)
            parts.append(drawtext(font, path, size=text_size, x=str(MARGIN),
                                  y=str((height - block) // 2 + i * line_h), box=False))
        chip_x, chip_y = MARGIN, MARGIN
    else:
        chip_x, chip_y = (width - MARGIN - chip_w, 28) if base else (MARGIN, MARGIN)
    if chip_text:
        box, ink = (hexcolour, "white") if base else ("white", hexcolour)
        parts.append(f"drawbox=x={chip_x}:y={chip_y}:w={chip_w}:h={chip_h}:color={box}:t=fill")
        path = _write(folder, "chip.txt", chip_text)
        parts.append(drawtext(font, path, size=CHIP_SIZE, x=str(chip_x + 22), y=str(chip_y + 14),
                              color=ink, box=False))
    return ",".join(parts)


def compose(base_jpg, label, chip, colour, out, size=SIZE_FULL) -> Path:
    """One ffmpeg call: the base picture (or a solid team-colour card when
    there is none), a black band with the label in bold, and the team chip.
    For a Short the base already shows the Short's layout; only the band and
    chip are drawn. A JPEG under 2 MB; raises when ffmpeg fails."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    base = Path(base_jpg) if base_jpg and Path(base_jpg).is_file() else None
    font = find_font(bold=True)
    with tempfile.TemporaryDirectory(prefix="hc_thumb_") as tmp:
        folder = Path(tmp)
        graph = _filters(base is not None, size, label, chip, colour, font, folder)
        for quality in (2, 6):
            if base is not None:
                source = ["-i", str(base)]
            else:
                source = ["-f", "lavfi", "-i", f"color=c=0x{colour}:s={size[0]}x{size[1]}:r=1"]
            cmd = [get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-y", *source,
                   "-vf", graph, "-frames:v", "1", "-q:v", str(quality), str(out.resolve())]
            run_checked(cmd, timeout=60, cwd=str(folder))
            if out.stat().st_size <= MAX_BYTES:
                break
    return out


# ------------------------------------------------------------------ the arms


def _experiments_path() -> Path:
    return files.state_dir() / "experiments.jsonl"


def _series_key(item, job) -> str:
    topic = getattr(item, "topic", None) or getattr(item, "meeting", None) or getattr(job, "title", None) or ""
    return series.series_key(topic)


def _earlier_videos(key: str, video_kind: str, video_id: str) -> int:
    from ledger.uploads import get_ledger

    count = 0
    for upload in get_ledger().posted():
        if upload.kind == video_kind and upload.video_id != video_id and series.series_key(upload.meeting) == key:
            count += 1
    return count


def arm(job, video_kind: str, item=None, posted=None, cfg=None) -> str:
    """'custom' (ours) or 'auto' (the picture YouTube makes). Under 'test' a
    team's full videos (and its topic cuts) alternate, custom first; the
    choice is written down once per video. Shorts are not in the test: they
    get ours only when thumbnail_shorts is on."""
    from analytics import record
    from publish import settings as publishing_settings

    cfg = cfg or publishing_settings.current()
    if video_kind == "short":
        return "custom" if cfg.thumbnail_shorts else "auto"
    video_id = getattr(posted, "video_id", None) or ""
    for old in files.read_jsonl(_experiments_path()):  # a re-run must not flip the arm
        if video_id and old.get("video_id") == video_id and old.get("feature") == "thumbnail":
            return old.get("arm") or "auto"
    key = _series_key(item, job)
    if cfg.thumbnail_mode == "custom":
        chosen = "custom"
    elif cfg.thumbnail_mode == "test":
        chosen = "custom" if _earlier_videos(key, video_kind, video_id) % 2 == 0 else "auto"
    else:
        chosen = "auto"
    if video_id:
        files.append_jsonl(_experiments_path(), {
            "at": files.now_iso(), "video_id": video_id, "key": getattr(item, "key", ""),
            "series": key, "feature": "thumbnail", "arm": chosen})
        record.set_arm(video_id, "thumbnail", chosen)
    return chosen


def _settle(video_id: str, video_kind: str, result: str) -> None:
    """Write down what really happened to a 'custom' video: 'custom' once the
    picture is on YouTube, 'custom_failed' while it is not (a refused picture,
    a bad base frame, a task given up), so the A/B only counts real pictures.
    Only writes when the result changed since the last row."""
    if not video_id or video_kind == "short":
        return
    from analytics import record

    last = None
    for old in files.read_jsonl(_experiments_path()):
        if old.get("video_id") == video_id and old.get("feature") == "thumbnail":
            last = old.get("arm")
    if last is None or last == result:
        return
    files.append_jsonl(_experiments_path(), {
        "at": files.now_iso(), "video_id": video_id, "feature": "thumbnail", "arm": result})
    record.set_arm(video_id, "thumbnail", result)


# ---------------------------------------------------------------- the builder


def _base_row(job, video_kind: str, index) -> tuple[Path | None, dict]:
    """The picture to start from: the best base frame of this video in the job
    folder (None for a card or when the job has none)."""
    folder = _job_dir(job)
    rows = _read_json(folder / "thumbnails" / "thumbs.json")
    if not isinstance(rows, list):
        return None, {}
    mine = [r for r in rows if isinstance(r, dict) and r.get("video") == video_kind
            and (video_kind == "full" or r.get("index") == index)]
    mine.sort(key=lambda r: r.get("index") or 0)
    for row in mine:
        name = row.get("file")
        if name and (folder / name).is_file():
            return folder / name, row
    return None, (mine[0] if mine else {})


def _info(job, item, posted, video_kind: str, index):
    if video_kind == "short":
        return {"index": index}
    if video_kind == "topic":
        return item if hasattr(item, "hook") else {"hook": "", "text": ""}
    return getattr(posted, "title", "") or ""


def build(job, item, posted, *, video_kind: str, index, cfg):
    """The follow-up builder: composes the picture now and saves it beside the
    queue. None when this video is not in the custom arm."""
    if video_kind not in ("full", "short", "topic"):
        return None
    if video_kind == "short" and not cfg.thumbnail_shorts:
        return None
    if arm(job, video_kind, item, posted, cfg) != "custom":
        return None
    base, _row = _base_row(job, video_kind, index)
    label = label_for(job, video_kind, _info(job, item, posted, video_kind, index), cfg)
    chip, colour = team_chip(job, item)
    size = SIZE_SHORT if video_kind == "short" else SIZE_FULL
    task = extras.Task(kind=FEATURE, video_kind=video_kind, data={"arm": "custom", "label": label or ""})
    try:
        with tempfile.TemporaryDirectory(prefix="hc_thumb_") as tmp:
            made = compose(base, label, chip, colour, Path(tmp) / "thumb.jpg", size)
            task.file = extras.stash_file(task.id, made, "jpg")
    except BaseException:
        _settle(getattr(posted, "video_id", None) or "", video_kind, "custom_failed")
        raise
    now = datetime.now(UTC)
    task.not_before = (now + (SHORT_DELAY if video_kind == "short" else timedelta(0))).isoformat(timespec="seconds")
    return task


# ----------------------------------------------------------------- the runner


def run(task) -> optional.Outcome:
    """Set the saved picture on the video. Shorts first wait until YouTube has
    processed them."""
    jpg = Path(task.file) if task.file else None
    if jpg is None or not jpg.is_file():
        _settle(task.video_id, task.video_kind, "custom_failed")
        return optional.Outcome(optional.SKIPPED, reason="The thumbnail picture is gone")
    now = datetime.now(UTC)
    if task.video_kind == "short":
        checked = optional.call("thumbnails", 1, lambda: youtube_extra.processing_status(task.video_id),
                                attempt=task.attempts)
        if checked.status != optional.DONE:
            return checked
        if checked.value is None:
            return optional.Outcome(optional.SKIPPED, reason="The video is gone")
        status = (checked.value or {}).get("processingStatus")
        if status in ("failed", "terminated"):
            return optional.Outcome(optional.SKIPPED, reason="YouTube could not process this Short")
        if status != "succeeded":
            wait = timedelta(minutes=30) if task.attempts == 0 else timedelta(hours=2)
            return optional.Outcome(optional.RETRY, reason="YouTube is still processing this Short",
                                    retry_at=now + wait)
    seen: dict = {}

    def call():
        try:
            return youtube_extra.set_thumbnail(task.video_id, jpg)
        except BaseException as exc:
            seen["status"] = optional._error_parts(exc)[0]
            raise

    outcome = optional.call("thumbnails", youtube_extra.UNITS_THUMBNAIL, call, attempt=task.attempts,
                            forbidden_reason=FORBIDDEN_TEXT)
    if outcome.status == optional.SKIPPED and outcome.reason == FORBIDDEN_TEXT:
        extras.block_kind(FEATURE, BLOCK_HOURS)
    elif outcome.status == optional.RETRY and seen.get("status") == 429:
        return optional.Outcome(optional.RETRY, reason="YouTube asked us to slow down; trying again tomorrow",
                                retry_at=now + timedelta(days=1))
    elif outcome.status == optional.DONE:
        _settle(task.video_id, task.video_kind, "custom")
        return optional.Outcome(optional.DONE)
    _settle(task.video_id, task.video_kind, "custom_failed")
    return outcome


extras.register(FEATURE, build, run)
