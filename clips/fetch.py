"""Get a source video onto disk and learn what it is.

A local file is probed where it lies (never copied or deleted). A YouTube video
is downloaded with yt-dlp; the "most replayed" heatmap, chapters and captions
come back with it so the picker has something to go on.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from clips.models import Caption, Fetched, Platform, SourceVideo

logger = logging.getLogger(__name__)

_NOT_THE_VIDEO = {".part", ".ytdl", ".json", ".vtt", ".srt", ".jpg", ".webp", ".png", ".description"}


class FetchError(RuntimeError):
    """Getting or reading a video failed. The message says what to do."""


def fetch(video: SourceVideo, dest_dir: Path, *, max_height: int = 1080) -> Fetched:
    """Make the video's file available and describe it."""
    if video.path:
        path = Path(video.path)
        if not path.is_file():
            raise FetchError(f"the file {path} is missing; put it back in the folder or remove the video")
        return probe(path)
    if video.platform is not Platform.YOUTUBE or not video.url:
        raise FetchError(f"video {video.id} has no file and no YouTube address, so there is nothing to fetch")
    from clips.config import clips_settings

    if not clips_settings().youtube_downloads:
        raise FetchError(
            f"video {video.id} is on YouTube and has no local file; YouTube downloads are off "
            "(CLIPS_YOUTUBE_DOWNLOADS=false). Put the file in a watched folder instead, or switch the setting on "
            "if you have decided to allow downloads."
        )
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    info = _ytdlp_download(video.url, dest, video.native_id, _format_for(max_height))
    path = _find_file(dest, video.native_id)
    fetched = probe(path)
    fetched.heatmap = _heatmap(info)
    fetched.chapters = _chapters(info)
    fetched.captions = _captions(video.url)
    return fetched


def probe(path: Path) -> Fetched:
    """Width, height, fps, length and audio of a local file."""
    path = Path(path)
    try:
        from slice.profile import probe_media

        info = probe_media(path)
    except Exception as exc:
        raise FetchError(
            f"could not read {path.name} with ffprobe: {exc}. Check that ffprobe is installed and the file plays."
        ) from exc
    return Fetched(
        video_path=str(path),
        width=info.width,
        height=info.height,
        fps=float(info.fps),
        duration_s=float(info.duration),
        has_audio=bool(info.has_audio),
    )


def cleanup(media_dir: Path, keep_hours: float) -> int:
    """Delete downloaded files in media_dir older than keep_hours. Returns how many."""
    root = Path(media_dir)
    if not root.is_dir():
        return 0
    cutoff = time.time() - keep_hours * 3600
    root_resolved = root.resolve()
    removed = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if root_resolved not in path.resolve().parents:
                continue  # never touch anything outside media_dir
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("could not remove %s: %s", path, exc)
    return removed


# --- yt-dlp

def _format_for(max_height: int) -> str:
    """H.264 first, at most max_height; fall back to anything that fits."""
    h = int(max_height)
    return (
        f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    )


def _ytdlp_download(url: str, dest: Path, stem: str, fmt: str) -> dict[str, Any]:
    """Download one video as dest/<stem>.<ext> and return yt-dlp's info.
    One small function so tests can replace it."""
    import yt_dlp

    from ingest.youtube import ydl_base_opts

    opts = {
        **ydl_base_opts(),
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(dest / f"{stem}.%(ext)s"),
        "restrictfilenames": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True) or {}
    except Exception as exc:
        raise FetchError(_explain(url, exc)) from exc


def _explain(url: str, exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "not a bot" in low or "sign in" in low:
        hint = (
            "YouTube wants a sign-in (bot check). Set YTDLP_COOKIES_FILE to a fresh cookies file, "
            "or run from a home connection."
        )
    elif "javascript" in low or "js runtime" in low or "challenge" in low:
        hint = "Install Node or Deno so yt-dlp can solve the YouTube challenge, and update yt-dlp."
    elif "429" in low or "too many" in low:
        hint = "YouTube is rate limiting this address. Wait and try again later."
    elif "private" in low or "unavailable" in low:
        hint = "The video is private or gone; skip it."
    else:
        hint = "Update yt-dlp and try again."
    first = text.splitlines()[0] if text else "unknown error"
    return f"could not download {url}: {first}. {hint}"


def _find_file(dest: Path, stem: str) -> Path:
    matches = sorted(p for p in dest.glob(f"{stem}.*") if p.suffix.lower() not in _NOT_THE_VIDEO)
    if not matches:
        raise FetchError(f"the download finished but no video file named {stem} was found in {dest}")
    return matches[0]


def _heatmap(info: dict) -> list[tuple[float, float, float]]:
    points = []
    for p in info.get("heatmap") or []:
        try:
            points.append((float(p["start_time"]), float(p["end_time"]), float(p["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return points


def _chapters(info: dict) -> list[tuple[float, float, str]]:
    chapters = []
    for c in info.get("chapters") or []:
        try:
            chapters.append((float(c["start_time"]), float(c["end_time"]), str(c.get("title") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    return chapters


def _captions(url: str) -> list[Caption]:
    """Sentence-level captions from YouTube; empty when there are none."""
    try:
        from transcribe.native import fetch_youtube_transcript

        result = fetch_youtube_transcript(url)
    except Exception as exc:  # noqa: BLE001 - captions are a bonus, never a reason to fail
        logger.warning("captions unavailable for %s: %s", url, exc)
        return []
    if not result:
        return []
    _words, segments = result
    return [
        Caption(start_s=float(s.start), end_s=float(s.end), text=s.text.strip())
        for s in segments
        if s.text and s.text.strip()
    ]
