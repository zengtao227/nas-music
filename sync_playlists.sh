#!/bin/bash
MUSIC_DIR="/volume1/homes/Mia/Music"
LOG_FILE="$MUSIC_DIR/.spotdl_playlists_sync.log"
LOCKFILE="/tmp/spotdl_playlists_sync.lock"

[ -f "$LOCKFILE" ] && exit 0
touch "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Playlist sync started" >> "$LOG_FILE"

/usr/local/bin/docker run --rm \
  -v "$MUSIC_DIR":/music \
  -v /volume1/docker/jellyfin/config/data/playlists:/jellyfin_playlists \
  --entrypoint python3 \
  spotdl-local:latest \
  /music/sync_playlists.py >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Playlist sync done" >> "$LOG_FILE"
