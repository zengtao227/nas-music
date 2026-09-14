#!/bin/bash
MUSIC_DIR="/volume1/homes/Mia/Music"
LOG_FILE="$MUSIC_DIR/.spotdl_liked_sync.log"
LOCKFILE="/tmp/spotdl_liked_sync.lock"

exec 9>"$LOCKFILE"
flock -n 9 || exit 0

# Rotate log files when they exceed 5 MB; keep one .bak copy.
# WHY collision audit too: it re-appends the same unchanged decisions every run
# and had reached 85 MB by 2026-09-14.
for f in "$LOG_FILE" "$MUSIC_DIR/.collision_audit.jsonl"; do
  if [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 5242880 ]; then
    mv "$f" "${f}.bak"
  fi
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Liked sync started" >> "$LOG_FILE"

/usr/local/bin/docker run --rm \
  -v "$MUSIC_DIR":/music \
  --entrypoint python3 \
  spotdl-local:latest \
  /music/sync_liked.py >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Liked sync done" >> "$LOG_FILE"
