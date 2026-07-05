#!/bin/bash
MUSIC_DIR="/volume1/homes/Mia/Music"
LOG_FILE="$MUSIC_DIR/.spotdl_liked_sync.log"
LOCKFILE="/tmp/spotdl_liked_sync.lock"

exec 9>"$LOCKFILE"
flock -n 9 || exit 0

# Rotate log when it exceeds 5 MB; keep one .bak copy
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5242880 ]; then
  mv "$LOG_FILE" "${LOG_FILE}.bak"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Liked sync started" >> "$LOG_FILE"

/usr/local/bin/docker run --rm \
  -v "$MUSIC_DIR":/music \
  --entrypoint python3 \
  spotdl-local:latest \
  /music/sync_liked.py >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Liked sync done" >> "$LOG_FILE"
