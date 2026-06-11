FROM python:3.12-slim

# Deno: external JS runtime required by yt-dlp for full YouTube support (since 2025.11.12)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 app \
 && python -m venv /opt/venv \
 && chown -R app:app /opt/venv \
 && mkdir -p /data /config \
 && chown app:app /data

COPY requirements.txt /tmp/requirements.txt
USER app
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY --chown=app:app app/ /app/
COPY --chown=app:app entrypoint.sh /entrypoint.sh
# exec bit can be lost when the project travels via zip/Windows
RUN chmod +x /entrypoint.sh

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD curl -fs http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
