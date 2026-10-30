# Highlight Cutter API image (CPU-only).
#
# Build:  docker compose build
# The first build takes a while: torch + WhisperX + pyannote are large.

# The ffmpeg and ffprobe the app runs: 9.0.1, the version the renderer is
# written and tested on. Distro builds broke it: Debian 12's 5.1 (no title
# card, short audio in the shorts, a 27-hour final video) and Debian 13's 7.1
# (xfade refused every job with dissolves: "current rate of 1/0 is invalid").
# A static build pinned by digest, so every rebuild gets the same binaries.
# CI's render tests run on this same image (.github/workflows/tests.yml reads
# this line). To upgrade: change the tag and digest here, nowhere else.
FROM mwader/static-ffmpeg:9.0.1@sha256:54e55b0cb8f672870fc38ceb2e6c411855cb3b39c505f5f3b2505ee01ed5f2b7 AS ffmpeg

# Debian 13 (trixie). Switching the base rebuilds every layer: the server
# needs room for two images at once (prune Docker's build cache first:
# DEPLOY.md, Day-to-day).
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: Debian's 7.1, only for its shared libraries. WhisperX pins
# torchcodec < 0.8, which loads ffmpeg 4-7 libraries (pyannote reads audio
# files with it). The ffmpeg/ffprobe commands are the 9.0.1 build below.
# git: requirements.txt installs WhisperX and yt-dlp from GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch first. Without this, pip pulls the CUDA build from PyPI
# (several extra GB of NVIDIA libraries that a CPU instance never uses).
# Versions match WhisperX's pins (torch~=2.8.0, torchvision~=0.23.0).
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.8.*" "torchvision==0.23.*" "torchaudio==2.8.*"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# The pinned ffmpeg/ffprobe 9.0.1 (static: no libraries needed). /usr/local/bin
# comes before Debian's /usr/bin on PATH, so the app (FFMPEG_BIN=ffmpeg) and
# WhisperX (it runs "ffmpeg" to read audio) both get 9.0.1. Copied after the
# big pip layers, so a rebuild can reuse them from Docker's cache. The build
# stops if the commands found on PATH aren't these.
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
RUN [ "$(command -v ffmpeg)" = /usr/local/bin/ffmpeg ] \
    && [ "$(command -v ffprobe)" = /usr/local/bin/ffprobe ] \
    && ffmpeg -hide_banner -version | head -n 1

# Run as an unprivileged user. Model weights (Hugging Face, torch hub, nltk)
# are cached under /home/app/.cache, which compose mounts as a volume so they
# survive rebuilds and restarts instead of re-downloading every time.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data /home/app/.cache \
    && chown -R app:app /data /home/app/.cache

COPY --chown=app:app . .

USER app
ENV STORAGE_DIR=/data \
    HF_HOME=/home/app/.cache/huggingface \
    TORCH_HOME=/home/app/.cache/torch \
    NLTK_DATA=/home/app/.cache/nltk_data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Exactly ONE worker: jobs run as in-process background tasks and the job
# store keeps an in-memory cache, so multiple workers would not see each
# other's jobs.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
