import os

import pytest

from clips.config import MARKER, clips_settings


@pytest.fixture(autouse=True)
def _clips_env(_isolated_settings, tmp_path, monkeypatch):
    """Every clips test gets the per-test storage folder the main conftest sets
    (STORAGE_DIR), marked as a clips folder, with every clips setting at its
    default and no real `.env.clips` in reach."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / MARKER).write_text("clips\n", encoding="utf-8")
    for name in [n for n in os.environ if n.startswith("CLIPS_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLIPS_STORAGE_DIR", str(storage))
    # The release-date gate is off here so tests that use today's file dates are not skipped;
    # the tests of the gate set CLIPS_NOT_BEFORE themselves.
    monkeypatch.setenv("CLIPS_NOT_BEFORE", "")
    monkeypatch.setenv("CLIPS_ENV_FILE", str(tmp_path / "no-such.env.clips"))
    clips_settings.cache_clear()
    yield
    clips_settings.cache_clear()


@pytest.fixture
def clips_storage(tmp_path):
    """The clips data folder of this test."""
    return tmp_path / "storage"
