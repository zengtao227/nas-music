#!/usr/bin/env python3
"""
Syncs Mia's Spotify Liked Songs to /music via spotapi + spotDL.
- New liked songs  → added to liked.spotdl, then spotdl sync downloads them
- Unliked songs    → removed from liked.spotdl, then spotdl sync deletes files
Runs inside the spotdl-local Docker container with /music mounted.
"""
import json
import subprocess
import pathlib

import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
OUTPUT_TEMPLATE = "{artists}/{album}/{title}"
BATCH_SIZE = 20  # small batches to stay within Spotify API rate limits


def get_liked_ids() -> set[str]:
    """Returns set of Spotify track IDs from Liked Songs."""
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


def load_save_file() -> tuple[dict, set[str]]:
    """Returns (save_data, set_of_song_ids_in_file)."""
    if not SAVE_FILE.exists():
        return {"type": "sync", "query": "liked-songs", "songs": []}, set()
    data = json.loads(SAVE_FILE.read_text())
    return data, {s["song_id"] for s in data.get("songs", [])}


def write_save_file(data: dict) -> None:
    SAVE_FILE.write_text(json.dumps(data))


def spotdl(*args: str) -> int:
    return subprocess.run(["spotdl"] + list(args), cwd=str(MUSIC_DIR)).returncode


def main() -> None:
    print("=== Liked Songs Sync ===", flush=True)

    current_ids = get_liked_ids()
    print(f"Liked Songs: {len(current_ids)}", flush=True)

    save_data, saved_ids = load_save_file()

    new_ids = current_ids - saved_ids
    removed_ids = saved_ids - current_ids
    print(f"New: {len(new_ids)}  Removed: {len(removed_ids)}", flush=True)

    # Download new songs in small batches (each batch = one spotDL call)
    # spotDL fetches Spotify metadata only for songs in the batch → avoids mass rate-limiting
    if new_ids:
        id_list = list(new_ids)
        for i in range(0, len(id_list), BATCH_SIZE):
            batch = id_list[i:i + BATCH_SIZE]
            urls = [f"https://open.spotify.com/track/{sid}" for sid in batch]
            print(f"Batch {i // BATCH_SIZE + 1}: downloading {len(batch)} songs...", flush=True)
            # --save-file adds downloaded songs to liked.spotdl for future sync/deletion tracking
            spotdl("download", *urls, "--output", OUTPUT_TEMPLATE, "--save-file", str(SAVE_FILE))

    # Remove unliked songs from save file, then spotdl sync deletes their local files
    if removed_ids:
        save_data, _ = load_save_file()
        save_data["songs"] = [s for s in save_data["songs"] if s["song_id"] not in removed_ids]
        write_save_file(save_data)
        print(f"Removed {len(removed_ids)} songs from liked.spotdl", flush=True)
        if SAVE_FILE.exists():
            print("Running spotdl sync to delete local files...", flush=True)
            spotdl("sync", str(SAVE_FILE), "--output", OUTPUT_TEMPLATE)

    print("Done.", flush=True)


if __name__ == "__main__":
    main()
