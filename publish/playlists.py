"""Team playlists: each public video goes into its team's playlist once
YouTube has made it public.

A team gets up to three public playlists, named from the templates in the
publishing settings (the full meetings, the Shorts, the topic cuts). The
playlist is looked up in the team registry (publish/series.py), then by its
exact title on the channel, and only then created, so a lost state file never
makes a second playlist. Nothing here ever marks a playlist as a podcast.

Unlisted and private videos are never added: the builder queues nothing for
them, and the runner checks the video's real privacy right before the add
(a video pulled back to private in Studio is skipped). A scheduled video is
retried just after its publish time.

Every call goes through optional.call (permission check, quota, error
mapping); the follow-up queue (publish/extras.py) runs the runner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.logging import get_logger
from ledger import files
from publish import extras, optional, series, youtube, youtube_extra
from publish import settings as publishing_settings

logger = get_logger(__name__)

FEATURE = "playlists"
DESCRIPTION = "Real R&D team meetings from FloLabs Innovations Group."
PLAYLIST_BACKFILL_PER_DAY = 60  # about 3000 quota units, so uploads keep theirs
SHORT_MAX_SECONDS = 180
AFTER_PUBLISH = timedelta(minutes=15)
RECREATE_AFTER = timedelta(minutes=1)

_TEMPLATES = {"full": "playlist_full_template", "shorts": "playlist_shorts_template",
              "cuts": "playlist_cuts_template"}


def which_for(video_kind: str, cfg=None) -> str | None:
    """'full', 'shorts' or 'cuts' for an upload kind (full | short | topic);
    None when playlists are off or that kind has no playlist."""
    cfg = cfg or publishing_settings.current()
    if not cfg.playlists:
        return None
    if video_kind == "full":
        return "full"
    if video_kind == "short":
        return "shorts" if cfg.playlist_shorts else None
    if video_kind == "topic":
        return "cuts" if cfg.playlist_cuts else None
    return None


def title_for(label: str, which: str, cfg=None) -> str:
    cfg = cfg or publishing_settings.current()
    template = getattr(cfg, _TEMPLATES[which])
    return template.replace("{series}", label)


def ensure(series_key: str, which: str, cfg=None) -> optional.Outcome:
    """The team's playlist of this kind: DONE with its id as the value, or
    the Outcome that stopped it (retry, needs_reconnect, skipped...)."""
    row = series.lookup_key(series_key)
    if row is None:
        return optional.Outcome(optional.SKIPPED, reason="The team is not in the team list")
    known = row.playlists.get(which, "")
    if known:
        return optional.Outcome(optional.DONE, value=known)
    title = title_for(row.label, which, cfg)
    found = optional.call(FEATURE, youtube_extra.UNITS_LIST, lambda: youtube_extra.find_playlist(title))
    if found.status != optional.DONE:
        return found
    playlist_id = found.value
    if not playlist_id:
        made = optional.call(
            FEATURE, youtube_extra.UNITS_INSERT,
            lambda: youtube_extra.create_playlist(title, DESCRIPTION, privacy="public"))
        if made.status != optional.DONE:
            return made
        playlist_id = made.value
    try:
        series.set_playlist(row.key, which, playlist_id)
    except ValueError as exc:
        logger.warning("could not remember the %s playlist for %s: %s", which, row.key, exc)
    return optional.Outcome(optional.DONE, value=playlist_id)


# ------------------------------------------------------------------ queue


def _builder(job, item, posted, *, video_kind, index, cfg):
    if (getattr(posted, "privacy", "") or "") != "public":
        return None  # unlisted and private videos are never added
    which = which_for(video_kind, cfg)
    if which is None:
        return None
    topic = getattr(item, "topic", None) or getattr(item, "meeting", "") or ""
    row = series.lookup(topic)
    if row is None:
        return None
    publish_at = getattr(posted, "publish_at", None)
    not_before = extras._parse(publish_at) if publish_at else None
    task = extras.Task(
        kind="playlist", series_key=row.key, video_kind=video_kind,
        not_before=extras._iso(not_before) if not_before else extras._iso(extras._now()),
        data={"which": which, "waits_for_public": bool(publish_at)})
    return task


def _runner(task: extras.Task) -> optional.Outcome:
    which = (task.data or {}).get("which") or which_for(task.video_kind)
    if not which or not task.series_key:
        return optional.Outcome(optional.SKIPPED, reason="No playlist for this video")
    attempt = task.attempts

    # 1. is it public now?
    seen = optional.call(FEATURE, youtube_extra.UNITS_LIST,
                         lambda: youtube_extra.video_privacy(task.video_id), attempt=attempt)
    if seen.status != optional.DONE:
        return seen
    state = seen.value
    if state is None:
        return optional.Outcome(optional.SKIPPED, reason="The video is gone from YouTube")
    if state.get("privacyStatus") != "public":
        when = extras._parse(state.get("publishAt")) if state.get("publishAt") else None
        if when is not None:
            return optional.Outcome(optional.RETRY, reason="Still scheduled; will add it once it is public",
                                    retry_at=when + AFTER_PUBLISH)
        return optional.Outcome(optional.SKIPPED, reason="not public (pulled in Studio?)")

    # 2. the playlist
    playlist = ensure(task.series_key, which)
    if playlist.status != optional.DONE:
        return playlist
    playlist_id = playlist.value

    # 3. already in it?
    has, gone = _call_watching(
        youtube_extra.UNITS_LIST, lambda: youtube_extra.playlist_has_video(playlist_id, task.video_id), attempt)
    if gone:
        return _forget(task, which)
    if has.status != optional.DONE:
        return has
    if has.value:
        return optional.Outcome(optional.DONE, reason="Already in the playlist")

    # 4. add it
    added, gone = _call_watching(
        youtube_extra.UNITS_INSERT, lambda: youtube_extra.add_to_playlist(playlist_id, task.video_id), attempt)
    if gone:
        return _forget(task, which)
    return added


def _call_watching(units: int, fn, attempt: int) -> tuple[optional.Outcome, bool]:
    """optional.call, also telling whether Google said playlistNotFound."""
    seen = {"gone": False}

    def run():
        try:
            return fn()
        except Exception as exc:
            if optional._error_parts(exc)[1] == "playlistNotFound":
                seen["gone"] = True
            raise

    outcome = optional.call(FEATURE, units, run, attempt=attempt)
    return outcome, seen["gone"]


def _forget(task: extras.Task, which: str) -> optional.Outcome:
    """The playlist was deleted on YouTube: drop its ID so the next try makes
    (or finds) it again."""
    try:
        series.set_playlist(task.series_key, which, "")
    except ValueError as exc:
        logger.warning("could not clear the %s playlist for %s: %s", which, task.series_key, exc)
    return optional.Outcome(optional.RETRY, reason="The playlist was gone; it will be made again",
                            retry_at=datetime.now(UTC) + RECREATE_AFTER)


# ------------------------------------------------------------------ backfill


def _backfill_path():
    return files.state_dir() / "playlist_backfill.json"


def backfill_status() -> dict:
    """The last result of the daily backfill (for the page): when it ran, a plain
    note, and the counts. {"at": None} before the first run."""
    data = files.read_json(_backfill_path(), None)
    if not isinstance(data, dict) or not data.get("at"):
        return {"at": None}
    return {k: v for k, v in data.items() if k != "queued"}


def _save_backfill(result: dict, queued: dict) -> None:
    files.write_json(_backfill_path(), {**result, "at": files.now_iso(), "queued": queued})


def _backfill_team(video: dict, known, listed) -> tuple[str | None, str]:
    """(team key, why not) for a listed video: the ledger's meeting first, then
    the title against the team registry. Never guesses."""
    from autopilot import never_post
    from publish import channel_check

    title = video["title"]
    if never_post.match(title, None, listed) or never_post.match_words(title, listed):
        return None, "never"
    topics = [title]
    if known is not None:
        if channel_check._match_known(known, listed):
            return None, "never"
        tier, _team = channel_check._team_tier(known)
        if tier in ("never", "archive"):
            return None, "never"
        topics = [known.meeting, title]
    for topic in topics:
        if not series.series_key(topic):
            continue
        row = series.lookup(topic)
        if row is None:
            continue
        if series.tier_of(topic, None) in ("never", "archive") or row.tier in ("never", "archive"):
            return None, "never"
        if row.tier == "public":
            return row.key, ""
    return None, "no_team"


def playlist_backfill(service=None, now: datetime | None = None) -> bool:
    """The daily pass (extras.register_daily): every public video already on the
    channel (posted before the app, or before playlists) goes into its team's
    playlist, by queueing the same playlist task new uploads use. Newest first, at
    most PLAYLIST_BACKFILL_PER_DAY a day. Videos with no known team are counted,
    not guessed; never-post and never/archive teams are never touched. Returns
    False (try again on the next run) when playlists are off or the permission is
    missing, True once it has run."""
    from autopilot import never_post
    from ledger.uploads import get_ledger
    from publish import channel_check

    cfg = publishing_settings.current()
    old = files.read_json(_backfill_path(), None)
    queued = dict(old.get("queued") or {}) if isinstance(old, dict) else {}
    result = {"added": 0, "skipped_no_team": 0, "skipped_not_public": 0, "skipped_never": 0,
              "left_for_tomorrow": 0, "note": ""}

    def finish(note: str, ok: bool) -> bool:
        result["note"] = note
        _save_backfill(result, queued)
        return ok

    if not cfg.playlists:
        return finish("Playlists are switched off, so past videos were not added", False)
    scope = youtube.FEATURE_SCOPES.get(FEATURE)
    if scope is not None and not youtube.has_scope(scope):
        return finish("Reconnect YouTube to put past videos in playlists", False)
    try:
        videos, _truncated = channel_check._videos(service or channel_check._build_service())
    except Exception as exc:  # noqa: BLE001 - not connected, network, quota: tried again tomorrow
        logger.warning("playlist backfill could not list the channel: %s", exc)
        return finish(f"Could not list the channel: {str(exc)[:150]}", True)

    posted = {u.video_id: u for u in get_ledger().posted() if u.video_id}
    listed = never_post.entries()
    have = {t.video_id for t in extras.all_tasks() if t.kind == "playlist" and t.video_id}
    videos.sort(key=lambda v: v.get("published_at") or "", reverse=True)
    for video in videos:
        vid = video["id"]
        if video["privacy"] != "public":
            result["skipped_not_public"] += 1
            continue
        if vid in queued or vid in have:
            continue
        known = posted.get(vid)
        if known is not None and known.kind in ("full", "short", "topic"):
            kind = known.kind
        else:
            seconds = video.get("duration")
            kind = "short" if seconds is not None and seconds <= SHORT_MAX_SECONDS else "full"
        team, why = _backfill_team(video, known, listed)
        which = which_for(kind, cfg) if team else None
        if team is None or which is None:
            result["skipped_never" if why == "never" else "skipped_no_team"] += 1
            continue
        if result["added"] >= PLAYLIST_BACKFILL_PER_DAY:
            result["left_for_tomorrow"] += 1
            continue
        title = video["title"]
        task = extras.Task(kind="playlist", video_id=vid, series_key=team, video_kind=kind,
                           meeting=(known.meeting if known else "") or title, topic=title,
                           item_key=f"backfill:{vid}", data={"which": which, "backfill": True})
        extras._add(task, None, SimpleNamespace(key=task.item_key, topic=title, meeting=task.meeting, job_id="",
                                                meeting_id=None), SimpleNamespace(video_id=vid), kind, None)
        queued[vid] = files.now_iso()
        result["added"] += 1
    return finish("", True)


extras.register("playlist", _builder, _runner)
extras.register_daily("playlist", "playlist_backfill", playlist_backfill)
