# ruff: noqa: BLE001  (the loop must survive any failure in a step, so broad catches are deliberate)
"""The loop that drives the clips flow: watch, process, schedule, upload.

Each step is a method of Runner. The pieces it calls (downloading, picking,
rendering, uploading, the rights check) live in a Stages bundle so a test can
swap them for fakes. `tick()` runs every step once and never raises, so one bad
step or one bad video cannot stop the loop.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from clips.config import ClipsSettings, ConfigError, clips_settings
from clips.fetch import NotReady
from clips.models import (
    Clip,
    ClipKind,
    ClipStatus,
    Moment,
    Platform,
    RightsVerdict,
    Source,
    SourceVideo,
    VideoStatus,
)
from clips.package import timed_credit


def _lazy(module: str, name: str) -> Callable[..., Any]:
    """A callable that imports `module.name` on first use (keeps imports light)."""

    def call(*args: Any, **kwargs: Any) -> Any:
        return getattr(importlib.import_module(module), name)(*args, **kwargs)

    call.__name__ = name
    return call


@dataclass
class Stages:
    """The functions the runner calls. Tests pass fakes; default() uses the real ones."""

    list_new_videos: Callable[..., Any]
    fetch: Callable[..., Any]
    candidate_windows: Callable[..., Any]
    pick_moments: Callable[..., Any]
    render_short: Callable[..., Any]
    render_long: Callable[..., Any]
    write_copy: Callable[..., Any]
    check_copy: Callable[..., Any]
    next_slots: Callable[..., Any]
    upload_clip: Callable[..., Any]
    check_source: Callable[..., Any]
    check_video: Callable[..., Any]
    credit_line: Callable[..., Any]
    kill_switch_on: Callable[..., Any]
    cleanup: Callable[..., Any]
    # Extras, so the runner needs nothing else from the other modules.
    ffmpeg_available: Callable[..., Any] = lambda: True
    breaker_open: Callable[..., Any] = lambda store: False
    note_upload_result: Callable[..., Any] = lambda store, ok, detail="": None
    connected_channel: Callable[..., Any] = lambda: None
    loudness_curve: Callable[..., Any] = lambda path, **kw: []
    scene_changes: Callable[..., Any] = lambda path, **kw: []
    allowance_on: Callable[..., Any] = lambda day, first_post, settings: 1000
    check_clip: Callable[..., Any] = lambda clip, source: RightsVerdict(ok=True)
    assert_isolated: Callable[..., Any] = lambda: None
    # Checks that hold for every clip at once (isolation, a connected clips channel). Returns
    # the reason uploads cannot go ahead, or "" when they can.
    upload_preflight: Callable[..., Any] = lambda: ""
    # The language model for picking moments and writing copy (None when no key is set), and the
    # pull-back of already-uploaded, not-yet-public videos that the kill switch needs.
    make_llm: Callable[..., Any] = lambda: None
    pull_back: Callable[..., Any] = lambda store: {"pulled_back": [], "failed": [], "skipped": ""}
    # Long YouTube videos are fetched as a few stretches, never whole (clips.chunks).
    is_chunked: Callable[..., Any] = lambda video: False
    fetch_chunks: Callable[..., Any] = lambda video, dest_dir, **kw: []

    @classmethod
    def default(cls) -> Stages:
        return cls(
            list_new_videos=_lazy("clips.watch", "list_new_videos"),
            fetch=_lazy("clips.fetch", "fetch"),
            candidate_windows=_lazy("clips.signals", "candidate_windows"),
            pick_moments=_lazy("clips.pick", "pick_moments"),
            render_short=_lazy("clips.render", "render_short"),
            render_long=_lazy("clips.render", "render_long"),
            write_copy=_lazy("clips.package", "write_copy"),
            check_copy=_lazy("clips.package", "check_copy"),
            next_slots=_lazy("clips.slots", "next_slots"),
            upload_clip=_lazy("clips.uploader", "upload_clip"),
            check_source=_lazy("clips.rights", "check_source"),
            check_video=_lazy("clips.rights", "check_video"),
            credit_line=_lazy("clips.rights", "credit_line"),
            kill_switch_on=_lazy("clips.guard", "kill_switch_on"),
            cleanup=_lazy("clips.fetch", "cleanup"),
            ffmpeg_available=_lazy("clips.render", "ffmpeg_available"),
            breaker_open=_lazy("clips.guard", "breaker_open"),
            note_upload_result=_lazy("clips.guard", "note_upload_result"),
            connected_channel=_lazy("clips.uploader", "connected_channel"),
            loudness_curve=_lazy("clips.signals", "loudness_curve"),
            scene_changes=_lazy("clips.signals", "scene_changes"),
            allowance_on=_lazy("clips.slots", "allowance_on"),
            check_clip=_lazy("clips.rights", "check_clip"),
            assert_isolated=_lazy("clips.guard", "assert_isolated"),
            upload_preflight=_default_preflight,
            make_llm=_default_llm,
            pull_back=_lazy("clips.guard", "pull_back_now"),
            is_chunked=_lazy("clips.chunks", "is_chunked"),
            fetch_chunks=_lazy("clips.chunks", "fetch_chunks"),
        )


YT_OFF_NOTE = (
    "YouTube downloads are off (CLIPS_YOUTUBE_DOWNLOADS=false); use a watched folder, "
    "or switch the setting on if you have decided to allow downloads"
)


def _default_preflight() -> str:
    """Why no upload can go ahead right now, whichever clip it is, or "" when all is well."""
    guard = importlib.import_module("clips.guard")
    try:
        guard.assert_isolated()
        found = importlib.import_module("clips.uploader").connected_channel()
        if not found:
            return "No YouTube channel is connected. Connect the clips channel on the page."
        guard.check_channel(found[0])
    except Exception as exc:
        return str(exc) or type(exc).__name__
    return ""


def _default_llm() -> Any:
    """The app's configured language model, or None (no key, or this process is not on the clips
    folder: the model's usage goes into the cost meter under STORAGE_DIR, which must be the clips one)."""
    try:
        importlib.import_module("clips.guard").assert_isolated()
        return importlib.import_module("clips.llm").default_llm()
    except Exception as exc:
        logging.getLogger(__name__).warning("clips: no language model, using the signal-only fallback: %s", exc)
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _free_bytes(path: Path) -> int:
    """Free space on the disk holding `path` (a missing folder is measured by its nearest parent)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


CLAIM_STALE_S = 4 * 3600  # a claim older than this belongs to a process that died
GRACE = timedelta(minutes=15)  # a slot closer than this (or past) is booked again, never uploaded
MIN_FREE_BYTES = 3 * 1024**3
ARCHIVE_REJECTED_AFTER = timedelta(days=1)
ARCHIVE_UPLOADED_AFTER = timedelta(days=3)


def _is_guard_error(exc: BaseException) -> bool:
    """True for clips.guard.GuardError, matched by name so the guard module stays lazy."""
    return any(cls.__name__ == "GuardError" for cls in type(exc).__mro__)


class Runner:
    def __init__(
        self,
        store: Any = None,
        *,
        llm: Any = None,
        settings: ClipsSettings | None = None,
        stages: Stages | None = None,
    ) -> None:
        if store is None:
            from clips.store import Store

            store = Store()
        self.store = store
        self._llm = llm
        self._llm_ready = llm is not None
        self.settings = settings or clips_settings()
        self.stages = stages or Stages.default()
        self._auto_approved = 0  # running count for the current tick, reported by tick()
        self._below_bar = 0  # moments the quality bar dropped from the video being processed

    @property
    def llm(self) -> Any:
        """The language model: the one passed in, else the app's configured one (looked up once)."""
        if not self._llm_ready:
            self._llm_ready = True
            self._llm = self.stages.make_llm()
        return self._llm

    @llm.setter
    def llm(self, value: Any) -> None:
        self._llm, self._llm_ready = value, True

    # ---------------------------------------------------------- shared helpers

    def _data_dir(self) -> Path:
        """The folder the store lives in (claims and the heartbeat go next to the database)."""
        path = getattr(self.store, "path", None)
        return Path(path).parent if path else Path(self.settings.storage_dir)

    def _claim_path(self, kind: str, item_id: str) -> Path:
        name = hashlib.sha1(item_id.encode()).hexdigest()[:20]
        return self._data_dir() / "claims" / f"{kind}-{name}.lock"

    def _claim_live(self, kind: str, item_id: str) -> bool:
        try:
            return time.time() - self._claim_path(kind, item_id).stat().st_mtime < CLAIM_STALE_S
        except OSError:
            return False

    @contextmanager
    def _claimed(self, kind: str, item_id: str) -> Iterator[bool]:
        """Take a claim on one item, across processes (the loop and the page's pass). Yields False
        when another process holds it. A claim left by a dead process expires."""
        path = self._claim_path(kind, item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        got = False
        for _ in range(2):
            try:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                got = True
                break
            except FileExistsError:
                if self._claim_live(kind, item_id):
                    break
                with suppress(OSError):
                    path.unlink()
        try:
            yield got
        finally:
            if got:
                with suppress(OSError):
                    path.unlink()

    def _beat(self) -> None:
        """Touch the heartbeat file the loop's health check reads."""
        with suppress(OSError):
            (self._data_dir() / "last_tick").write_text(_now().isoformat(), encoding="utf-8")

    @staticmethod
    def _source_problem(source: Source | None, verdict: Any) -> str:
        """Why a source cannot be used right now, or empty."""
        if source is None:
            return "source no longer exists"
        if not source.enabled:
            return "source is switched off"
        if not verdict.ok:
            return "; ".join(verdict.reasons) or "rights check failed"
        return ""

    # ----------------------------------------------------------------- watch

    def watch(self) -> int:
        """Look for new videos on every enabled, rights-clear source. Returns how many were saved."""
        saved = 0
        try:
            self.settings.not_before_date  # noqa: B018 - raises ConfigError for a value that is not a date
        except ConfigError as exc:
            self._say_once("watch_config", f"watching is stopped: {exc}")
            return 0
        for source in self.store.list_sources(enabled_only=True):
            if source.platform is Platform.YOUTUBE and not self.settings.youtube_downloads:
                self._note_downloads_off(source)
                continue
            verdict = self.stages.check_source(source)
            if not verdict.ok:
                self.store.add_event(
                    "watch", f"{source.id} skipped: {'; '.join(verdict.reasons)}", source_id=source.id
                )
                continue
            try:
                known = self.store.known_native_ids(source.id)
                videos = self.stages.list_new_videos(source, known)
            except Exception as exc:  # one broken source must not stop the others
                self.store.add_event("watch", f"{source.id} failed: {exc}", source_id=source.id)
                continue
            for video in videos:
                saved += self._save_new_video(source, video)
        return saved

    def _note_downloads_off(self, source: Source) -> None:
        """Say once a day (not once per loop turn) that a YouTube source is not being listed."""
        self._say_once(f"yt_off:{source.id}", f"{source.id} not listed: {YT_OFF_NOTE}", source_id=source.id)

    def _say_once(self, key: str, message: str, *, source_id: str = "") -> None:
        """Write a watch event at most once per day for this key."""
        today = _now().date().isoformat()
        if self.store.get_flag(key) == today:
            return
        self.store.set_flag(key, today)
        self.store.add_event("watch", message, source_id=source_id)

    def _save_new_video(self, source: Source, video: SourceVideo) -> int:
        video = video.model_copy(update={"source_id": source.id})
        verdict = self.stages.check_video(source, video)
        if verdict.ok:
            video.status = VideoStatus.NEW
        else:
            video.status = VideoStatus.SKIPPED
            video.skip_reason = "; ".join(verdict.reasons) or "rights check failed"
        self.store.save_video(video)
        return 1

    # --------------------------------------------------------------- process

    def process(self, limit: int | None = None) -> int:
        """Turn NEW videos (oldest first) into DRAFT clips. Returns how many clips were made."""
        limit = self.settings.videos_per_tick if limit is None else limit
        self._recover_interrupted()
        made = tried = 0
        if limit <= 0:
            return 0
        for video in self.store.list_videos(status=VideoStatus.NEW, limit=1000):
            if tried >= limit:
                break
            source = self.store.get_source(video.source_id)
            if source is not None and not source.enabled:
                continue  # stays NEW and resumes when the source is switched on again
            if self._low_disk() or self._backlog_full():
                break
            with self._claimed("video", video.id) as got:
                if not got:
                    continue
                fresh = self.store.get_video(video.id)
                if fresh is None or fresh.status is not VideoStatus.NEW:
                    continue
                tried += 1
                try:
                    made += self._process_video(fresh)
                except Exception as exc:  # a failed video is marked and the loop goes on
                    fresh.status = VideoStatus.FAILED
                    fresh.error = str(exc)[:500]
                    self.store.save_video(fresh)
                    self.store.add_event(
                        "process", f"{fresh.id} failed: {exc}", source_id=fresh.source_id
                    )
        if made:
            self.auto_approve_drafts()  # new Shorts go through at once when the switch is on
        return made

    def _recover_interrupted(self) -> None:
        """A video left FETCHED or PICKED with nobody working on it was cut off (restart, crash).
        With no clips yet it goes back to NEW; otherwise it is marked failed so nothing is made twice."""
        for status in (VideoStatus.FETCHED, VideoStatus.PICKED):
            for video in self.store.list_videos(status=status, limit=1000):
                if self._claim_live("video", video.id):
                    continue
                made = [c for c in self.store.list_clips(limit=100000) if c.video_id == video.id]
                if made:
                    video.status, video.error = VideoStatus.FAILED, "interrupted after some clips were made"
                else:
                    video.status, video.error = VideoStatus.NEW, ""
                    if self._is_chunk_file(video):
                        video.path = ""  # an older run saved a chunk here; the video must be re-planned
                self.store.save_video(video)
                self.store.add_event(
                    "process", f"{video.id} was interrupted, now {video.status.value}", source_id=video.source_id
                )

    def _is_chunk_file(self, video: SourceVideo) -> bool:
        """True when video.path is one of our downloaded stretches (<native_id>_cNN_*) in the media folder."""
        if not video.path:
            return False
        path = Path(video.path)
        try:
            inside = path.resolve().parent == Path(self.settings.media_dir).resolve()
        except OSError:
            return False
        return inside and re.fullmatch(rf"{re.escape(video.native_id)}_c\d+_.*", path.name) is not None

    def _low_disk(self) -> bool:
        """True (with one event an hour) when the disk is too full to fetch and render."""
        minimum = int(float(getattr(self.settings, "min_free_gb", 3)) * 1024**3)
        try:
            free = _free_bytes(self._data_dir())
        except OSError:
            return False
        if free >= minimum:
            return False
        stamp = self.store.get_flag("low_disk_at", "")
        if not stamp or (_now() - datetime.fromisoformat(stamp)) > timedelta(hours=1):
            self.store.set_flag("low_disk_at", _now().isoformat())
            self.store.add_event(
                "process", f"not enough free disk ({free // 1024**2} MiB); nothing is fetched or rendered"
            )
        return True

    def _backlog_full(self) -> bool:
        """True when the clips already made would take more than max_ready_days to post."""
        days = int(getattr(self.settings, "max_ready_days", 3))
        if days <= 0:
            return False
        waiting = len(
            self.store.list_clips(
                status=[ClipStatus.DRAFT, ClipStatus.APPROVED, ClipStatus.SCHEDULED], limit=100000
            )
        )
        today, first = _now().date(), self.store.first_post_date()
        room = sum(
            self.stages.allowance_on(today + timedelta(days=i), first, self.settings) for i in range(days)
        )
        return waiting >= room

    def _process_video(self, video: SourceVideo) -> int:
        source = self.store.get_source(video.source_id)
        if source is None:
            return self._skip(video, "source no longer exists")
        if not source.enabled:
            return 0  # switched off: the video stays NEW until the source is on again
        verdict = self.stages.check_source(source)
        if verdict.ok:
            verdict = self.stages.check_video(source, video)
        if not verdict.ok:
            return self._skip(video, "; ".join(verdict.reasons) or "rights check failed")

        s = self.settings
        self._below_bar = 0
        # A long YouTube video is fetched as a few stretches. Never when downloads are off: then the
        # normal fetch runs and refuses the download itself.
        if video.platform is Platform.YOUTUBE and s.youtube_downloads and self.stages.is_chunked(video):
            try:
                items = self._chunk_items(video, source)
            except NotReady as exc:  # live or still processing: stays NEW for a later pass
                self.store.add_event("process", f"{video.id} not ready: {exc}", source_id=source.id)
                return 0
        else:
            fetched = self.stages.fetch(video, s.media_dir, max_height=s.download_max_height)
            video.path = fetched.video_path
            video.status = VideoStatus.FETCHED
            self.store.save_video(video)
            moments = self._pick(video, fetched, source)
            video.status = VideoStatus.PICKED
            self.store.save_video(video)
            items = [(fetched, None, m) for m in moments if self._keep(m, source)]
        return self._render_items(video, source, items)

    def _render_items(self, video: SourceVideo, source: Source, items: list[tuple[Any, float | None, Moment]]) -> int:
        """Render each (file, where that file starts in the original, moment). One failure keeps the rest."""
        if items and not self.stages.ffmpeg_available():
            raise RuntimeError("ffmpeg is not installed, so nothing can be rendered")
        credit = self.stages.credit_line(source, video)
        made, errors = 0, []
        for fetched, offset, moment in items:
            try:
                self._make_clip(video, fetched, source, moment, credit, offset_s=offset)
                made += 1
            except Exception as exc:  # one bad moment must not lose the others
                errors.append(str(exc))
                self.store.add_event(
                    "process", f"{video.id} moment failed: {exc}", source_id=source.id
                )
        if items and not made:
            raise RuntimeError(f"every moment failed: {errors[0]}")
        video.status = VideoStatus.DONE
        video.error = ""
        self.store.save_video(video)
        if not items and self._below_bar:
            message = (
                f"{video.id}: no moment was good enough (the bar is {self.settings.min_moment_score}); "
                "nothing was made"
            )
        else:
            message = f"{video.id}: {made} clip(s) made"
        self.store.add_event("process", message, source_id=source.id, data={"clips": made})
        return made

    def _chunk_items(self, video: SourceVideo, source: Source) -> list[tuple[Any, float | None, Moment]]:
        """Fetch the strongest stretches of a long video and pick Shorts inside each. The best moments
        of all stretches go on, best first, up to stream_shorts_max."""
        s = self.settings
        chunks = list(self.stages.fetch_chunks(video, s.media_dir, max_height=s.download_max_height) or [])
        if not chunks:
            raise RuntimeError("no stretch of the video could be fetched")
        # video.path stays empty: a chunk file is not the video, and a path would make a retry
        # treat one stretch as the whole thing.
        video.status = VideoStatus.FETCHED
        self.store.save_video(video)
        found: list[tuple[Any, float, Moment, float]] = []
        for chunk in chunks:
            try:
                picked = self._pick(video, chunk.fetched, source, longs=0, shorts=min(s.shorts_per_video, s.stream_shorts_max))
            except Exception as exc:  # one failed stretch must not lose the others
                self.store.add_event(
                    "process", f"{video.id} stretch at {int(chunk.offset_s)} s failed: {exc}", source_id=source.id
                )
                continue
            found += [
                (chunk.fetched, float(chunk.offset_s), m, float(chunk.strength))
                for m in picked
                if m.kind is ClipKind.SHORT and self._keep(m, source)
            ]
        video.status = VideoStatus.PICKED
        self.store.save_video(video)
        found.sort(key=lambda x: (x[2].score, x[3]), reverse=True)
        chosen = self._drop_duplicates(found)[: s.stream_shorts_max]
        return [(fetched, offset, moment) for fetched, offset, moment, _ in chosen]

    @staticmethod
    def _drop_duplicates(found: list[tuple[Any, float, Moment, float]]) -> list[tuple[Any, float, Moment, float]]:
        """Keep the best of any moments that cover mostly the same part of the original (stretches can overlap)."""
        kept: list[tuple[Any, float, Moment, float]] = []
        spans: list[tuple[float, float]] = []
        for item in found:
            start = item[1] + item[2].start_s
            end = item[1] + item[2].end_s
            if any(min(end, b) - max(start, a) > 0.5 * max(min(end - start, b - a), 1e-6) for a, b in spans):
                continue
            kept.append(item)
            spans.append((start, end))
        return kept

    def _skip(self, video: SourceVideo, reason: str) -> int:
        video.status = VideoStatus.SKIPPED
        video.skip_reason = reason
        self.store.save_video(video)
        self.store.add_event("process", f"{video.id} skipped: {reason}", source_id=video.source_id)
        return 0

    def _llm_picked(self, moment: Moment) -> bool:
        """True when a language model chose this moment (the signal-only fallback says so in its reason)."""
        return self.llm is not None and not moment.reason.startswith("picked from signals")

    def _clears_bar(self, moment: Moment) -> bool:
        """The quality bar holds for model-picked moments only; signal-only scores are not comparable."""
        return not self._llm_picked(moment) or moment.score >= self.settings.min_moment_score

    def _pick(
        self, video: SourceVideo, fetched: Any, source: Source, *, shorts: int | None = None, longs: int | None = None
    ) -> list[Moment]:
        s = self.settings
        n_short = s.shorts_per_video if shorts is None else shorts
        n_long = s.long_per_video if longs is None else longs
        path = Path(fetched.video_path)
        loud = scenes = None
        try:
            if getattr(fetched, "has_audio", True):
                loud = self.stages.loudness_curve(path)
        except Exception as exc:  # no signal is better than no clips
            self.store.add_event("process", f"{video.id}: no loudness curve ({exc})", source_id=source.id)
        try:
            scenes = self.stages.scene_changes(path)
        except Exception as exc:
            self.store.add_event("process", f"{video.id}: no scene changes ({exc})", source_id=source.id)
        signals = {"loudness": loud, "scenes": scenes}
        windows = list(self.stages.candidate_windows(fetched, kind=ClipKind.SHORT, **signals) or [])
        if n_long > 0:
            windows += list(self.stages.candidate_windows(fetched, kind=ClipKind.LONG, **signals) or [])
        moments = self.stages.pick_moments(
            video,
            fetched,
            source,
            llm=self.llm,
            max_shorts=n_short,
            max_long=n_long,
            candidates=windows or None,
        )
        limits = {ClipKind.SHORT: n_short, ClipKind.LONG: n_long}
        counts = {ClipKind.SHORT: 0, ClipKind.LONG: 0}
        kept = []
        for moment in moments:
            if not self._clears_bar(moment):
                self._below_bar += 1
                continue
            if counts[moment.kind] < limits[moment.kind]:
                counts[moment.kind] += 1
                kept.append(moment)
        return kept

    @staticmethod
    def _keep(moment: Moment, source: Source) -> bool:
        """Drop story spoilers and licensed music when the source says so."""
        if moment.spoiler and source.avoid_spoilers:
            return False
        return not (moment.has_music and source.skip_music)

    def _make_clip(
        self,
        video: SourceVideo,
        fetched: Any,
        source: Source,
        moment: Moment,
        credit: str,
        *,
        offset_s: float | None = None,
    ) -> None:
        from clips.store import new_clip_id

        clip_id = new_clip_id()
        # Where the clip starts in the original video; only stretches of a long video have an offset.
        origin_s = 0.0 if offset_s is None else offset_s + moment.start_s
        written = timed_credit(credit, video, origin_s)  # the description's link carries the time
        src = Path(fetched.video_path)
        out_dir = self.settings.renders_dir
        if moment.kind is ClipKind.LONG:
            rendered = self.stages.render_long(src, moment, source, out_dir, clip_id=clip_id, credit=credit)
        else:
            captions = fetched.captions if self.settings.captions and fetched.captions else None
            rendered = self.stages.render_short(
                src, moment, source, out_dir, clip_id=clip_id, captions=captions, credit=credit
            )
        copy = self.stages.write_copy(moment, source, video, kind=moment.kind, credit=written, llm=self.llm)
        problems = self.stages.check_copy(copy, kind=moment.kind, credit=written)
        clip = Clip(
            id=clip_id,
            source_id=source.id,
            video_id=video.id,
            kind=moment.kind,
            moment=moment.model_copy(update={"layout": rendered.layout}),
            file=rendered.file,
            thumb=rendered.thumb,
            duration_s=rendered.duration_s,
            text=copy,
            source_offset_s=origin_s,
            credit=written,
            rights=source.rights,
            evidence_url=source.evidence_url,
            status=ClipStatus.DRAFT,
            error="; ".join(problems),
        )
        self.store.save_clip(clip)

    # ---------------------------------------------------------- auto-approve

    def auto_approve_on(self) -> bool:
        """The Auto-approve Shorts switch. The store flag, once set, wins over the setting."""
        flag = self.store.get_flag("auto_approve", "")
        if flag in ("on", "off"):
            return flag == "on"
        return bool(self.settings.auto_approve)

    def set_auto_approve(self, on: bool) -> bool:
        """Flip the switch (kept in the store, so it outlives this process) and log who changed it."""
        self.store.set_flag("auto_approve", "on" if on else "off")
        self.store.add_event("auto_approve", f"auto-approve of Shorts switched {'on' if on else 'off'}")
        return on

    def auto_approve_drafts(self) -> int:
        """With the switch on, approve every DRAFT Short, warnings or not. Longer clips are left alone.

        It calls approve(), the same path a person's click takes, so a source that is gone, switched
        off or failing its rights check refuses here exactly as it does for a person. Nothing is
        approved while the kill switch is on. Returns how many were approved."""
        try:
            if not self.auto_approve_on() or self.stages.kill_switch_on():
                return 0
        except Exception:
            return 0  # cannot tell whether posting is stopped: approve nothing
        done = 0
        for clip in self.store.list_clips(status=ClipStatus.DRAFT, limit=100000):
            if clip.kind is not ClipKind.SHORT:
                continue
            warnings = clip.error  # approve() clears it, so keep it in the log first
            try:
                self.approve(clip.id)
            except (ValueError, KeyError):
                continue  # refused like a manual approval: it stays a draft for a person
            note = f" (had warnings: {warnings})" if warnings else ""
            self.store.add_event(
                "auto_approve", f"{clip.id} approved automatically, nobody reviewed it{note}",
                clip_id=clip.id, source_id=clip.source_id, data={"warnings": warnings} if warnings else None,
            )
            done += 1
        self._auto_approved += done
        return done

    # -------------------------------------------------------------- schedule

    def schedule(self) -> int:
        """Give approved clips their publish times. Returns how many were scheduled.

        A scheduled clip whose time has passed (the loop was stopped) goes back for a fresh slot:
        a missed slot is never posted late and all at once."""
        self.auto_approve_drafts()  # drafts that were already waiting when the switch went on
        rebooked = self.store.rebook_overdue(_now() + GRACE)
        if rebooked:
            self.store.add_event(
                "schedule", f"{len(rebooked)} overdue clip(s) sent back for a new slot", data={"clips": len(rebooked)}
            )
        statuses = [ClipStatus.APPROVED]
        if not self.settings.review_required:
            statuses.append(ClipStatus.DRAFT)
        clips = self.store.list_clips(status=statuses)
        if not self.settings.review_required:
            # A draft whose copy still has problems needs a person even with review off.
            clips = [c for c in clips if c.status is ClipStatus.APPROVED or not c.error]
        ready = [c for c in clips if self._rights_ok_to_schedule(c)]
        if not ready:
            return 0
        slots = self.stages.next_slots(
            _now(),
            self.store.booked_times(),
            self.settings,
            self.store.first_post_date(),
            count=len(ready),
        )
        done = 0
        for clip, slot in zip(ready, slots, strict=False):
            self.store.set_clip_status(clip.id, ClipStatus.SCHEDULED, publish_at=slot, error="")
            done += 1
        if done:
            self.store.add_event("schedule", f"{done} clip(s) scheduled", data={"clips": done})
        return done

    def _rights_ok_to_schedule(self, clip: Clip) -> bool:
        source = self.store.get_source(clip.source_id)
        verdict = self.stages.check_source(source) if source else None
        reason = self._source_problem(source, verdict)
        if not reason:
            return True
        self.store.set_clip_status(clip.id, ClipStatus.HELD, error=reason)
        self.store.add_event("schedule", f"{clip.id} held: {reason}", clip_id=clip.id, source_id=clip.source_id)
        return False

    # ---------------------------------------------------------------- upload

    def due_clips(self) -> list[Clip]:
        """Scheduled clips to upload now (private, with a publish time still ahead), earliest first."""
        now = _now()
        horizon = now + timedelta(hours=self.settings.upload_ahead_hours)
        due = [
            c
            for c in self.store.list_clips(status=ClipStatus.SCHEDULED)
            if c.publish_at is not None and now + GRACE < c.publish_at <= horizon
        ]
        due.sort(key=lambda c: c.publish_at)
        return due

    def upload_due(self, limit: int | None = None) -> int:
        """Upload scheduled clips that are due soon (private with a publish time). Returns how many went up."""
        limit = self.settings.uploads_per_tick if limit is None else limit
        recovered = self.store.recover_stale_uploads()
        if recovered:
            self.store.add_event("upload", f"{len(recovered)} cut-off upload(s) put back to scheduled")
        self._rebook_late()
        due = self.due_clips()
        if not due or limit <= 0:
            return 0
        reason = self.stages.upload_preflight()
        if reason:
            # Nothing is wrong with the clips: wait, and say so once per reason.
            if self.store.get_flag("upload_wait") != reason:
                self.store.set_flag("upload_wait", reason)
                self.store.add_event("upload", f"uploads are waiting: {reason}")
            return 0
        self.store.set_flag("upload_wait", "")
        uploaded = 0
        for clip in due[:limit]:
            if self.stages.kill_switch_on() or self.stages.breaker_open(self.store):
                break
            if self._upload_one(clip):
                uploaded += 1
        return uploaded

    def _rebook_late(self) -> int:
        """A SCHEDULED clip whose publish time has passed or is under 15 minutes away goes back to
        APPROVED (the schedule step gives it a new slot, with review on or off). Never posted late."""
        moved = self.store.rebook_overdue(_now() + GRACE)
        for clip_id in moved:
            clip = self.store.get_clip(clip_id)
            self.store.add_event(
                "upload", f"{clip_id} missed its slot (past or under 15 minutes away); back to approved for a new slot",
                clip_id=clip_id, source_id=clip.source_id if clip else "",
            )
        return len(moved)

    def _upload_one(self, clip: Clip) -> bool:
        source = self.store.get_source(clip.source_id)
        # upload_clip takes the clip itself (SCHEDULED -> UPLOADING in one SQLite step), so only one
        # process, the loop or the page's pass, can send it. The loser raises ClipBusy.
        try:
            result = self.stages.upload_clip(clip, source, self.store)
        except Exception as exc:
            names = {c.__name__ for c in type(exc).__mro__}
            if "ClipBusy" in names:
                return False  # someone else has it: not a failure, nothing to record
            if "NotScheduledError" in names:
                # nothing was sent: new slot, not a failure and not counted
                self.store.set_clip_status(clip.id, ClipStatus.APPROVED, publish_at=None, error="")
                self.store.add_event(
                    "upload", f"{clip.id} back to approved for a new slot: {exc}", clip_id=clip.id,
                    source_id=clip.source_id,
                )
                return False
            if "GuardError" in names:
                return self._after_guard_refusal(clip, exc)
            self.store.set_clip_status(clip.id, ClipStatus.SCHEDULED, error=str(exc)[:500])
            self.store.add_event("upload", f"{clip.id} failed: {exc}", clip_id=clip.id, source_id=clip.source_id)
            if "UploadError" not in names:
                # upload_clip already counts its own UploadError; count anything else here
                self._count_result(False, str(exc))
            return False
        self.store.set_clip_status(clip.id, ClipStatus.UPLOADED, youtube_id=result.youtube_id, error="")
        self.store.add_event(
            "upload",
            f"{clip.id} uploaded as {result.youtube_id}",
            clip_id=clip.id,
            source_id=clip.source_id,
            data={"youtube_id": result.youtube_id},
        )
        try:
            stopped = self.stages.kill_switch_on()
        except Exception:
            stopped = True  # cannot tell: be safe
        if stopped:
            # Stop was pressed while this clip was going up: it must not be left to go public.
            self._pull_back("a clip finished uploading after the kill switch went on")
        return True  # upload_clip already recorded the success for the breaker

    def _after_guard_refusal(self, clip: Clip, exc: Exception) -> bool:
        """A guard refused this clip. Only a refusal about the clip itself (rights, file, never-post)
        holds it. Switched-off posting and a slot that just passed leave it scheduled."""
        now = _now()
        waiting = self.stages.kill_switch_on() or self.stages.breaker_open(self.store)
        late = clip.publish_at is not None and clip.publish_at <= now + GRACE
        if waiting or late:
            self.store.set_clip_status(clip.id, ClipStatus.SCHEDULED, error="")
            self.store.add_event("upload", f"{clip.id} waits: {exc}", clip_id=clip.id, source_id=clip.source_id)
            return False
        self.store.set_clip_status(clip.id, ClipStatus.HELD, error=str(exc))
        self.store.add_event("upload", f"{clip.id} held: {exc}", clip_id=clip.id, source_id=clip.source_id)
        return False

    def _count_result(self, ok: bool, detail: str) -> None:
        try:
            self.stages.note_upload_result(self.store, ok, detail)
        except Exception as exc:  # the count is a safety net, not a reason to crash
            self.store.add_event("upload", f"could not record the upload result: {exc}")

    # ------------------------------------------------------------------ tick

    def tick(self) -> dict:
        """Run every step once. Never raises. Returns the count each step produced."""
        result: dict[str, Any] = {}
        self._beat()
        try:
            if self.stages.kill_switch_on():
                # Stop by a KILL file (not the button) never pulled anything back: do it here, every turn,
                # until nothing uploaded is still waiting to go public.
                result = {"killed": True}
                pulled = self._pull_back("kill switch is on")
                if pulled.get("pulled_back") or pulled.get("failed"):
                    result["pulled_back"] = len(pulled.get("pulled_back", []))
                    result["pull_back_failed"] = len(pulled.get("failed", []))
                return result
        except Exception as exc:
            self._safe_event("tick", f"kill switch check failed, stopping: {exc}")
            return {"killed": True, "error": str(exc)}
        try:
            self.stages.assert_isolated()
        except Exception as exc:
            # The company's folder (or none): no fetching, no model calls, no uploads from here.
            reason = str(exc) or type(exc).__name__
            self._safe_event("tick", f"not running, this process is not on the clips folder: {reason}")
            return {"blocked": reason}
        self._auto_approved = 0
        for name, step in (
            ("watched", self.watch),
            ("processed", self.process),
            ("scheduled", self.schedule),
            ("uploaded", self.upload_due),
        ):
            try:
                result[name] = step()
            except Exception as exc:
                result[name] = 0
                result.setdefault("errors", {})[name] = str(exc)
                self._safe_event("tick", f"{name} step failed: {exc}")
            self._beat()
        result["auto_approved"] = self._auto_approved
        try:
            self.stages.cleanup(self.settings.media_dir, self.settings.media_retention_hours)
            self._cleanup_renders()
        except Exception as exc:
            self._safe_event("tick", f"cleanup failed: {exc}")
        self._beat()
        self._safe_event("tick", "tick done", data={k: v for k, v in result.items() if k != "errors"})
        return result

    def _pull_back(self, why: str) -> dict:
        """Take uploaded, not-yet-public videos back to private. Never raises."""
        try:
            out = self.stages.pull_back(self.store)
        except Exception as exc:
            self._safe_event("kill", f"could not pull back scheduled videos ({why}): {exc}")
            return {"pulled_back": [], "failed": [], "skipped": str(exc)}
        return out if isinstance(out, dict) else {"pulled_back": [], "failed": [], "skipped": ""}

    def _cleanup_renders(self) -> int:
        """Delete the video and picture files of clips that are done with: rejected ones after a day,
        uploaded ones a few days after their publish time. Only files inside the renders folder."""
        now, gone = _now(), 0
        root = Path(self.settings.renders_dir).resolve()
        for clip in self.store.list_clips(status=[ClipStatus.REJECTED, ClipStatus.UPLOADED], limit=100000):
            if not (clip.file or clip.thumb):
                continue
            if clip.status is ClipStatus.REJECTED:
                stamp, wait = clip.updated_at or clip.created_at, ARCHIVE_REJECTED_AFTER
            else:
                stamp, wait = clip.publish_at or clip.updated_at, ARCHIVE_UPLOADED_AFTER
            if stamp is None:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            if now - stamp < wait:
                continue
            for name in (clip.file, clip.thumb):
                if not name:
                    continue
                path = Path(name).resolve()
                if path.is_relative_to(root):
                    with suppress(OSError):
                        path.unlink()
                        gone += 1
            self.store.set_clip_status(clip.id, clip.status, file="", thumb="")
        if gone:
            self.store.add_event("cleanup", f"{gone} rendered file(s) deleted", data={"files": gone})
        return gone

    def _safe_event(self, kind: str, message: str, data: dict | None = None) -> None:
        try:
            self.store.add_event(kind, message, data=data)
        except Exception:  # noqa: S110 - nowhere left to report it
            pass

    # -------------------------------------------------------- review actions

    def approve(self, clip_id: str) -> Clip:
        """DRAFT -> APPROVED. Refuses when the source no longer passes the rights check."""
        clip = self._get_clip(clip_id)
        if clip.status is not ClipStatus.DRAFT:
            raise ValueError(f"clip {clip_id} is {clip.status.value}, only a draft can be approved")
        source = self.store.get_source(clip.source_id)
        verdict = self.stages.check_source(source) if source else None
        why = self._source_problem(source, verdict)
        if why:
            raise ValueError(f"cannot approve: {why}")
        # Compare-and-set: if a person rejected or removed it since we read it, that decision stands.
        if not self.store.transition(clip_id, ClipStatus.DRAFT, ClipStatus.APPROVED, error=""):
            raise ValueError(f"clip {clip_id} is no longer a draft, so it was not approved")
        return self._get_clip(clip_id)

    def release(self, clip_id: str) -> Clip:
        """HELD -> APPROVED (it gets a fresh slot). Refuses unless the rights checks pass again."""
        clip = self._get_clip(clip_id)
        if clip.status is not ClipStatus.HELD:
            raise ValueError(f"clip {clip_id} is {clip.status.value}, only a held clip can be released")
        source = self.store.get_source(clip.source_id)
        verdict = self.stages.check_source(source) if source else None
        why = self._source_problem(source, verdict)
        if not why:
            final = self.stages.check_clip(clip, source)
            if not final.ok:
                why = "; ".join(final.reasons) or "rights check failed"
        if why:
            raise ValueError(f"cannot release: {why}")
        self.store.add_event("release", f"{clip_id} released by a person", clip_id=clip_id, source_id=clip.source_id)
        return self.store.set_clip_status(clip_id, ClipStatus.APPROVED, publish_at=None, error="")

    def reject(self, clip_id: str, reason: str = "") -> Clip:
        """Turn a clip down. Anything not yet on YouTube can be rejected."""
        clip = self._get_clip(clip_id)
        if clip.status in (ClipStatus.UPLOADED, ClipStatus.UPLOADING, ClipStatus.REJECTED):
            raise ValueError(f"clip {clip_id} is {clip.status.value} and cannot be rejected")
        return self.store.set_clip_status(clip_id, ClipStatus.REJECTED, reject_reason=reason)

    def _get_clip(self, clip_id: str) -> Clip:
        clip = self.store.get_clip(clip_id)
        if clip is None:
            raise KeyError(f"no clip {clip_id}")
        return clip

    # ----------------------------------------------------------------- status

    def status(self) -> dict:
        """A snapshot for the page and the command line."""
        clips = self.store.list_clips(limit=100000)
        counts = {s.value: 0 for s in ClipStatus}
        for clip in clips:
            counts[clip.status.value] += 1
        sources = self.store.list_sources()
        enabled = sum(1 for s in sources if s.enabled)
        youtube = [] if self.settings.youtube_downloads else [
            {"id": s.id, "name": s.name, "note": YT_OFF_NOTE} for s in sources if s.platform is Platform.YOUTUBE
        ]
        return {
            "clips": counts,
            "sources": {"enabled": enabled, "disabled": len(sources) - enabled},
            "youtube_downloads": self.settings.youtube_downloads,
            "auto_approve": self._safely(self.auto_approve_on, False),
            "youtube_sources_off": youtube,
            "next_slots": self._next_slots_iso(),
            "kill_switch": self._safely(self.stages.kill_switch_on, False),
            "breaker_open": self._safely(lambda: self.stages.breaker_open(self.store), False),
            "isolated": self._isolated(),
            "channel": self._channel(),
            "events": self.store.list_events(limit=10),
        }

    def _next_slots_iso(self) -> list[str]:
        slots = self._safely(
            lambda: self.stages.next_slots(
                _now(), self.store.booked_times(), self.settings, self.store.first_post_date(), count=3
            ),
            [],
        )
        return [s.isoformat() for s in slots]

    def _isolated(self) -> bool:
        try:
            self.stages.assert_isolated()
        except Exception:
            return False
        return True

    def _channel(self) -> dict | None:
        """The signed-in channel. Not asked for at all when this process is not on the clips folder:
        the sign-in token it would read would be the company's."""
        if not self._isolated():
            return None
        found = self._safely(self.stages.connected_channel, None)
        return {"id": found[0], "title": found[1]} if found else None

    @staticmethod
    def _safely(call: Callable[[], Any], default: Any) -> Any:
        try:
            return call()
        except Exception:
            return default
