"""Translated titles and descriptions: the generator, the checks, the cache and the cost record."""

import json
import time
from types import SimpleNamespace

import pytest

from core.config import get_settings
from costs import db
from costs.record import record_call
from decide.gemini_client import LLMUsage
from publish import translate
from publish.describe import SHORTS_HEADING

TITLE = "Sensor demo with ESP32 boards"
LINKS = "Docs: https://example.com/docs\nContact: team@example.com"
SHORTS = f"{SHORTS_HEADING}\n• Quick tip — https://youtu.be/abc123"
DESC = (
    "A demo of 3 sensors from FloLabs Alpha.\n\n"
    "0:00 Intro\n"
    "2:30 Wiring the ESP32\n"
    "1:02:03 Results\n\n"
    f"{LINKS}\n\n"
    "#FloLabs #Alpha\n\n"
    f"{SHORTS}"
)
SERIES = "Alpha"


def _good(lang):
    return {
        "title": f"[{lang}] Sensor demo with ESP32 boards",
        "description": (
            f"[{lang}] A demo of 3 sensors from FloLabs Alpha.\n\n"
            f"0:00 [{lang}] Intro\n2:30 [{lang}] Wiring the ESP32\n1:02:03 [{lang}] Results\n\n"
            "[[KEEP-2]]\n\n#FloLabs #Alpha\n\n[[KEEP-1]]"),
    }


class FakeCaller:
    model = "fake-model"

    def __init__(self, answer=None, error=None, delay=0.0):
        self.answer, self.error, self.delay = answer, error, delay
        self.usage = LLMUsage()
        self.seen = []

    def generate(self, system, contents, schema=None):
        self.seen.append(json.loads(contents))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        self.usage.calls += 1
        self.usage.prompt_tokens += 1000
        self.usage.output_tokens += 500
        return SimpleNamespace(text=json.dumps(self.answer))


def _run(answer, langs=("de", "hi"), **kw):
    caller = FakeCaller(answer)
    out = translate.localizations(TITLE, DESC, list(langs), cache_key="k1", caller=caller, links_text=LINKS,
                                  series=SERIES, **kw)
    return out, caller


def test_good_answer_passes_and_blocks_are_verbatim():
    out, caller = _run({"de": _good("de"), "hi": _good("hi")})
    assert set(out) == {"de", "hi"}
    for lang in out:
        text = out[lang]["description"]
        assert LINKS in text and SHORTS in text and "[[KEEP" not in text
        assert text.endswith(SHORTS)
    sent = caller.seen[0]
    assert "example.com" not in sent["description"] and "youtu.be" not in sent["description"]
    assert "ESP32" in sent["KEEP"] and "FloLabs" in sent["KEEP"] and "Alpha" in sent["KEEP"]


def _bad(lang, **change):
    entry = _good(lang)
    for key, fn in change.items():
        entry[key] = fn(entry[key])
    return entry


@pytest.mark.parametrize("name,change", [
    ("number", {"description": lambda d: d.replace("3 sensors", "sensors")}),
    ("chapter time", {"description": lambda d: d.replace("2:30", "2:31")}),
    ("first chapter", {"description": lambda d: d.replace("0:00", "0:01")}),
    ("chapter count", {"description": lambda d: d.replace("1:02:03 [de] Results\n", "")}),
    ("changed url", {"description": lambda d: d.replace("[[KEEP-1]]", "[[KEEP-1]]\nhttps://evil.example/x")}),
    ("flolabs", {"description": lambda d: d.replace("FloLabs", "FloLabor")}),
    ("tool name", {"description": lambda d: d.replace("ESP32", "ESP 32")}),
    ("angle", {"title": lambda t: t + " <b>"}),
    ("long title", {"title": lambda t: "x" * 101}),
    ("hashtag", {"description": lambda d: d.replace("#Alpha", "#Alfa")}),
])
def test_a_failing_language_is_dropped_alone(name, change):
    out, _ = _run({"de": _bad("de", **change), "hi": _good("hi")})
    assert set(out) == {"hi"}, name


def test_added_url_in_text_drops_language():
    entry = _good("de")
    entry["description"] = entry["description"].replace("Intro", "Intro see https://x.example/a")
    out, _ = _run({"de": entry, "hi": _good("hi")})
    assert set(out) == {"hi"}


@pytest.mark.parametrize("added", [
    "see www.evil.example", "see evil.example/x", "see HTTPS://evil.example/x", "see Evil.Example",
    "see www.evil", "see bücher.example",
])
def test_scheme_less_and_uppercase_links_drop_language(added):
    entry = _good("de")
    entry["description"] = entry["description"].replace("Intro", "Intro " + added)
    out, _ = _run({"de": entry, "hi": _good("hi")})
    assert set(out) == {"hi"}, added


def test_a_domain_already_in_the_source_is_fine():
    src = "Docs at www.example.com and Node.js"
    ok = {"title": "t", "description": "Doku unter WWW.example.com und Node.js"}
    assert translate.check("T", src, ok, "de")


def test_late_answer_is_cached_and_cost_recorded():
    slow = FakeCaller({"de": _good("de")}, delay=0.3)
    out = translate.localizations(TITLE, DESC, ["de"], cache_key="late", caller=slow, links_text=LINKS,
                                  series=SERIES, timeout_s=0.05)
    assert out == {}
    deadline = time.time() + 5
    while time.time() < deadline and not list((get_settings().storage_dir / "state" / "translations").glob("*.json")):
        time.sleep(0.05)
    again = FakeCaller(error=RuntimeError("must not be called"))
    out2 = translate.localizations(TITLE, DESC, ["de"], cache_key="late", caller=again, links_text=LINKS,
                                   series=SERIES)
    assert "de" in out2 and again.seen == []
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE key='ai:translate:late'").fetchone()[0] == 1


def test_cost_is_recorded_when_the_answer_is_bad_json():
    caller = FakeCaller({"de": _good("de")})
    real = caller.generate

    def bad(*a, **k):
        real(*a, **k)
        return SimpleNamespace(text="not json")
    caller.generate = bad
    assert translate.localizations(TITLE, DESC, ["de"], cache_key="badjson", caller=caller) == {}
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE key='ai:translate:badjson'").fetchone()[0] == 1


def test_missing_placeholder_drops_language():
    entry = _good("de")
    entry["description"] = entry["description"].replace("[[KEEP-2]]", "")
    out, _ = _run({"de": entry, "hi": _good("hi")})
    assert set(out) == {"hi"}


def test_hindi_description_over_5000_bytes_is_dropped():
    entry = _good("hi")
    entry["description"] = "अ" * 1800 + "\n" + entry["description"]  # 3 bytes each
    out, _ = _run({"de": _good("de"), "hi": entry})
    assert set(out) == {"de"}


def test_check_needs_the_same_room_for_the_shorts_block():
    src_desc = "Hello 1\n0:00 Start"
    ok = {"title": "t", "description": "Hallo 1\n0:00 Start"}
    assert translate.check("T", src_desc, ok, "de")
    big = {"title": "t", "description": "Hallo 1\n0:00 Start\n" + "x" * 4300}
    assert not translate.check("T", src_desc, big, "de")


def test_errors_and_timeouts_give_nothing():
    caller = FakeCaller(error=RuntimeError("boom"))
    assert translate.localizations(TITLE, DESC, ["de"], cache_key="k", caller=caller) == {}
    slow = FakeCaller({"de": _good("de")}, delay=0.5)
    assert translate.localizations(TITLE, DESC, ["de"], cache_key="k", caller=slow, timeout_s=0.05) == {}
    junk = FakeCaller(None)
    junk.generate = lambda *a, **k: SimpleNamespace(text="not json")
    assert translate.localizations(TITLE, DESC, ["de"], cache_key="k", caller=junk) == {}


def test_the_cache_avoids_a_second_call():
    out1, caller = _run({"de": _good("de")}, langs=("de",))
    assert len(caller.seen) == 1 and "de" in out1
    again = FakeCaller(error=RuntimeError("must not be called"))
    out2 = translate.localizations(TITLE, DESC, ["de"], cache_key="k1", caller=again, links_text=LINKS,
                                   series=SERIES)
    assert out2 == out1 and again.seen == []
    assert list((get_settings().storage_dir / "state" / "translations").glob("*.json"))


def test_the_ai_cost_is_recorded_once_per_key():
    _run({"de": _good("de")}, langs=("de",), job_id="j1", meeting="Weekly")
    with db.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM events WHERE key='ai:translate:k1'")]
    assert len(rows) == 1 and rows[0]["calls"] == 1 and rows[0]["input_tokens"] == 1000
    assert rows[0]["job_id"] == "j1" and rows[0]["kind"] == "ai"


def test_record_call_is_idempotent():
    usage = {"calls": 1, "prompt_tokens": 100, "output_tokens": 50}
    record_call("ai:translate:x", usage, job_id="j", meeting="m")
    record_call("ai:translate:x", usage, job_id="j", meeting="m")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE key='ai:translate:x'").fetchone()[0] == 1
    record_call("ai:translate:none", {"calls": 0}, job_id="j", meeting="m")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE key='ai:translate:none'").fetchone()[0] == 0
