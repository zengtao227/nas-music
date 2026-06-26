#!/usr/bin/env python3
"""
Sync Mia's Spotify Liked Songs to /music.

liked.spotdl = Spotify state database (not a download log).
Architecture:
  1. spotapi  → current liked song IDs (authenticated via sp_dc)
  2. diff     → added = current - saved, removed = saved - current
  3. added    → spotdl save (metadata) then spotdl download (audio)
  4. removed  → prune liked.spotdl then spotdl sync (deletes files)

Enhancement:
  - Local Spotify ID cache (WOAS tags) to avoid unnecessary spotdl checks
"""
import json
import pathlib
import subprocess
import time

import mutagen
import spotapi

MUSIC_DIR = pathlib.Path("/music")
PLAYLISTS_DIR = MUSIC_DIR / "Playlists"
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
BATCH_FILE = MUSIC_DIR / "liked_batch.spotdl"
OUTPUT_TEMPLATE = "{artists}/{album}/{title}"
BATCH_SIZE = 50

MAX_DELETIONS = 30

# --- Local cache for Spotify IDs ---
LOCAL_CACHE_FILE = MUSIC_DIR / ".local_id_cache.json"
CACHE_TTL = 8 * 60  # 8 minutes


def spotdl(*args: str) -> int:
    return subprocess.run(["spotdl", *args], cwd=str(MUSIC_DIR)).returncode


def song_id_from_file(mp3: pathlib.Path) -> str:
    try:
        tags = mutagen.File(mp3)
        woas = tags.get("WOAS") if tags else None
    except Exception:
        return ""
    if not woas:
        return ""
    return str(woas).rstrip("/").split("/")[-1]


def scan_local_spotify_ids() -> set[str]:
    ids: set[str] = set()
    for mp3 in MUSIC_DIR.rglob("*.mp3"):
        if PLAYLISTS_DIR in mp3.parents:
            continue
        sid = song_id_from_file(mp3)
        if sid:
            ids.add(sid)
    return ids


def load_local_ids_cached() -> set[str]:
    try:
        if LOCAL_CACHE_FILE.exists():
            data = json.loads(LOCAL_CACHE_FILE.read_text())
            ts = data.get("ts", 0)
            if time.time() - ts < CACHE_TTL:
                return set(data.get("ids", []))
    except Exception:
        pass

    ids = scan_local_spotify_ids()

    try:
        tmp = LOCAL_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "ids": list(ids)}))
        tmp.replace(LOCAL_CACHE_FILE)
    except Exception:
        pass

    return ids


def get_liked_ids() -> set[str]:
    sp_dc = SP_DC_FILE.read_text().strip()
    cfg = spotapi.Config(logger=spotapi.NoopLogger())
    dump = {"identifier": "mia", "password": "", "cookies": {"sp_dc": sp_dc}}
    login = spotapi.Login.from_cookies(dump, cfg)
    ids: set[str] = set()
    for chunk in spotapi.PrivatePlaylist(login).paginate_saved_tracks():
        for item in chunk.get("items", []):
            uri = (item.get("track") or {}).get("_uri", "")
            if uri:
                ids.add(uri.split(":")[-1])
    return ids


def load_save_file() -> tuple[list, set[str]]:
    if not SAVE_FILE.exists():
        return [], set()
    try:
        data = json.loads(SAVE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        print("WARNING: liked.spotdl corrupted, starting fresh", flush=True)
        return [], set()
    songs = data if isinstance(data, list) else data.get("songs", [])
    return songs, {s["song_id"] for s in songs if "song_id" in s}


def write_save_file(songs: list) -> None:
    tmp = SAVE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(songs))
    tmp.replace(SAVE_FILE)


def merge_batch_file(existing: list) -> list:
    if not BATCH_FILE.exists():
        return existing
    raw = json.loads(BATCH_FILE.read_text())
    batch = raw if isinstance(raw, list) else raw.get("songs", [])
    known = {s["song_id"] for s in existing}
    merged = existing + [s for s in batch if s.get("song_id") not in known]
    BATCH_FILE.unlink(missing_ok=True)
    return merged


def delete_files_for_ids(removed_ids: set[str]) -> int:
    deleted = 0
    for mp3 in MUSIC_DIR.rglob("*.mp3"):
        if PLAYLISTS_DIR in mp3.parents:
            continue
        if song_id_from_file(mp3) in removed_ids:
            mp3.unlink()
            deleted += 1
            for parent in (mp3.parent, mp3.parent.parent):
                if parent != MUSIC_DIR and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
    return deleted


def main() -> None:
    print("=== Liked Songs Sync ===", flush=True)

    current_ids = get_liked_ids()
    print(f"Spotify liked: {len(current_ids)}", flush=True)

    songs, saved_ids = load_save_file()
    added_ids = current_ids - saved_ids
    removed_ids = saved_ids - current_ids
    print(f"Added: {len(added_ids)}  Removed: {len(removed_ids)}", flush=True)

    # --- local prefilter cache ---
    local_ids = load_local_ids_cached()

    # --- download added songs ---
    if added_ids:
        id_list = list(added_ids)
        total = (len(id_list) + BATCH_SIZE - 1) // BATCH_SIZE

        for i in range(0, len(id_list), BATCH_SIZE):
            batch_ids = id_list[i:i + BATCH_SIZE]
            n = i // BATCH_SIZE + 1

            missing_ids = [sid for sid in batch_ids if sid not in local_ids]

            print(f"Batch {n}/{total}: {len(batch_ids)} songs, missing {len(missing_ids)}", flush=True)

            # Save metadata for ALL batch songs — liked.spotdl must track even
            # songs already on disk, so unlike detection works correctly later.
            urls_all = [f"https://open.spotify.com/track/{sid}" for sid in batch_ids]
            BATCH_FILE.unlink(missing_ok=True)
            rc = spotdl("save", *urls_all, "--save-file", str(BATCH_FILE))
            if rc != 0 or not BATCH_FILE.exists():
                print(f"Batch {n}: save failed (rc={rc}), skipping", flush=True)
                BATCH_FILE.unlink(missing_ok=True)
                continue

            songs = merge_batch_file(songs)
            write_save_file(songs)

            # Download only songs not already on disk.
            if missing_ids:
                urls_missing = [f"https://open.spotify.com/track/{sid}" for sid in missing_ids]
                spotdl("download", *urls_missing, "--output", OUTPUT_TEMPLATE)
            else:
                print(f"Batch {n}: all already on disk, download skipped", flush=True)

    # --- remove unliked songs ---
    if removed_ids:
        if len(removed_ids) > MAX_DELETIONS:
            print("WARNING: too many removals, skipping delete", flush=True)
        else:
            songs = [s for s in songs if s.get("song_id") not in removed_ids]
            write_save_file(songs)
            n = delete_files_for_ids(removed_ids)
            print(f"Removed {len(removed_ids)} songs, deleted {n} files", flush=True)

    print("Done.", flush=True)
