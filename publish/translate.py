"""Translated titles and descriptions for YouTube's `localizations` field.

The feature is off by default (the `translations` setting): YouTube's
developer rules need the owner's consent. Nothing here uploads anything; the
upload code asks `localizations()` for the languages that passed `check()`.

One AI call per video asks for {lang: {title, description}}. What must never
change is sent as "keep exactly" (URLs, hashtags, chapter times, FloLabs, the
series name, tool names), and the "Shorts from this meeting" block and the
owner's links text are not sent at all: they are swapped for a placeholder
line and copied back verbatim after the translation.

NEVER-POST: the never-post list cannot read other languages, so a translation
is never checked against it. That is why `check()` is strict: any URL added or
changed, any < or >, or any changed protected token drops the language. A
dropped language is not retried. Any error or timeout gives {} (the video
just goes up without translations).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

from .describe import (
    _ANY_NUMBER,
    DESCRIPTION_LIMIT,
    SHORTS_HEADING,
    SHORTS_ROOM,
    TITLE_LIMIT,
)

logger = get_logger(__name__)

_URL = re.compile(r"https?://[\x21-\x7e]+", re.IGNORECASE)
# anything YouTube could turn into a link without a scheme: www.x, or a name.tld
_DOMAIN = re.compile(r"\b(?:www\.[\w-]+|[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,})", re.IGNORECASE)
_URL_TAIL = ".,;:!?)]}\"'"
_HASHTAG = re.compile(r"#[^\W_][\w]*")
_CHAPTER = re.compile(r"^[ \t]*(\d{1,2}:\d{2}(?::\d{2})?)[ \t]+\S", re.MULTILINE)
_PLACEHOLDER = "[[KEEP-{}]]"
_PLACEHOLDER_RE = re.compile(r"\[\[KEEP-\d+\]\]")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


# ------------------------------------------------------------------ checks


def _urls(text: str) -> list[str]:
    return sorted(u.rstrip(_URL_TAIL) for u in _URL.findall(text))


def _domains(text: str) -> set[str]:
    return {d.lower() for d in _DOMAIN.findall(text)}


def _chapter_times(text: str) -> list[str]:
    return _CHAPTER.findall(text)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").replace(".", "") for n in _ANY_NUMBER.findall(text)}


def _tool_tokens(text: str) -> set[str]:
    """Tool-like tokens, found the way describe._topic_tag finds them: letters
    mixed with digits, or CamelCase."""
    found = set()
    for word in re.findall(r"[A-Za-z0-9]+", _URL.sub(" ", _PLACEHOLDER_RE.sub(" ", text))):
        has_digit = any(c.isdigit() for c in word) and any(c.isalpha() for c in word)
        camel = re.fullmatch(r"[A-Z]?[a-z]+[A-Z][A-Za-z0-9]*", word) is not None
        if len(word) >= 3 and (has_digit or camel):
            found.add(word)
    return found


def protected_tokens(text: str, series: str = "") -> set[str]:
    tokens = _tool_tokens(text) | set(_HASHTAG.findall(text))
    if "FloLabs" in text:
        tokens.add("FloLabs")
    if series.strip():
        tokens.add(series.strip())
    return tokens


def check(src_title: str, src_desc: str, out: dict, lang: str = "", *, series: str = "") -> bool:
    """True when the translation `out` ({'title', 'description'}) is safe to
    send to YouTube. `series` is the series name to keep exactly, if any."""
    title, desc = (out or {}).get("title"), (out or {}).get("description")
    if not isinstance(title, str) or not isinstance(desc, str):
        return False
    if not title.strip() or len(title) > TITLE_LIMIT or "\n" in title or "<" in title or ">" in title:
        return False
    if src_desc.strip() and not desc.strip():
        return False
    if "<" in desc or ">" in desc:
        return False
    size = len(desc.encode("utf-8"))
    src_room = DESCRIPTION_LIMIT - len(src_desc.encode("utf-8"))
    if size > DESCRIPTION_LIMIT or DESCRIPTION_LIMIT - size < min(SHORTS_ROOM, src_room):
        return False
    both = f"{title}\n{desc}"
    if not _numbers(f"{src_title}\n{src_desc}") <= _numbers(both):
        return False
    src_times = _chapter_times(src_desc)
    out_times = _chapter_times(desc)
    if out_times != src_times or (src_times and out_times[0] != "0:00"):
        return False
    if _urls(f"{src_title}\n{src_desc}") != _urls(both):
        return False
    # no link-like text (www.x, name.tld, any case, any script) that the source did not have
    if not _domains(both) <= _domains(f"{src_title}\n{src_desc}"):
        return False
    # the title's tokens must stay in the title, the description's in the description
    return (all(tok in title for tok in protected_tokens(src_title, series) if tok in src_title)
            and all(tok in desc for tok in protected_tokens(src_desc, series) if tok in src_desc))


# ------------------------------------------------------------------ the AI call


_SYSTEM = (
    "You translate a YouTube video's title and description into the languages asked for. Answer with one JSON "
    'object: {"<language code>": {"title": "...", "description": "..."}}, one entry per language, nothing else. '
    "Rules: keep every line in the same order and keep every line break. Keep exactly as written, never "
    "translated or changed: every URL, every hashtag, every time at the start of a line (like 0:00 or 12:34 or "
    "1:02:03; only the text after it is translated), the word FloLabs, the series name, and every tool or product "
    "name in the KEEP list. Keep every number, written with ASCII digits 0-9. Lines like [[KEEP-1]] stay exactly "
    "as they are, on their own line. Never add a link, never use the characters < or >, never add or drop a "
    "line. Keep the title under 100 characters."
)


def _split(description: str, links_text: str) -> tuple[str, list[str]]:
    """The description with the Shorts block and the links text swapped for
    placeholder lines, and the verbatim blocks those lines stand for."""
    kept: list[str] = []
    text = description
    block_at = text.find(SHORTS_HEADING)
    if block_at >= 0:
        kept.append(text[block_at:])
        text = text[:block_at] + _PLACEHOLDER.format(len(kept))
    links = (links_text or "").strip()
    if links and links in text:
        kept.append(links)
        text = text.replace(links, _PLACEHOLDER.format(len(kept)), 1)
    return text, kept


def _restore(text: str, kept: list[str]) -> str | None:
    for number, block in enumerate(kept, 1):
        marker = _PLACEHOLDER.format(number)
        if text.count(marker) != 1:
            return None
        text = text.replace(marker, block)
    return text


def _schema(langs: list[str]) -> dict:
    entry = {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
             "required": ["title", "description"], "additionalProperties": False}
    return {"type": "object", "properties": {lang: entry for lang in langs}, "required": list(langs),
            "additionalProperties": False}


def _cache_path(title: str, description: str, langs: list[str], model: str) -> Path:
    digest = hashlib.sha256(json.dumps([title, description, list(langs), model], ensure_ascii=False)
                            .encode("utf-8")).hexdigest()
    return get_settings().storage_dir / "state" / "translations" / f"{digest}.json"


def _read_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("translations: could not write the cache %s", path.name)


def _usage_now(caller) -> dict:
    usage = getattr(caller, "usage", None)
    return dict(usage.as_dict()) if hasattr(usage, "as_dict") else {}


def _usage_since(before: dict, after: dict) -> dict:
    return {k: (after.get(k) or 0) - (before.get(k) or 0)
            for k in ("calls", "prompt_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}


def _parse(text: str) -> dict:
    data = json.loads(_FENCE.sub("", (text or "").strip()))
    if not isinstance(data, dict):
        raise TypeError("the answer is not a JSON object")
    return data


def localizations(title: str, description: str, langs: list[str], *, cache_key: str = "", caller=None,
                  timeout_s: float = 90, links_text: str = "", series: str = "", job_id: str = "",
                  meeting: str = "") -> dict[str, dict[str, str]]:
    """{lang: {'title', 'description'}} for the languages that passed check().
    Never raises: any error or timeout gives {}. `cache_key` names the cost
    row ("ai:translate:<cache_key>"); the answer itself is cached by content so
    a retry or resume never pays twice."""
    langs = [lang for lang in dict.fromkeys(langs or []) if isinstance(lang, str) and lang.strip()]
    if not langs or not (title or "").strip():
        return {}
    try:
        from decide.gemini_client import llm_model, make_caller

        model = getattr(caller, "model", None) or llm_model(get_settings())
        path = _cache_path(title, description, langs, str(model))
        cached = _read_cache(path)
        if cached is not None:
            return {k: v for k, v in cached.items() if k in langs and isinstance(v, dict)}
        caller = caller or make_caller()
        body, kept = _split(description, links_text)
        keep = sorted(protected_tokens(f"{title}\n{body}", series))
        payload = json.dumps({"languages": langs, "KEEP": keep, "title": title, "description": body},
                             ensure_ascii=False)
        def work() -> dict[str, dict[str, str]]:
            # Runs in the worker thread and finishes on its own even when the
            # caller has stopped waiting: the cost is recorded (also on a bad
            # answer) and the result is cached, so the next attempt is free.
            before = _usage_now(caller)
            try:
                response = caller.generate(_SYSTEM, payload, _schema(langs))
                answer = _parse(response.text)
            finally:
                used = _usage_since(before, _usage_now(caller))
                if cache_key and used.get("calls"):
                    try:
                        from costs.record import record_call
                        record_call(f"ai:translate:{cache_key}", used, job_id=job_id, meeting=meeting)
                    except Exception as exc:  # noqa: BLE001 - the meter must not break an upload
                        logger.warning("translations: cost not recorded (%s)", type(exc).__name__)
            result: dict[str, dict[str, str]] = {}
            for lang in langs:
                entry = answer.get(lang)
                if not isinstance(entry, dict) or not isinstance(entry.get("title"), str) \
                        or not isinstance(entry.get("description"), str):
                    continue
                full = _restore(entry["description"], kept)
                candidate = {"title": entry["title"].strip(), "description": full if full is not None else ""}
                if full is not None and check(title, description, candidate, lang, series=series):
                    result[lang] = candidate
                else:
                    logger.info("translations: %s dropped (failed the checks)", lang)
            _write_cache(path, result)
            return result

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(work).result(timeout=timeout_s)
        finally:
            pool.shutdown(wait=False)
    except FutureTimeout:
        logger.warning("translations: the AI call took longer than %.0fs; none used", timeout_s)
        return {}
    except Exception as exc:  # noqa: BLE001 - this must never stop an upload
        logger.warning("translations: none used (%s: %s)", type(exc).__name__, exc)
        return {}
