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

TITLE_STYLES = ("topic_first", "meeting_date")


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
    min_gap_hours: float = 2.5
    # At most this many videos go public on one day (team time zone)...
    max_per_day: int = 5
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
}
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
    if name == "title_style":
        if value not in TITLE_STYLES:
            raise ValueError(f"title_style: one of {', '.join(TITLE_STYLES)}.")
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


# About how many public meetings the team has in a week (research/youtube-growth),
# for the warning below.
WEEKLY_MEETINGS = 9


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
    if WEEKLY_MEETINGS + shorts > 7 * p.max_per_day:
        return (f"About {WEEKLY_MEETINGS} public meetings a week make {WEEKLY_MEETINGS + shorts} videos, "
                f"but only {7 * p.max_per_day} fit in a week: some would be skipped.")
    return None


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
    _cross_check(values)
    prefs.set_publishing(values)
    return Publishing(**values)


def as_dict(p: Publishing | None = None) -> dict:
    return asdict(p or current())
