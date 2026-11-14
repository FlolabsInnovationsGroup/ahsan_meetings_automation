"""Turn a chosen moment into a finished video file with ffmpeg.

Each command is built by a pure function that returns the argument list, so it
can be tested without ffmpeg. Shorts are 1080x1920 H.264/AAC, levelled to about
-14 LUFS. Nothing here changes the picture to dodge copyright matching.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from clips import layouts
from clips.captions import write_ass
from clips.config import clips_settings
from clips.models import Caption, Layout, Moment, Rendered, Source

LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
LONG_W, LONG_H = 1920, 1080


def _ffmpeg() -> str:
    from core.config import get_settings

    return get_settings().ffmpeg_bin


def _ffprobe() -> str:
    from core.config import get_settings

    return get_settings().ffprobe_bin


def ffmpeg_available() -> bool:
    """True when the ffmpeg and ffprobe programs can be found."""
    return shutil.which(_ffmpeg()) is not None and shutil.which(_ffprobe()) is not None


def filter_text_path(path: Path) -> str:
    """A file path escaped for use inside a filtergraph (Windows `C:` and quotes included)."""
    from slice.fonts import filter_escape

    return filter_escape(Path(path).as_posix())


def subtitles_filter(ass_path: Path, fonts_dir: Path | None = None) -> str:
    """The libass filter that burns in an ASS file, with the font folder if known."""
    opts = [f"filename={filter_text_path(ass_path)}"]
    if fonts_dir is not None:
        opts.append(f"fontsdir={filter_text_path(fonts_dir)}")
    return "subtitles=" + ":".join(opts)


def _seconds(value: float) -> str:
    return f"{value:.3f}"


def _video_encode(fps: int, *, maxrate: str) -> list[str]:
    # CRF with a ceiling: the quality survives YouTube's own re-encode
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-maxrate", maxrate, "-bufsize", "20M",
            "-pix_fmt", "yuv420p", "-r", str(fps)]


def _audio_encode() -> list[str]:
    return ["-af", LOUDNORM, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def build_short_cmd(ffmpeg: str, src: Path, moment: Moment, graph: str, out_file: Path, *, fps: int = 30) -> list[str]:
    """One run: cut, lay out, burn in the subtitles (already inside `graph`), level, encode."""
    duration = max(0.1, moment.end_s - moment.start_s)
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", _seconds(moment.start_s), "-t", _seconds(duration), "-i", str(src),
        "-filter_complex", graph, "-map", "[vout]", "-map", "0:a:0?",
        *_video_encode(fps, maxrate="10M"), *_audio_encode(),
        "-movflags", "+faststart", str(out_file),
    ]


def build_long_cmd(ffmpeg: str, src: Path, moment: Moment, ass_path: Path, fonts_dir: Path | None,
                   out_file: Path, *, fps: int = 30) -> list[str]:
    """Landscape: 1080p at most (never scaled up), the credit burned in, same loudness and encode."""
    duration = max(0.1, moment.end_s - moment.start_s)
    scale = (f"scale='min({LONG_W},iw)':'min({LONG_H},ih)':force_original_aspect_ratio=decrease:"
             "force_divisible_by=2")
    vf = f"{scale},setsar=1,{subtitles_filter(ass_path, fonts_dir)},format=yuv420p"
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", _seconds(moment.start_s), "-t", _seconds(duration), "-i", str(src),
        "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?",
        *_video_encode(fps, maxrate="12M"), *_audio_encode(),
        "-movflags", "+faststart", str(out_file),
    ]


def build_thumb_cmd(ffmpeg: str, video: Path, at_s: float, out_file: Path) -> list[str]:
    """Grab one frame as a JPEG."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", _seconds(at_s), "-i", str(video),
            "-frames:v", "1", "-q:v", "3", str(out_file)]


def _timeout(duration_s: float) -> float:
    return 120.0 + 12.0 * duration_s


def _probe_size(src: Path) -> tuple[int, int]:
    """Width and height of the first video stream."""
    from core.proc import run_checked

    cmd = [_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
           "-of", "csv=p=0:s=x", str(src)]
    text = run_checked(cmd, timeout=60).stdout.strip().splitlines()[0]
    w, h = text.split("x")[:2]
    return int(w), int(h)


def _require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or not on the path")


def _fonts_dir() -> Path | None:
    from slice.fonts import find_font

    font = find_font(bold=True)
    return font.parent if font else None


def _thumbnail(video: Path, duration_s: float, out_file: Path) -> str:
    """A frame about a third in, where the action usually is. Empty text when it fails."""
    from core.proc import MediaCommandError, run_checked

    try:
        run_checked(build_thumb_cmd(_ffmpeg(), video, duration_s * 0.35, out_file), timeout=60)
    except MediaCommandError:
        return ""
    return str(out_file) if out_file.exists() else ""


def render_short(
    src: Path,
    moment: Moment,
    source: Source | None,
    out_dir: Path,
    *,
    clip_id: str,
    captions: list[Caption] | None = None,
    credit: str = "",
) -> Rendered:
    """Make `<clip_id>.mp4` (1080x1920) and `<clip_id>.jpg` in out_dir."""
    from core.proc import run_checked

    _require_ffmpeg()
    settings = clips_settings()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height = _probe_size(src)
    layout = layouts.choose_layout(moment, source, width, height)
    box = source.facecam_box if source else None
    graph = layouts.video_filter(layout, width, height, facecam_box=box)
    out_file = out_dir / f"{clip_id}.mp4"
    duration = moment.end_s - moment.start_s
    with tempfile.TemporaryDirectory(prefix="clipass-") as tmp:
        ass = write_ass(Path(tmp) / "text.ass", hook=moment.hook, credit=credit,
                        captions=captions or [], start_s=moment.start_s, end_s=moment.end_s, layout=layout)
        full = f"{graph};[v]{subtitles_filter(ass.resolve(), _fonts_dir())}[vout]"
        run_checked(build_short_cmd(_ffmpeg(), Path(src), moment, full, out_file, fps=settings.output_fps),
                    timeout=_timeout(duration))
    thumb = _thumbnail(out_file, duration, out_dir / f"{clip_id}.jpg")
    return Rendered(file=str(out_file), thumb=thumb, duration_s=duration, width=1080, height=1920, layout=layout)


def render_long(
    src: Path,
    moment: Moment,
    source: Source | None,
    out_dir: Path,
    *,
    clip_id: str,
    credit: str = "",
) -> Rendered:
    """Make a landscape `<clip_id>.mp4` (1080p at most) and its thumbnail."""
    from core.proc import run_checked

    _require_ffmpeg()
    settings = clips_settings()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height = _probe_size(src)
    out_w, out_h = min(LONG_W, width) // 2 * 2, min(LONG_H, height) // 2 * 2
    out_file = out_dir / f"{clip_id}.mp4"
    duration = moment.end_s - moment.start_s
    with tempfile.TemporaryDirectory(prefix="clipass-") as tmp:
        ass = write_ass(Path(tmp) / "text.ass", hook="", credit=credit, captions=[],
                        start_s=moment.start_s, end_s=moment.end_s, layout=Layout.FULL_WIDTH,
                        out_w=LONG_W, out_h=LONG_H)
        run_checked(build_long_cmd(_ffmpeg(), Path(src), moment, ass.resolve(), _fonts_dir(), out_file,
                                   fps=settings.output_fps), timeout=_timeout(duration))
    thumb = _thumbnail(out_file, duration, out_dir / f"{clip_id}.jpg")
    return Rendered(file=str(out_file), thumb=thumb, duration_s=duration, width=out_w, height=out_h,
                    layout=Layout.FULL_WIDTH)
