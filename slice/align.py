"""Word-level forced alignment for a Short's captions.

Zoom transcripts have sentence times only, so caption words were spread over
each sentence by character weight and drifted. Here the Short's own audio is
aligned to the known sentence text with whisperx (wav2vec2 alignment, no
transcription), giving real word times. Anything that goes wrong (whisperx
not installed, no model for the language, an empty result) leaves that
sentence to the proportional spreading, with a log line."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

from core.logging import get_logger
from core.models import Word

logger = get_logger(__name__)

ALIGN_RATE = 16000
_EDGE_S = 0.05  # a sentence must lie inside the clip (within this) to be aligned


@lru_cache(maxsize=4)
def _model(language: str):
    """The alignment model for `language`, loaded once per process."""
    import whisperx

    return whisperx.load_align_model(language_code=language, device="cpu")


def _align_backend(segments: list[dict], audio: np.ndarray, language: str) -> list[dict]:
    """whisperx.align on `segments` ([{"text", "start", "end"}], seconds from
    the audio start); returns its segments, each with a "words" list."""
    import whisperx

    model, meta = _model(language)
    result = whisperx.align(segments, model, meta, audio, "cpu", return_char_alignments=False)
    return result["segments"]


def _load_audio(flac: Path, start_s: float, end_s: float) -> np.ndarray:
    from slice.ffmpeg_wrapper import ffmpeg_bin

    raw = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-nostdin", "-ss", f"{max(start_s, 0):.3f}", "-t", f"{end_s - start_s:.3f}",
         "-i", str(flac), "-vn", "-ac", "1", "-ar", str(ALIGN_RATE), "-f", "f32le", "-"],
        check=True, capture_output=True, timeout=300).stdout
    return np.frombuffer(raw, dtype="<f4").copy()


def _fill(times: list[tuple[float | None, float | None]], lo: float, hi: float) -> list[tuple[float, float]] | None:
    """Words the aligner gave no time (numbers, symbols) borrow the gap
    between their neighbours; None when nothing was timed."""
    if all(a is None or b is None for a, b in times):
        return None
    out: list[tuple[float, float]] = []
    prev_end = lo
    for k, (a, b) in enumerate(times):
        if a is None or b is None:
            nxt = next((times[j][0] for j in range(k + 1, len(times)) if times[j][0] is not None), hi)
            out.append((prev_end, max(prev_end, nxt)))
        else:
            out.append((float(a), float(b)))
        prev_end = out[-1][1]
    return out


def align_clip(flac: Path, items: list[Word], clip_start: float, clip_end: float,
               language: str = "en") -> dict[int, list[tuple[float, float]]]:
    """{id(item): [(start, end) per word]} in SOURCE seconds for the
    transcript items lying inside [clip_start, clip_end]; items that could not
    be aligned are absent (caption_cues then spreads their words). Never
    raises: a failure returns what it has ({} when nothing)."""
    inside = [it for it in items
              if it.word.split() and it.start >= clip_start - _EDGE_S and it.end <= clip_end + _EDGE_S]
    if not inside or clip_end <= clip_start:
        return {}
    segments = [{"text": " ".join(it.word.split()), "start": max(0.0, it.start - clip_start),
                 "end": max(0.01, it.end - clip_start)} for it in inside]
    try:
        audio = _load_audio(flac, clip_start, clip_end)
        aligned = _align_backend(segments, audio, language)
    except Exception as exc:  # noqa: BLE001 - captions must never fail the Short
        logger.warning("caption alignment unavailable (%s: %s): words spread across each sentence",
                       type(exc).__name__, exc)
        return {}
    out: dict[int, list[tuple[float, float]]] = {}
    if len(aligned) != len(inside):
        logger.warning("caption alignment returned %d segments for %d: words spread across each sentence",
                       len(aligned), len(inside))
        return {}
    for it, seg in zip(inside, aligned, strict=True):
        words = seg.get("words") or []
        n = len(it.word.split())
        if len(words) != n:
            logger.warning("caption alignment gave %d words for %d at %.1fs: spread across the sentence",
                           len(words), n, it.start)
            continue
        times = _fill([(w.get("start"), w.get("end")) for w in words], it.start - clip_start, it.end - clip_start)
        if times is None:
            logger.warning("caption alignment timed no words at %.1fs: spread across the sentence", it.start)
            continue
        out[id(it)] = [(a + clip_start, b + clip_start) for a, b in times]
    return out
