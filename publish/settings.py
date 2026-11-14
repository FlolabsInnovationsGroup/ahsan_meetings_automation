"""Publishing choices the owner makes on the page (side panel, "Publishing"):
when videos go public, how many Shorts a meeting gets, and the words and
fields that go with each upload.

Saved in storage_dir/state/prefs.json under "publishing" (ledger/prefs.py).
A choice nobody made has the default below. Every value is checked when it is
saved, and a damaged saved value falls back to its default, so a bad file can
never make the app post more, or sooner, than chosen.

Defaults follow the research in research/youtube-growth/ (2026-11-14): spread
public videos out instead of posting a meeting's videos at once, leave time to
catch a mistake before anything goes public, fewer Shorts, titles that lead
with the topic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from ledger import prefs

# Every choice setting: name -> the values it accepts.
_CHOICES: dict[str, tuple[str, ...]] = {
    "title_style": ("topic_first", "meeting_date"),
    "shorts_style": ("classic", "new", "alternate"),
    "shorts_top_text": ("title", "none"),
    "shorts_layout": ("auto", "fit"),
    "shorts_screen_zoom": ("whole_slide", "zoom"),
    "shorts_speaker_layout": ("full", "panel"),
    "thumbnail_mode": ("off", "test", "custom"),
    "series_mode": ("allow_list", "allow_all"),
    "series_archive_privacy": ("unlisted", "skip"),
    "shorts_link_target": ("topic", "full"),
}
TITLE_STYLES = _CHOICES["title_style"]


@dataclass(frozen=True)
class Publishing:
    # --- when videos go public (publish/schedule.py) ----------------------
    # Upload public videos as private with a time YouTube makes them public
    # (status.publishAt), spread out by the rules below. Off = public at once.
    scheduled: bool = True
    # The first slot is at least this long after the upload, so a mistake can
    # be caught in YouTube Studio before anyone sees it.
    review_hours: float = 12.0
    # At least this long between two videos going public.
    min_gap_hours: float = 2.0
    # At most this many videos go public on one day (team time zone)...
    max_per_day: int = 6
    # ...of which at most this many are Shorts.
    shorts_per_day: int = 3
    # Slots only between these hours (team time zone, settings.local_timezone);
    # start < end, end is exclusive.
    window_start_hour: int = 10
    window_end_hour: int = 22
    # A meeting's Shorts go public at least this long after its full video,
    # so the link in each Short points at a video that is already public.
    shorts_after_full_hours: float = 24.0
    # A video with no free slot within this many days is not uploaded; it
    # waits for a person (Publish button).
    max_days_ahead: int = 7

    # --- what is made and posted ------------------------------------------
    # Shorts cut per meeting (at most SHORTS_COUNT from .env).
    shorts_per_meeting: int = 2
    # The autopilot holds a meeting for a person instead of posting it when
    # its final video is longer / shorter than this (minutes; 0 = no limit).
    hold_over_minutes: float = 120.0
    hold_under_minutes: float = 3.0

    # --- the words (publish/describe.py) ----------------------------------
    # "topic_first": the best moment's title, then the team ("Moving the
    # session store to Redis | FloLabs IoT"); "meeting_date": the meeting's
    # name and day, as before.
    title_style: str = "topic_first"
    # A few hashtags at the end of each description.
    hashtags: bool = True
    # Added at the end of every full video's description (website, socials).
    links_text: str = ""

    # --- upload fields (publish/youtube.py) -------------------------------
    # YouTube category: 28 Science & Technology, 27 Education, 22 People & Blogs.
    category_id: str = "28"
    # Language of the titles and of the speech.
    language: str = "en"

    # --- Shorts look (render/shorts look; "new style" fields only matter when
    # a Short is made in the new style) --------------------------------------
    # classic, new, or alternate (one of each per meeting, logged to compare).
    shorts_style: str = "alternate"
    shorts_hook_seconds: float = 2.5
    shorts_top_text: str = "title"
    shorts_end_line: str = "Full meeting on our channel"
    shorts_caption_px: int = 80
    shorts_caption_max_chars: int = 28
    shorts_layout: str = "auto"
    shorts_screen_zoom: str = "whole_slide"
    shorts_speaker_layout: str = "full"
    shorts_hd: bool = True
    shorts_trim_pauses: bool = True
    shorts_max_pause_s: float = 0.6

    # --- sound ---------------------------------------------------------------
    shorts_loudness: bool = True
    full_loudness: bool = True
    loudness_target_lufs: float = -14.0
    audio_denoise: bool = False

    # --- full video look -------------------------------------------------------
    full_hd: bool = False
    full_hook_overlay: bool = False

    # --- thumbnails ------------------------------------------------------------
    thumbnail_mode: str = "test"
    thumbnail_shorts: bool = False
    thumbnail_faces: bool = False
    thumbnail_label: bool = True

    # --- caption tracks --------------------------------------------------------
    captions_full: bool = True
    captions_shorts: bool = False

    # --- translations ----------------------------------------------------------
    # Off until the owner consents (YouTube's developer rules).
    translations: bool = False
    translation_languages: str = "es,pt,hi,id,ja,de"

    # --- playlists -------------------------------------------------------------
    playlists: bool = True
    playlist_shorts: bool = True
    playlist_cuts: bool = True
    playlist_full_template: str = "FloLabs {series}: team meetings"
    playlist_shorts_template: str = "FloLabs {series}: Shorts"
    playlist_cuts_template: str = "FloLabs {series}: topics"

    # --- teams -----------------------------------------------------------------
    series_mode: str = "allow_list"
    series_archive_privacy: str = "unlisted"

    # --- topic cuts --------------------------------------------------------------
    topic_cuts: bool = True
    topic_cuts_per_meeting: int = 1
    topic_min_minutes: float = 5.0
    topic_max_minutes: float = 20.0
    topic_min_score: float = 7.0
    topic_cold_open: bool = True
    topic_front_door: bool = True
    shorts_link_target: str = "topic"

    # --- views -----------------------------------------------------------------
    views_pull: bool = True
    views_include_other: bool = True
    views_watch_time: bool = True
    # Rankings stay off until YouTube accepts the derived-metrics amendment.
    views_rankings: bool = False

    # --- YouTube quota and follow-ups --------------------------------------------
    quota_daily_units: int = 10000
    quota_reserve_units: int = 2000
    extras_retry_days: int = 14


DEFAULTS = Publishing()

# name: (low, high) for the numbers
_RANGES: dict[str, tuple[float, float]] = {
    "review_hours": (0, 72),
    "min_gap_hours": (0, 24),
    "max_per_day": (1, 24),
    "shorts_per_day": (0, 24),
    "window_start_hour": (0, 23),
    "window_end_hour": (1, 24),
    "shorts_after_full_hours": (0, 168),
    "max_days_ahead": (1, 30),
    "shorts_per_meeting": (0, 10),
    "hold_over_minutes": (0, 24 * 60),
    "hold_under_minutes": (0, 24 * 60),
    "shorts_hook_seconds": (0, 5),
    "shorts_caption_px": (56, 110),
    "shorts_caption_max_chars": (16, 40),
    "shorts_max_pause_s": (0.3, 2.0),
    "loudness_target_lufs": (-23, -10),
    "topic_cuts_per_meeting": (0, 3),
    "topic_min_minutes": (2, 30),
    "topic_max_minutes": (5, 40),
    "topic_min_score": (0, 10),
    "quota_daily_units": (1000, 1000000),
    "quota_reserve_units": (0, 9000),
    "extras_retry_days": (1, 30),
}
END_LINE_LIMIT = 40  # characters
PLAYLIST_LIMIT = 150  # characters, with a 50-character series filled in
_PLAYLIST_TEMPLATES = ("playlist_full_template", "playlist_shorts_template", "playlist_cuts_template")
_CATEGORIES = {"1", "2", "10", "15", "17", "19", "20", "22", "23", "24", "25", "26", "27", "28"}
LINKS_LIMIT = 1000  # characters


def _check(name: str, value):
    """The value, cleaned, or ValueError with a plain message."""
    default = getattr(DEFAULTS, name)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{name}: choose on or off.")  # noqa: TRY004 - a bad value from the page, answered as 400
        return value
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name}: must be a number.")  # noqa: TRY004 - a bad value from the page, answered as 400
        low, high = _RANGES[name]
        if not low <= value <= high:
            raise ValueError(f"{name}: choose between {low:g} and {high:g}.")
        return int(value) if isinstance(default, int) else float(value)
    if name in _CHOICES:
        if value not in _CHOICES[name]:
            raise ValueError(f"{name}: one of {', '.join(_CHOICES[name])}.")
        return value
    if name == "shorts_end_line":
        value = str(value).strip()
        if len(value) > END_LINE_LIMIT:
            raise ValueError(f"shorts_end_line: at most {END_LINE_LIMIT} characters.")
        if "<" in value or ">" in value:
            raise ValueError("shorts_end_line: no < or > signs.")
        return value
    if name == "translation_languages":
        codes = [c.strip() for c in str(value).split(",")]
        if not 1 <= len(codes) <= 10:
            raise ValueError("translation_languages: between 1 and 10 language codes.")
        for c in codes:
            if not (2 <= len(c) <= 8 and all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in c)):
                raise ValueError(f"translation_languages: '{c}' is not a language code such as es or pt-BR.")
        if len({c.lower() for c in codes}) != len(codes):
            raise ValueError("translation_languages: each language only once.")
        return ",".join(codes)
    if name in _PLAYLIST_TEMPLATES:
        value = str(value).strip()
        if value.count("{series}") != 1 or value.replace("{series}", "").count("{") or value.replace("{series}", "").count("}"):
            raise ValueError(f"{name}: use {{series}} exactly once and no other braces.")
        if "<" in value or ">" in value:
            raise ValueError(f"{name}: no < or > signs.")
        if len(value.replace("{series}", "x" * 50)) > PLAYLIST_LIMIT:
            raise ValueError(f"{name}: too long (a playlist name is at most {PLAYLIST_LIMIT} characters).")
        return value
    if name == "category_id":
        value = str(value).strip()
        if value not in _CATEGORIES:
            raise ValueError("category_id: not a YouTube category number.")
        return value
    if name == "language":
        value = str(value).strip()
        if not (2 <= len(value) <= 8 and all(c.isalnum() or c == "-" for c in value)):
            raise ValueError("language: a language code such as en or en-GB.")
        return value
    if name == "links_text":
        value = str(value).replace("\r\n", "\n").strip()
        if len(value) > LINKS_LIMIT:
            raise ValueError(f"links_text: at most {LINKS_LIMIT} characters.")
        if "<" in value or ">" in value:
            raise ValueError("links_text: YouTube doesn't allow < or > in a description.")
        return value
    raise ValueError(f"Unknown setting: {name}.")


def _cross_check(values: dict) -> None:
    if values["window_start_hour"] >= values["window_end_hour"]:
        raise ValueError("The release window must start before it ends.")
    if values["shorts_per_day"] > values["max_per_day"]:
        raise ValueError("Shorts per day can't be more than videos per day.")
    if values["topic_min_minutes"] >= values["topic_max_minutes"]:
        raise ValueError("A topic cut's shortest length must be less than its longest.")
    if values["quota_reserve_units"] >= values["quota_daily_units"]:
        raise ValueError("The quota kept in reserve must be less than the daily quota.")
    main = str(values["language"]).split("-")[0].lower()
    if any(c.split("-")[0].lower() == main for c in str(values["translation_languages"]).split(",")):
        raise ValueError("Translations can't include the video's own language.")


# About how many public meetings the team has in a week (research/youtube-growth),
# for the warning below.
WEEKLY_MEETINGS = 9
# About what share of meetings make a topic cut (score and length rules).
TOPIC_SHARE = 0.6


def capacity_warning(p: Publishing | None = None) -> str | None:
    """A plain sentence when the choices can't fit a normal week, so Shorts
    would pile up and be skipped; None when they fit. Not an error: the owner
    may want exactly that."""
    p = p or current()
    if not p.scheduled:
        return None
    shorts = WEEKLY_MEETINGS * p.shorts_per_meeting
    if shorts > 7 * p.shorts_per_day:
        return (f"About {WEEKLY_MEETINGS} public meetings a week with {p.shorts_per_meeting} Shorts each is "
                f"{shorts} Shorts, but only {7 * p.shorts_per_day} fit in a week: some Shorts would be skipped.")
    cuts = WEEKLY_MEETINGS * TOPIC_SHARE * p.topic_cuts_per_meeting if p.topic_cuts else 0
    videos = WEEKLY_MEETINGS + cuts + shorts
    if videos > 7 * p.max_per_day:
        if not cuts:
            return (f"About {WEEKLY_MEETINGS} public meetings a week make {WEEKLY_MEETINGS + shorts} videos, "
                    f"but only {7 * p.max_per_day} fit in a week: some would be skipped.")
        return (f"About {WEEKLY_MEETINGS} public meetings a week with topic cuts make about {round(videos)} videos, "
                f"but only {7 * p.max_per_day} fit in a week: some would be skipped.")
    if p.min_gap_hours > 0:
        fit = int((p.window_end_hour - p.window_start_hour) // p.min_gap_hours) + 1
        if fit < p.max_per_day:
            return (f"Only {fit} videos fit in the release hours with {p.min_gap_hours:g} h between them, "
                    f"but videos per day is {p.max_per_day}.")
    return None


def _without_main_language(values: dict) -> None:
    """Drop the video's own language from the translation list, so a list the
    owner never chose (the default, or a file saved before this check existed)
    can't clash with the language they did choose."""
    main = str(values["language"]).split("-")[0].lower()
    kept = [c for c in str(values["translation_languages"]).split(",") if c.split("-")[0].lower() != main]
    if not kept:
        kept = [c for c in DEFAULTS.translation_languages.split(",") if c.split("-")[0].lower() != main]
    values["translation_languages"] = ",".join(kept)


def current() -> Publishing:
    """The choices in force: saved ones that pass the checks, else defaults."""
    saved = prefs.publishing()
    values = asdict(DEFAULTS)
    for name in values:
        if name in saved:
            try:
                values[name] = _check(name, saved[name])
            except ValueError:
                pass  # a damaged value keeps its default
    _without_main_language(values)
    try:
        _cross_check(values)
    except ValueError:
        return DEFAULTS
    return Publishing(**values)


def save(changes: dict) -> Publishing:
    """Check and save `changes` (any subset of the fields). Raises ValueError
    with a plain message and saves nothing when a value is wrong."""
    names = {f.name for f in fields(Publishing)}
    unknown = sorted(set(changes) - names)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(unknown)}.")
    values = asdict(current())
    for name, value in changes.items():
        values[name] = _check(name, value)
    if "translation_languages" not in changes:
        _without_main_language(values)  # only an explicit list is refused for clashing
    _cross_check(values)
    prefs.set_publishing(values)
    return Publishing(**values)


def as_dict(p: Publishing | None = None) -> dict:
    return asdict(p or current())


@dataclass(frozen=True)
class ShortsLook:
    """How one Short is rendered; a frozen snapshot so a render can't change mid-way."""
    style: str
    hook_seconds: float
    top_text: str
    end_line: str
    caption_px: int
    caption_max_chars: int
    layout: str
    screen_zoom: str
    speaker_layout: str
    hd: bool
    loudness: bool
    target_lufs: float
    denoise: bool
    trim_pauses: bool
    max_pause_s: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FullLook:
    """How a full video (and topic cut) is rendered."""
    loudness: bool
    target_lufs: float
    hd: bool
    hook_overlay: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Looks:
    shorts: ShortsLook
    full: FullLook

    def as_dict(self) -> dict:
        return {"shorts": self.shorts.as_dict(), "full": self.full.as_dict()}


def looks(p: Publishing | None = None) -> Looks:
    """The look choices in force (a snapshot), for the render code and manifests."""
    p = p or current()
    return Looks(
        shorts=ShortsLook(
            style=p.shorts_style, hook_seconds=p.shorts_hook_seconds, top_text=p.shorts_top_text,
            end_line=p.shorts_end_line, caption_px=p.shorts_caption_px,
            caption_max_chars=p.shorts_caption_max_chars, layout=p.shorts_layout,
            screen_zoom=p.shorts_screen_zoom, speaker_layout=p.shorts_speaker_layout, hd=p.shorts_hd,
            loudness=p.shorts_loudness, target_lufs=p.loudness_target_lufs, denoise=p.audio_denoise,
            trim_pauses=p.shorts_trim_pauses, max_pause_s=p.shorts_max_pause_s),
        full=FullLook(loudness=p.full_loudness, target_lufs=p.loudness_target_lufs, hd=p.full_hd,
                      hook_overlay=p.full_hook_overlay),
    )
