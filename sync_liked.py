#!/usr/bin/env python3
"""
Sync Mia's Spotify Liked Songs to /music.

liked.spotdl = Spotify state database (not a download log).
Architecture:
  1. spotapi  → current liked song IDs (authenticated via sp_dc)
  2. diff     → added = current - saved, removed = saved - current
  3. added    → spotdl save (metadata) then spotdl download (audio)
  4. removed  → prune liked.spotdl then spotdl sync (deletes files)

spotdl save is called only for the delta, so steady-state runs are cheap.
"""
import json
import pathlib
import subprocess

import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
BATCH_FILE = MUSIC_DIR / "liked_batch.spotdl"
OUTPUT_TEMPLATE = "{artists}/{album}/{title}"
BATCH_SIZE = 50


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
    songs: list = data if isinstance(data, list) else data.get("songs", [])
    return songs, {s["song_id"] for s in songs if "song_id" in s}


def write_save_file(songs: list) -> None:
    tmp = SAVE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(songs))
    tmp.replace(SAVE_FILE)  # atomic rename — never leaves a half-written file


def spotdl(*args: str) -> int:
    return subprocess.run(["spotdl", *args], cwd=str(MUSIC_DIR)).returncode


def merge_batch_file(existing: list) -> list:
    """Merge BATCH_FILE contents into existing song list; return updated list."""
    if not BATCH_FILE.exists():
        return existing
    raw = json.loads(BATCH_FILE.read_text())
    batch: list = raw if isinstance(raw, list) else raw.get("songs", [])
    known = {s["song_id"] for s in existing}
    merged = existing + [s for s in batch if s.get("song_id") not in known]
    BATCH_FILE.unlink(missing_ok=True)
    return merged


def main() -> None:
    print("=== Liked Songs Sync ===", flush=True)

    current_ids = get_liked_ids()
    print(f"Spotify liked: {len(current_ids)}", flush=True)

    songs, saved_ids = load_save_file()
    added_ids = current_ids - saved_ids
    removed_ids = saved_ids - current_ids
    print(f"Added: {len(added_ids)}  Removed: {len(removed_ids)}", flush=True)

    # --- download added songs ---
    if added_ids:
        id_list = list(added_ids)
        total = (len(id_list) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(id_list), BATCH_SIZE):
            batch_ids = id_list[i : i + BATCH_SIZE]
            urls = [f"https://open.spotify.com/track/{sid}" for sid in batch_ids]
            n = i // BATCH_SIZE + 1
            print(f"Batch {n}/{total}: {len(batch_ids)} songs", flush=True)

            # Save metadata first so liked.spotdl tracks even already-on-disk songs
            BATCH_FILE.unlink(missing_ok=True)
            rc = spotdl("save", *urls, "--save-file", str(BATCH_FILE))
            if rc != 0 or not BATCH_FILE.exists():
                print(f"Batch {n}: save failed (rc={rc}), skipping — will retry next run", flush=True)
                BATCH_FILE.unlink(missing_ok=True)
                continue
            songs = merge_batch_file(songs)
            write_save_file(songs)

            # Download audio (skips songs already on disk — that's fine)
            spotdl("download", *urls, "--output", OUTPUT_TEMPLATE)

    # --- remove unliked songs ---
    if removed_ids:
        songs = [s for s in songs if s.get("song_id") not in removed_ids]
        write_save_file(songs)
        print(f"Removed {len(removed_ids)} songs from liked.spotdl", flush=True)
        spotdl("sync", str(SAVE_FILE), "--output", OUTPUT_TEMPLATE)

    print("Done.", flush=True)


if __name__ == "__main__":
    main()
