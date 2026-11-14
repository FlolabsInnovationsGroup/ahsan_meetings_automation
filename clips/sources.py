"""The source registry: add, enable and disable sources, with the rights rule enforced."""

from __future__ import annotations

import re
from typing import Any

from clips.models import Platform, RightsBasis, Source
from clips.rights import check_source
from clips.store import Store


class RightsError(ValueError):
    """Raised when a source may not be enabled. `reasons` says why in plain words."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__(" ".join(self.reasons) or "The rights record is not enough.")


def make_source_id(name: str) -> str:
    """A short lowercase slug from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40].strip("-") or "source"


def _free_id(store: Store, base: str) -> str:
    candidate, n = base, 2
    while store.source_id_used(candidate):  # also ids of deleted sources: their videos and clips remain
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def add_source(
    store: Store,
    *,
    name: str,
    platform: Platform,
    rights: RightsBasis = RightsBasis.NONE,
    enabled: bool = False,
    **fields: Any,
) -> Source:
    """Save a new source. Asking for enabled=True is refused unless the rights record passes."""
    source_id = fields.pop("id", None)
    if source_id:
        if store.get_source(source_id) is None and store.source_id_used(source_id):
            raise ValueError(f"The id '{source_id}' belonged to a removed source and is never reused.")
    else:
        source_id = _free_id(store, make_source_id(name))
    source = Source(id=source_id, name=name, platform=platform, rights=rights, enabled=enabled, **fields)
    if enabled:
        verdict = check_source(source)
        if not verdict.ok:
            raise RightsError(verdict.reasons)
    return store.save_source(source)


def _get(store: Store, source_id: str) -> Source:
    source = store.get_source(source_id)
    if source is None:
        raise KeyError(source_id)
    return source


def enable_source(store: Store, source_id: str) -> Source:
    source = _get(store, source_id)
    verdict = check_source(source)
    if not verdict.ok:
        raise RightsError(verdict.reasons)
    return store.save_source(source.model_copy(update={"enabled": True}))


def disable_source(store: Store, source_id: str) -> Source:
    source = _get(store, source_id)
    return store.save_source(source.model_copy(update={"enabled": False}))
