"""Render every video deliverable of a job (plan §6.4, D6-D9, D18).

Order (chosen for disk and for "mandatory first"):
    audio.flac -> cleaned track -> highlights track -> title card
    -> intro / outro tracks (the owner's clips, converted to the meeting's format)
    -> final.mp4 = intro + highlights + card + cleaned + outro   (mandatory)
    -> highlights.mp4 / cleaned.mp4 (optional copies) -> tracks deleted
    -> removed.mp4 (optional) -> shorts (optional) -> audio.flac deleted

Only the cleaned track and final.mp4 are mandatory: anything else that fails
becomes a failed artifact plus a warning and the job still finishes
(partial-success contract, D18). The title card, intro and outro are only
ever left out of final.mp4 (with a warning), never the reason it fails.
Cancellation, the job wall clock and a full disk are never softened.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import publish.settings as publish_settings
from core.config import get_settings
from core.errors import RenderAssertError, is_fatal
from core.logging import get_logger
from core.models import (
    ArtifactInfo,
    EditDecisionList,
    EDLRange,
    JobStatus,
    RemovedRange,
    RenderManifest,
    ShortClip,
    Word,
)
from core.timeline import parse_rate, round_half_up
from edl.builder import validate_edl
from slice.ffmpeg_wrapper import (
    drawtext,
    extract_audio_flac,
    gain_to_target,
    measure_lufs,
)
from slice.fonts import find_font
from slice.pipeline import (
    ShortResult,
    Track,
    assemble,
    card_text,
    load_job_silences,
    pick_style,
    render_card,
    render_clip,
    render_removed,
    render_short_clip,
    render_track,
)
from slice.profile import MediaInfo, assert_concat_compatible

logger = get_logger(__name__)

StatusFn = Callable[[JobStatus], None]
ProgressFn = Callable[[int, int], None]


@dataclass
class RenderInputs:
    job_dir: Path
    source: Path
    media: MediaInfo
    edl: EditDecisionList  # the cleaned meeting
    removed: list[RemovedRange]
    reel_edl: EditDecisionList | None
    shorts: list[ShortClip]
    words: list[Word]  # source timeline (captions)
    title: str = ""
    # the final video's file name (Final_<Meeting>_<date>_Youtube.mp4,
    # core/naming.py); its key in the artifacts stays "final.mp4"
    final_name: str = "final.mp4"
    # the owner's clips for the start / end of final.mp4 (slice/intro_outro.py)
    intro: Path | None = None
    outro: Path | None = None
    # Zoom's recording type (None for uploads) and the job id, for the Shorts look
    recording_type: str | None = None
    job_id: str = ""


@dataclass
class RenderOutputs:
    cleaned: RenderManifest  # the cleaned meeting's own timeline (starts at 0)
    final_path: Path
    final_frames: int
    final_duration_s: float
    # where the cleaned meeting starts in final.mp4 (intro + highlights + card)
    final_offset_s: float
    highlights: RenderManifest | None = None
    card_s: float = 0.0
    # final.mp4 = intro_s + reel_s + cleaned + outro_s; reel_s is the
    # highlights reel + title card (0 = no reel), which starts at intro_s
    intro_s: float = 0.0
    reel_s: float = 0.0
    outro_s: float = 0.0
    intro_name: str = ""
    outro_name: str = ""
    removed: RenderManifest | None = None
    shorts: list[ShortResult] = field(default_factory=list)
    artifacts: dict[str, ArtifactInfo] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    render_look: dict = field(default_factory=dict)  # the look choices this render used
    # full-video levelling: what the meeting measured, and the gain applied (None = left alone)
    loudness_measured_lufs: float | None = None
    loudness_gain_db: float | None = None
    full_hd: bool = False


def short_name(index: int) -> str:
    return f"shorts/short_{index:02d}.mp4"


def render_outputs(inp: RenderInputs, *, set_status: StatusFn, on_progress: ProgressFn | None = None) -> RenderOutputs:
    settings = get_settings()
    media = inp.media
    job_dir = inp.job_dir
    # everything temporary lives under tmp/, which startup reconciliation
    # wipes after a crash (the FLAC alone is ~0.5-1 GB for a long meeting)
    scratch = job_dir / "tmp"
    work = scratch / "render"
    flac = scratch / "audio.flac"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, ArtifactInfo] = {}
    warnings: list[str] = []
    timings: dict[str, float] = {}
    looks = publish_settings.looks()  # once per job: a snapshot
    look = looks.full
    font = find_font()
    bold_font = find_font(bold=True)
    if font is None:
        warnings.append("no font found: removed-part labels, the title card and burned-in captions are skipped")

    def soft_fail(name: str, what: str, exc: Exception, kind: str = "video") -> None:
        if is_fatal(exc):
            raise exc
        logger.exception("%s failed", what)
        artifacts[name] = ArtifactInfo(path=name, kind=kind, status="failed", reason=_short_reason(exc))
        warnings.append(f"{what} failed: {_short_reason(exc)}")

    tracks: list[Track] = []
    try:
        set_status(JobStatus.SLICING)
        t0 = time.monotonic()
        extract_audio_flac(inp.source, flac, media.audio_channels, media.duration)
        measured: float | None = None
        gain: float | None = None
        if look.loudness:
            # once, on the whole meeting (the gate ignores the silences); a nicety that never fails a render
            t_loud = time.monotonic()
            measured = measure_lufs(flac)
            gain = gain_to_target(measured, look.target_lufs)
            timings["loudness"] = round(time.monotonic() - t_loud, 2)
            if gain is None:
                warnings.append("sound level not adjusted: the meeting's loudness could not be measured")
        hd = look.hd
        cleaned = render_track(inp.source, flac, inp.edl, media, work / "cleaned", "cleaned", on_progress,
                               **_look_kwargs(hd, gain))
        tracks.append(cleaned)
        timings["cleaned"] = round(time.monotonic() - t0, 2)

        highlights: Track | None = None
        if inp.reel_edl is not None and inp.reel_edl.ranges:
            set_status(JobStatus.RENDERING_HIGHLIGHTS)
            t0 = time.monotonic()
            try:
                overlays = None
                if look.hook_overlay and bold_font is not None:
                    overlays = _hook_overlay(inp.title, media, work / "highlights", bold_font)
                highlights = render_track(inp.source, flac, inp.reel_edl, media, work / "highlights", "highlights",
                                          on_progress, overlays, **_look_kwargs(hd, gain))
                tracks.append(highlights)
            except Exception as exc:  # noqa: BLE001 — optional artifact
                soft_fail("highlights.mp4", "highlights reel", exc)
            timings["highlights"] = round(time.monotonic() - t0, 2)
        else:
            artifacts["highlights.mp4"] = ArtifactInfo(path="highlights.mp4", status="skipped",
                                                       reason="not enough highlight-worthy material")

        card: Track | None = None
        if highlights is not None and settings.title_card_s > 0 and font is not None:
            try:
                card = render_card(inp.title, media, settings.title_card_s, work / "card", font, bold_font, **_look_kwargs(hd))
                tracks.append(card)
            except Exception as exc:
                if is_fatal(exc):
                    raise
                logger.exception("title card failed")
                warnings.append(f"title card skipped: {_short_reason(exc)}")

        # ---- final.mp4 (mandatory)
        set_status(JobStatus.ASSEMBLING)

        def clip_track(kind: str, path: Path | None) -> Track | None:
            """The intro/outro track, or None. A clip that can't be converted,
            or whose encoding doesn't match the cleaned meeting's (checked
            here, header-only, so the warning names the right piece), is
            left out with a warning: like the title card, it must never
            cost the job."""
            if path is None:
                return None
            try:
                track = render_clip(path, media, work / kind, kind, **_look_kwargs(hd))
                tracks.append(track)
                assert_concat_compatible([cleaned.video, track.video])
                return track
            except Exception as exc:
                if is_fatal(exc):
                    raise
                logger.exception("%s %s left out of final.mp4", kind, path.name)
                warnings.append(f"{kind} skipped: {path.name}: {_short_reason(exc)}")
                return None

        t0 = time.monotonic()
        intro, outro = clip_track("intro", inp.intro), clip_track("outro", inp.outro)
        if inp.intro or inp.outro:
            timings["intro_outro"] = round(time.monotonic() - t0, 2)
        t0 = time.monotonic()
        final_path = job_dir / inp.final_name
        parts = [t for t in (intro, highlights, card, cleaned, outro) if t is not None]
        # if the joined file still fails its asserts, drop the generated
        # pieces one at a time and retry: the title card first (the clips
        # were already checked against the meeting), then the intro, the
        # outro. Never the reel or the meeting.
        droppable = [(t, label) for t, label in ((card, "title card"), (intro, "intro"), (outro, "outro"))
                     if t is not None]
        while True:
            try:
                header = assemble(parts, final_path, work / "final", "final")
                break
            except RenderAssertError:
                if not droppable:
                    raise
                dropped, label = droppable.pop(0)
                logger.exception("final.mp4 with the %s failed its asserts; retrying without it", label)
                warnings.append(f"{label} skipped: it did not match the meeting video's encoding")
                parts = [t for t in parts if t is not dropped]
        card, intro, outro = (t if any(p is t for p in parts) else None for t in (card, intro, outro))
        final_frames = sum(t.frames for t in parts)
        offset_frames = sum(t.frames for t in parts[:next(i for i, t in enumerate(parts) if t is cleaned)])
        intro_frames = intro.frames if intro is not None else 0
        fps = media.fps
        artifacts["final.mp4"] = ArtifactInfo(path=inp.final_name, mandatory=True,
                                              duration_s=float(Fraction(final_frames) / fps))
        timings["final"] = round(time.monotonic() - t0, 2)
        cleaned.manifest.measured_frames = cleaned.frames
        cleaned.manifest.measured_duration_s = cleaned.duration_s

        if highlights is not None and settings.highlights_file_enabled:
            try:
                assemble([highlights], job_dir / "highlights.mp4", work / "hl_out", "highlights")
                artifacts["highlights.mp4"] = ArtifactInfo(path="highlights.mp4", duration_s=highlights.duration_s)
            except Exception as exc:  # noqa: BLE001
                soft_fail("highlights.mp4", "highlights.mp4", exc)
        if settings.keep_cleaned_separately:
            try:
                assemble([cleaned], job_dir / "cleaned.mp4", work / "cleaned_out", "cleaned")
                artifacts["cleaned.mp4"] = ArtifactInfo(path="cleaned.mp4", duration_s=cleaned.duration_s)
            except Exception as exc:  # noqa: BLE001
                soft_fail("cleaned.mp4", "cleaned.mp4", exc)
        out = RenderOutputs(
            cleaned=cleaned.manifest,
            final_path=final_path,
            final_frames=final_frames,
            final_duration_s=header.video_duration or float(Fraction(final_frames) / fps),
            final_offset_s=float(Fraction(offset_frames) / fps),
            highlights=highlights.manifest if highlights is not None else None,
            card_s=card.duration_s if card is not None else 0.0,
            intro_s=float(Fraction(intro_frames) / fps),
            reel_s=float(Fraction(offset_frames - intro_frames) / fps),
            outro_s=outro.duration_s if outro is not None else 0.0,
            intro_name=inp.intro.name if intro is not None and inp.intro else "",
            outro_name=inp.outro.name if outro is not None and inp.outro else "",
            artifacts=artifacts,
            warnings=warnings,
            timings=timings,
            render_look=looks.as_dict(),
            loudness_measured_lufs=measured,
            loudness_gain_db=gain,
            full_hd=hd,
        )
        for t in tracks:  # free the disk before the optional renders
            t.delete()
        shutil.rmtree(work, ignore_errors=True)

        # ---- removed.mp4 (optional)
        set_status(JobStatus.RENDERING_REMOVED)
        t0 = time.monotonic()
        try:
            out.removed = render_removed(inp.source, flac, inp.removed, media, job_dir / "removed.mp4",
                                         work / "removed", font, on_progress, **_look_kwargs(hd, gain))
            if out.removed is None:
                artifacts["removed.mp4"] = ArtifactInfo(path="removed.mp4", status="skipped",
                                                        reason="nothing removed was long enough to show")
            else:
                artifacts["removed.mp4"] = ArtifactInfo(path="removed.mp4",
                                                        duration_s=out.removed.measured_duration_s)
        except Exception as exc:  # noqa: BLE001
            soft_fail("removed.mp4", "removed-parts video", exc)
        timings["removed"] = round(time.monotonic() - t0, 2)

        # ---- shorts (optional, each on its own)
        if inp.shorts:
            set_status(JobStatus.RENDERING_SHORTS)
            t0 = time.monotonic()
            analysis_s = 0.0
            silences = load_job_silences(job_dir)  # for trimming long pauses (new-style Shorts)
            for n, clip in enumerate(inp.shorts, 1):
                name = short_name(clip.index)
                try:
                    result = render_short_clip(
                        inp.source, flac, clip, media, inp.words, job_dir / name,
                        job_dir / name.replace(".mp4", ".srt"), work / f"short_{clip.index:02d}", font,
                        width=settings.shorts_width, height=settings.shorts_height,
                        burn_captions=settings.shorts_burn_captions,
                        style=pick_style(looks.shorts.style, inp.job_id, clip.index), look=looks.shorts,
                        recording_type=inp.recording_type, job_id=inp.job_id, index=clip.index,
                        silences=silences,
                    )
                    analysis_s += result.analysis_s
                    out.shorts.append(result)
                    artifacts[name] = ArtifactInfo(path=name, duration_s=result.duration_s)
                    artifacts[name.replace(".mp4", ".srt")] = ArtifactInfo(path=name.replace(".mp4", ".srt"),
                                                                           kind="text")
                    warnings.extend(f"short {clip.index}: {note}" for note in result.notes)
                except Exception as exc:  # noqa: BLE001
                    soft_fail(name, f"short {clip.index}", exc)
                if on_progress:
                    on_progress(n, len(inp.shorts))
            timings["shorts"] = round(time.monotonic() - t0, 2)
            timings["shorts_analysis"] = round(analysis_s, 2)
        return out
    finally:
        for t in tracks:
            t.delete()
        shutil.rmtree(scratch, ignore_errors=True)


def _look_kwargs(hd: bool, gain_db: float | None = None) -> dict:
    """The full-video look options, only when they differ from today's: with
    everything off the renderers are called exactly as before."""
    kw: dict = {}
    if hd:
        kw["hd"] = True
    if gain_db is not None:
        kw["gain_db"] = gain_db
    return kw


def _hook_overlay(title: str, media: MediaInfo, work_dir: Path, bold_font: Path) -> dict[int, str] | None:
    """The topic as a large line over the first 3 s of the highlights reel:
    the first reel range only (the time restarts at 0 after the trim). The
    same wording as the title card. None when the title has no text."""
    lines = card_text(title)
    if not lines:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    textfile = work_dir / "hook_title.txt"
    textfile.write_text("\n".join(lines), encoding="utf-8")
    height, width = media.even_height, media.even_width
    size = max(12, min(round(height * 0.075), int(0.9 * width / (0.55 * max(len(x) for x in lines)))))
    return {0: drawtext(bold_font, textfile, size=size, y="h*0.12") + ":enable=lt(t\\,3)"}


def _short_reason(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    return first[:300]


# ------------------------------------------------------------------ topic cut


@dataclass
class TopicRender:
    """What `render_topic_cut` made: the file and how it is built."""

    path: Path
    frames: int
    duration_s: float
    open_s: float  # the cold open (0 = none)
    card_s: float  # the title card (0 = none)
    body_s: float  # the topic itself
    outro_s: float
    notes: list[str] = field(default_factory=list)


def _single_range_edl(edl: EditDecisionList, start_s: float, end_s: float) -> EditDecisionList | None:
    """A one-range EDL on the cleaned EDL's frame grid for source seconds
    [start_s, end_s], or None when it is empty."""
    if edl.fps is None:
        return None
    fps = parse_rate(edl.fps)
    a = round_half_up(Fraction(start_s).limit_denominator(1000) * fps)
    b = round_half_up(Fraction(end_s).limit_denominator(1000) * fps)
    if edl.total_frames:
        b = min(b, edl.total_frames)
    if b <= a:
        return None
    one = EditDecisionList(
        ranges=[EDLRange(start=float(Fraction(a) / fps), end=float(Fraction(b) / fps), start_frame=a, end_frame=b)],
        source_duration=edl.source_duration, fps=edl.fps, fade_frames=edl.fade_frames, total_frames=edl.total_frames)
    validate_edl(one)
    return one


def render_topic_cut(
    inp: RenderInputs,
    cut,
    title: str,
    subtitle: str,
    out_path: Path,
    work_dir: Path,
    font: Path | None,
    bold_font: Path | None,
    gain_db: float | None = None,
    hd: bool = False,
    on_progress: ProgressFn | None = None,
) -> TopicRender:
    """One topic cut as its own video: the cold open (the best 15-40 s), a title
    card, the topic from its start, then the owner's outro. Frame-exact, with
    the full video's profile, loudness gain and HD choice. `cut` is a
    decide.topics.TopicCut. The cold open, the card and the outro are only
    ever left out (with a note), never the reason the cut fails; an error in
    the topic itself is raised. Everything temporary lives in `work_dir`,
    which is removed at the end (it also holds its own audio.flac, because the
    meeting's is long gone by now)."""
    from edl.builder import slice_edl

    settings = get_settings()
    media = inp.media
    work_dir = Path(work_dir)
    notes: list[str] = []
    tracks: list[Track] = []
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        flac = work_dir / "audio.flac"
        extract_audio_flac(inp.source, flac, media.audio_channels, media.duration)
        kw = _look_kwargs(hd, gain_db)
        body_edl = slice_edl(inp.edl, cut.src_start_frame, cut.src_end_frame)
        body = render_track(inp.source, flac, body_edl, media, work_dir / "body", "topic", on_progress, **kw)
        tracks.append(body)

        opening: Track | None = None
        if cut.open_span is not None:
            try:
                one = _single_range_edl(inp.edl, cut.open_span[0], cut.open_span[1])
                if one is not None:
                    opening = render_track(inp.source, flac, one, media, work_dir / "open", "topic_open", **kw)
                    tracks.append(opening)
            except Exception as exc:  # the open is a nicety
                if is_fatal(exc):
                    raise
                logger.exception("topic cut: cold open left out")
                notes.append(f"cold open skipped: {_short_reason(exc)}")

        card: Track | None = None
        if settings.title_card_s > 0 and font is not None:
            try:
                card = render_card(title, media, settings.title_card_s, work_dir / "card", font, bold_font,
                                   subtitle=subtitle, **_look_kwargs(hd))
                tracks.append(card)
            except Exception as exc:
                if is_fatal(exc):
                    raise
                logger.exception("topic cut: title card left out")
                notes.append(f"title card skipped: {_short_reason(exc)}")

        outro: Track | None = None
        if inp.outro is not None:
            try:
                outro = render_clip(inp.outro, media, work_dir / "outro", "outro", **_look_kwargs(hd))
                tracks.append(outro)
                assert_concat_compatible([body.video, outro.video])
            except Exception as exc:
                if is_fatal(exc):
                    raise
                logger.exception("topic cut: outro left out")
                notes.append(f"outro skipped: {_short_reason(exc)}")
                outro = None

        parts = [t for t in (opening, card, body, outro) if t is not None]
        droppable = [(t, label) for t, label in ((card, "title card"), (opening, "cold open"), (outro, "outro"))
                     if t is not None]
        while True:
            try:
                header = assemble(parts, out_path, work_dir / "out", "topic")
                break
            except RenderAssertError:
                if not droppable:
                    raise
                dropped, label = droppable.pop(0)
                logger.exception("topic cut with the %s failed its asserts; retrying without it", label)
                notes.append(f"{label} skipped: it did not match the meeting video's encoding")
                parts = [t for t in parts if t is not dropped]
        frames = sum(t.frames for t in parts)
        fps = media.fps

        def secs(t: Track | None) -> float:
            return t.duration_s if t is not None and any(p is t for p in parts) else 0.0

        return TopicRender(path=out_path, frames=frames,
                           duration_s=header.video_duration or float(Fraction(frames) / fps),
                           open_s=secs(opening), card_s=secs(card), body_s=body.duration_s, outro_s=secs(outro),
                           notes=notes)
    finally:
        for t in tracks:
            t.delete()
        shutil.rmtree(work_dir, ignore_errors=True)
