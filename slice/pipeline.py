"""Renderers that turn EDLs into finished files and RenderManifests.

Everything is built from TRACKS: a video-only file of exactly N frames (every
piece re-encoded once with the STANDARD profile) plus the matching audio as
sample-exact FLAC chunks. `assemble` joins one or more tracks into a finished
mp4 — video by `concat -c copy`, audio by ONE AAC encode over all chunks —
and asserts the frame and sample counts from the header before publishing
the file with an atomic rename. So:

    cleaned.mp4    = assemble([cleaned])
    highlights.mp4 = assemble([highlights])
    final.mp4      = assemble([intro, highlights, title card, cleaned, outro])
    removed.mp4    = assemble([removed, with a label on every clip])

and final.mp4's audio is encoded once from the lossless chunks, never from
the other files' AAC (which would add a generation and priming gaps).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from core.config import get_settings
from core.errors import RenderAssertError, is_fatal
from core.logging import get_logger
from core.models import (
    EditDecisionList,
    EDLRange,
    RemovedRange,
    RenderManifest,
    RenderPiece,
    ShortClip,
    Word,
)
from core.proc import stream_checked
from core.timeline import (
    AUDIO_RATE,
    fmt_clock,
    parse_rate,
    rate_str,
    round_half_up,
    samples_at_frame,
    to_frame,
)

from . import layout as layout_mod
from .captions import (
    Cue,
    ass_document,
    ass_document_new,
    caption_cues,
    choose_hook,
    srt_text,
)
from .ffmpeg_wrapper import (
    AudioPiece,
    audio_input_args,
    build_card_graph,
    build_clip_graph,
    build_short_graph,
    build_short_graph_new,
    concat_copy,
    drawtext,
    encode_aac,
    ffmpeg_bin,
    gain_to_target,
    measure_lufs,
    mux,
    render_audio_chunk,
    render_card_video,
    render_clip_audio,
    render_clip_video,
    render_short,
    render_silence_flac,
    render_video_part,
    subtitles_filter,
)
from .fonts import font_family
from .plan import InputSpan, output_offsets, plan_video
from .profile import (
    BASE_ARGS,
    MediaInfo,
    StreamHeader,
    assert_concat_compatible,
    assert_duration,
    assert_frames,
    ffmpeg_timeout,
    probe_header,
    probe_media,
)
from .silence import SAMPLE_RATE as SILENCE_RATE
from .silence import WINDOW_SAMPLES, SilenceMap, silent_runs
from .silence import _db as _window_db

logger = get_logger(__name__)

# AAC works in 1024-sample frames; the encoded track may differ from the exact
# FLAC length by up to one frame at the tail.
AAC_TOLERANCE_S = 2048 / AUDIO_RATE

ProgressFn = Callable[[int, int], None]


def edl_frame_ranges(edl: EditDecisionList, fps: Fraction) -> list[tuple[int, int]]:
    """Exact frame ranges from an EDL (frame fields if present, else snapped)."""
    out = []
    for r in edl.ranges:
        s = r.start_frame if r.start_frame is not None else to_frame(r.start, fps)
        e = r.end_frame if r.end_frame is not None else to_frame(r.end, fps)
        out.append((s, e))
    return out


def audio_channels(media: MediaInfo) -> int:
    """Channels of audio.flac (extract_audio_flac keeps mono sources mono)."""
    return 1 if media.audio_channels == 1 else 2


@dataclass
class Track:
    """A rendered but not yet muxed part of a deliverable."""

    kind: str
    fps: Fraction
    video: Path
    audio_chunks: list[Path]
    frames: int
    samples: int
    manifest: RenderManifest
    steps_done: int = 0
    steps_total: int = 0
    # dB applied to this track's audio when final.mp4 is encoded (loudness
    # levelling); None = left alone (intro, outro, title card)
    gain_db: float | None = None

    @property
    def duration_s(self) -> float:
        return float(Fraction(self.frames) / self.fps)

    def delete(self) -> None:
        self.video.unlink(missing_ok=True)
        for p in self.audio_chunks:
            p.unlink(missing_ok=True)


class _Steps:
    def __init__(self, total: int, on_progress: ProgressFn | None, done: int = 0):
        self.total, self.done, self.on_progress = total, done, on_progress

    def __call__(self) -> None:
        self.done += 1
        if self.on_progress:
            self.on_progress(self.done, self.total)


def render_track(
    source: Path,
    audio_flac: Path,
    edl: EditDecisionList,
    media: MediaInfo,
    work_dir: Path,
    kind: str = "cleaned",
    on_progress: ProgressFn | None = None,
    overlays: dict[int, str] | None = None,
    extra_steps: int = 0,
    hd: bool = False,
    gain_db: float | None = None,
) -> Track:
    """Render the kept ranges of `edl` with dissolves (edl.fade_frames; 0 =
    hard cuts) into a video-only file + exact FLAC chunks inside `work_dir`,
    asserting the exact frame/sample count of every piece. `overlays` maps a
    range index to an extra filter (e.g. a drawtext label) for that range.

    Intermediate pieces are deleted as soon as they are concatenated; the
    track's own files stay in `work_dir` (the caller removes the folder)."""
    settings = get_settings()
    fps = parse_rate(edl.fps) if edl.fps else media.fps
    d = edl.fade_frames
    ranges = edl_frame_ranges(edl, fps)
    total_src = edl.total_frames or media.total_frames
    width, height = media.even_width, media.even_height
    max_batch_frames = int(settings.render_max_batch_content_s * fps)
    parts = plan_video(ranges, d, settings.xfade_batch_size, max_batch_frames)

    offsets = output_offsets(ranges)
    expected_frames = sum(e - s for s, e in ranges)
    # Audio: hard cut at every kept-range boundary (the dissolve midpoint),
    # 20 ms micro-fades where the cut is inside the source.
    bounds = [samples_at_frame(o, fps) for o in offsets] + [samples_at_frame(expected_frames, fps)]
    audio_pieces = [
        AudioPiece(
            src_start=samples_at_frame(s, fps),
            samples=bounds[i + 1] - bounds[i],
            fade_in=s > 0,
            fade_out=e < total_src,
        )
        for i, (s, e) in enumerate(ranges)
    ]
    expected_samples = bounds[-1]
    chunk = max(1, settings.audio_inputs_per_command)
    audio_chunks = [audio_pieces[i : i + chunk] for i in range(0, len(audio_pieces), chunk)]
    step = _Steps(len(parts) + 1 + len(audio_chunks) + extra_steps, on_progress)

    manifest = RenderManifest(
        kind=kind,
        fps=rate_str(fps),
        width=width,
        height=height,
        fade_frames=d,
        pieces=[RenderPiece(src_start_frame=s, src_end_frame=e, out_start_frame=o)
                for (s, e), o in zip(ranges, offsets)],
        expected_frames=expected_frames,
        expected_audio_samples=expected_samples,
        audio_sample_rate=AUDIO_RATE,
        parts=[{"kind": p.kind, "inputs": len(p.inputs), "frames": p.frames, "join": p.join} for p in parts],
        seams=[
            {
                "out_time_s": float(Fraction(offsets[i + 1]) / fps),
                "src_from_s": float(Fraction(ranges[i][1]) / fps),
                "src_to_s": float(Fraction(ranges[i + 1][0]) / fps),
            }
            for i in range(len(ranges) - 1)
        ] if d else [],
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    # ---- video: batches + seams, each asserted, then concat -c copy
    t0 = time.monotonic()
    piece_paths: list[Path] = []
    for idx, part in enumerate(parts):
        dest = work_dir / f"{kind}_part_{idx:03d}.mp4"
        render_video_part(source, part, fps, width, height, work_dir / f"{kind}_part_{idx:03d}.txt", dest,
                          overlays, hd)
        assert_frames(dest, part.frames, f"{part.kind} {idx}")
        piece_paths.append(dest)
        step()
    manifest.timings_s["video_parts"] = round(time.monotonic() - t0, 2)

    t0 = time.monotonic()
    video_path = work_dir / f"{kind}_video.mp4"
    if len(piece_paths) == 1:
        piece_paths[0].replace(video_path)
    else:
        assert_concat_compatible(piece_paths)
        concat_copy(piece_paths, video_path, work_dir / f"{kind}_concat.txt",
                    content_s=float(Fraction(expected_frames) / fps))
        for p in piece_paths:
            p.unlink(missing_ok=True)
    assert_frames(video_path, expected_frames, f"{kind} video")
    step()
    manifest.timings_s["video_concat"] = round(time.monotonic() - t0, 2)

    # ---- audio: exact FLAC chunks (joined and encoded once in assemble)
    t0 = time.monotonic()
    chunk_paths: list[Path] = []
    for idx, pieces in enumerate(audio_chunks):
        dest = work_dir / f"{kind}_audio_{idx:03d}.flac"
        render_audio_chunk(audio_flac, pieces, work_dir / f"{kind}_audio_{idx:03d}.txt", dest)
        got = probe_header(dest).audio_duration_ts
        want = sum(p.samples for p in pieces)
        if got != want:
            raise RenderAssertError(f"{kind} audio chunk {idx}: {got} samples, expected exactly {want}")
        chunk_paths.append(dest)
        step()
    manifest.timings_s["audio_chunks"] = round(time.monotonic() - t0, 2)
    return Track(kind, fps, video_path, chunk_paths, expected_frames, expected_samples, manifest,
                 step.done, step.total, gain_db)


def assemble(
    tracks: list[Track],
    out_path: Path,
    work_dir: Path,
    kind: str,
    on_step: Callable[[], None] | None = None,
) -> StreamHeader:
    """Join tracks into `out_path`: video by concat -c copy (asserted
    compatible), audio by one AAC encode over every track's exact chunks,
    then mux and assert exact frames and audio length from the header.

    Muxes inside `work_dir` and publishes with an atomic rename only once
    every assert has passed, so a failed render never leaves a wrong file at
    out_path. The tracks' own files are left for the caller."""
    if not tracks:
        raise ValueError("nothing to assemble")
    fps = tracks[0].fps
    frames = sum(t.frames for t in tracks)
    samples = sum(t.samples for t in tracks)
    content_s = float(Fraction(frames) / fps)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    try:
        if len(tracks) == 1:
            video = tracks[0].video
        else:
            if any(t.fps != fps for t in tracks):
                raise RenderAssertError(f"{kind}: tracks have different frame rates")
            video = work_dir / f"{kind}_joined_video.mp4"
            made.append(video)
            assert_concat_compatible([t.video for t in tracks])
            concat_copy([t.video for t in tracks], video, work_dir / f"{kind}_joined.txt", content_s=content_s)
            assert_frames(video, frames, f"{kind} video")
        audio = work_dir / f"{kind}_audio.m4a"
        made.append(audio)
        encode_aac([c for t in tracks for c in t.audio_chunks], audio, work_dir / f"{kind}_audio_concat.txt",
                   content_s=content_s, gains=[t.gain_db for t in tracks for _ in t.audio_chunks])
        if on_step:
            on_step()
        muxed = work_dir / f"{kind}_muxed.mp4"
        made.append(muxed)
        mux(video, audio, muxed, content_s=content_s)
        header = assert_frames(muxed, frames, f"{kind} output")
        want_s = samples / AUDIO_RATE
        if header.audio_duration is None or abs(header.audio_duration - want_s) > AAC_TOLERANCE_S:
            raise RenderAssertError(
                f"{kind} output audio is {header.audio_duration}s, expected {want_s:.6f}s (±{AAC_TOLERANCE_S:.3f})"
            )
        assert_duration(header, frames, fps, f"{kind} output", AAC_TOLERANCE_S)
        os.replace(muxed, out_path)
        if on_step:
            on_step()
        return header
    finally:
        for p in made:
            p.unlink(missing_ok=True)


def _finish_manifest(manifest: RenderManifest, header: StreamHeader) -> RenderManifest:
    manifest.measured_frames = header.video_frames
    manifest.measured_duration_s = header.video_duration
    manifest.measured_audio_duration_s = header.audio_duration
    return manifest


def render_cleaned(
    source: Path,
    audio_flac: Path,
    edl: EditDecisionList,
    media: MediaInfo,
    out_path: Path,
    work_dir: Path,
    on_progress: ProgressFn | None = None,
    kind: str = "cleaned",
) -> RenderManifest:
    """One EDL -> one finished mp4 (render_track + assemble). `work_dir` is
    removed at the end, even on failure."""
    try:
        track = render_track(source, audio_flac, edl, media, work_dir, kind, on_progress, extra_steps=2)
        steps = _Steps(track.steps_total, on_progress, track.steps_done)
        t0 = time.monotonic()
        header = assemble([track], out_path, work_dir, kind, on_step=steps)
        track.manifest.timings_s["assemble"] = round(time.monotonic() - t0, 2)
        logger.info("%s rendered: %d ranges, %d frames (%.2fs), audio %.3fs", kind, len(edl.ranges),
                    track.frames, track.duration_s, header.audio_duration or 0.0)
        return _finish_manifest(track.manifest, header)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------- removed.mp4


def removed_label(r: RemovedRange) -> str:
    text = f"Removed {fmt_clock(r.start)}–{fmt_clock(r.end)}"
    return f"{text} · {r.reason}" if r.reason else text


def render_removed(
    source: Path,
    audio_flac: Path,
    removed: list[RemovedRange],
    media: MediaInfo,
    out_path: Path,
    work_dir: Path,
    font: Path | None,
    on_progress: ProgressFn | None = None,
    hd: bool = False,
    gain_db: float | None = None,
) -> RenderManifest | None:
    """Every removed range long enough to see (tier "video"), hard cuts, each
    labelled with its SOURCE time and reason (plan D8). None when nothing
    qualifies. Without a font the clips are rendered unlabelled."""
    fps = media.fps
    shown = [r for r in removed if r.tier == "video"]
    if not shown:
        return None
    ranges = []
    for r in shown:
        s = r.start_frame if r.start_frame is not None else to_frame(r.start, fps)
        e = r.end_frame if r.end_frame is not None else to_frame(r.end, fps)
        ranges.append(EDLRange(start=float(Fraction(s) / fps), end=float(Fraction(e) / fps), start_frame=s,
                               end_frame=e))
    edl = EditDecisionList(ranges=ranges, source_duration=media.video_duration, fps=rate_str(fps), fade_frames=0,
                           total_frames=media.total_frames)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        overlays = None
        if font is not None:
            size = max(14, round(media.even_height * 0.045))
            overlays = {}
            for i, r in enumerate(shown):
                label = work_dir / f"label_{i:03d}.txt"
                label.write_text(removed_label(r), encoding="utf-8")
                overlays[i] = drawtext(font, label, size=size, y=str(max(8, round(media.even_height * 0.03))))
        track = render_track(source, audio_flac, edl, media, work_dir, "removed", on_progress, overlays,
                             extra_steps=2, hd=hd, gain_db=gain_db)
        steps = _Steps(track.steps_total, on_progress, track.steps_done)
        header = assemble([track], out_path, work_dir, "removed", on_step=steps)
        return _finish_manifest(track.manifest, header)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------- title card


def card_text(title: str, width_chars: int = 28, max_lines: int = 3) -> list[str]:
    lines = textwrap.wrap(" ".join(title.split()), width_chars) if title.strip() else []
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width_chars - 1].rstrip() + "…"
    return lines


def render_card(
    title: str,
    media: MediaInfo,
    seconds: float,
    work_dir: Path,
    font: Path,
    bold_font: Path | None = None,
    subtitle: str = "Full meeting",
    hd: bool = False,
) -> Track:
    """A short black card ("<topic>" / "Full meeting") with silent audio,
    encoded with the STANDARD profile and the source's colour tags so it
    concatenates with tracks cut from the source."""
    fps = media.fps
    width, height = media.even_width, media.even_height
    frames = max(1, round_half_up(Fraction(seconds).limit_denominator(1000) * fps))
    samples = samples_at_frame(frames, fps)
    work_dir.mkdir(parents=True, exist_ok=True)
    lines = card_text(title)
    title_file = None
    title_size = round(height * 0.075)
    if lines:
        title_file = work_dir / "card_title.txt"
        title_file.write_text("\n".join(lines), encoding="utf-8")
        # ~0.55 em per character for a sans font: keep the longest line inside 90% of the width
        title_size = max(12, min(title_size, int(0.9 * width / (0.55 * max(len(x) for x in lines)))))
    subtitle_file = work_dir / "card_subtitle.txt"
    subtitle_file.write_text(subtitle, encoding="utf-8")
    graph = build_card_graph(fps=fps, width=width, height=height, frames=frames, font=font,
                             bold_font=bold_font or font, title_file=title_file, subtitle_file=subtitle_file,
                             title_size=title_size, subtitle_size=max(10, round(title_size * 0.6)),
                             color_tags=dict(media.color_tags))
    video = work_dir / "card_video.mp4"
    render_card_video(video, graph, work_dir / "card_graph.txt", fps, frames, hd)
    assert_frames(video, frames, "title card")
    audio = work_dir / "card_audio.flac"
    render_silence_flac(audio, samples, audio_channels(media))
    got = probe_header(audio).audio_duration_ts
    if got != samples:
        raise RenderAssertError(f"title card audio: {got} samples, expected exactly {samples}")
    manifest = RenderManifest(kind="card", fps=rate_str(fps), width=width, height=height, expected_frames=frames,
                              expected_audio_samples=samples)
    return Track("card", fps, video, [audio], frames, samples, manifest)


# ------------------------------------------------------------- intro / outro


def render_clip(path: Path, media: MediaInfo, work_dir: Path, kind: str, hd: bool = False) -> Track:
    """The owner's intro or outro clip as a track of final.mp4, converted to
    the MEETING's format so `assemble` can join it by concat -c copy: the
    STANDARD profile at the meeting's size and frame rate, fitted inside the
    frame with black bars, the meeting's colour tags, and audio resampled to
    the meeting's channel count (silence if the clip has none). Hard cuts at
    both joins.

    Length: the NEAREST whole frame on the meeting's grid (as a source's own
    frame count is taken, MediaInfo.total_frames), so the clip keeps its
    length to within half a frame; if that rounds up, the last frame is
    held. Its audio is exactly the samples of those frames."""
    clip = probe_media(path)
    fps = media.fps
    width, height = media.even_width, media.even_height
    frames = max(1, round_half_up(Fraction(clip.video_duration) * fps))
    samples = samples_at_frame(frames, fps)
    work_dir.mkdir(parents=True, exist_ok=True)
    video = work_dir / f"{kind}_video.mp4"
    graph = build_clip_graph(fps=fps, width=width, height=height, frames=frames, color_tags=dict(media.color_tags))
    render_clip_video(path, video, graph, work_dir / f"{kind}_graph.txt", fps, frames, hd)
    assert_frames(video, frames, kind)
    audio = work_dir / f"{kind}_audio.flac"
    if clip.has_audio:
        render_clip_audio(path, audio, samples, audio_channels(media))
    else:
        render_silence_flac(audio, samples, audio_channels(media))
    got = probe_header(audio).audio_duration_ts
    if got != samples:
        raise RenderAssertError(f"{kind} audio: {got} samples, expected exactly {samples}")
    manifest = RenderManifest(kind=kind, fps=rate_str(fps), width=width, height=height, expected_frames=frames,
                              expected_audio_samples=samples)
    logger.info("%s %s: %dx%d @ %s -> %d frames (%.3fs) at %dx%d @ %s, %s", kind, path.name, clip.width,
                clip.height, rate_str(clip.fps), frames, float(Fraction(frames) / fps), width, height, rate_str(fps),
                "with audio" if clip.has_audio else "no audio: silence")
    return Track(kind, fps, video, [audio], frames, samples, manifest)


# -------------------------------------------------------------------- shorts


@dataclass
class ShortResult:
    clip: ShortClip
    path: Path
    srt_path: Path
    frames: int
    duration_s: float
    captions: int
    header: StreamHeader | None = None
    notes: list[str] = field(default_factory=list)
    # how it was rendered (the new Shorts look, or today's)
    style: str = "classic"
    layout_kind: str = "fit"
    layout_metrics: dict = field(default_factory=dict)
    gain_db: float | None = None
    hook_text: str = ""
    look: dict = field(default_factory=dict)
    analysis_s: float = 0.0  # time spent on the layout analysis and the loudness measurement
    trimmed_s: float = 0.0  # pauses cut out of the Short (new style, 0 when none)


def pick_style(setting: str, job_id: str, index: int) -> str:
    """'classic' or 'new' for one Short. 'alternate' gives consecutive Shorts of a
    meeting opposite styles, starting from a per-meeting offset, and always the
    same answer for the same job (a re-render keeps each Short's style)."""
    if setting in ("classic", "new"):
        return setting
    offset = int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16)
    return ("new", "classic")[(offset + index) % 2]


# ------------------------------------------------- pauses inside a new-style short

CACHED_SILENCE_MIN_S = 1.0  # silences.json only holds pauses at least this long
MAX_SHORT_SPANS = 12  # one video input per kept span: more is slow, so the shortest pauses stay in


def load_job_silences(job_dir: Path) -> SilenceMap | None:
    """The job's cached silence map (silences.json), or None when there is none."""
    try:
        data = json.loads((Path(job_dir) / "silences.json").read_text(encoding="utf-8"))
        return SilenceMap(ranges=[(float(s), float(e)) for s, e in data["silences"]],
                          threshold_db=data.get("threshold_db"), audio_s=float(data.get("audio_s", 0.0)),
                          note=data.get("note", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clip_silent_runs(audio_flac: Path, start_s: float, end_s: float, threshold_db: float,
                     min_s: float) -> list[tuple[float, float]]:
    """Pauses of at least `min_s` inside [start_s, end_s] only (source seconds), measured
    again from the audio with the job's threshold. For a pause limit below the cached
    silence map's 1 s minimum."""
    dur = max(0.0, end_s - start_s)
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}", "-i", str(audio_flac),
           "-vn", "-ac", "1", "-ar", str(SILENCE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", "-"]
    raw = b"".join(stream_checked(cmd, 1 << 20, timeout=ffmpeg_timeout(max(dur, 1.0) / 2.0 + 30.0)))
    step = WINDOW_SAMPLES * 2
    raw = raw[: len(raw) - len(raw) % step]
    if not raw:
        return []
    levels = _window_db(np.frombuffer(raw, dtype="<i2").reshape(-1, WINDOW_SAMPLES))
    return [(round(start_s + a, 3), round(start_s + b, 3))
            for a, b in silent_runs(levels, threshold_db, min_s)]


def kept_spans(clip_start: float, clip_end: float, silences: list[tuple[float, float]], max_pause_s: float,
               keep_s: float = 0.2, fps: Fraction = Fraction(25), max_spans: int = MAX_SHORT_SPANS,
               ) -> list[tuple[int, int]]:
    """The source frame ranges (start, end) a trimmed short keeps, in order. A pause
    (from `silences`, source seconds) longer than `max_pause_s` inside the clip is cut
    down to `keep_s`: half of it stays on each side of the cut. Cuts are on frame
    boundaries, so the ranges are contiguous in the output. A pause at the very start
    or end of the clip is left alone. More than `max_spans` ranges: the cuts of the
    shortest pauses are put back. With nothing to trim: one range, the whole clip."""
    s, e = to_frame(clip_start, fps), to_frame(clip_end, fps)
    cuts: list[tuple[int, int, float]] = []
    for a, b in sorted(silences):
        if b - a <= max(max_pause_s, keep_s) + 1e-9:
            continue
        cs, ce = to_frame(a + keep_s / 2, fps), to_frame(b - keep_s / 2, fps)
        if cs <= s or ce >= e or ce - cs < 1:
            continue  # touches the clip edge, or nothing would go
        if cuts and cs < cuts[-1][1]:
            continue
        cuts.append((cs, ce, b - a))
    if len(cuts) > max_spans - 1:
        longest = sorted(sorted(cuts, key=lambda c: -c[2])[: max_spans - 1])
        cuts = longest
    spans, pos = [], s
    for cs, ce, _ in cuts:
        spans.append((pos, cs))
        pos = ce
    spans.append((pos, e))
    return spans


def remap_cues(cues: list[Cue], spans_s: list[tuple[float, float]]) -> list[Cue]:
    """Move caption cues (clip seconds) onto the trimmed output. `spans_s` are the kept
    ranges in clip seconds. A cue entirely inside a removed pause is dropped; a cue
    that runs into one is shortened to the kept part; the rest are shifted earlier."""
    offs, total = [], 0.0
    for a, b in spans_s:
        offs.append(total)
        total += b - a

    def at_start(t: float) -> float:
        for (a, b), o in zip(spans_s, offs, strict=True):
            if t < b:
                return o + max(0.0, t - a)
        return total

    def at_end(t: float) -> float:
        for (a, b), o in reversed(list(zip(spans_s, offs, strict=True))):
            if t > a:
                return o + min(b, t) - a
        return 0.0

    out: list[Cue] = []
    for c in cues:
        ns, ne = at_start(c.start), at_end(c.end)
        if ne - ns >= 0.04:
            out.append(Cue(round(ns, 3), round(ne, 3), c.text))
    return out


def _pause_spans(audio_flac: Path, start_s: float, end_s: float, s: int, e: int, fps: Fraction,
                 silences: SilenceMap | None, max_pause_s: float) -> list[tuple[int, int]]:
    """The source frame ranges a trimmed Short keeps ([(s, e)] when nothing is trimmed)."""
    if silences is None:
        return [(s, e)]
    if silences.note and not silences.ranges:
        # the job could not tell pauses from quiet speech (nothing is cut in the full video): cut nothing here either
        return [(s, e)]
    ranges = [(a, b) for a, b in silences.ranges if b > start_s and a < end_s]
    if max_pause_s < CACHED_SILENCE_MIN_S and silences.threshold_db is not None:
        # the cached map only has pauses of 1 s or more: measure this clip again for the shorter ones
        ranges = clip_silent_runs(audio_flac, start_s, end_s, silences.threshold_db, max_pause_s)
    return kept_spans(start_s, end_s, ranges, max_pause_s, fps=fps)


def render_short_clip(
    source: Path,
    audio_flac: Path,
    clip: ShortClip,
    media: MediaInfo,
    words: list[Word],
    out_path: Path,
    srt_path: Path,
    work_dir: Path,
    font: Path | None,
    *,
    width: int = 1080,
    height: int = 1920,
    burn_captions: bool = True,
    style: str = "classic",
    look=None,
    recording_type: str | None = None,
    job_id: str = "",
    index: int = 0,
    silences: SilenceMap | None = None,
) -> ShortResult:
    """One vertical short cut from the SOURCE at [clip.start, clip.end]
    (plan D7): blurred background, captions burned in (and written as .srt),
    audio from the FLAC with short fades. Frame-exact, asserted. With
    style='new' (and a look) the new Shorts look is rendered instead."""
    if style == "new" and look is not None:
        return _render_short_new(source, audio_flac, clip, media, words, out_path, srt_path, work_dir, font,
                                 width=width, height=height, burn_captions=burn_captions, look=look,
                                 recording_type=recording_type, silences=silences)
    fps = media.fps
    s = min(max(0, to_frame(clip.start, fps)), media.total_frames - 1)
    e = min(max(s + 1, to_frame(clip.end, fps)), media.total_frames)
    frames = e - s
    a0, a1 = samples_at_frame(s, fps), samples_at_frame(e, fps)
    piece = AudioPiece(src_start=a0, samples=a1 - a0, fade_in=True, fade_out=True)
    _, pre = audio_input_args(audio_flac, piece)
    start_s, end_s = float(Fraction(s) / fps), float(Fraction(e) / fps)
    cues = caption_cues(words, start_s, end_s)
    work_dir.mkdir(parents=True, exist_ok=True)
    notes = []
    try:
        srt_tmp = work_dir / "captions.srt"  # published with the short, never alone
        srt_tmp.write_text(srt_text(cues), encoding="utf-8")
        subtitles = ""
        if burn_captions and font is not None:
            ass = work_dir / "captions.ass"
            ass.write_text(ass_document(cues, title=clip.title, duration_s=end_s - start_s,
                                        font_family=font_family(font), width=width, height=height),
                           encoding="utf-8")
            subtitles = subtitles_filter(ass, font.parent)
        elif burn_captions:
            notes.append("no font found: captions not burned in")
        graph = build_short_graph(frames=frames, fps=fps, width=width, height=height, pre=pre,
                                  samples=piece.samples, subtitles=subtitles)
        tmp = work_dir / "short.mp4"
        render_short(source, audio_flac, InputSpan(s, e), piece, graph, work_dir / "short_graph.txt", tmp, fps)
        header = assert_frames(tmp, frames, f"short {clip.index}")
        want_s = piece.samples / AUDIO_RATE
        if header.audio_duration is None or abs(header.audio_duration - want_s) > AAC_TOLERANCE_S:
            raise RenderAssertError(f"short {clip.index} audio is {header.audio_duration}s, expected {want_s:.3f}s")
        assert_duration(header, frames, fps, f"short {clip.index}", AAC_TOLERANCE_S)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, out_path)
        os.replace(srt_tmp, srt_path)
        return ShortResult(clip, out_path, srt_path, frames, float(Fraction(frames) / fps), len(cues), header, notes)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _render_short_new(
    source: Path,
    audio_flac: Path,
    clip: ShortClip,
    media: MediaInfo,
    words: list[Word],
    out_path: Path,
    srt_path: Path,
    work_dir: Path,
    font: Path | None,
    *,
    width: int,
    height: int,
    burn_captions: bool,
    look,
    recording_type: str | None,
    silences: SilenceMap | None = None,
) -> ShortResult:
    """The new Shorts look: a cropped slide or speaker, a hook card, bigger
    captions, levelled sound. A failure in the layout analysis or the loudness
    measurement never fails the Short: it falls back to fit / no gain with a note."""
    fps = media.fps
    s = min(max(0, to_frame(clip.start, fps)), media.total_frames - 1)
    e = min(max(s + 1, to_frame(clip.end, fps)), media.total_frames)
    frames = e - s
    a0, a1 = samples_at_frame(s, fps), samples_at_frame(e, fps)
    piece = AudioPiece(src_start=a0, samples=a1 - a0, fade_in=True, fade_out=True)
    _, pre = audio_input_args(audio_flac, piece)
    start_s, end_s = float(Fraction(s) / fps), float(Fraction(e) / fps)
    notes: list[str] = []
    fit = layout_mod.Layout(kind="fit", panel=(0, 0, media.width, media.height))

    t0 = time.monotonic()
    lay = fit
    if look.layout == "auto":
        try:
            gray = layout_mod.sample_frames(source, start_s, end_s, src_size=(media.width, media.height))
            lay = layout_mod.analyse(gray, media.width, media.height, recording_type)
        except Exception as exc:
            if is_fatal(exc):
                raise
            logger.warning("short %s: layout analysis failed: %s", clip.index, exc)
            notes.append(f"layout analysis failed ({type(exc).__name__}): shown whole")
            lay = fit
    gain_db: float | None = None
    if look.loudness:
        try:
            gain_db = gain_to_target(measure_lufs(audio_flac, piece), look.target_lufs)
            if gain_db is None:
                notes.append("loudness could not be measured: sound left as it is")
        except Exception as exc:
            if is_fatal(exc):
                raise
            logger.warning("short %s: loudness measurement failed: %s", clip.index, exc)
            notes.append(f"loudness measurement failed ({type(exc).__name__}): sound left as it is")
    analysis_s = round(time.monotonic() - t0, 2)

    spans = [(s, e)]
    if getattr(look, "trim_pauses", False):
        try:
            spans = _pause_spans(audio_flac, start_s, end_s, s, e, fps, silences, look.max_pause_s)
        except Exception as exc:
            if is_fatal(exc):
                raise
            logger.warning("short %s: pause trimming failed: %s", clip.index, exc)
            notes.append(f"pause trimming failed ({type(exc).__name__}): pauses left in")
            spans = [(s, e)]
    frames = sum(b - a for a, b in spans)
    trimmed_s = float(Fraction((e - s) - frames) / fps)
    pieces = [AudioPiece(src_start=samples_at_frame(a, fps), samples=samples_at_frame(b, fps) - samples_at_frame(a, fps),
                         fade_in=True, fade_out=True) for a, b in spans]
    if len(pieces) > 1:
        pres = [audio_input_args(audio_flac, pc)[1] for pc in pieces]
    total_samples = sum(pc.samples for pc in pieces)

    cues = caption_cues(words, start_s, end_s, max_chars=look.caption_max_chars, max_words=5)
    if len(spans) > 1:  # captions follow the picture: cues move up over the removed pauses
        cues = remap_cues(cues, [(float(Fraction(a - s) / fps), float(Fraction(b - s) / fps)) for a, b in spans])
    out_s = float(Fraction(frames) / fps)
    hook = choose_hook(clip.screen_hook, clip.title)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        srt_tmp = work_dir / "captions.srt"  # the plain captions, no hook
        srt_tmp.write_text(srt_text(cues), encoding="utf-8")
        subtitles = ""
        if burn_captions and font is not None:
            ass = work_dir / "captions.ass"
            # ass_document_new wraps every cue to two rows itself (cap_two_lines)
            ass.write_text(ass_document_new(cues, hook=hook, title=clip.title, end_line=look.end_line,
                                            duration_s=out_s, look=look,
                                            font_family=font_family(font), width=width, height=height),
                           encoding="utf-8")
            subtitles = subtitles_filter(ass, font.parent)
        elif burn_captions:
            notes.append("no font found: captions not burned in")
        if len(spans) > 1:
            graph = build_short_graph_new(spans_frames=[b - a for a, b in spans], fps=fps, width=width,
                                          height=height, pre=pres, samples=total_samples, layout=lay, look=look,
                                          subtitles=subtitles, gain_db=gain_db, n_audio=len(pieces),
                                          audio_pieces=pieces, src_size=(media.width, media.height))
            render_arg = ([InputSpan(a, b) for a, b in spans], pieces)
        else:
            graph = build_short_graph_new(spans_frames=[frames], fps=fps, width=width, height=height, pre=pre,
                                          samples=piece.samples, layout=lay, look=look, subtitles=subtitles,
                                          gain_db=gain_db, src_size=(media.width, media.height))
            render_arg = (InputSpan(s, e), piece)
        tmp = work_dir / "short.mp4"
        render_short(source, audio_flac, *render_arg, graph, work_dir / "short_graph.txt", tmp, fps, hd=look.hd)
        header = assert_frames(tmp, frames, f"short {clip.index}")
        want_s = total_samples / AUDIO_RATE
        if header.audio_duration is None or abs(header.audio_duration - want_s) > AAC_TOLERANCE_S:
            raise RenderAssertError(f"short {clip.index} audio is {header.audio_duration}s, expected {want_s:.3f}s")
        assert_duration(header, frames, fps, f"short {clip.index}", AAC_TOLERANCE_S)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, out_path)
        os.replace(srt_tmp, srt_path)
        if trimmed_s:
            logger.info("short %s: %d pauses shortened, %.1fs trimmed", clip.index, len(spans) - 1, trimmed_s)
        logger.info("short %s: style=new layout=%s gain_db=%s hd=%s analysis=%.1fs", clip.index, lay.kind,
                    gain_db, look.hd, analysis_s)
        return ShortResult(clip, out_path, srt_path, frames, float(Fraction(frames) / fps), len(cues), header,
                           notes, style="new", layout_kind=lay.kind, layout_metrics=dict(lay.metrics),
                           gain_db=gain_db, hook_text=hook, look=look.as_dict(), analysis_s=analysis_s,
                           trimmed_s=round(trimmed_s, 3))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
