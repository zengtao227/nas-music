#!/usr/bin/env python3
"""
One-time rebuild: repopulate liked.spotdl from Spotify liked songs via spotdl save.

spotdl save fetches song metadata without downloading audio.
Runs inside the spotdl-local Docker container with /music mounted.
"""
import json
import subprocess
import pathlib

import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
BATCH_SIZE = 50  # larger than download batches — no rate-limit concern for save


def get_liked_ids() -> list[str]:
    sp_dc = SP_DC_FILE.read_text().strip()
    cfg = spotapi.Config(logger=spotapi.NoopLogger())
    dump = {"identifier": "mia", "password": "", "cookies": {"sp_dc": sp_dc}}
    login = spotapi.Login.from_cookies(dump, cfg)
    ids: list[str] = []
    for chunk in spotapi.PrivatePlaylist(login).paginate_saved_tracks():
        for item in chunk.get("items", []):
            uri = (item.get("track") or {}).get("_uri", "")
            if uri:
                ids.append(uri.split(":")[-1])
    return ids


def main() -> None:
    print("=== Rebuild liked.spotdl ===", flush=True)
    print("Fetching liked song IDs from Spotify...", flush=True)
    ids = get_liked_ids()
    print(f"Found {len(ids)} liked songs on Spotify", flush=True)

    # Preserve any entries already in the file
    all_songs: list = []
    if SAVE_FILE.exists():
        raw = json.loads(SAVE_FILE.read_text())
        all_songs = raw if isinstance(raw, list) else raw.get("songs", [])
        print(f"Existing liked.spotdl has {len(all_songs)} entries — will merge", flush=True)
    known_ids: set[str] = {s["song_id"] for s in all_songs}

    temp = SAVE_FILE.with_suffix(".rebuild.spotdl")
    urls = [f"https://open.spotify.com/track/{sid}" for sid in ids]
    total_batches = (len(urls) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(urls), BATCH_SIZE):
        batch = urls[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"Batch {batch_num}/{total_batches}: saving {len(batch)} songs...", flush=True)
        subprocess.run(
            ["spotdl", "save"] + batch + ["--save-file", str(temp)],
            cwd=str(MUSIC_DIR),
        )
        if temp.exists():
            raw2 = json.loads(temp.read_text())
            batch_songs: list = raw2 if isinstance(raw2, list) else raw2.get("songs", [])
            for s in batch_songs:
                if s["song_id"] not in known_ids:
                    all_songs.append(s)
                    known_ids.add(s["song_id"])
            temp.unlink(missing_ok=True)

    print(f"Writing {len(all_songs)} entries to liked.spotdl...", flush=True)
    SAVE_FILE.write_text(json.dumps(all_songs))
    print(f"Done. liked.spotdl now has {len(all_songs)} / {len(ids)} songs tracked.", flush=True)


if __name__ == "__main__":
    main()
