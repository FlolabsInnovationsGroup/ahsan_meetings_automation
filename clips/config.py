"""Settings for the clips subsystem.

Read from the environment (prefix CLIPS_) and from a `.env.clips` file in the
working folder (or the file named by CLIPS_ENV_FILE).

The clips process must be started with STORAGE_DIR set to the same folder as
CLIPS_STORAGE_DIR. The parts of the main app it reuses (YouTube sign-in, upload
journal, quota count, cost meter) then keep their files inside the clips folder
and can never touch the company's data. `clips.guard.assert_isolated()` checks
this before anything is uploaded.
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MARKER = ".clips-storage"  # a file with this name marks a folder as the clips data folder


class ConfigError(ValueError):
    """A setting has a value that cannot be used. The message names the setting and says what to write."""


class ClipsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLIPS_", env_file=".env.clips", extra="ignore")

    # --- supply
    # YouTube's terms forbid downloading a video without YouTube's own written permission, even when the
    # creator agrees. So downloads are off: the normal supply is files in a watched folder (our own
    # recordings, or files a creator sent us). Switching downloads on is the owner's decision.
    youtube_downloads: bool = False
    # Nothing dated before this day (ISO date, the game's release date) is clipped, unless its source's
    # basis is OFFICIAL. Empty switches the gate off. A video with no known date passes.
    not_before: str = "2026-11-19"

    # --- where things live, and who may call the API
    storage_dir: Path = Path("storage-clips")
    api_token: str = ""  # X-API-Key for the clips page; empty leaves it open (local use only)
    public_base_url: str = "http://localhost:8091"

    # --- the one channel this system may post to, and channels it must never post to
    channel_id: str = ""  # the clips channel's id (UC...); uploads are refused while empty
    forbidden_channel_ids: str = ""  # comma separated; put the company channel's id here
    channel_name: str = ""

    # --- posting pace. The research says start slow: hourly posting hurts a new channel.
    posts_per_day: int = Field(1, ge=0, le=24)  # the allowance never drops below this
    # allowance per week since the first post, e.g. "1,2,2,3,3,3,3,4" (the last value repeats). The research
    # ramp: 1 in week 1, 2 in week 2, 3 by week 4, 4 or more only from week 8.
    ramp_steps: str = "1,2,2,3,3,3,3,4"
    ramp_per_week: int = Field(0, ge=0, le=24)  # when above 0, replaces ramp_steps with a straight-line climb
    max_posts_per_day: int = Field(6, ge=1, le=24)  # the allowance never passes this
    min_gap_minutes: int = Field(180, ge=15, le=1440)  # at least this long between two posts
    window_start_hour: int = Field(0, ge=0, le=23)  # posts go out between these hours ...
    window_end_hour: int = Field(24, ge=1, le=24)  # ... in the time zone below
    timezone: str = "America/New_York"
    lead_minutes: int = Field(30, ge=0, le=1440)  # the earliest slot is this far from now
    # upload (private, timed) this long before release; a short window keeps what the kill switch
    # cannot take back small
    upload_ahead_hours: int = Field(6, ge=0, le=168)
    review_required: bool = True  # a person must approve each clip before it is scheduled
    # Only the starting value of the "Auto-approve Shorts" switch. Once the switch is set (page or command
    # line) the store flag wins. When on, Shorts are approved without a person; longer clips still wait.
    auto_approve: bool = False

    # --- what gets made
    shorts_per_video: int = Field(3, ge=0, le=20)
    long_per_video: int = Field(1, ge=0, le=5)
    short_min_s: float = 15.0
    short_max_s: float = 58.0  # stay under a minute
    long_min_s: float = 180.0
    long_max_s: float = 600.0
    download_max_height: int = 1080
    output_fps: int = 30
    captions: bool = True  # burn in what is said, when we have the words
    vision: bool = False  # let a vision model look at frames of candidate moments
    vision_model: str = ""  # empty: the provider's default small vision model
    default_hashtags: str = "#gta6,#gtavi"
    media_retention_hours: int = Field(48, ge=1)

    # --- the loop and its brakes
    loop_minutes: int = Field(15, ge=1, le=1440)
    videos_per_tick: int = Field(1, ge=0, le=20)  # source videos processed per loop turn
    uploads_per_tick: int = Field(2, ge=0, le=20)
    max_upload_failures: int = Field(3, ge=1)  # this many failed uploads in a row open the breaker

    @property
    def ramp_list(self) -> list[int]:
        """ramp_steps as numbers; unreadable parts are dropped, and an empty list means no steps."""
        steps = []
        for part in self.ramp_steps.split(","):
            try:
                steps.append(max(int(part.strip()), 0))
            except ValueError:
                continue
        return steps

    @property
    def not_before_date(self) -> date | None:
        """not_before as a date; None when the gate is off. ConfigError when it is not a date."""
        text = self.not_before.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ConfigError(
                f"CLIPS_NOT_BEFORE is '{text}', which is not a date. Write it as YYYY-MM-DD (for example "
                "2026-11-19), or leave it empty to switch the gate off."
            ) from None

    @property
    def db_path(self) -> Path:
        return self.storage_dir / "clips.sqlite"

    @property
    def media_dir(self) -> Path:
        return self.storage_dir / "media"

    @property
    def renders_dir(self) -> Path:
        return self.storage_dir / "renders"

    @property
    def marker_path(self) -> Path:
        return self.storage_dir / MARKER

    @property
    def kill_path(self) -> Path:
        return self.storage_dir / "KILL"

    @property
    def forbidden_ids(self) -> list[str]:
        return [part.strip() for part in self.forbidden_channel_ids.split(",") if part.strip()]

    @property
    def hashtags(self) -> list[str]:
        return [part.strip() for part in self.default_hashtags.split(",") if part.strip()]


@lru_cache
def clips_settings() -> ClipsSettings:
    """The clips settings, read once per process. Tests call `clips_settings.cache_clear()`."""
    return ClipsSettings(_env_file=os.environ.get("CLIPS_ENV_FILE", ".env.clips"))
