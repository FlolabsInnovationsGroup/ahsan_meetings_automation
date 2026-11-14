"""Download only the strongest stretches of a long YouTube video or stream.

A long video is never fetched whole. We read its metadata, optionally listen to
its smallest audio track to find loud moments, choose a handful of stretches
around the strongest peaks, and download just those. Each stretch is its own
file in the media folder, so fetch.cleanup removes them like any other media.

The yt-dlp calls are three small functions that tests replace.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from clips import fetch as fetch_mod
from clips.fetch import FetchError, NotReady
from clips.models import Chunk, Fetched, Platform, SourceVideo

logger = logging.getLogger(__name__)

_LEAD_MAX_S = 20.0  # start this long before a peak, so the build-up is in the chunk
_EVEN_STRENGTH = 0.0  # evenly spaced stretches carry no signal
_CHAPTER_STRENGTH = 0.3  # a chapter start is a weak hint on its own


def _setting(name: str) -> Any:
    from clips.config import clips_settings

    return getattr(clips_settings(), name)


_NOT_READY = ("is_live", "is_upcoming", "post_live")


def is_chunked(video: SourceVideo) -> bool:
    """A YouTube video with no local file that is longer than chunk_minutes_over."""
    if video.path or video.platform is not Platform.YOUTUBE or not video.url:
        return False
    return video.duration_s > float(_setting("chunk_minutes_over")) * 60.0


# ------------------------------------------------------------------ planning (pure)


def _level_at(points: list[tuple[float, float, float]], t: float) -> float:
    """Heatmap value at time t (0 outside every bin)."""
    for start, end, value in points:
        if start <= t < end:
            return value
    return 0.0


def _nearest(curve: list[tuple[float, float]], t: float) -> float | None:
    """Loudness near time t, from a curve sorted by time."""
    if not curve:
        return None
    lo, hi = 0, len(curve) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if curve[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    return curve[lo][1]


def _candidates(
    heatmap: list[tuple[float, float, float]],
    loudness: list[tuple[float, float]],
    chapters: list[tuple[float, float, str]],
) -> list[tuple[float, float]]:
    """(time, score 0..1) candidates; heatmap and loudness agreeing score higher."""
    top = max((v for _, _, v in heatmap), default=0.0)
    heat = [(s, e, v / top) for s, e, v in heatmap] if top > 0 else []
    out: list[tuple[float, float]] = []
    for start, end, value in heat:
        mid = (start + end) / 2
        loud = _nearest(loudness, mid)
        out.append((mid, value if loud is None else 0.6 * value + 0.4 * loud))
    for t, level in loudness:
        h = _level_at(heat, t) if heat else None
        out.append((t, level if h is None else 0.6 * h + 0.4 * level))
    for start, _end, _title in chapters:
        out.append((start + 10.0, _CHAPTER_STRENGTH))
    return out


def plan_ranges(
    duration_s: float,
    *,
    heatmap: list[tuple[float, float, float]],
    loudness: list[tuple[float, float]],
    chapters: list[tuple[float, float, str]],
    count: int,
    length_s: float,
) -> list[tuple[float, float, float]]:
    """Choose up to `count` stretches of `length_s` as (start_s, end_s, strength 0..1), best first.

    Stretches sit around the strongest peaks, never overlap, stay inside the
    video and are spread out. With no signal they are evenly spaced.
    """
    if duration_s <= 0 or count <= 0 or length_s <= 0:
        return []
    if duration_s <= length_s:
        return [(0.0, float(duration_s), _EVEN_STRENGTH)]
    loud = sorted((t, v) for t, v in loudness if 0 <= t <= duration_s)
    cands = [(t, min(1.0, max(0.0, s))) for t, s in _candidates(heatmap, loud, chapters) if 0 <= t <= duration_s]
    cands = [c for c in cands if c[1] > 0]
    if not cands:
        return _even_ranges(duration_s, count, length_s)
    return _pick_peaks(cands, duration_s, count, length_s)


def _even_ranges(duration_s: float, count: int, length_s: float) -> list[tuple[float, float, float]]:
    """Evenly spaced stretches, each centred in its share of the video."""
    n = max(1, min(count, int(duration_s // length_s)))
    share = duration_s / n
    out = []
    for i in range(n):
        start = min(max(0.0, i * share + (share - length_s) / 2), duration_s - length_s)
        out.append((start, start + length_s, _EVEN_STRENGTH))
    return out


def _pick_peaks(
    cands: list[tuple[float, float]], duration_s: float, count: int, length_s: float
) -> list[tuple[float, float, float]]:
    lead = min(_LEAD_MAX_S, length_s * 0.2)
    # Peaks must be at least this far apart: spread out, and never closer than one stretch.
    gap = max(length_s, duration_s / (count * 3))
    if gap * count > duration_s:
        gap = length_s
    chosen: list[tuple[float, float, float]] = []
    peaks: list[float] = []
    for t, score in sorted(cands, key=lambda c: (-c[1], c[0])):
        if len(chosen) >= count:
            break
        if any(abs(t - p) < gap for p in peaks):
            continue
        start = min(max(0.0, t - lead), duration_s - length_s)
        end = start + length_s
        if any(start < ce and cs < end for cs, ce, _ in chosen):
            continue
        peaks.append(t)
        chosen.append((start, end, score))
    return chosen


# ------------------------------------------------------------------ yt-dlp (replaced in tests)


def _ytdlp_metadata(url: str) -> dict[str, Any]:
    """Duration, heatmap and chapters of a video, without downloading it."""
    import yt_dlp

    from ingest.youtube import ydl_base_opts

    try:
        with yt_dlp.YoutubeDL({**ydl_base_opts(), "skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        raise FetchError(fetch_mod._explain(url, exc)) from exc


def _ytdlp_scout_audio(url: str, dest: Path, stem: str) -> Path | None:
    """Download only the smallest audio track as dest/<stem>.<ext>; None when there is none."""
    import yt_dlp

    from ingest.youtube import ydl_base_opts

    opts = {
        **ydl_base_opts(),
        "format": "worstaudio",  # audio-only; never a combined video, so no whole download
        "outtmpl": str(dest / f"{stem}.%(ext)s"),
        "restrictfilenames": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        raise FetchError(fetch_mod._explain(url, exc)) from exc
    found = sorted(p for p in dest.glob(f"{stem}.*") if p.suffix.lower() not in fetch_mod._NOT_THE_VIDEO)
    return found[0] if found else None


def _ytdlp_download_ranges(
    url: str, dest: Path, stems: list[str], ranges: list[tuple[float, float]], fmt: str
) -> list[Path | None]:
    """Download each (start_s, end_s) range to dest/<stem>.<ext>; None for a range that failed.
    One range per call so one failure never loses the rest."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    from ingest.youtube import ydl_base_opts

    paths: list[Path | None] = []
    for stem, (start, end) in zip(stems, ranges, strict=True):
        opts = {
            **ydl_base_opts(),
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": str(dest / f"{stem}.%(ext)s"),
            "restrictfilenames": True,
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,  # cut exactly where planned
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
            paths.append(fetch_mod._find_file(dest, stem))
        except Exception as exc:  # noqa: BLE001 - reported by the caller as a skipped chunk
            logger.warning("chunk %s of %s failed: %s", stem, url, exc)
            paths.append(None)
    return paths


# ------------------------------------------------------------------ fetching


def _loudness(video: SourceVideo, dest: Path) -> list[tuple[float, float]]:
    """Loudness over the whole video from its smallest audio track. The audio file is
    always deleted. Any failure means no loudness signal, never a failed video."""
    from clips.signals import loudness_curve

    stem = f"{video.native_id}_scout"
    try:
        audio = _ytdlp_scout_audio(video.url, dest, stem)
        return loudness_curve(audio, step_s=1.0) if audio else []
    except Exception as exc:  # noqa: BLE001 - the heatmap or even spacing still works
        logger.warning("audio scout of %s failed: %s", video.id, exc)
        return []
    finally:
        for leftover in dest.glob(f"{stem}.*"):
            try:
                leftover.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("could not remove scout file %s: %s", leftover, exc)


def _shift(chunk: Fetched, start_s: float, end_s: float, meta: dict[str, Any]) -> None:
    """Give the chunk the heatmap and chapters that fall inside it, times relative to the chunk."""
    for s, e, v in fetch_mod._heatmap(meta):
        if e > start_s and s < end_s:
            chunk.heatmap.append((max(s, start_s) - start_s, min(e, end_s) - start_s, v))
    for s, e, title in fetch_mod._chapters(meta):
        if e > start_s and s < end_s:
            chunk.chapters.append((max(s, start_s) - start_s, min(e, end_s) - start_s, title))


def fetch_chunks(video: SourceVideo, dest_dir: Path, *, max_height: int = 1080) -> list[Chunk]:
    """Download the strongest stretches of a long video, best first."""
    from clips.config import clips_settings

    if not clips_settings().youtube_downloads:
        raise FetchError(
            f"video {video.id} is on YouTube and has no local file; YouTube downloads are off "
            "(CLIPS_YOUTUBE_DOWNLOADS=false). Put the file in a watched folder instead, or switch the setting on "
            "if you have decided to allow downloads."
        )
    if video.path or video.platform is not Platform.YOUTUBE or not video.url:
        raise FetchError(f"video {video.id} has no YouTube address to download chunks from")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    meta = _ytdlp_metadata(video.url)
    if meta.get("live_status") in _NOT_READY:
        # Live, upcoming or still being processed: the range download would hit a partial file.
        raise NotReady(f"video {video.id} is {meta['live_status']}; it will be tried again on a later pass")
    duration = float(meta.get("duration") or video.duration_s or 0.0)
    max_hours = float(_setting("max_stream_hours"))
    if duration > max_hours * 3600:
        raise FetchError(
            f"video {video.id} is {duration / 3600:.1f} hours long, over the {max_hours:g} hour limit "
            "(max_stream_hours); it is skipped"
        )
    if duration <= 0:
        raise FetchError(f"could not learn how long video {video.id} is, so no chunks were planned")
    loudness = _loudness(video, dest) if _setting("scout_audio") else []
    plan = plan_ranges(
        duration,
        heatmap=fetch_mod._heatmap(meta),
        loudness=loudness,
        chapters=fetch_mod._chapters(meta),
        count=int(_setting("chunks_per_video")),
        length_s=float(_setting("chunk_seconds")),
    )
    if not plan:
        raise FetchError(f"no stretches could be planned for video {video.id}")
    stems = [f"{video.native_id}_c{i:02d}_{int(start)}" for i, (start, _e, _s) in enumerate(plan)]
    paths = _ytdlp_download_ranges(
        video.url, dest, stems, [(s, e) for s, e, _x in plan], fetch_mod._format_for(max_height)
    )
    chunks = _probe_all(video, plan, paths, meta)
    if not chunks:
        raise FetchError(
            f"none of the {len(plan)} stretches of video {video.id} could be downloaded; "
            "check the log, update yt-dlp and try again"
        )
    return chunks


def _probe_all(
    video: SourceVideo,
    plan: list[tuple[float, float, float]],
    paths: list[Path | None],
    meta: dict[str, Any],
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for (start, end, strength), path in zip(plan, paths, strict=True):
        if path is None:
            logger.warning("video %s: stretch at %.0fs was not downloaded, skipping it", video.id, start)
            continue
        try:
            fetched = fetch_mod.probe(path)
        except FetchError as exc:
            logger.warning("video %s: stretch at %.0fs is unreadable, skipping it: %s", video.id, start, exc)
            continue
        _shift(fetched, start, end, meta)
        chunks.append(Chunk(offset_s=start, strength=strength, fetched=fetched))
    return chunks
