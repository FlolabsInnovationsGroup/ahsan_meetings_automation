"""Custom thumbnails: a real frame with a label, or a designed card (an
illustration, the topic in big capitals), and a team chip, set on the video
after it is uploaded. The drawing is publish/thumbart.py, the AI design call
publish/thumbdesign.py.

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
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from core.config import get_settings
from core.logging import get_logger
from ledger import files
from publish import extras, optional, series, thumbart, thumbdesign, youtube_extra

logger = get_logger(__name__)

FEATURE = "thumbnail"
SIZE_FULL = (1280, 720)
SIZE_SHORT = (1080, 1920)
MAX_BYTES = 2 * 1024 * 1024  # YouTube's limit is 2 MB
CHIP_CHARS = 22
SHORT_DELAY = timedelta(minutes=20)
LABEL_FALLBACK_WORDS = 7
LABEL_FALLBACK_CHARS = 60
NO_PICTURE_TEXT = ("No usable picture from the recording and no safe words for a card, "
                   "so YouTube keeps its own picture")
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


_TRAILING_PARTS = re.compile(r"(?:\s*\([^()]*\))+\s*$")
_EDGE_PUNCT = " \t,;:.-–—|/"
_LOOSE_ENDS = frozenset(
    ["a", "an", "and", "as", "at", "but", "by", "for", "from", "if", "in", "into", "of", "on", "or", "so", "than", "that", "the", "then", "to", "with", "without", "via", "per", "our", "your", "my", "its", "their", "this", "these", "those", "is", "are", "was", "be"])
_BREAK_BEFORE = frozenset(["and", "but", "for", "from", "into", "of", "on", "or", "so", "that", "to", "with", "without", "because", "while", "when", "where"])


def _shorten(words: list[str]) -> list[str]:
    """At most LABEL_FALLBACK_WORDS words, cut at a natural point: after a
    comma or colon, else before a joining word, else at the limit; never ends
    on a word like "and", "of" or "the"."""
    if len(words) > LABEL_FALLBACK_WORDS:
        head = words[:LABEL_FALLBACK_WORDS]
        cut = len(head)
        for i in range(len(head) - 1, 1, -1):  # after punctuation that ends a phrase
            if head[i].endswith((",", ":", ";", "–", "—")):
                cut = i + 1
                break
        else:
            for i in range(LABEL_FALLBACK_WORDS, 3, -1):  # before a joining word
                if words[i].lower().strip(_EDGE_PUNCT) in _BREAK_BEFORE:
                    cut = i
                    break
        words = head[:cut]
    while len(words) > 1 and (words[-1].lower().strip(_EDGE_PUNCT) in _LOOSE_ENDS or not words[-1].strip(_EDGE_PUNCT)):
        words = words[:-1]
    if words:
        words[-1] = words[-1].rstrip(_EDGE_PUNCT)
    return [w for w in words if w]


def title_label(job, title: str | None, cfg=None) -> str | None:
    """A label made from the video's own title: the topic before any "(date)"
    and any " | FloLabs ..." tail, at most about 7 words. None when the title
    is empty or the words fail the never-post and person-name checks."""
    from publish import describe, run
    from publish import settings as publishing_settings

    cfg = cfg or publishing_settings.current()
    if not cfg.thumbnail_label:
        return None
    try:
        raw = str(title or "")
        if "<" in raw or ">" in raw:
            return None
        topic = _TRAILING_PARTS.sub("", raw.split("|")[0].strip()).strip(_EDGE_PUNCT)
        words = _shorten(describe.one_line(topic, 200).split())
        while len(words) > 1 and len(" ".join(words)) > LABEL_FALLBACK_CHARS:
            words = _shorten(words[:-1])
        label = " ".join(words)
        if not label:
            return None
        if describe._names_someone(label, describe._speaker_names(job)):
            return None
        if run.listed_words(label, "") is not None:
            return None
        return label
    except Exception:
        logger.exception("thumbnail title label could not be made")
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


def _save_jpeg(img, out: Path) -> Path:
    """A JPEG under YouTube's 2 MB limit (the quality steps down until it is)."""
    for quality in (92, 86, 78, 68, 58):
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.stat().st_size <= MAX_BYTES:
            break
    return out


def compose(base_jpg, label, chip, colour, out, size=SIZE_FULL, design=None) -> Path:
    """The picture, drawn with Pillow (publish/thumbart.py). With a base
    picture: that frame with a dark band, the label and the team chip. Without
    one: a designed card from `design` (template, art, palette, lines,
    accent_line, subtitle), or one made from `label` alone. A JPEG under 2 MB.
    Raises ValueError when a card has no title that can be drawn."""
    from PIL import Image

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    chip = (chip or "").strip()[:CHIP_CHARS]
    base = Path(base_jpg) if base_jpg and Path(base_jpg).is_file() else None
    colour = colour.lstrip("#")
    if base is not None:
        with Image.open(base) as opened:
            frame = opened.convert("RGB")
        words = (label or "").split()
        for count in range(len(words), -1, -1):  # a label too long for two lines loses its last words
            try:
                img = thumbart.render_frame(frame, " ".join(words[:count]), chip, colour, size)
                break
            except ValueError:
                continue
        else:
            img = thumbart.render_frame(frame, "", chip, colour, size)
    else:
        design = design or thumbdesign.default_design(label or "", chip)
        if not design:
            raise ValueError("a card needs a title")
        img = thumbart.render_card({**design, "chip": chip, "chip_colour": colour}, size)
    return _save_jpeg(img, out)



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


def _segments_text(job) -> tuple[str, str]:
    """(the words of the video's best moment, all the words) from the
    transcript the job kept; empty strings when it is gone."""
    from publish import describe

    rows = _read_json(_job_dir(job) / "segments.json")
    if not isinstance(rows, list):
        return "", ""
    texts = [(row.get("start"), str(row.get("text") or "")) for row in rows if isinstance(row, dict)]
    everything = " ".join(t for _s, t in texts)[:400_000]
    moment = ""
    try:
        span = describe.best_moment_range(job)
    except Exception:  # noqa: BLE001 - the whole transcript still serves
        span = None
    if span is not None:
        moment = " ".join(t for s, t in texts if isinstance(s, (int, float)) and span[0] - 1 <= s <= span[1])
    return moment or everything[:thumbdesign.EXCERPT_CHARS], everything


def _said(job, video_kind: str, info) -> tuple[str, str, str]:
    """(hook, excerpt sent to the AI, every word said that a card may use)."""
    try:
        hook, clip = _candidate(job, video_kind, info)
    except Exception:  # noqa: BLE001
        hook, clip = "", ""
    moment, everything = _segments_text(job)
    excerpt = clip or moment
    return str(hook or ""), excerpt, " ".join(x for x in (clip, everything) if x)


def _shorter(label: str) -> str | None:
    """The label without its last word(s), cut at a natural point; None when
    less than two words would be left."""
    words = _shorten(label.split()[:-1])
    return " ".join(words) if len(words) >= 2 else None


def _card_data(design: dict, source: str) -> dict:
    return {"label": " ".join(design["lines"]), "card": True, "design": source, "template": design["template"],
            "art": design["art"], "palette": design["palette"]}


def make_picture(job, item, *, video_kind: str, index, title: str, base, cfg, out: Path, cost_key: str = "",
                 meeting: str = "") -> dict | None:
    """Make the thumbnail picture at `out` and say what it is: {"label", "card",
    "design", ...} for the task's data, or None when no picture with words can
    be made (YouTube then keeps its own). A real frame first (the best frame
    the recording gave), a designed card otherwise, as the thumbnail_design
    setting allows; the AI designs a card when thumbnail_ai is on, else (and
    whenever it fails) the card is made from the video's own topic title."""
    chip, colour = team_chip(job, item)
    size = SIZE_SHORT if video_kind == "short" else SIZE_FULL
    mode = cfg.thumbnail_design
    base = base if mode != "card_only" else None
    info = _info(job, item, SimpleNamespace(title=title), video_kind, index)
    if base is not None:  # the real picture, with a label from the clip or the title
        label = label_for(job, video_kind, info, cfg) or title_label(job, title, cfg)
        compose(base, label, chip, colour, out, size)
        return {"label": label or "", "card": False, "design": "frame"}
    if mode == "frame_only":
        return None
    topic = title_label(job, title, cfg)
    if not topic:  # no safe words (a name, a never-post word, setting off): no card
        return None
    if cfg.thumbnail_ai:
        hook, excerpt, said = _said(job, video_kind, info)
        design = thumbdesign.ask(
            job, title=" ".join(str(title or "").split())[:200], hook=hook, team=chip, excerpt=excerpt,
            corpus=f"{said} {topic} {hook}", cost_key=cost_key, job_id=_job_id(job), meeting=meeting)
        if design:
            try:
                compose(None, "", chip, colour, out, size, design=design)
                return _card_data(design, "ai")
            except ValueError:
                logger.info("thumbnail: the AI's title does not fit the card; using the topic title")
    while topic:  # the topic title; fewer words when it is too long to draw
        design = thumbdesign.default_design(topic, chip, seed=cost_key)
        if design:
            try:
                compose(None, "", chip, colour, out, size, design=design)
                return _card_data(design, "topic")
            except ValueError:
                pass
        topic = _shorter(topic)
    return None


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
    video_id = getattr(posted, "video_id", None) or ""
    task = extras.Task(kind=FEATURE, video_kind=video_kind, data={"arm": "custom", "label": "", "card": base is None})
    try:
        with tempfile.TemporaryDirectory(prefix="hc_thumb_") as tmp:
            out = Path(tmp) / "thumb.jpg"
            data = make_picture(job, item, video_kind=video_kind, index=index, title=getattr(posted, "title", "") or "",
                                base=base, cfg=cfg, out=out, cost_key=video_id or getattr(item, "key", ""),
                                meeting=getattr(item, "meeting", "") or "")
            if data is not None:
                task.file = extras.stash_file(task.id, out, "jpg")
    except BaseException:
        _settle(video_id, video_kind, "custom_failed")
        raise
    if data is None:  # no usable frame and no usable words: YouTube keeps its own picture
        _settle(video_id, video_kind, "custom_failed")
        task.state, task.reason = extras.SKIPPED, NO_PICTURE_TEXT
        return task
    task.data = {"arm": "custom", **data}
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


# ------------------------------------------------------- redoing empty cards


def _was_empty_card(task) -> bool:
    """A thumbnail task made before labels were guaranteed: a picture with an
    EMPTY label that was not redone yet. Old tasks have 'label': '' and no 'card'
    key; those are redone even when the job's files are gone (jobs are deleted
    after about 30 hours, so for a posted video they usually are): the picture
    is then built from the posted title and the team. When the job folder is
    still there, only a card (no usable base frame for this video) is redone."""
    data = task.data if isinstance(task.data, dict) else {}
    if task.kind != FEATURE or task.state not in (extras.DONE, extras.WAITING, extras.NEEDS_RECONNECT):
        return False
    if data.get("arm") != "custom" or "label" not in data or data.get("label") or data.get("redo"):
        return False
    if "card" in data:
        return bool(data["card"])
    job = SimpleNamespace(job_id=task.job_id)
    if not isinstance(_read_json(_job_dir(job) / "thumbnails" / "thumbs.json"), list):
        return True  # the job is gone: an empty picture that was never redone
    base, _row = _base_row(job, task.video_kind, task.index)
    return base is None


def redo_empty_cards() -> bool:
    """One-time pass (extras.register_once): every posted video whose thumbnail
    was made with no words gets a new picture with the current rules (a real
    frame if the job still has one, else an AI-designed card when allowed or the
    deterministic title card, from the posted title and the team: never an empty
    one), put back in the same follow-up queue (same quota and soft-fail rules).
    Only videos the app posted that are still published, each only once. False
    when a picture could not be made, so the next queue run tries those again."""
    from ledger.uploads import get_ledger
    from publish import settings as publishing_settings

    cfg = publishing_settings.current()
    clean = True
    for task in extras.all_tasks():
        if not _was_empty_card(task):
            continue
        try:
            upload = get_ledger().get(task.item_key)
            if (upload is None or upload.state != "published" or not task.video_id
                    or upload.video_id != task.video_id):
                continue  # not a video the app posted any more: leave it
            # what the ledger still knows: the posted title and the team
            team = task.meeting or upload.meeting or ""
            job = SimpleNamespace(job_id=task.job_id, id=task.job_id, title=team)
            item = SimpleNamespace(key=task.item_key, topic=task.topic or team, meeting=team)
            base, _row = _base_row(job, task.video_kind, task.index)  # None when the job's files are gone
            with tempfile.TemporaryDirectory(prefix="hc_thumb_") as tmp:
                out = Path(tmp) / "thumb.jpg"
                data = make_picture(job, item, video_kind=task.video_kind, index=task.index, title=upload.title,
                                    base=base, cfg=cfg, out=out, cost_key=task.video_id, meeting=team)
                if data is None:
                    extras._update(task.id, data={**task.data, "redo": "no picture"})  # nothing safe: leave it as is
                    continue
                extras.replace_picture(task.id, out, {**task.data, **data, "redo": True})
        except Exception:
            clean = False
            logger.exception("empty thumbnail card %s could not be redone", task.id)
    return clean



extras.register(FEATURE, build, run)
extras.register_once(FEATURE, "thumbnail_redo_empty_cards", redo_empty_cards)
