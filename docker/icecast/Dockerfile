# Icecast, configured from the environment.
#
# Built here rather than pulled from a third-party image so the configuration
# contract is ours: the passwords come from the same .env as everything else,
# and the cache headers the design requires are in a file we control.

FROM debian:bookworm-slim

LABEL org.opencontainers.image.title="streamchen-icecast"
LABEL org.opencontainers.image.description="Icecast 2 configured for streamchen"
LABEL org.opencontainers.image.source="https://github.com/crazynudelsieb/streamchen"
LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    icecast2 \
    gettext-base \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY icecast.xml /etc/icecast2/icecast.template.xml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    mkdir -p /var/log/icecast2 && \
    chown -R icecast2:icecast /etc/icecast2 /var/log/icecast2

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/status.xsl >/dev/null || exit 1

USER icecast2
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
