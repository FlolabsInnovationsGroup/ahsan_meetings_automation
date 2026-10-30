"""The server and CI run ONE ffmpeg: the static build pinned on the
Dockerfile's "AS ffmpeg" line. ffmpeg 5.1 (a 27-hour final video) and 7.1
(xfade refused every job with dissolves) reached the server because the
render tests only ever ran on a developer's 9.0.1 and were skipped in CI."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"^FROM (mwader/static-ffmpeg:(\d+\.\d+(?:\.\d+)?)@sha256:[0-9a-f]{64}) AS ffmpeg$",
                 re.MULTILINE)


def _pin() -> tuple[str, str]:
    """(image, ffmpeg version) of the pinned build."""
    pins = PIN.findall((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert len(pins) == 1, "the Dockerfile must pin exactly one ffmpeg image, by version and digest"
    return pins[0]


def test_the_image_runs_the_pinned_ffmpeg():
    _pin()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/" in dockerfile


def test_ci_tests_on_the_pinned_ffmpeg():
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    # CI reads the image from the Dockerfile's own line, so the two can't drift
    assert "AS ffmpeg$" in workflow and "docker create" in workflow
    assert 'REQUIRE_FFMPEG: "1"' in workflow


@pytest.mark.skipif(not os.environ.get("REQUIRE_FFMPEG"), reason="CI only: there ffmpeg must be the pinned build")
@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
def test_ffmpeg_on_path_is_the_pinned_build(tool):
    _, version = _pin()
    assert shutil.which(tool), f"{tool} is not on PATH: every render test would be skipped"
    first = subprocess.run([tool, "-hide_banner", "-version"], capture_output=True, text=True, check=True).stdout
    assert re.match(rf"{tool} version n?{re.escape(version)}(?![\d.])", first), first.splitlines()[0]
