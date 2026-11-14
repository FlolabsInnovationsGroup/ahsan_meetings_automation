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

from core.logging import get_logger
from publish import extras, optional, series, youtube_extra
from publish import settings as publishing_settings

logger = get_logger(__name__)

FEATURE = "playlists"
DESCRIPTION = "Real R&D team meetings from FloLabs Innovations Group."
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


extras.register("playlist", _builder, _runner)
