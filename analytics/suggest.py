"""What does best: medians, ranks and plain-sentence suggestions.

Active only when the views_rankings setting is on (it stays off until YouTube
accepts the derived-metrics amendment). Everything here is read-only: it reads
the views database and, for the team tier, the team list; it never writes
series.json, the settings or the database, and it never changes anything by
itself. The suggestions are sentences for the owner to act on or ignore.

A video's day-7 (day-28) number is the stored snapshot nearest to the day it
went public plus 7 (28) days, within a few days; the engaged views from the
analytics report are used instead when they cover that day.
A group is shown only with at least MIN_VIDEOS videos and MIN_DATA_DAYS days
of data behind it.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

from costs.db import local_day

from . import db
from .report import FORMATS, NO_TEAM, _public_day

MIN_VIDEOS = 8
MIN_DATA_DAYS = 28
TOLERANCE = {7: 3, 28: 7}  # how far from the target day a snapshot may be


def _view_number(pub: date, offset: int, snaps: list[dict], yta: list[dict], today: date,
                 yta_start: str | None = None) -> int | None:
    """The video's views `offset` days after it went public, or None. `yta_start` is the first day the
    analytics report has any data; engaged views are used only when it covers the video from its public
    day (else the sum would miss the first days, so the snapshot is used)."""
    target = pub + timedelta(days=offset)
    if target > today:
        return None
    t = target.isoformat()
    ranged = [r for r in yta if pub.isoformat() <= r["day"] <= t]
    start = yta_start or (min(r["day"] for r in yta) if yta else None)
    covered = start is not None and start <= pub.isoformat()
    if covered and ranged and max(r["day"] for r in ranged) >= t and all(
            r["engaged_views"] is not None for r in ranged):
        return int(sum(r["engaged_views"] for r in ranged))
    best, gap = None, None
    for s in snaps:
        if s["views"] is None:
            continue
        distance = abs((date.fromisoformat(s["day"]) - target).days)
        if distance <= TOLERANCE[offset] and (gap is None or distance < gap):
            best, gap = s, distance
    return int(best["views"]) if best else None


def _median(values: list[int]) -> int:
    return round(statistics.median(values))


def _group(rows: list[dict], today: date) -> dict | None:
    """Day-7 and day-28 medians for the rows, or None when the group is too small
    or has less than 4 weeks of data."""
    d7 = [r["d7"] for r in rows if r["d7"] is not None]
    if len(d7) < MIN_VIDEOS:
        return None
    first = min(r["pub"] for r in rows)
    if (today - first).days < MIN_DATA_DAYS:
        return None
    d28 = [r["d28"] for r in rows if r["d28"] is not None]
    return {
        "videos": len(d7), "d7_median": _median(d7),
        "d28_videos": len(d28), "d28_median": _median(d28) if len(d28) >= MIN_VIDEOS else None,
    }


def _load_rows(today: date) -> list[dict]:
    out: list[dict] = []
    with db.connect() as con:
        snaps: dict[str, list[dict]] = {}
        for s in con.execute("SELECT video_id, day, views FROM snapshots ORDER BY day").fetchall():
            snaps.setdefault(s["video_id"], []).append(dict(s))
        yta: dict[str, list[dict]] = {}
        for y in con.execute("SELECT video_id, day, engaged_views FROM yta_daily ORDER BY day").fetchall():
            yta.setdefault(y["video_id"], []).append(dict(y))
        first_yta = con.execute("SELECT MIN(day) FROM yta_daily").fetchone()[0]
        for v in con.execute("SELECT * FROM videos WHERE ours=1").fetchall():
            v = dict(v)
            if v["privacy"] not in (None, "", "public"):
                continue  # unlisted archive videos are low on purpose
            day = _public_day(v)
            if not day:
                continue
            pub = date.fromisoformat(day)
            vid = v["video_id"]
            out.append({
                "video_id": vid, "job_id": v["job_id"], "pub": pub,
                "kind": v["kind"] if v["kind"] in FORMATS else "other",
                "team": v["series_label"] or v["series_key"] or NO_TEAM,
                "short_style": v["short_style"], "thumb_arm": v["thumb_arm"], "title_style": v["title_style"],
                "d7": _view_number(pub, 7, snaps.get(vid, []), yta.get(vid, []), today, first_yta),
                "d28": _view_number(pub, 28, snaps.get(vid, []), yta.get(vid, []), today, first_yta),
            })
    return out


def _tiers() -> dict[str, str]:
    """team label -> tier, read from the team list without seeding or writing."""
    try:
        from publish import series

        loaded = series._load()
        if not isinstance(loaded, tuple):
            return {}
        return {r.label: r.tier or "" for r in loaded[1].values()}
    except Exception:  # noqa: BLE001 - only used to skip a pointless suggestion
        return {}


def _arms(rows: list[dict], field: str) -> list[dict]:
    """Day-7 medians per value of `field`; empty unless at least two values
    each have MIN_VIDEOS videos."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r[field] and r["d7"] is not None:
            by.setdefault(str(r[field]), []).append(r)
    arms = [{"arm": k, "videos": len(v), "d7_median": _median([x["d7"] for x in v]),
             "jobs": {x["job_id"] for x in v if x["job_id"]}}
            for k, v in sorted(by.items()) if len(v) >= MIN_VIDEOS]
    return arms if len(arms) >= 2 else []


def _public(arm: dict) -> dict:
    return {k: v for k, v in arm.items() if k != "jobs"}


def suggestions() -> dict:
    """{} when views_rankings is off; otherwise the groups, ranks and sentences."""
    from publish import settings as publishing_settings

    if not publishing_settings.current().views_rankings:
        return {}
    today = date.fromisoformat(local_day())
    rows = _load_rows(today)
    out: dict = {"enabled": True, "min_videos": MIN_VIDEOS, "min_data_days": MIN_DATA_DAYS,
                 "teams": [], "formats": [], "short_styles": [], "thumbnail_arms": [], "title_styles": [],
                 "suggestions": []}
    sentences: list[str] = out["suggestions"]

    for kind in FORMATS:
        g = _group([r for r in rows if r["kind"] == kind], today)
        if g:
            out["formats"].append({"label": kind, **g})

    # teams are compared on their full meetings, so the figures are like for like
    teams: dict[str, list[dict]] = {}
    for r in rows:
        if r["kind"] == "full":
            teams.setdefault(r["team"], []).append(r)
    ranked = []
    for label, items in teams.items():
        g = _group(items, today)
        if g:
            ranked.append({"label": label, **g})
    ranked.sort(key=lambda t: (-t["d7_median"], t["label"].lower()))
    third = len(ranked) // 3
    tiers = _tiers()
    for i, t in enumerate(ranked):
        t["position"] = i + 1
        t["rank"] = None
        if third >= 1:
            t["rank"] = "top" if i < third else ("bottom" if i >= len(ranked) - third else "middle")
        head = f"{t['label']}: day-7 median of {t['d7_median']:,} views over {t['videos']} videos is in the"
        if t["rank"] == "bottom" and tiers.get(t["label"]) not in ("archive", "never"):
            sentences.append(f"{head} bottom third; consider Archive")
        elif t["rank"] == "top":
            sentences.append(f"{head} top third; consider a 3rd Short or more topic cuts")
    out["teams"] = ranked

    styles = _arms([r for r in rows if r["kind"] == "short"], "short_style")
    by_style = {a["arm"]: a for a in styles}
    pairs = 0
    if "new" in by_style and "classic" in by_style:
        new, old = by_style["new"], by_style["classic"]
        pairs = len(new["jobs"] & old["jobs"])
        sentences.append(f"New Shorts style: day-7 median {new['d7_median']:,} vs {old['d7_median']:,} "
                         f"for classic over {pairs} pairs")
    out["short_styles"] = [_public(a) | ({"pairs": pairs} if a["arm"] in ("new", "classic") else {}) for a in styles]
    for field, key, word in (("thumb_arm", "thumbnail_arms", "Thumbnails"), ("title_style", "title_styles", "Title styles")):
        arms = _arms([r for r in rows if r["kind"] != "short"], field)
        out[key] = [_public(a) for a in arms]
        if arms:
            parts = ", ".join(f"{a['arm']} {a['d7_median']:,} over {a['videos']} videos" for a in arms)
            sentences.append(f"{word}, day-7 medians: {parts}")
    return out
