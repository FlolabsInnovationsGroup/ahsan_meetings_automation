# Highlight Cutter API image (CPU-only).
#
# Build:  docker compose build
# The first build takes a while: torch + WhisperX + pyannote are large.

# TEMPORARY: back on Debian 12 so this deploy reuses the server's cached
# layers (the Debian 13 build ran out of disk). Once old jobs are cleared and
# Docker's build cache is pruned, switch to python:3.12-slim-trixie again:
# Debian 13's ffmpeg is 7.1, which the renderer is written and tested for;
# Debian 12's ffmpeg 5.1 broke it (no drawtext text_align, so no title card;
# audio lengths off in the shorts; a 27-hour final video).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: slicing + probing (and the shared libs torchcodec loads).
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
