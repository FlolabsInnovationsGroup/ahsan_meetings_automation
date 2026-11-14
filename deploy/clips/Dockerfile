# Clips image: its own process, separate from the production image.
# Build from the repo root:  docker build -f deploy/clips/Dockerfile -t clips-app:latest .
# No torch or WhisperX here: clips uses YouTube captions or a paid API.

# Same pinned ffmpeg 9.0.1 as the repo's Dockerfile (change both together).
FROM mwader/static-ffmpeg:9.0.1@sha256:54e55b0cb8f672870fc38ceb2e6c411855cb3b39c505f5f3b2505ee01ed5f2b7 AS ffmpeg

# Deno: the JavaScript runtime yt-dlp needs to solve YouTube's checks. yt-dlp's EJS notes ask for
# Deno 2.3.0 or later, and requirements.txt takes the newest yt-dlp, so keep this at 2.3 or above
# (the build below fails when it is older). Pin by digest once you have looked it up:
#   docker buildx imagetools inspect denoland/deno:bin-2.5.6
FROM denoland/deno:bin-2.5.6 AS deno

FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: requirements.txt installs yt-dlp from GitHub.
# fonts-dejavu-core: the static ffmpeg has no fonts and this base image has none, so without it
# the hook bar, burned-in captions and credit line of every Short would silently not be drawn.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Install the app's light packages; skip the heavy speech and torch ones.
RUN grep -viE '^(torch|torchvision|torchaudio|whisperx|pyannote|faster-whisper|ctranslate2|nltk)' requirements.txt > requirements-clips.txt \
    && pip install -r requirements-clips.txt

COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=deno /deno /usr/local/bin/deno
RUN ffmpeg -hide_banner -version | head -n 1 && deno --version | head -n 1 \
    && deno --version | head -n 1 | awk '{split($2, v, "."); exit !(v[1] > 2 || (v[1] == 2 && v[2] >= 3))}'

RUN test -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data-clips \
    && chown -R app:app /data-clips

COPY --chown=app:app . .

USER app
ENV STORAGE_DIR=/data-clips \
    CLIPS_STORAGE_DIR=/data-clips

EXPOSE 8091

# This check is for the web page. The loop service has its own check in docker-compose.yml
# (it serves no HTTP; it is healthy while the file last_tick is fresh).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health', timeout=4)"

# Default: the web page. The loop service overrides this with: python -m clips loop
CMD ["uvicorn", "clips.api:app", "--host", "0.0.0.0", "--port", "8091", "--workers", "1"]
