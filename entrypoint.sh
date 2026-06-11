#!/bin/sh
set -u

# Sites (especially YouTube) break old yt-dlp versions within weeks.
# Refresh on every container start unless explicitly disabled.
if [ "${AUTO_UPDATE_YTDLP:-true}" = "true" ]; then
    echo "[entrypoint] updating yt-dlp..."
    pip install -q --no-cache-dir -U "yt-dlp[default]" \
        || echo "[entrypoint] yt-dlp update failed (offline?); using installed version"
fi
yt-dlp --version

exec gunicorn --workers 1 --threads 16 --timeout 0 \
    --bind 0.0.0.0:8080 --access-logfile - server:app
