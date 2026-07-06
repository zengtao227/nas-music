#!/bin/bash
# Runs as root via cron (daily 03:00). Resolves YouTube URLs for songs spotdl
# could not match (missing_ids.json), so the next 5-minute sync can download
# them via the hybrid path without manual intervention.
MUSIC_DIR="/volume1/homes/Mia/Music"
LOG_FILE="$MUSIC_DIR/.fallback_resolver.log"
LOCKFILE="/tmp/fallback_resolver.lock"

exec 9>"$LOCKFILE"
flock -n 9 || exit 0

# Rotate log when it exceeds 5 MB; keep one .bak copy
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5242880 ]; then
  mv "$LOG_FILE" "${LOG_FILE}.bak"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Fallback resolver started" >> "$LOG_FILE"

/usr/local/bin/docker run --rm \
  -v "$MUSIC_DIR":/music \
  --entrypoint python3 \
  spotdl-local:latest \
  /music/fallback_resolver.py >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Fallback resolver done" >> "$LOG_FILE"
