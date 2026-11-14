"""The Views report: raw sums of what YouTube says, per video, format and team.

Numbers in a window are differences between stored daily snapshots:

- a video that went public inside the window: its latest total;
- a video with a snapshot at or before the window start: latest minus that one;
- a video first seen after the window start but public before it: the gain
  since its first snapshot, marked partial ("since <date>").

Per-format and per-team figures are raw sums and counts only: no ratios,
medians or rankings (the API policy until YouTube accepts the derived-metrics
amendment). After the analytics reconnect, the watch-time sums come from
yta_daily.
"""

from __future__ import annotations

from datetime import date, timedelta

from costs.db import local_day

from . import db

MAX_DAYS = 3650
BY = ("video", "format", "team")
FORMATS = ("full", "short", "topic", "other")
NO_TEAM = "No team"


def _day_of(text) -> str | None:
    """YYYY-MM-DD in the team's time zone from an ISO stamp, or None."""
    if not text:
        return None
    text = str(text)
    day = local_day(text) if len(text) > 10 else text
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    return day


def _public_day(video: dict) -> str | None:
    """The day the video goes (or went) public."""
    if video.get("privacy") != "public" and video.get("publish_at"):
        return _day_of(video["publish_at"]) or _day_of(video.get("published_at"))
    return _day_of(video.get("published_at"))


def _window_numbers(video: dict, snaps: list[dict], start: str) -> dict:
    """views / likes / comments in the window for one video, plus the flag."""
    out = {"views": 0, "likes": 0, "comments": 0, "partial_since": None}
    if not snaps:
        return out
    latest = snaps[-1]
    public_day = _public_day(video)
    before = [s for s in snaps if s["day"] <= start]
    if public_day and public_day > start:
        base = None
    elif before:
        base = before[-1]
    else:
        base = snaps[0]
        out["partial_since"] = base["day"]
    for key in ("views", "likes", "comments"):
        now = latest[key] or 0
        out[key] = now if base is None else max(0, now - (base[key] or 0))
    return out


def _watch_time(con, start: str, end: str) -> dict[str, dict]:
    rows = con.execute(
        "SELECT video_id, SUM(engaged_views) ev, SUM(views) v, SUM(minutes_watched) m, "
        "SUM(avg_view_duration_s * views) dur, SUM(avg_view_pct * views) pct, SUM(subs_gained) s, "
        "SUM(CASE WHEN avg_view_duration_s IS NOT NULL THEN views END) wv "
        "FROM yta_daily WHERE day > ? AND day <= ? GROUP BY video_id", (start, end)).fetchall()
    out = {}
    for r in rows:
        wv = r["wv"] or 0
        out[r["video_id"]] = {
            "engaged_views": r["ev"], "views": r["v"] or 0, "minutes_watched": round(r["m"] or 0, 1),
            "avg_view_duration_s": round(r["dur"] / wv, 1) if wv and r["dur"] is not None else None,
            "avg_view_pct": round(r["pct"] / wv, 1) if wv and r["pct"] is not None else None,
            "subs_gained": r["s"] or 0,
        }
    return out


def _blank(label: str) -> dict:
    return {"label": label, "videos": 0, "views": 0, "likes": 0, "comments": 0, "total_views": 0}


def _add(group: dict, row: dict) -> None:
    group["videos"] += 1
    for key in ("views", "likes", "comments", "total_views"):
        group[key] += row[key] or 0


def units_today() -> int:
    """Quota units the app counted today for the views pulls (stats and analytics)."""
    try:
        from publish import quota

        kinds = quota.today().get("by_kind") or {}
        return int(kinds.get("views", 0)) + int(kinds.get("analytics", 0))
    except Exception:  # noqa: BLE001 - bookkeeping must not break the page
        return 0


def report(days: int = 30, by: str = "video") -> dict:
    """The report for the last `days` days (today is day 1). Raises ValueError
    for a bad number of days or a bad `by`."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be a whole number from 1 to {MAX_DAYS}")
    if by not in BY:
        raise ValueError("by must be video, format or team")
    end = local_day()
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()  # the baseline day
    first_day = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
    videos: list[dict] = []
    with db.connect() as con:
        snaps: dict[str, list[dict]] = {}
        for s in con.execute("SELECT video_id, day, views, likes, comments FROM snapshots ORDER BY day").fetchall():
            snaps.setdefault(s["video_id"], []).append(dict(s))
        watch = _watch_time(con, start, end)
        has_watch = con.execute("SELECT COUNT(*) FROM yta_daily").fetchone()[0] > 0
        for v in con.execute("SELECT * FROM videos ORDER BY COALESCE(published_at, first_seen) DESC").fetchall():
            v = dict(v)
            video_snaps = snaps.get(v["video_id"], [])
            nums = _window_numbers(v, video_snaps, start)
            latest = video_snaps[-1] if video_snaps else None
            videos.append({
                "video_id": v["video_id"], "title": v["title"] or v["video_id"],
                "kind": v["kind"] if v["kind"] in FORMATS else "other",
                "team": v["series_label"] or v["series_key"] or NO_TEAM,
                "ours": bool(v["ours"]), "goes_public": _public_day(v),
                "privacy": v["privacy"], "short_style": v["short_style"],
                "views": nums["views"], "likes": nums["likes"], "comments": nums["comments"],
                "total_views": (latest or {}).get("views") or 0,
                "partial_since": nums["partial_since"], "has_data": latest is not None,
                "watch": watch.get(v["video_id"]),
            })
        pulls = con.execute("SELECT MIN(at) a, MAX(at) b FROM pulls WHERE ok=1").fetchone()
        first_pull, last_pull = pulls["a"], pulls["b"]
    groups: list[dict] = []
    if by in ("format", "team"):
        table: dict[str, dict] = {}
        for row in videos:
            label = row["kind"] if by == "format" else row["team"]
            _add(table.setdefault(label, _blank(label)), row)
        if by == "format":
            groups = [table.get(k) or _blank(k) for k in FORMATS]
        else:
            groups = sorted(table.values(), key=lambda g: g["label"].lower())
    styles: dict[str, dict] = {}
    totals = _blank("All")
    for row in videos:
        _add(totals, row)
        if row["kind"] == "short" and row["short_style"]:
            _add(styles.setdefault(row["short_style"], _blank(row["short_style"])), row)
    watch_totals = None
    if has_watch:
        watch_totals = {"engaged_views": 0, "minutes_watched": 0.0, "subs_gained": 0}
        for w in watch.values():
            watch_totals["engaged_views"] += w["engaged_views"] or 0
            watch_totals["minutes_watched"] = round(watch_totals["minutes_watched"] + w["minutes_watched"], 1)
            watch_totals["subs_gained"] += w["subs_gained"]
    return {
        "range": {"days": days, "start": first_day, "end": end}, "by": by,
        "videos": videos, "groups": groups, "totals": totals,
        "short_styles": sorted(styles.values(), key=lambda g: g["label"]),
        "first_pull_at": first_pull, "last_pull_at": last_pull,
        "units_today": units_today(), "watch": watch_totals,
    }
