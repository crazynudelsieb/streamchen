# Multi-stage production Dockerfile for the streamchen API.

# Build stage - includes build tools and dependencies
FROM python:3.14.7-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# No libpq: asyncpg speaks the Postgres wire protocol itself, so nothing here
# links against it. libffi is for argon2-cffi.
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Production stage - minimal runtime image
FROM python:3.14.7-slim AS production

LABEL org.opencontainers.image.title="streamchen"
LABEL org.opencontainers.image.description="Collaborative radio — one room, one stream, everybody's queue"
LABEL org.opencontainers.image.url="https://github.com/crazynudelsieb/streamchen"
LABEL org.opencontainers.image.source="https://github.com/crazynudelsieb/streamchen"
LABEL org.opencontainers.image.documentation="https://github.com/crazynudelsieb/streamchen/blob/main/README.md"
LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

# XDG_CACHE_HOME is where yt-dlp keeps YouTube's player javascript once it has
# fetched and interpreted it. Given nowhere to write it does that again on every
# extraction, which every search and every song added pays for: the container
# user has no home directory, so the default (~/.cache) cannot be created and
# the cache silently never hits.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    XDG_CACHE_HOME=/var/cache/streamchen-extractor

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN groupadd -r -g 10001 streamchen && useradd -r -u 10001 -g streamchen streamchen

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app

RUN mkdir -p /var/cache/streamchen-extractor && \
    chown -R streamchen:streamchen /app /var/cache/streamchen-extractor
USER streamchen

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/healthz || exit 1

# One worker process, several event-loop workers inside it. WebSocket rooms are
# fanned out through Redis pub/sub, so scaling out is a matter of running more
# containers rather than more processes here.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
