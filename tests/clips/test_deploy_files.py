"""Cheap checks on the deploy files. Standard library only."""

import re
from pathlib import Path

from clips.config import ClipsSettings

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "clips"


def _read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_compose_has_own_project_and_image():
    text = _read("docker-compose.yml")
    assert re.search(r"^name: clips$", text, re.MULTILINE)
    assert "image: clips-app:latest" in text
    assert "highlight-cutter" not in text


def test_compose_storage_variables_match():
    text = _read("docker-compose.yml")
    storage = re.search(r"^\s+STORAGE_DIR: (\S+)", text, re.MULTILINE).group(1)
    clips = re.search(r"^\s+CLIPS_STORAGE_DIR: (\S+)", text, re.MULTILINE).group(1)
    assert storage == clips == "/data-clips"
    assert "clips-data:/data-clips" in text


def test_compose_exposes_only_localhost_8091():
    text = _read("docker-compose.yml")
    ports = re.findall(r'^\s+- "(\d[^"]*:\d+)"', text, re.MULTILINE)
    assert ports == ["127.0.0.1:8091:8091"]


def test_compose_has_two_services():
    text = _read("docker-compose.yml")
    assert re.search(r"^  web:", text, re.MULTILINE)
    assert re.search(r"^  loop:", text, re.MULTILINE)
    assert '"-m", "clips", "loop"' in text


def test_env_example_mentions_every_setting():
    text = _read("env.example")
    missing = [n for n in ClipsSettings.model_fields if f"CLIPS_{n.upper()}=" not in text]
    assert missing == []


def test_env_example_mentions_app_settings():
    text = _read("env.example")
    names = (
        "STORAGE_DIR", "LLM_PROVIDER", "ANTHROPIC_API_KEY", "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET", "PUBLIC_BASE_URL", "HC_API_TOKEN", "FFMPEG_BIN",
        "YTDLP_COOKIES_FILE", "YTDLP_PROXY",
    )
    for name in names:
        assert f"{name}=" in text
    assert "company channel" in text


def test_env_example_has_no_secret_looking_values():
    text = _read("env.example")
    assert not re.search(r"sk-ant-|AIza|GOCSPX|ya29\.|UC[\w-]{22}|googleusercontent", text)


def test_dockerfile_basics():
    text = _read("Dockerfile")
    assert "USER app" in text
    assert "deno" in text
    assert "clips.api:app" in text and "8091" in text


def test_dockerfile_installs_a_font():
    # the static ffmpeg and the slim base image have none; captions and credit would not be drawn
    assert re.search(r"apt-get install[^\n]*fonts-dejavu-core", _read("Dockerfile"))


def test_dockerfile_deno_is_new_enough_for_yt_dlp():
    text = _read("Dockerfile")
    major, minor = map(int, re.search(r"denoland/deno:bin-(\d+)\.(\d+)\.\d+", text).groups())
    assert (major, minor) >= (2, 3)
    assert "v[2] >= 3" in text  # and the build fails when it is not


def test_loop_has_its_own_healthcheck_and_web_probes_health():
    compose, docker = _read("docker-compose.yml"), _read("Dockerfile")
    loop = compose[compose.index("  loop:") :]
    assert "healthcheck:" in loop and "last_tick" in loop
    assert "/health" in docker


def test_env_example_says_the_api_token_is_required():
    text = _read("env.example")
    assert "REQUIRED" in text[text.index("CLIPS_API_TOKEN=") :][:400]


def test_readme_steps_are_in_a_workable_order():
    text = _read("README.md")
    assert "git clone -b clp" in text
    assert "/youtube/callback" in text
    assert text.index("clips init") < text.index("up -d")
    assert "sqlite3" in text and "youtube_token.json" in text and "state" in text


def test_compose_mounts_the_watched_inbox_folder():
    compose = _read("docker-compose.yml")
    assert re.search(r"-\s+\.\./\.\./clips-inbox:/clips-inbox(:ro)?\s", compose)
    assert "mkdir -p clips-inbox" in _read("README.md")


def test_env_example_upload_ahead_matches_the_short_default():
    match = re.search(r"^CLIPS_UPLOAD_AHEAD_HOURS=(\d+)", _read("env.example"), re.MULTILINE)
    assert match and int(match.group(1)) == ClipsSettings().upload_ahead_hours <= 6


def test_readme_explains_the_kill_file_and_a_failed_pull_back():
    text = _read("README.md")
    kill = text[text.index("## 12.") : text.index("## 13.")]
    assert "touch /data-clips/KILL" in kill and "YouTube Studio" in kill and "private" in kill
