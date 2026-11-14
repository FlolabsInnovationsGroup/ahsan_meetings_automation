"""ffmpeg command builders/runners for the v2 renderer (plan §6).

Every video piece is re-encoded exactly once with the STANDARD profile
(slice/profile.py); stream copy is retired because copy-mode clips start at the
previous keyframe, which put ~78 s of removed content back into the 98-minute
regression job. Cut points are whole source frames:

* video inputs seek a quarter frame early (`-ss`) and `fps` resamples onto a
  grid anchored at the SEEK POINT (start_time=0, round=down), so output frame j
  is whatever is on screen at source time (a + j)/F. Anchoring on the first
  decoded frame instead (setpts=PTS-STARTPTS) shifted whole ranges out of A/V
  sync when that frame is late: a VFR held frame across the cut, or a video
  stream that starts after the audio. The chain then pads the tail by cloning
  (so a short source can never make a piece a frame short) and trims to
  exactly N frames;
* audio is cut from a timestamp-normalised 48 kHz FLAC by sample count, with
  cumulative sample boundaries so the total is exact even at 29.97 fps.

Filter graphs are written to a file (long graphs would hit the command-line
limit) and read with `-/filter_complex <file>` on ffmpeg 7 and newer, or the
older `-filter_complex_script <file>` below 7: the old flag was deprecated in
7.0 and is gone in 8.0, and the new one does not exist before 7.0. See
`filter_graph_args`.

The renderer is written and tested on ffmpeg 9.0.1, which the Docker image
pins (Dockerfile, "AS ffmpeg"); CI runs the render tests on that same build.
Distro builds broke it: Debian 12's 5.1 shipped a 27-hour final video, and
Debian 13's 7.1 rejects the xfade chain ("current rate of 1/0 is invalid").
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.proc import run_checked
from core.timeline import AUDIO_RATE, rate_str, ts

from .fonts import filter_path
from .plan import InputSpan, VideoPart
from .profile import (
    AUDIO_ARGS,
    BASE_ARGS,
    HD_TIMEOUT_FACTOR,
    ffmpeg_timeout,
    shorts_video_args,
    video_args,
)

logger = get_logger(__name__)

# Samples read on each side of an audio cut so the accurate-seek rounding
# (microsecond -ss) never eats into the samples we keep.
AUDIO_SEEK_MARGIN = 480  # 10 ms @ 48 kHz
MICRO_FADE_SAMPLES = 960  # 20 ms


def ffmpeg_bin() -> str:
    return get_settings().ffmpeg_bin


# First ffmpeg that reads an option's value from a file with `-/<option>`.
FILE_OPTION_FROM_MAJOR = 7


def parse_ffmpeg_major(version_output: str) -> int | None:
    """The major version on the first line of `ffmpeg -version`:
    "5.1.9-0+deb12u1" -> 5, "n7.1-3-g1ab2c3" -> 7, "9.0.1-full_build-..." -> 9.
    None for a git build ("N-117234-g...") or anything unrecognisable."""
    match = re.search(r"ffmpeg version n?(\d+)\.\d", version_output or "")
    return int(match.group(1)) if match else None


@lru_cache(maxsize=8)
def ffmpeg_major(binary: str) -> int | None:
    """Major version of this ffmpeg binary (asked once per binary), or None
    when it can't be told. A missing or broken ffmpeg fails properly, with
    its own message, at its first real use."""
    try:
        output = run_checked([binary, "-hide_banner", "-version"], timeout=15).stdout
    except Exception:  # noqa: BLE001
        return None
    return parse_ffmpeg_major(output)


def filter_graph_args(graph_path: Path) -> list[str]:
    """The arguments that make ffmpeg read its filter graph from `graph_path`:
    `-/filter_complex` on ffmpeg 7+ (and when the version is unknown, which
    means a recent git build), `-filter_complex_script` on older ones."""
    major = ffmpeg_major(ffmpeg_bin())
    old = major is not None and major < FILE_OPTION_FROM_MAJOR
    return ["-filter_complex_script" if old else "-/filter_complex", str(graph_path)]


# --------------------------------------------------------------------- video


def video_input_args(src: Path, span: InputSpan, fps: Fraction) -> list[str]:
    """Seek a quarter frame before `span.start` and read a few frames past the
    end (the graph trims exactly). After the seek, frame n sits at (n - a +
    1/4)/F, so the chain's round-down fps grid maps it to slot n - a with a
    quarter-frame margin either side: frame a-1 is dropped by the accurate
    seek, and a coarse track timescale (e.g. 1/fps, where half a frame is not
    representable) or the microsecond -ss rounding cannot move a frame into
    the neighbouring slot."""
    args: list[str] = []
    if span.start > 0:
        args += ["-ss", ts(Fraction(4 * span.start - 1, 4) / fps)]
    args += ["-t", ts(Fraction(span.frames + 3) / fps), "-i", str(src)]
    return args


def exact_frames_chain(k: int, frames: int, fps: Fraction, normalise: str = "") -> str:
    """The start of every video input chain: exactly `frames` frames on the
    source grid, timestamps from 0 (`normalise`, if given, ends with ",").

    No setpts before `fps`: the input timestamps are already relative to the
    seek point (or to the file start, which is also where the audio FLAC
    starts), and `start_time=0` keeps that anchor, padding with the first
    decoded frame if it arrives late. Re-anchoring on the first frame would
    pull everything after a late first frame early, out of sync with audio."""
    return (f"[{k}:v]fps={rate_str(fps)}:start_time=0:round=down,{normalise}"
            f"tpad=stop_mode=clone:stop=3,trim=end_frame={frames},setpts=PTS-STARTPTS")


def video_input_chain(k: int, span: InputSpan, fps: Fraction, width: int, height: int, label: str,
                      overlay: str = "") -> str:
    """Normalised input: `overlay` (e.g. a drawtext label) is applied after the trim."""
    chain = exact_frames_chain(k, span.frames, fps, f"scale={width}:{height},setsar=1,format=yuv420p,")
    return f"{chain}{',' + overlay if overlay else ''}[{label}]"


def build_video_part_graph(part: VideoPart, fps: Fraction, width: int, height: int,
                           overlays: dict[int, str] | None = None) -> str:
    """Filter graph for one batch/seam: per-input normalise+trim (+ an optional
    per-range overlay), then an xfade chain (dissolves) or a concat (hard
    cuts). Output label [vout]."""
    n = len(part.inputs)
    lines = []
    for k, span in enumerate(part.inputs):
        label = "vout" if n == 1 else f"v{k}"
        overlay = ""
        if overlays and k < len(part.input_ranges):
            overlay = overlays.get(part.input_ranges[k], "")
        lines.append(video_input_chain(k, span, fps, width, height, label, overlay))
    if n > 1 and part.join == "concat":
        lines.append("".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
    elif n > 1:
        d = Fraction(part.fade_frames) / fps
        prev = "v0"
        for k, off in enumerate(part.xfade_offsets(), start=1):
            out = "vout" if k == n - 1 else f"x{k}"
            lines.append(
                f"[{prev}][v{k}]xfade=transition=fade:duration={ts(d)}:offset={ts(Fraction(off) / fps)}[{out}]"
            )
            prev = out
    return ";\n".join(lines)


def build_video_part_cmd(
    src: Path, part: VideoPart, fps: Fraction, width: int, height: int, graph_path: Path, dest: Path,
    overlays: dict[int, str] | None = None, hd: bool = False,
) -> list[str]:
    graph_path.write_text(build_video_part_graph(part, fps, width, height, overlays), encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    for span in part.inputs:
        cmd += video_input_args(src, span, fps)
    cmd += [*filter_graph_args(graph_path), "-map", "[vout]", "-an", *video_args(fps, hd),
            "-frames:v", str(part.frames), str(dest)]
    return cmd


def render_video_part(
    src: Path, part: VideoPart, fps: Fraction, width: int, height: int, graph_path: Path, dest: Path,
    overlays: dict[int, str] | None = None, hd: bool = False,
) -> None:
    cmd = build_video_part_cmd(src, part, fps, width, height, graph_path, dest, overlays, hd)
    logger.debug("render %s (%d inputs, %d frames): %s", part.kind, len(part.inputs), part.frames, dest.name)
    run_checked(cmd, timeout=ffmpeg_timeout(float(Fraction(part.frames) / fps)) * (HD_TIMEOUT_FACTOR if hd else 1.0))


def _concat_quote(path: str) -> str:
    """Single-quote a path for a concat-demuxer list. Inside quotes the only
    special character is the quote itself, written as '\\'' (close, escaped
    quote, reopen); unescaped, an apostrophe anywhere in the storage path
    ("O'Brien jobs") broke every multi-part render."""
    return "'" + path.replace("'", "'\\''") + "'"


def concat_copy(pieces: list[Path], dest: Path, list_path: Path, durations_s: list[str] | None = None,
                content_s: float = 0.0) -> None:
    """concat demuxer, stream copy. Callers must assert the pieces share one
    encoder profile first (profile.assert_concat_compatible)."""
    lines = []
    for i, piece in enumerate(pieces):
        lines.append(f"file {_concat_quote(piece.resolve().as_posix())}")
        if durations_s is not None:
            lines.append(f"duration {durations_s[i]}")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy",
           "-movflags", "+faststart", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 20.0))


# --------------------------------------------------------------------- audio


def extract_audio_flac(src: Path, dest: Path, channels: int, source_duration_s: float) -> None:
    """Normalise the source audio ONCE to a gap-free 48 kHz FLAC. Cutting audio
    straight from the mp4 drifted +319 ms over 111 cuts (source pts gaps);
    every deliverable's audio is cut from this file instead."""
    ch = 1 if channels == 1 else 2
    # regular frames: a late-starting track makes aresample emit a short
    # padding frame first, which the FLAC encoder would take as its block size
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(src), "-vn", "-af",
           f"aresample=async=1:first_pts=0,aresample={AUDIO_RATE},{AUDIO_REFRAME}",
           "-ar", str(AUDIO_RATE), "-ac", str(ch), "-c:a", "flac", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(source_duration_s / 10.0))


@dataclass(frozen=True)
class AudioPiece:
    src_start: int  # sample index in audio.flac
    samples: int  # exact length
    fade_in: bool
    fade_out: bool


def audio_input_args(flac: Path, piece: AudioPiece) -> tuple[list[str], int]:
    pre = min(AUDIO_SEEK_MARGIN, piece.src_start)
    seek = piece.src_start - pre
    args: list[str] = []
    if seek > 0:
        args += ["-ss", ts(Fraction(seek, AUDIO_RATE))]
    args += ["-t", ts(Fraction(pre + piece.samples + AUDIO_SEEK_MARGIN, AUDIO_RATE)), "-i", str(flac)]
    return args, pre


# Re-chunk the joined audio into regular frames before the FLAC encoder,
# which takes its block size from the first frame it gets: a cut that left
# only 12 samples in the first decoded frame made it fail with "invalid block
# size: 12" (found on the 98-minute regression meeting). p=0: never pad, the
# sample count must stay exact.
AUDIO_REFRAME = "asetnsamples=n=4096:p=0"


def build_audio_graph(pieces: list[AudioPiece], pres: list[int], first_input: int = 0,
                      out_label: str = "aout") -> str:
    lines = []
    n = len(pieces)
    for k, (piece, pre) in enumerate(zip(pieces, pres)):
        # tail-pad with silence so a piece that runs into the end of the file
        # still yields exactly `samples` samples. Unbounded: the source audio
        # may end up to 1 s before the video (ingest allows it), and atrim
        # ends the stream at end_sample anyway.
        chain = (f"[{first_input + k}:a]apad,"
                 f"atrim=start_sample={pre}:end_sample={pre + piece.samples},asetpts=PTS-STARTPTS")
        fade = min(MICRO_FADE_SAMPLES, piece.samples // 4)
        if piece.fade_in and fade:
            chain += f",afade=t=in:ss=0:ns={fade}"
        if piece.fade_out and fade:
            chain += f",afade=t=out:ss={piece.samples - fade}:ns={fade}"
        if n == 1:
            lines.append(f"{chain},{AUDIO_REFRAME}[{out_label}]")
        else:
            lines.append(f"{chain}[a{k}]")
    if n > 1:
        lines.append("".join(f"[a{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1,{AUDIO_REFRAME}[{out_label}]")
    return ";\n".join(lines)


def build_audio_chunk_cmd(flac: Path, pieces: list[AudioPiece], graph_path: Path, dest: Path) -> list[str]:
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    pres = []
    for piece in pieces:
        args, pre = audio_input_args(flac, piece)
        cmd += args
        pres.append(pre)
    graph_path.write_text(build_audio_graph(pieces, pres), encoding="utf-8")
    cmd += [*filter_graph_args(graph_path), "-map", "[aout]", "-c:a", "flac", "-ar", str(AUDIO_RATE), str(dest)]
    return cmd


def render_audio_chunk(flac: Path, pieces: list[AudioPiece], graph_path: Path, dest: Path) -> None:
    cmd = build_audio_chunk_cmd(flac, pieces, graph_path, dest)
    total_s = sum(p.samples for p in pieces) / AUDIO_RATE
    run_checked(cmd, timeout=ffmpeg_timeout(total_s / 10.0))


# Peak ceiling for levelled audio: -1.5 dBFS, as the limiter's linear value.
PEAK_LIMIT = f"{10 ** (-1.5 / 20):.4f}"


LUFS_FLOOR = -70.0
_LUFS_SUMMARY = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?|-?inf)\s*LUFS", re.MULTILINE | re.IGNORECASE)


def measure_lufs(flac: Path, piece: AudioPiece | None = None, timeout: float | None = None) -> float | None:
    """Integrated loudness (LUFS, EBU R128) of a piece of the FLAC, or the
    whole file. None when it is quieter than -70 (silence) or anything fails.
    About 5 s for a 101-minute meeting."""
    try:
        cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "info"]
        total_s = 0.0
        if piece is not None:
            args, pre = audio_input_args(flac, piece)
            cmd += args
            af = f"atrim=start_sample={pre}:end_sample={pre + piece.samples},asetpts=PTS-STARTPTS,"
            total_s = piece.samples / AUDIO_RATE
        else:
            cmd += ["-i", str(flac)]
            af = ""
        cmd += ["-vn", "-af", f"{af}ebur128=framelog=quiet", "-f", "null", "-"]
        if timeout is None:
            timeout = ffmpeg_timeout(max(total_s, 60.0) / 10.0)
        result = run_checked(cmd, timeout=timeout)
        found = _LUFS_SUMMARY.findall(result.stderr or "")
        if not found:
            return None
        value = float(found[-1])
    except Exception:  # noqa: BLE001 - loudness is a nicety: never fail a render over it
        return None
    if not math.isfinite(value) or value <= LUFS_FLOOR:
        return None
    return value


def gain_to_target(measured: float | None, target: float, max_boost: float = 20.0,
                   max_cut: float = 10.0) -> float | None:
    """The dB gain that brings `measured` loudness to `target`, limited to
    +max_boost / -max_cut. None when nothing was measured."""
    if measured is None:
        return None
    return max(-max_cut, min(max_boost, target - measured))


def encode_aac(chunks: list[Path], dest: Path, graph_path: Path, content_s: float = 0.0,
               gains: list[float | None] | None = None) -> None:
    """Join the exact FLAC chunks (lossless concat filter) and encode AAC once.
    ffmpeg's AAC encoder is single-threaded (~50x realtime here), so the
    timeout scales with the audio length. `gains` (dB, aligned with `chunks`,
    None = leave alone) turns single chunks up or down before the join, then
    one limiter catches the peaks (latency=1: without it alimiter delays the
    audio by 239 samples). With no gain set the command is the plain one."""
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    for chunk in chunks:
        cmd += ["-i", str(chunk)]
    if gains and any(g is not None for g in gains):
        lines, labels = [], ""
        for k in range(len(chunks)):
            g = gains[k] if k < len(gains) else None
            if g is None:
                labels += f"[{k}:a]"
            else:
                lines.append(f"[{k}:a]volume={g:.2f}dB[g{k}]")
                labels += f"[g{k}]"
        lines.append(f"{labels}concat=n={len(chunks)}:v=0:a=1[aj]")
        lines.append(f"[aj]alimiter=limit={PEAK_LIMIT}:level=0:latency=1[aout]")
        graph_path.write_text(";\n".join(lines), encoding="utf-8")
        cmd += [*filter_graph_args(graph_path), "-map", "[aout]"]
    elif len(chunks) == 1:
        cmd += ["-map", "0:a:0"]
    else:
        graph_path.write_text("".join(f"[{k}:a]" for k in range(len(chunks))) +
                              f"concat=n={len(chunks)}:v=0:a=1[aout]", encoding="utf-8")
        cmd += [*filter_graph_args(graph_path), "-map", "[aout]"]
    cmd += [*AUDIO_ARGS, "-vn", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 10.0))


def mux(video: Path, audio: Path, dest: Path, content_s: float = 0.0) -> None:
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
           "-c", "copy", "-movflags", "+faststart", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 20.0))


def render_silence_flac(dest: Path, samples: int, channels: int) -> None:
    """Exactly `samples` samples of silence (the title card's audio)."""
    layout = "mono" if channels == 1 else "stereo"
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl={layout}",
           "-af", f"atrim=end_sample={samples}", "-c:a", "flac", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(samples / AUDIO_RATE))


# ------------------------------------------------------------ text overlays


def drawtext(font: Path, textfile: Path, *, size: int, x: str = "(w-tw)/2", y: str = "24", color: str = "white",
             box: bool = True) -> str:
    """A drawtext filter reading its text from a UTF-8 file (no escaping of
    arbitrary text) with an explicit font file (fontconfig lookups crash the
    Windows build). expansion=none: drawtext still expands %{...} and
    backslashes in a textfile, so "100% done" failed and "C:\\new" lost its
    backslashes."""
    parts = [f"fontfile={filter_path(font)}", f"textfile={filter_path(textfile)}", "expansion=none",
             f"fontsize={size}", f"fontcolor={color}", "text_align=C", f"x={x}", f"y={y}"]
    if box:
        parts += ["box=1", "boxcolor=black@0.6", f"boxborderw={max(4, size // 3)}"]
    return "drawtext=" + ":".join(parts)


COLOR_OPTS = {"color_primaries": "color_primaries", "color_transfer": "color_trc", "color_space": "colorspace",
              "color_range": "range"}


def color_params(tags: dict[str, str], *, reset_missing: bool = False) -> str:
    """setparams for the source's colour tags, so a generated frame (title
    card) encodes with the same SPS/VUI as pieces cut from the source — else
    `concat -c copy` would join two different streams (the extradata assert
    catches that).

    reset_missing: also set every tag the source does NOT have to "unknown",
    for frames that bring tags of their own (an intro/outro clip). Left
    alone, a bt709-tagged or full-range clip kept its tags when the meeting
    was untagged, so x264 wrote a different SPS and the clip was always
    dropped. The title card's colour source carries no tags, so it doesn't
    need this."""
    opts = []
    for key, opt in COLOR_OPTS.items():
        value = tags.get(key)
        if value and value != "unknown":
            opts.append(f"{opt}={value}")
        elif reset_missing:
            opts.append(f"{opt}=unknown")
    return ("setparams=" + ":".join(opts) + ",") if opts else ""


def build_card_graph(*, fps: Fraction, width: int, height: int, frames: int, font: Path, bold_font: Path,
                     title_file: Path | None, subtitle_file: Path, title_size: int, subtitle_size: int,
                     color_tags: dict[str, str]) -> str:
    """Black card, the meeting topic (optional) above a smaller grey
    "Full meeting", fading in and out."""
    duration = Fraction(frames) / fps
    fade = min(Fraction(3, 10), duration / 4)
    gap = max(8, height // 40)
    chain = (f"color=c=black:s={width}x{height}:r={rate_str(fps)},format=yuv420p,setsar=1,"
             f"{color_params(color_tags)}")
    if title_file is not None:
        chain += drawtext(bold_font, title_file, size=title_size, y=f"h/2-th-{gap}", box=False) + ","
        sub_y = f"h/2+{gap}"
    else:
        sub_y = "(h-th)/2"
    chain += drawtext(font, subtitle_file, size=subtitle_size, y=sub_y, color="0xBBBBBB", box=False) + ","
    chain += (f"fade=t=in:st=0:d={ts(fade)},fade=t=out:st={ts(duration - fade)}:d={ts(fade)},"
              f"trim=end_frame={frames}[vout]")
    return chain


def render_card_video(dest: Path, graph: str, graph_path: Path, fps: Fraction, frames: int,
                      hd: bool = False) -> None:
    graph_path.write_text(graph, encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS, *filter_graph_args(graph_path), "-map", "[vout]", "-an",
           *video_args(fps, hd), "-frames:v", str(frames), str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(float(Fraction(frames) / fps)) * (HD_TIMEOUT_FACTOR if hd else 1.0))


# ------------------------------------------------------------ intro / outro


def build_clip_graph(*, fps: Fraction, width: int, height: int, frames: int, color_tags: dict[str, str]) -> str:
    """An intro/outro clip on the meeting's frame grid and frame size: the
    same exact-frames chain as a piece cut from the source (fps resample from
    t=0, clone-pad, trim to `frames`), then square pixels (an anamorphic clip
    keeps its shape), scaled to fit inside width x height with its aspect
    kept, centred on black, and tagged EXACTLY like the meeting (every tag
    the meeting lacks reset to unknown) so it encodes to the same SPS.

    Levels are converted to the meeting's range (full-range pixels labelled
    tv looked crushed, tv pixels labelled pc washed out; the scaler treats
    an untagged clip as tv). Matrix/primaries/transfer are only labelled,
    not converted: right for the usual SDR bt709 or untagged HD clip, while
    a bt601 or HDR clip shows slightly shifted colours."""
    out_range = "pc" if color_tags.get("color_range") == "pc" else "tv"
    normalise = (f"scale=w=trunc(iw*sar/2)*2:h=ih,setsar=1,"
                 f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
                 f"out_range={out_range},"
                 f"pad={width}:{height}:-1:-1:color=black,setsar=1,format=yuv420p,"
                 f"{color_params(color_tags, reset_missing=True)}")
    return exact_frames_chain(0, frames, fps, normalise) + "[vout]"


def render_clip_video(src: Path, dest: Path, graph: str, graph_path: Path, fps: Fraction, frames: int,
                      hd: bool = False) -> None:
    graph_path.write_text(graph, encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(src), *filter_graph_args(graph_path), "-map", "[vout]", "-an",
           *video_args(fps, hd), "-frames:v", str(frames), str(dest)]
    # decoding a 1080p60 clip costs more than its length suggests
    run_checked(cmd, timeout=ffmpeg_timeout(4.0 * float(Fraction(frames) / fps)) * (HD_TIMEOUT_FACTOR if hd else 1.0))


def render_clip_audio(src: Path, dest: Path, samples: int, channels: int) -> None:
    """The clip's audio as exactly `samples` samples of 48 kHz FLAC with the
    meeting's channel count: normalised like audio.flac (gap-free from t=0,
    where the clip's video starts too), then padded with silence or cut to
    the length of the clip's frames, so A/V stay in sync at every join."""
    layout = "mono" if channels == 1 else "stereo"
    chain = (f"aresample=async=1:first_pts=0,aresample={AUDIO_RATE},"
             f"aformat=sample_rates={AUDIO_RATE}:channel_layouts={layout},"
             f"apad,atrim=end_sample={samples},{AUDIO_REFRAME}")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(src), "-vn", "-af", chain, "-c:a", "flac", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(samples / AUDIO_RATE))


# --------------------------------------------------------------------- shorts

SHORT_FADE_SAMPLES = 4800  # 100 ms in/out: a short starts and ends mid-conversation


def build_short_graph(*, frames: int, fps: Fraction, width: int, height: int, pre: int, samples: int,
                      subtitles: str = "") -> str:
    """Vertical short: the frame scaled to fit the width, over a blurred,
    zoomed copy of itself (blurred at quarter size, then upscaled — same look,
    a fraction of the cost), optional burned captions, audio from the FLAC."""
    bw, bh = max(2, width // 4 // 2 * 2), max(2, height // 4 // 2 * 2)
    fade = min(SHORT_FADE_SAMPLES, samples // 4)
    lines = [
        exact_frames_chain(0, frames, fps) + ",split=2[b0][f0]",
        (f"[b0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
         f"boxblur=luma_radius=8:luma_power=2:chroma_radius=4:chroma_power=2,scale={width}:{height},setsar=1[bg]"),
        f"[f0]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg]",
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2{',' + subtitles if subtitles else ''},format=yuv420p[vout]",
        f"[1:a]apad,atrim=start_sample={pre}:end_sample={pre + samples},asetpts=PTS-STARTPTS"
        + (f",afade=t=in:ss=0:ns={fade},afade=t=out:ss={samples - fade}:ns={fade}" if fade else "") + "[aout]",
    ]
    return ";\n".join(lines)


# The new Shorts look (slide/speaker crops over a dark blur, levelled sound).
SHORT_W, SHORT_H = 1080, 1920
SHORT_BAND_TOP, SHORT_BAND_H = 480, 650  # where a slide sits: under the hook, above the captions
SHORT_PANEL_H = 1350  # the 4:5 speaker panel
SHORT_PANEL_Y = 285
SHORT_ZOOM_W = 960
SHORT_TIMEOUT_FACTOR = 4.0
AFFTDN_DELAY = 1200  # afftdn delays its output by 1200 samples (25 ms) and keeps the length
SHORT_HD_TIMEOUT_FACTOR = 6.0  # HD medium costs up to 65% more on gallery content
SHORT_BG_BLUR = ("boxblur=luma_radius=8:luma_power=2:chroma_radius=4:chroma_power=2,"
                 "eq=brightness=-0.3:saturation=0.7")


def _even(value: float, floor: int = 2) -> int:
    return max(floor, int(value) // 2 * 2)


def _even_box(box, bounds: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """An (x, y, w, h) crop with even numbers, inside `bounds` (w, h) if given."""
    x, y, w, h = (int(v) for v in box)
    w, h = _even(w), _even(h)
    if bounds is not None:
        bw, bh = bounds
        w, h = min(w, _even(bw)), min(h, _even(bh))
        x, y = max(0, min(x, bw - w)), max(0, min(y, bh - h))
    return max(0, x) // 2 * 2, max(0, y) // 2 * 2, w, h


def _zoom_box(panel: tuple[int, int, int, int], change_box) -> tuple[int, int, int, int]:
    """A 960-wide crop of the slide panel centred on the part that changes,
    kept inside the panel (the whole panel when it is not wider than 960)."""
    px, py, pw, ph = panel
    if change_box is None or pw <= SHORT_ZOOM_W:
        return panel
    w = SHORT_ZOOM_W
    h = _even(w * ph / pw)
    cx = change_box[0] + change_box[2] / 2
    cy = change_box[1] + change_box[3] / 2
    x = round(min(max(px, cx - w / 2), px + pw - w))
    y = round(min(max(py, cy - h / 2), py + ph - h))
    return _even_box((x, y, w, h), (px + pw, py + ph))


def _speaker_crop(aspect: str, focus_x) -> str:
    """crop to full height at `aspect` (w:h) around the speaker, inside the frame."""
    fx = f"{int(focus_x)}" if focus_x is not None else "iw/2"
    return (f"crop=w='trunc(ih*{aspect}/2)*2':h='trunc(ih/2)*2':"
            f"x='trunc(min(max(0,{fx}-ow/2),iw-ow)/2)*2':y=0")


def _blur_stamp(lines: list[str], head: str, stamp, shown, src_size) -> str:
    """Blur Zoom's burned-in timestamp (a (x, y, w, h) source box found by slice.layout) so it does not
    show in the Short. Only that small box is touched, never a face or a name label. `shown` is the
    source box the Short displays (None: all of it); a stamp outside it needs no blur. Adds the lines
    to `lines` and returns the new head to carry on from."""
    if not stamp:
        return head
    x, y, w, h = _even_box(stamp, src_size)
    if shown is not None:
        sx, sy, sw, sh = shown
        if x + w <= sx or x >= sx + sw or y + h <= sy or y >= sy + sh:
            return head
    luma, chroma = max(1, min(10, min(w, h) // 2 - 1)), max(1, min(5, min(w, h) // 4 - 1))
    lines.append(f"{head},split=2[s0][s1]")
    lines.append(f"[s1]crop={w}:{h}:{x}:{y},boxblur=luma_radius={luma}:luma_power=3:"
                 f"chroma_radius={chroma}:chroma_power=3[sb]")
    lines.append(f"[s0][sb]overlay={x}:{y}[hs]")
    return "[hs]null"


def build_short_graph_new(*, spans_frames: list[int], fps: Fraction, width: int, height: int, pre,
                          samples: int, layout, look, subtitles: str = "", gain_db: float | None = None,
                          n_audio: int = 1, audio_pieces: list[AudioPiece] | None = None,
                          src_size: tuple[int, int] | None = None) -> str:
    """Vertical short, new look. One video input per span (hard-cut together
    when there are several), then by `layout.kind`: a screen share or gallery
    is cropped to its content and shown 1080 wide over a dark blurred copy; a
    speaker fills the frame (or sits in a 4:5 panel); anything else is today's
    fit. Audio: the FLAC piece(s) after the video inputs, optional denoise,
    optional gain with a peak limiter, trimmed to exactly `samples` again.
    `pre` is the seek margin of the one audio piece (a list for several;
    `audio_pieces` then holds the pieces). `src_size` (w, h) lets crops be
    clamped to the source."""
    n_spans = len(spans_frames)
    sy = height / SHORT_H
    kind = getattr(layout, "kind", "fit")
    panel = getattr(layout, "panel", None)
    if kind in ("screen", "gallery") and not panel:
        kind = "fit"
    speaker_full = kind == "speaker" and getattr(look, "speaker_layout", "full") == "full"
    lines: list[str] = []
    if n_spans == 1:
        head = exact_frames_chain(0, spans_frames[0], fps)
    else:
        for k, frames in enumerate(spans_frames):
            lines.append(exact_frames_chain(k, frames, fps) + f"[v{k}]")
        head = "".join(f"[v{k}]" for k in range(n_spans)) + f"concat=n={n_spans}:v=1:a=0"
    sub = f",{subtitles}" if subtitles else ""
    stamp = getattr(layout, "stamp", None)
    if speaker_full:
        head = _blur_stamp(lines, head, stamp, None, src_size)
        lines.append(f"{head},{_speaker_crop('9/16', getattr(layout, 'focus_x', None))},"
                     f"scale={width}:{height}:flags=lanczos,setsar=1{sub},format=yuv420p[vout]")
    else:
        bw, bh = max(2, width // 4 // 2 * 2), max(2, height // 4 // 2 * 2)
        shown = None
        if kind in ("screen", "gallery"):
            box = panel
            if kind == "screen" and getattr(look, "screen_zoom", "whole_slide") == "zoom":
                box = _zoom_box(tuple(int(v) for v in panel), getattr(layout, "change_box", None))
            shown = _even_box(box, src_size)
        head = _blur_stamp(lines, head, stamp, shown, src_size)
        lines.append(f"{head},split=2[b0][f0]")
        lines.append(f"[b0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                     f"{SHORT_BG_BLUR},scale={width}:{height},setsar=1[bg]")
        if kind in ("screen", "gallery"):
            x, y, w, h = shown
            top, band = _even(SHORT_BAND_TOP * sy), _even(SHORT_BAND_H * sy)
            lines.append(f"[f0]crop={w}:{h}:{x}:{y},scale={width}:-2:flags=lanczos,setsar=1[fg]")
            pos = f"x='(W-w)/2':y='{top}+max(0,trunc(({band}-h)/4)*2)'"
        elif kind == "speaker":
            lines.append(f"[f0]{_speaker_crop('4/5', getattr(layout, 'focus_x', None))},"
                         f"scale={width}:{_even(SHORT_PANEL_H * sy)}:flags=lanczos,setsar=1[fg]")
            pos = f"x=0:y={round(SHORT_PANEL_Y * sy)}"
        else:
            lines.append(f"[f0]scale={width}:{height}:force_original_aspect_ratio=decrease:"
                         f"force_divisible_by=2,setsar=1[fg]")
            pos = "x=(W-w)/2:y=(H-h)/2"
        lines.append(f"[bg][fg]overlay={pos}{sub},format=yuv420p[vout]")

    # audio
    fade = min(SHORT_FADE_SAMPLES, samples // 4)
    denoise = "afftdn=nr=10:nf=-40," if getattr(look, "denoise", False) else ""
    gain = (f",volume={gain_db:.2f}dB,alimiter=limit={PEAK_LIMIT}:level=0:latency=1,"
            f"apad,atrim=end_sample={samples}") if gain_db is not None else ""
    if n_audio > 1:
        if not audio_pieces or len(audio_pieces) != n_audio or not isinstance(pre, (list, tuple)):
            raise ValueError("several audio pieces need audio_pieces and a list of seek margins")
        if denoise or gain:
            lines.append(build_audio_graph(audio_pieces, list(pre), n_spans, "araw"))
            if denoise:  # undo afftdn's delay, then restore the exact length
                denoise = (f"{denoise}atrim=start_sample={AFFTDN_DELAY},asetpts=PTS-STARTPTS,"
                           f"apad,atrim=end_sample={samples},")
            lines.append(f"[araw]{denoise.rstrip(',')}{',' if denoise else ''}"
                         f"{gain.lstrip(',') if gain else 'anull'}[aout]")
        else:
            lines.append(build_audio_graph(audio_pieces, list(pre), n_spans, "aout"))
    else:
        lines.append(f"[{n_spans}:a]{denoise}apad,"
                     f"atrim=start_sample={pre + (AFFTDN_DELAY if denoise else 0)}:"
                     f"end_sample={pre + (AFFTDN_DELAY if denoise else 0) + samples},"
                     f"asetpts=PTS-STARTPTS"
                     + (f",afade=t=in:ss=0:ns={fade},afade=t=out:ss={samples - fade}:ns={fade}" if fade else "")
                     + f"{gain}[aout]")
    return ";\n".join(lines)


def subtitles_filter(ass_path: Path, fonts_dir: Path | None) -> str:
    opts = [f"filename={filter_path(ass_path)}"]
    if fonts_dir is not None:
        opts.append(f"fontsdir={filter_path(fonts_dir)}")
    return "subtitles=" + ":".join(opts)


def render_short(src: Path, flac: Path, span: InputSpan | list[InputSpan], piece: AudioPiece | list[AudioPiece],
                 graph: str, graph_path: Path, dest: Path, fps: Fraction, hd: bool = False) -> None:
    """Encode one short. `span` and `piece` are one each (the classic graph) or
    lists (the new graph with several cuts); video inputs come first, then the
    audio inputs. `hd` selects the high-quality encode, which takes longer."""
    spans = span if isinstance(span, list) else [span]
    pieces = piece if isinstance(piece, list) else [piece]
    graph_path.write_text(graph, encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    for s in spans:
        cmd += video_input_args(src, s, fps)
    for p in pieces:
        cmd += audio_input_args(flac, p)[0]
    frames = sum(s.frames for s in spans)
    cmd += [*filter_graph_args(graph_path), "-map", "[vout]", "-map", "[aout]",
            *(shorts_video_args(fps, True) if hd else video_args(fps)), *AUDIO_ARGS,
            "-frames:v", str(frames), "-movflags", "+faststart", str(dest)]
    # a 1080x1920 blur+overlay encode is ~3-4x the work of a 720p piece
    factor = SHORT_HD_TIMEOUT_FACTOR if hd else SHORT_TIMEOUT_FACTOR
    run_checked(cmd, timeout=ffmpeg_timeout(factor * float(Fraction(frames) / fps)))
