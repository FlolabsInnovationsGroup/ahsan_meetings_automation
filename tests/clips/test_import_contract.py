"""Pins the functions of the existing app that clips imports.

Clips never edits the app, so a rename or a changed parameter on main must fail
here loudly after the next merge, not at upload time."""

import importlib
import inspect

import pytest

# (module, name, parameters clips passes, by keyword or position, either works here)
FUNCTIONS = [
    ("core.config", "get_settings", []),
    ("core.proc", "run_checked", ["cmd", "timeout"]),
    ("core.logging", "get_logger", ["name"]),
    ("costs.record", "record_call", ["key", "usage", "meeting"]),
    ("decide.gemini_client", "make_caller", []),
    ("decide.anthropic_client", "request_params", ["model", "system_prompt", "contents", "schema", "max_output_tokens"]),
    ("decide.anthropic_client", "parse_message", ["message"]),
    ("decide.anthropic_client", "_call_anthropic", ["client", "params"]),
    ("ingest.youtube", "ydl_base_opts", []),
    ("transcribe.native", "fetch_youtube_transcript", ["url"]),
    ("slice.profile", "probe_media", ["path"]),
    ("slice.fonts", "find_font", ["bold"]),
    ("slice.fonts", "font_family", ["path"]),
    ("slice.fonts", "filter_escape", ["value"]),
    ("publish.youtube", "start_connect", []),
    ("publish.youtube", "finish_connect", ["state", "code"]),
    ("publish.youtube", "disconnect", []),
    ("publish.youtube", "_load_credentials", []),
    ("publish.youtube", "_count", ["units", "kind"]),
    ("publish.youtube", "friendly_error", ["exc"]),
    ("publish.youtube", "upload_video", ["path", "title", "description", "privacy", "tags", "session_uri",
                                         "start_byte", "on_session", "publish_at", "notify", "category_id", "language"]),
    ("publish.youtube", "session_status", []),
    ("publish.youtube", "find_by_marker", []),
    ("publish.youtube", "read_status", []),
    ("publish.once", "upload_once", ["item", "open_file", "title", "description", "privacy", "by", "publish_at",
                                     "notify", "category_id", "language"]),
    ("ledger.uploads", "get_ledger", []),
]

# classes, constants and attributes clips relies on
NAMES = [
    ("publish.youtube", "YoutubeNotConnectedError"),
    ("publish.youtube", "_pending"),
    ("publish.youtube", "_pending_lock"),
    ("publish.once", "NeverPost"),
    ("ledger.uploads", "Item"),
    ("ledger.uploads", "AlreadyPosted"),
    ("ledger.uploads", "Refused"),
    ("decide.gemini_client", "LLMUsage"),
    ("core.proc", "MediaCommandError"),
]


@pytest.mark.parametrize(("module", "name", "params"), FUNCTIONS)
def test_function_exists_and_takes_what_clips_passes(module, name, params):
    func = getattr(importlib.import_module(module), name)
    assert callable(func), f"{module}.{name} is no longer callable"
    have = inspect.signature(func).parameters
    has_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in have.values())
    missing = [p for p in params if p not in have and not has_kwargs]
    assert not missing, f"{module}.{name} lost the parameter(s) {missing}; clips passes them"


@pytest.mark.parametrize(("module", "name"), NAMES)
def test_name_still_exists(module, name):
    assert hasattr(importlib.import_module(module), name), f"{module}.{name} is gone"


def test_item_still_takes_the_fields_clips_sets():
    from ledger.uploads import Item

    item = Item(key="clips:x", source="clips:x", kind="short", job_id="x", meeting="")
    assert item.key == "clips:x"


def test_settings_still_carry_what_clips_reads():
    from core.config import get_settings

    settings = get_settings()
    for attr in ("ffmpeg_bin", "ffprobe_bin", "storage_dir", "ffmpeg_timeout_seconds", "llm_provider"):
        assert hasattr(settings, attr), f"core.config settings lost {attr}"


def test_the_caller_clips_wraps_still_has_the_attributes_it_reads():
    from decide.gemini_client import LLMUsage

    for attr in ("calls", "output_tokens"):
        assert hasattr(LLMUsage(), attr)
