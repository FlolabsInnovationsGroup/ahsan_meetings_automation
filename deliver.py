"""Everything after the decisions: render the videos, write the transcripts,
chapters and report, then manifest.json + bundle.zip (plan §6.4, D9-D12, D18).

The zip the owner gets for each meeting:
    Final_<Meeting>_<YYYY-MM-DD>_Youtube.mp4
                         intro, highlights reel, title card, the cleaned meeting, outro
                         (the intro/outro are the owner's clips, when there are any);
                         its key in job.artifacts is "final.mp4" (core/naming.py)
    highlights.mp4       the reel on its own
    shorts/short_NN.mp4  vertical shorts (+ .srt captions, shorts.json)
    removed.mp4          every cut of 1 s or more (not pure silences), labelled with time + reason
    report.pdf           what was removed, when and why; highlights; shorts; chapters
    transcript_removed.txt/.json   the removed text with original timestamps
    transcript_clean.txt/.json     what the final video says, with its times
    chapters.txt         YouTube chapters for the final video
    manifest.json        sizes, sha256, durations, status of every file

Mandatory: the final video, both transcripts, manifest, bundle. Anything else that
fails is recorded (status + reason + a warning) and the job still succeeds.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from bundle.package import fill_file_info, sha256_file, write_bundle, write_manifest
from core.config import get_settings
from core.errors import InsufficientDisk, is_fatal
from core.logging import get_logger
from core.models import ArtifactInfo, JobRecord, JobStatus, Moment, RenderManifest, Word
from core.timeline import parse_rate
from core.version import PIPELINE_VERSION
from decide.chapters import ChapterResult, finalize_chapters, format_chapters
from decide.pricing import estimate_cost_usd
from outputs import RenderInputs, RenderOutputs, render_outputs
from report.pdf import ReportData, build_report
from report.text import (
    clean_lines,
    removed_entries,
    write_clean_transcript,
    write_removed_transcript,
)
from slice.fonts import find_font
from slice.transcript import remap_transcript

logger = get_logger(__name__)

ChapterFn = Callable[[list[Word], float], ChapterResult]
ZIP_HEADROOM_BYTES = 100 * 1024**2


@dataclass
class DeliverInputs:
    render: RenderInputs
    reel: list[Moment] = field(default_factory=list)
    # None = chapters disabled
    chapter_fn: ChapterFn | None = None
    # the re-ranked moments and the transcript's segments: the topic cuts pick from them
    moments: list[Moment] = field(default_factory=list)
    segments: list = field(default_factory=list)


def deliver(job: JobRecord, update: Callable[[JobRecord], None], inp: DeliverInputs) -> None:
    """Runs the rest of the job; sets job.status DONE at the end. Raises on a
    mandatory failure (the caller marks the job failed)."""
    settings = get_settings()
    job_dir = inp.render.job_dir

    def set_status(status: JobStatus) -> None:
        job.status = status
        job.progress_current = job.progress_total = 0
        update(job)

    def on_progress(current: int, total: int) -> None:
        job.progress_current, job.progress_total = current, total
        update(job)

    with _stage(job, "rendering"):
        out = render_outputs(inp.render, set_status=set_status, on_progress=on_progress)
    job.warnings.extend(out.warnings)
    job.artifacts = dict(out.artifacts)
    final_name = job.artifacts["final.mp4"].path
    # a re-render of a job made before the Final_<Meeting>_<date>_Youtube.mp4
    # names (or whose title changed since) leaves its old video behind: it is
    # no longer an artifact, so it only costs disk
    for old in [job_dir / "final.mp4", *job_dir.glob("Final_*_Youtube.mp4")]:
        if old.name != final_name and old.is_file():
            old.unlink(missing_ok=True)
    job.output_video_path = str(out.final_path)
    job.final_offset_s = round(out.final_offset_s, 6)
    job.intro_s, job.outro_s = round(out.intro_s, 6), round(out.outro_s, 6)
    for name, secs in out.timings.items():
        job.stage_timings[f"render_{name}"] = secs
    _write_json(job_dir / "render_manifest.json", {
        **out.cleaned.model_dump(), "loudness_measured_lufs": out.loudness_measured_lufs,
        "loudness_gain_db": out.loudness_gain_db, "full_hd": out.full_hd})
    job.render_manifest_path = str(job_dir / "render_manifest.json")
    job.progress_current = job.progress_total = 0

    words = inp.render.words
    # A sentence whose caption span straddles a cut pause was still said.
    # Every pause cut counts, not only the removed ranges that are pure
    # silence: a pause at the end of a kept sentence merges with the removed
    # sentence after it into one removed range.
    silence = inp.render.edl.silence_cuts + [(r.start, r.end) for r in inp.render.removed if r.silence]
    clean_words = remap_transcript(words, manifest=out.cleaned, silence=silence)
    # what is said in the reel near the start of final.mp4 (its own timeline
    # = final's, shifted by the intro)
    reel_words = []
    if out.highlights is not None:
        reel_silence = inp.render.reel_edl.silence_cuts if inp.render.reel_edl is not None else []
        reel_words = remap_transcript(words, manifest=out.highlights, silence=reel_silence)
    _write_json(job_dir / "clean_transcript.json", [w.model_dump() for w in clean_words])
    job.clean_transcript_path = str(job_dir / "clean_transcript.json")

    with _stage(job, "reporting"):
        set_status(JobStatus.REPORTING)
        meeting = inp.render.title  # the same name as on the title card ("" = none known)
        source_s = inp.render.media.video_duration
        entries = removed_entries(inp.render.removed, words, out.removed, silence=silence)
        write_removed_transcript(job_dir / "transcript_removed.txt", job_dir / "transcript_removed.json", entries,
                                 meeting=meeting, source_duration_s=source_s)
        write_clean_transcript(job_dir / "transcript_clean.txt", job_dir / "transcript_clean.json",
                               clean_lines(clean_words, out.final_offset_s, reel_words, reel_offset_s=out.intro_s),
                               meeting=meeting, offset_s=out.final_offset_s, final_duration_s=out.final_duration_s,
                               intro_s=out.intro_s, outro_s=out.outro_s, final_name=final_name)
        for name in ("transcript_clean.txt", "transcript_clean.json", "transcript_removed.txt",
                     "transcript_removed.json"):
            job.artifacts[name] = ArtifactInfo(path=name, kind="json" if name.endswith(".json") else "text",
                                               mandatory=True)

        chapters_text, chapter_list = _chapters(job, job_dir, inp.chapter_fn, clean_words, out)
        topics = _topic_cuts(job, inp, out, chapter_list, clean_words, update)
        highlights = _reel_rows(out.highlights, inp.reel, offset_s=out.intro_s)
        shorts_rows = [(r.path.relative_to(job_dir).as_posix(), r.clip.start, r.clip.end, r.clip.title, r.clip.hook)
                       for r in out.shorts]
        if out.shorts:
            (job_dir / "shorts").mkdir(exist_ok=True)
            _write_json(job_dir / "shorts" / "shorts.json", [
                {"file": row[0], "source_start": round(row[1], 3), "source_end": round(row[2], 3),
                 "duration_s": round(r.duration_s, 3), "title": r.clip.title, "hook": r.clip.hook,
                 "category": r.clip.category, "captions": row[0].replace(".mp4", ".srt"),
                 "style": r.style, "layout": r.layout_kind, "gain_db": r.gain_db,
                 "screen_hook_shown": r.hook_text if r.style == "new" else "",
                 "trimmed_s": r.trimmed_s}
                for row, r in zip(shorts_rows, out.shorts, strict=True)])
            job.artifacts["shorts/shorts.json"] = ArtifactInfo(path="shorts/shorts.json", kind="json")

        _thumbnails(job, job_dir, inp.render.source, out, highlights, shorts_rows, reel=inp.reel)
        _topic_thumbnails(job, job_dir, inp.render.source, topics)

        if settings.report_enabled:
            try:
                fill_file_info(job_dir, job.artifacts, hashes=False)  # sizes for the report
                build_report(job_dir / "report.pdf", ReportData(
                    meeting=meeting,
                    job_id=job.job_id,
                    version=PIPELINE_VERSION,
                    created=(job.created_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC"),
                    source_name=Path(inp.render.source).name,
                    source_duration_s=source_s,
                    final_duration_s=out.final_duration_s,
                    cleaned_duration_s=out.cleaned.measured_duration_s or 0.0,
                    highlights_duration_s=_duration(out.highlights),
                    card_s=out.card_s,
                    intro_name=out.intro_name,
                    intro_s=out.intro_s,
                    outro_name=out.outro_name,
                    outro_s=out.outro_s,
                    removed=entries,
                    final_name=final_name,
                    merged_back_count=inp.render.edl.merged_gap_count,
                    merged_back_s=inp.render.edl.merged_gap_seconds,
                    highlights=highlights,
                    shorts=shorts_rows,
                    chapters=chapters_text,
                    # by the name in the zip (the final video's key "final.mp4" is not its file name)
                    artifacts=[(a.path, a.status, a.duration_s, a.bytes, a.reason)
                               for a in _ordered(job.artifacts).values()],
                    warnings=job.warnings,
                    llm_usage=job.llm_usage,
                    llm_model=_llm_model(),
                    llm_cost_usd=estimate_cost_usd(job.llm_usage, _llm_model()),
                    stage_timings=job.stage_timings,
                ), font=find_font(), bold_font=find_font(bold=True))
                job.artifacts["report.pdf"] = ArtifactInfo(path="report.pdf", kind="pdf")
            except Exception as exc:
                if is_fatal(exc):
                    raise
                logger.exception("report.pdf failed")
                job.artifacts["report.pdf"] = ArtifactInfo(path="report.pdf", kind="pdf", status="failed",
                                                           reason=str(exc)[:300])
                job.warnings.append(f"report.pdf failed: {exc}")

    with _stage(job, "bundling"):
        set_status(JobStatus.BUNDLING)
        job.artifacts = _ordered(job.artifacts)
        fill_file_info(job_dir, job.artifacts)
        needed = sum(a.bytes or 0 for a in job.artifacts.values() if a.status == "ok") + ZIP_HEADROOM_BYTES
        free = shutil.disk_usage(job_dir).free
        if free < needed:
            raise InsufficientDisk(f"only {free / 1024**3:.1f} GiB free, the bundle needs {needed / 1024**3:.1f} GiB")
        manifest = _manifest(job, inp, out, highlights, shorts_rows, entries, topics)
        write_manifest(job_dir / "manifest.json", manifest)
        bundle = write_bundle(job_dir, job.artifacts, job_dir / "bundle.zip", extra=["manifest.json"])
        info = ArtifactInfo(path="manifest.json", kind="json", mandatory=True)
        job.artifacts["manifest.json"] = info
        fill_file_info(job_dir, {"manifest.json": info})
        job.bundle_path = str(bundle)
        job.bundle_bytes = bundle.stat().st_size
        job.bundle_sha256 = sha256_file(bundle)

    _cleanup(job, job_dir, Path(inp.render.source))
    job.status = JobStatus.DONE
    job.progress_current = job.progress_total = 0
    update(job)


# ------------------------------------------------------------------ helpers


def _chapters(job: JobRecord, job_dir: Path, chapter_fn: ChapterFn | None, clean_words: list[Word],
              out: RenderOutputs) -> tuple[str, list]:
    """Topic chapters on the cleaned meeting, placed on final.mp4 (after the
    intro and the reel: shifted, with "00:00 Highlights" first when there is
    a reel; the intro belongs to the first chapter). Optional. Returns the text and the chapters
    themselves (cleaned timeline) for the topic cuts."""
    if chapter_fn is None:
        return "", []
    cleaned_s = out.cleaned.measured_duration_s or 0.0
    try:
        result = chapter_fn(clean_words, cleaned_s)
    except Exception as exc:
        if is_fatal(exc):
            raise
        job.warnings.append(f"chapters skipped: {exc}")
        job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text", status="failed",
                                                     reason=str(exc)[:300])
        return "", []
    if not result.ok:
        # the problems are phrased for the model's retry ("... — split it where
        # the topic changes"); the owner only needs the part before the dash
        reason = "; ".join(p.split(" — ")[0] for p in result.problems)
        job.warnings.append("chapters skipped: " + reason)
        job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text", status="skipped",
                                                     reason=reason[:300])
        return "", []
    entries, problems = finalize_chapters(result.chapters, final_duration_s=out.final_duration_s,
                                          reel_s=out.reel_s, intro_s=out.intro_s)
    if problems:
        job.warnings.append("chapters skipped: " + "; ".join(problems))
        job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text", status="skipped",
                                                     reason="; ".join(problems)[:300])
        return "", []
    text = format_chapters(entries, out.final_duration_s)
    _write_json(job_dir / "chapters.json", {
        "timeline": "final",
        "cleaned_starts_at_s": round(out.final_offset_s, 3),
        "intro_s": round(out.intro_s, 3),
        "chapters_cleaned_timeline": [c.model_dump() for c in result.chapters],
        "entries": [{"t": t, "title": title} for t, title in entries],
    })
    (job_dir / "chapters.txt").write_text(text, encoding="utf-8")
    job.chapters_path = str(job_dir / "chapters.txt")
    job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text")
    return text, list(result.chapters)


def _reel_rows(manifest: RenderManifest | None, reel: list[Moment],
               offset_s: float = 0.0) -> list[tuple[float, float, float, str]]:
    """(time in final.mp4, source start, source end, title) per reel clip. The
    reel starts at `offset_s` in final.mp4 (right after the intro, if any).
    Pieces of one moment split by cut pauses are one clip."""
    if manifest is None:
        return []
    fps = parse_rate(manifest.fps)
    rows: list[tuple[float, float, float, str]] = []
    last_id = None
    for p in manifest.pieces:
        s, e = float(Fraction(p.src_start_frame) / fps), float(Fraction(p.src_end_frame) / fps)
        best = max(reel, key=lambda m: min(e, m.end) - max(s, m.start), default=None)
        if best is None or min(e, best.end) <= max(s, best.start):
            best = None
        if best is not None and rows and best.id == last_id:
            rows[-1] = (*rows[-1][:2], e, rows[-1][3])
            continue
        title = (best.title or best.category.replace("_", " ")) if best is not None else ""
        rows.append((offset_s + float(Fraction(p.out_start_frame) / fps), s, e, title))
        last_id = best.id if best is not None else None
    return rows


def _duration(manifest: RenderManifest | None) -> float:
    if manifest is None:
        return 0.0
    return float(Fraction(manifest.expected_frames) / parse_rate(manifest.fps))


_ORDER = ("final.mp4", "highlights.mp4", "removed.mp4", "shorts/", "topic/", "report.pdf", "transcript_removed", "transcript_clean",
          "chapters.txt", "cleaned.mp4")


def _ordered(artifacts: dict[str, ArtifactInfo]) -> dict[str, ArtifactInfo]:
    def key(name: str):
        for i, prefix in enumerate(_ORDER):
            if name.startswith(prefix):
                return (i, name)
        return (len(_ORDER), name)

    return {k: artifacts[k] for k in sorted(artifacts, key=key)}


CLIP_TEXT_LIMIT = 2000  # characters


def clip_text(words: list[Word], start: float, end: float) -> str:
    """What is said in [start, end] of the source, plain, at most CLIP_TEXT_LIMIT
    characters. Another part of the app checks a Short's title against it."""
    said = " ".join(w.word.strip() for w in words if start - 0.05 <= (w.start + w.end) / 2 <= end + 0.05)
    return " ".join(said.split())[:CLIP_TEXT_LIMIT]


def shorts_manifest_rows(shorts_rows, clips, words: list[Word], results=None) -> list[dict]:
    """The "shorts" rows of manifest.json: the file and times, the title and
    hook, the on-screen hook and stands-alone score (null when the re-rank gave
    none) and the clip's own words ("text"). `results` (the ShortResults, same
    order) adds how each Short was rendered: style, layout, gain_db and
    screen_hook_shown (the hook text on the card, or ''); a manifest without
    "style" is from before the new look and means 'classic'."""
    rows = [{"file": f, "source_start": round(s, 3), "source_end": round(e, 3), "title": t, "hook": h,
             "screen_hook": c.screen_hook, "stands_alone": c.stands_alone, "text": clip_text(words, s, e)}
            for (f, s, e, t, h), c in zip(shorts_rows, clips, strict=True)]
    for row, r in zip(rows, results or [], strict=False):
        row.update(style=r.style, layout=r.layout_kind, gain_db=r.gain_db,
                   screen_hook_shown=r.hook_text if r.style == "new" else "", trimmed_s=r.trimmed_s)
    return rows


def _manifest(job: JobRecord, inp: DeliverInputs, out: RenderOutputs, highlights, shorts_rows, entries,
              topics: list[dict] | None = None) -> dict:
    media = inp.render.media
    removed = inp.render.removed
    return {
        "job_id": job.job_id,
        "title": inp.render.title,
        "pipeline_version": PIPELINE_VERSION,
        "created_at": job.created_at,
        "finished_at": datetime.now(UTC),
        "source": {"name": Path(inp.render.source).name, "duration_s": round(media.video_duration, 3),
                   "fps": f"{media.fps.numerator}/{media.fps.denominator}", "width": media.width,
                   "height": media.height, "transcript": job.transcript_source},
        # in order: intro + highlights + title card + cleaned meeting + outro = duration
        "final": {"file": job.artifacts["final.mp4"].path,
                  "duration_s": round(out.final_duration_s, 3), "frames": out.final_frames,
                  "cleaned_starts_at_s": round(out.final_offset_s, 3),
                  "intro_file": out.intro_name or None, "intro_s": round(out.intro_s, 3),
                  "highlights_s": round(_duration(out.highlights), 3), "title_card_s": round(out.card_s, 3),
                  "cleaned_s": round(out.cleaned.measured_duration_s or 0.0, 3),
                  "outro_file": out.outro_name or None, "outro_s": round(out.outro_s, 3)},
        "artifacts": {name: a.model_dump(exclude={"on_disk"}) for name, a in job.artifacts.items()},
        "highlights": [{"final_at_s": round(a, 3), "source_start": round(s, 3), "source_end": round(e, 3),
                        "title": t} for a, s, e, t in highlights],
        "shorts": shorts_manifest_rows(shorts_rows, [r.clip for r in out.shorts], inp.render.words, out.shorts),
        "topics": topics or [],
        "render_look": out.render_look,
        "loudness_measured_lufs": out.loudness_measured_lufs,
        "loudness_gain_db": out.loudness_gain_db,
        "full_hd": out.full_hd,
        "removed": {
            "cuts": len(removed),
            "seconds": round(sum(r.end - r.start for r in removed), 3),
            "in_removed_video": sum(1 for e in entries if e.removed_video_at is not None),
            "transcript_only": sum(1 for e in entries if e.removed_video_at is None),
            "silence_cuts": sum(1 for r in removed if r.silence),
            "silence_s": round(sum(r.end - r.start for r in removed if r.silence), 3),
            "merged_back_count": inp.render.edl.merged_gap_count,
            "merged_back_s": round(inp.render.edl.merged_gap_seconds, 3),
        },
        "warnings": job.warnings,
        "stage_timings": job.stage_timings,
        "llm_usage": job.llm_usage,
        "llm_model": _llm_model(),
        "llm_cost_usd": estimate_cost_usd(job.llm_usage, _llm_model()),
    }


def _llm_model() -> str:
    """The configured AI model (the one this job's calls went to, unless the
    setting changed between deciding and rendering a decide_only job)."""
    from decide.gemini_client import llm_model

    return llm_model(get_settings())


def _cleanup(job: JobRecord, job_dir: Path, source: Path) -> None:
    """Disk hygiene after the zip (plan §6.5): on EC2 only the zip, the
    manifest and the job's own records stay; a source recording inside the
    job folder goes when the job is done."""
    settings = get_settings()
    if not settings.serve_individual_artifacts:
        for name, info in job.artifacts.items():
            if name == "manifest.json" or name.startswith("thumbnails/") or info.status != "ok":
                continue
            (job_dir / info.path).unlink(missing_ok=True)
            info.on_disk = False
        shutil.rmtree(job_dir / "shorts", ignore_errors=True)
    if settings.delete_source_when_done:
        for path in (source, Path(job.native_transcript_path) if job.native_transcript_path else None):
            if path is not None and _inside(path, job_dir):
                path.unlink(missing_ok=True)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


@contextmanager
def _stage(job: JobRecord, name: str):
    t0 = time.monotonic()
    try:
        yield
    finally:
        job.stage_timings[name] = round(time.monotonic() - t0, 2)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- thumbnails
def _thumbnails(job: JobRecord, job_dir: Path, source, out: RenderOutputs, highlights, shorts_rows,
                reel: list[Moment] | None = None) -> None:
    """Clean base frames (no text) for the full video's thumbnail candidates and
    each Short, cut while the source is still on disk; the upload-time step puts
    the words on them. Never fails the job."""
    from publish import thumbframes as tf
    from publish.settings import current as publishing

    try:
        # a re-render must never leave the previous run's frames behind
        shutil.rmtree(job_dir / tf.THUMBS_DIR, ignore_errors=True)
        cfg = publishing()
        if cfg.thumbnail_mode == "off" and not cfg.thumbnail_shorts:
            return
        with _stage(job, "thumbnails"):
            rows = _thumbnail_rows(job_dir, Path(source), out, highlights, shorts_rows, reel or [], cfg, tf)
            folder = job_dir / tf.THUMBS_DIR
            folder.mkdir(exist_ok=True)
            _write_json(folder / tf.THUMBS_FILE, rows)
            names = [f"{tf.THUMBS_DIR}/{tf.THUMBS_FILE}"] + [r["file"] for r in rows if r["file"]]
            for name in names:
                job.artifacts[name] = ArtifactInfo(path=name, kind="json" if name.endswith(".json") else "image")
    except Exception as exc:
        if is_fatal(exc):
            raise
        logger.exception("thumbnails failed")
        job.warnings.append(f"thumbnails skipped: {str(exc)[:200]}")


def _thumbnail_rows(job_dir: Path, source: Path, out: RenderOutputs, highlights, shorts_rows,
                    reel: list[Moment], cfg, tf) -> list[dict]:
    import re

    from slice.profile import probe_header

    header = probe_header(source)
    src_w, src_h = header.width, header.height
    result: list[dict] = []
    keyframes = None
    if cfg.thumbnail_mode != "off":
        keyframes = tf.score_keyframes(source)
        # moments: the 3 best highlights by score, then every Short's range
        def score_of(start: float, end: float) -> float:
            best = max(reel, key=lambda m: min(end, m.end) - max(start, m.start), default=None)
            if best is None or min(end, best.end) <= max(start, best.start):
                return 0.0
            return best.score

        moments = [(r[1], r[2]) for r in sorted(highlights, key=lambda r: -score_of(r[1], r[2]))[:3]]
        moments += [(r[1], r[2]) for r in shorts_rows]
        picks = []
        for start, end in moments:
            p = tf.pick(keyframes, start, end, faces=cfg.thumbnail_faces, src_w=src_w, src_h=src_h)
            if p.kind == tf.KIND_CARD or p.t is None:
                continue
            if any(abs(p.t - q[0].t) < 5.0 for q in picks):
                continue
            picks.append((p, start, end))
        picks.sort(key=lambda x: -x[0].score)
        picks = picks[:3]
        if not picks:
            start = min((m[0] for m in moments), default=0.0)
            end = max((m[1] for m in moments), default=start)
            result.append(tf.row(tf.VIDEO_FULL, 1, start, end, None, tf.KIND_CARD))
        for n, (p, start, end) in enumerate(picks, 1):
            name = f"{tf.THUMBS_DIR}/base_full_{n}.jpg"
            tf.extract(source, p.t, p.crop, job_dir / name)
            result.append(tf.row(tf.VIDEO_FULL, n, start, end, p.t, p.kind, p.crop, name,
                                 {"score": round(p.score, 2)}))
    shorts_out = {r.path.relative_to(job_dir).as_posix(): r for r in out.shorts}
    for row in shorts_rows:
        res = shorts_out.get(row[0])
        if res is None:
            continue
        m = re.search(r"short_(\d+)\.mp4$", row[0])
        number = int(m.group(1)) if m else len(result)
        style = getattr(res, "style", "classic")
        look = getattr(res, "look", None)
        if style == "new":
            hook_s = look.get("hook_seconds") if isinstance(look, dict) else getattr(look, "hook_seconds", None)
            if hook_s is None:
                hook_s = getattr(cfg, "shorts_hook_seconds", 2.5)
            t = float(hook_s) + 0.5
        else:
            t = 1.0
        t = min(t, max(res.duration_s - 0.2, 0.0))
        layout_kind = getattr(res, "layout_kind", "screen") or "screen"
        kind = tf.KIND_SHARE if layout_kind == "screen" else tf.KIND_CAMERA
        scores = {"in_short_s": round(t, 3), "style": style, "layout_kind": layout_kind}
        if kind == tf.KIND_CAMERA and not cfg.thumbnail_faces:
            # camera tiles would show faces: leave a card row, no base frame
            result.append(tf.row(tf.VIDEO_SHORT, number, row[1], row[2], None, tf.KIND_CARD,
                                 None, None, scores))
            continue
        name = f"{tf.THUMBS_DIR}/base_short_{number:02d}.jpg"
        tf.extract_vertical(res.path, t, job_dir / name)
        result.append(tf.row(tf.VIDEO_SHORT, number, row[1], row[2], t, kind,
                             None, name, scores))
    return result


# ---------------------------------------------------------------- topic cuts
TOPIC_MBPS = 2.5  # what a topic cut's file costs per second, with room to spare (see _one_topic_cut)


def _topic_name(index: int) -> str:
    return f"topic/topic_{index:02d}.mp4"


def _topic_cuts(job: JobRecord, inp: DeliverInputs, out: RenderOutputs, chapters: list, clean_words: list[Word],
                update: Callable[[JobRecord], None] | None = None) -> list[dict]:
    """Renders the topic cuts of a public team's meeting (topic/topic_NN.mp4) and
    returns their rows for manifest.json. Runs in its own stage, "render_topic".
    A cut that cannot be made (an update is waiting to restart the app, the disk
    is short, the render fails) is skipped or marked failed with a warning: this
    never fails the job."""
    from decide.topics import pick_cuts
    from publish import series
    from publish.settings import current as publishing

    try:
        cfg = publishing()
        if not cfg.topic_cuts or cfg.topic_cuts_per_meeting < 1 or not inp.moments:
            return []
        if series.tier_for_job(job) != "public":
            return []
        with _stage(job, "render_topic"):
            cuts = pick_cuts(out.cleaned, chapters, inp.moments, cfg, segments=inp.segments)
            rows = [_one_topic_cut(job, inp, out, cut, chapters, update) for cut in cuts]
            return [row for row in rows if row is not None]
    except Exception as exc:
        if is_fatal(exc):
            raise
        logger.exception("topic cuts failed")
        job.warnings.append(f"topic cuts skipped: {str(exc)[:200]}")
        return []


def _card_title(job: JobRecord, cut, text: str) -> str:
    """The words drawn on a topic cut's title card, picked with the rules of its YouTube title (so the
    picture never says what the title may not): the best moment's title, else the cut's first chapter
    title; each must be about the cut's own words, name no speaker and hold no never-post word. ""
    (a card with only the team name) when none fits."""
    from autopilot import never_post
    from publish import describe

    names = describe._speaker_names(job)
    room = describe.TITLE_LIMIT - len(describe.series_label(job)) - 3
    for raw in (cut.title, cut.chapter_titles[0] if cut.chapter_titles else ""):
        title = describe.one_line(raw, room)
        if (title and describe._grounded(title, text) and not describe._names_someone(title, names)
                and never_post.match_words(title) is None):
            return title
    return ""


def _one_topic_cut(job: JobRecord, inp: DeliverInputs, out: RenderOutputs, cut, chapters: list,
                   update: Callable[[JobRecord], None] | None) -> dict | None:
    from autopilot import engine
    from outputs import render_topic_cut
    from publish import describe

    settings = get_settings()
    job_dir = inp.render.job_dir
    number = cut.index + 1
    name = _topic_name(number)
    cut_s = cut.cleaned_end_s - cut.cleaned_start_s

    def skip(reason: str) -> None:
        logger.warning("topic cut %d skipped: %s", number, reason)
        job.warnings.append(f"topic cut {number} skipped: {reason}")
        job.artifacts[name] = ArtifactInfo(path=name, status="skipped", reason=reason[:300])

    if engine.deploying():
        skip("an update is waiting to restart the app, so a long render would be cut off")
        return None
    needed = int(cut_s * TOPIC_MBPS * 1e6 / 8 * 1.3) + settings.min_free_disk_bytes
    free = shutil.disk_usage(job_dir).free
    if free < needed:
        skip(f"only {free / 1024**3:.1f} GiB free, a {cut_s / 60:.0f} minute cut needs {needed / 1024**3:.1f} GiB")
        return None

    def on_progress(current: int, total: int) -> None:
        job.progress_current, job.progress_total = current, total
        if update is not None:
            update(job)

    target = job_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    work = job_dir / "tmp" / "topic"
    fps = parse_rate(out.cleaned.fps)
    start_s = float(Fraction(cut.src_start_frame) / fps)
    end_s = float(Fraction(cut.src_end_frame) / fps)
    text = clip_text(inp.render.words, start_s, end_s)
    card_title = _card_title(job, cut, text)
    try:
        result = render_topic_cut(
            inp.render, cut, card_title, describe.series_label(job), target, work, find_font(),
            find_font(bold=True), gain_db=out.loudness_gain_db, hd=out.full_hd, on_progress=on_progress)
    except Exception as exc:
        if is_fatal(exc):
            raise
        logger.exception("topic cut %d failed", number)
        target.unlink(missing_ok=True)
        reason = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
        job.artifacts[name] = ArtifactInfo(path=name, status="failed", reason=reason)
        job.warnings.append(f"topic cut {number} failed: {reason}")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
        job.progress_current = job.progress_total = 0
    job.artifacts[name] = ArtifactInfo(path=name, duration_s=round(result.duration_s, 3))
    job.warnings.extend(f"topic cut {number}: {note}" for note in result.notes)
    best = cut.best
    return {
        "file": name,
        "source_start": round(start_s, 3), "source_end": round(end_s, 3),
        "cleaned_start": round(cut.cleaned_start_s, 3), "cleaned_end": round(cut.cleaned_end_s, 3),
        "duration_s": round(result.duration_s, 3),
        "open_s": round(result.open_s, 3), "card_s": round(result.card_s, 3),
        "title": cut.title, "card_title": card_title, "hook": cut.hook, "screen_hook": best.screen_hook,
        # the chapters inside the cut, with times from the start of the topic itself
        # (the video has the cold open and the card first: add open_s + card_s)
        "chapters": [{"t": round(c.start - cut.cleaned_start_s, 3), "title": c.title}
                     for c in sorted(chapters, key=lambda c: c.start)
                     if cut.cleaned_start_s <= c.start < cut.cleaned_end_s],
        "best_score": round(float(cut.score_tuple[0]), 3),
        "stands_alone": best.stands_alone,
        "category": best.category,
        "thumb_src_s": round(cut.thumb_src_s, 3),
        "text": text,
    }


def _topic_thumbnails(job: JobRecord, job_dir: Path, source, topics: list[dict]) -> None:
    """The base frame for each topic cut (thumbnails/base_topic_NN.jpg) and its
    row in thumbs.json; the upload-time step puts the words on it. Never fails
    the job."""
    if not topics:
        return
    import re

    from publish import thumbframes as tf
    from publish.settings import current as publishing
    from slice.profile import probe_header

    try:
        cfg = publishing()
        if cfg.thumbnail_mode == "off":
            return
        with _stage(job, "topic_thumbnails"):
            header = probe_header(Path(source))
            keyframes = tf.score_keyframes(source)
            folder = job_dir / tf.THUMBS_DIR
            folder.mkdir(exist_ok=True)
            path = folder / tf.THUMBS_FILE
            try:
                rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            except (OSError, ValueError):
                rows = []
            rows = [r for r in rows if r.get("video") != tf.VIDEO_TOPIC]
            made = []
            for number, topic in enumerate(topics, 1):
                match = re.search(r"topic_(\d+)\.mp4$", topic["file"])
                number = int(match.group(1)) if match else number
                start, end = topic["source_start"], topic["source_end"]
                p = tf.pick(keyframes, start, end, faces=cfg.thumbnail_faces, peak=topic["thumb_src_s"],
                            src_w=header.width, src_h=header.height)
                if p.kind == tf.KIND_CARD or p.t is None:
                    rows.append(tf.row(tf.VIDEO_TOPIC, number, start, end, None, tf.KIND_CARD))
                    continue
                name = f"{tf.THUMBS_DIR}/base_topic_{number:02d}.jpg"
                tf.extract(source, p.t, p.crop, job_dir / name)
                rows.append(tf.row(tf.VIDEO_TOPIC, number, start, end, p.t, p.kind, p.crop, name,
                                   {"score": round(p.score, 2)}))
                made.append(name)
            _write_json(path, rows)
            for name in [f"{tf.THUMBS_DIR}/{tf.THUMBS_FILE}", *made]:
                job.artifacts[name] = ArtifactInfo(path=name, kind="json" if name.endswith(".json") else "image")
    except Exception as exc:
        if is_fatal(exc):
            raise
        logger.exception("topic thumbnails failed")
        job.warnings.append(f"topic thumbnails skipped: {str(exc)[:200]}")
