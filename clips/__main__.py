# ruff: noqa: BLE001  (the loop must survive any failure in a step, so broad catches are deliberate)
"""Command line for the clips system: python -m clips <command>.

Every command except `init` and `status` first checks that the process runs
against the clips data folder and not the company's. `status` runs anywhere, but
leaves out the YouTube channel (it would read the wrong sign-in token) and shows
`isolated: false` when this process is not on the clips folder.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from typing import Any

from clips.config import clips_settings
from clips.models import Platform, RightsBasis


def _guard() -> Any:
    return importlib.import_module("clips.guard")


def _runner() -> Any:
    """The runner every command uses (tests swap this for one with fake stages)."""
    from clips.runner import Runner

    return Runner()


def _store() -> Any:
    from clips.store import Store

    return Store()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clips", description="Cut, review and post gameplay clips.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="make and mark the clips data folder")
    sub.add_parser("status", help="show counts, slots, switches and recent events")
    sub.add_parser("sources", help="list sources")

    add = sub.add_parser("add-source", help="add a source")
    add.add_argument("name")
    add.add_argument("--platform", required=True, choices=[x.value for x in Platform])
    add.add_argument("--url", default="")
    add.add_argument("--folder", default="")
    add.add_argument("--channel-id", default="")
    add.add_argument("--rights", default=RightsBasis.NONE.value, choices=[x.value for x in RightsBasis])
    add.add_argument("--evidence-url", default="")
    add.add_argument("--evidence-note", default="")
    add.add_argument("--evidence-date", default="")
    add.add_argument("--credit", default="")
    add.add_argument("--enable", action="store_true", help="enable it now (needs a valid rights record)")

    for name in ("enable-source", "disable-source"):
        sub.add_parser(name).add_argument("source_id")

    sub.add_parser("watch", help="look for new videos")
    proc = sub.add_parser("process", help="cut clips from new videos")
    proc.add_argument("--limit", type=int, default=None)
    sub.add_parser("schedule", help="give approved clips a publish time")
    up = sub.add_parser("upload", help="upload clips that are due")
    up.add_argument("--limit", type=int, default=None)
    up.add_argument("--dry-run", action="store_true", help="show what is due and send nothing")
    sub.add_parser("tick", help="run every step once")
    loop = sub.add_parser("loop", help="tick every few minutes until stopped")
    loop.add_argument("--max-ticks", type=int, default=None, help=argparse.SUPPRESS)

    sub.add_parser("approve").add_argument("clip_id")
    sub.add_parser("release", help="put a held clip back in the queue").add_argument("clip_id")
    rej = sub.add_parser("reject")
    rej.add_argument("clip_id")
    rej.add_argument("reason", nargs="*")
    kill = sub.add_parser("kill", help="stop all posting")
    kill.add_argument("reason", nargs="*")
    sub.add_parser("resume", help="turn the kill switch off")
    sub.add_parser("breaker-reset", help="close the upload circuit breaker")
    auto = sub.add_parser("auto-approve", help="approve every Short without a person: on, off or status")
    auto.add_argument("state", choices=["on", "off", "status"])
    return p


def _out(text: str) -> None:
    print(text)


def _cmd_init(args: argparse.Namespace) -> int:
    path = clips_settings().storage_dir
    _guard().init_storage(path)
    _out(f"clips data folder ready: {path}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _out(json.dumps(_runner().status(), indent=2, default=str))
    return 0


def _cmd_sources(args: argparse.Namespace) -> int:
    sources = _store().list_sources()
    if not sources:
        _out("no sources yet")
    for s in sources:
        state = "enabled" if s.enabled else "disabled"
        _out(f"{s.id}\t{s.name}\t{s.platform.value}\t{s.rights.value}\t{state}")
    return 0


def _cmd_add_source(args: argparse.Namespace) -> int:
    from clips.sources import add_source

    source = add_source(
        _store(),
        name=args.name,
        platform=Platform(args.platform),
        rights=RightsBasis(args.rights),
        enabled=args.enable,
        url=args.url,
        folder=args.folder,
        channel_id=args.channel_id,
        evidence_url=args.evidence_url,
        evidence_note=args.evidence_note,
        evidence_date=args.evidence_date,
        credit=args.credit,
    )
    _out(f"added source {source.id} ({'enabled' if source.enabled else 'disabled'})")
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    from clips.sources import enable_source

    _out(f"enabled {enable_source(_store(), args.source_id).id}")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    from clips.sources import disable_source

    _out(f"disabled {disable_source(_store(), args.source_id).id}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    _out(f"{_runner().watch()} new video(s) found")
    return 0


def _cmd_process(args: argparse.Namespace) -> int:
    _out(f"{_runner().process(args.limit)} clip(s) made")
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    _out(f"{_runner().schedule()} clip(s) scheduled")
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    runner = _runner()
    if args.dry_run:
        return _dry_run(runner, args.limit)
    _out(f"{runner.upload_due(args.limit)} clip(s) uploaded")
    return 0


def _dry_run(runner: Any, limit: int | None) -> int:
    """Run each due clip through the uploader's guards with nothing sent."""
    s = runner.settings
    due = runner.due_clips()
    for clip in due[: limit if limit is not None else s.uploads_per_tick]:
        source = runner.store.get_source(clip.source_id)
        try:
            runner.stages.upload_clip(clip, source, runner.store, dry_run=True)
            _out(f"{clip.id}\twould upload for {clip.publish_at.isoformat()}")
        except Exception as exc:
            _out(f"{clip.id}\twould be refused: {exc}")
    _out(f"{len(due)} clip(s) due (dry run, nothing sent)")
    return 0


def _cmd_tick(args: argparse.Namespace) -> int:
    _out(json.dumps(_runner().tick(), default=str))
    return 0


def _cmd_loop(args: argparse.Namespace) -> int:
    runner = _runner()
    minutes = clips_settings().loop_minutes
    ticks = 0
    try:
        while args.max_ticks is None or ticks < args.max_ticks:
            try:
                _out(json.dumps(runner.tick(), default=str))
            except Exception as exc:  # one bad tick never ends the loop
                _out(f"tick failed: {exc}")
            ticks += 1
            if args.max_ticks is not None and ticks >= args.max_ticks:
                break
            _sleep(minutes * 60)
    except KeyboardInterrupt:
        _out("stopped")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    _out(f"approved {_runner().approve(args.clip_id).id}")
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    _out(f"released {_runner().release(args.clip_id).id} (back to approved, it gets a new slot)")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    clip = _runner().reject(args.clip_id, " ".join(args.reason))
    _out(f"rejected {clip.id}")
    return 0


def _cmd_kill(args: argparse.Namespace) -> int:
    report = _guard().set_kill_switch(True, " ".join(args.reason)) or {}
    done, failed = report.get("pulled_back") or [], report.get("failed") or []
    _out("kill switch ON: nothing new will be uploaded")
    if done:
        _out(f"{len(done)} uploaded video(s) that were waiting to go public were set back to private")
    if report.get("skipped"):
        _out(f"WARNING: uploaded videos could NOT be checked or pulled back: {report['skipped']}")
    if failed:
        _out(
            f"WARNING: {len(failed)} uploaded video(s) could NOT be set back to private and will still go public "
            "at their publish time. Set each one to private in YouTube Studio now:"
        )
        for clip_id in failed:
            clip = _safe_clip(clip_id)
            vid = clip.youtube_id if clip else ""
            _out(f"  {clip_id}  https://studio.youtube.com/video/{vid}/edit" if vid else f"  {clip_id}")
    return 0


def _safe_clip(clip_id: str) -> Any:
    try:
        return _store().get_clip(clip_id)
    except Exception:
        return None


def _cmd_resume(args: argparse.Namespace) -> int:
    _guard().set_kill_switch(False)
    _out("kill switch off")
    return 0


def _cmd_breaker_reset(args: argparse.Namespace) -> int:
    _guard().reset_breaker(_store())
    _out("breaker closed")
    return 0


def _cmd_auto_approve(args: argparse.Namespace) -> int:
    runner = _runner()
    if args.state != "status":
        runner.set_auto_approve(args.state == "on")
    on = runner.auto_approve_on()
    _out(f"auto-approve of Shorts is {'ON' if on else 'off'}")
    if on and args.state == "on":
        _out("every Short will be scheduled and posted without anyone looking, warnings included; longer clips still wait")
    return 0


_COMMANDS = {
    "auto-approve": _cmd_auto_approve,
    "init": _cmd_init,
    "status": _cmd_status,
    "sources": _cmd_sources,
    "add-source": _cmd_add_source,
    "enable-source": _cmd_enable,
    "disable-source": _cmd_disable,
    "watch": _cmd_watch,
    "process": _cmd_process,
    "schedule": _cmd_schedule,
    "upload": _cmd_upload,
    "tick": _cmd_tick,
    "loop": _cmd_loop,
    "approve": _cmd_approve,
    "release": _cmd_release,
    "reject": _cmd_reject,
    "kill": _cmd_kill,
    "resume": _cmd_resume,
    "breaker-reset": _cmd_breaker_reset,
}


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits on bad input; report it as an exit code
        return int(exc.code or 0)
    try:
        if args.command not in ("init", "status"):
            _guard().assert_isolated()
        return _COMMANDS[args.command](args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
