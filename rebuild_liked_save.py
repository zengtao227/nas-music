#!/usr/bin/env python3
"""
One-time rebuild: repopulate liked.spotdl from Spotify liked songs via spotdl save.

spotdl save fetches song metadata without downloading audio.
Runs inside the spotdl-local Docker container with /music mounted.

SAFETY: This script performs batch operations on the liked.spotdl database.
Use --dry-run to preview changes before applying them.
"""

import json
import subprocess
import pathlib
import sys

import spotapi
from shared import make_login

MUSIC_DIR = pathlib.Path("/music")
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
BATCH_SIZE = 50  # larger than download batches — no rate-limit concern for save


def get_liked_ids() -> list[str]:
    login = make_login()
    ids: list[str] = []
    for chunk in spotapi.PrivatePlaylist(login).paginate_saved_tracks():
        for item in chunk.get("items", []):
            uri = (item.get("track") or {}).get("_uri", "")
            if uri:
                ids.append(uri.split(":")[-1])
    return ids


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    print("=== Rebuild liked.spotdl ===", flush=True)
    if dry_run:
        print("[DRY RUN MODE - no files will be modified]", flush=True)

    print("Fetching liked song IDs from Spotify...", flush=True)
    ids = get_liked_ids()
    print(f"Found {len(ids)} liked songs on Spotify", flush=True)

    # Preserve any entries already in the file (merge mode)
    all_songs: list = []
    if SAVE_FILE.exists():
        raw = json.loads(SAVE_FILE.read_text())
        all_songs = raw if isinstance(raw, list) else raw.get("songs", [])
        print(
            f"Existing liked.spotdl has {len(all_songs)} entries — will merge",
            flush=True,
        )
    known_ids: set[str] = {s["song_id"] for s in all_songs}

    # Filter out already-known IDs to avoid redundant API calls
    new_ids = [sid for sid in ids if sid not in known_ids]
    print(f"New IDs to fetch: {len(new_ids)}", flush=True)

    if not new_ids:
        print("No new songs to add. liked.spotdl is already complete.", flush=True)
        return

    if dry_run:
        print(
            f"\n[DRY RUN] Would fetch metadata for {len(new_ids)} songs and merge with {len(all_songs)} existing entries",
            flush=True,
        )
        print(
            f"[DRY RUN] Final count would be: {len(all_songs) + len(new_ids)} entries",
            flush=True,
        )
        return

    temp = SAVE_FILE.with_suffix(".rebuild.spotdl")
    urls = [f"https://open.spotify.com/track/{sid}" for sid in new_ids]
    total_batches = (len(urls) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(urls), BATCH_SIZE):
        batch = urls[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(
            f"Batch {batch_num}/{total_batches}: saving {len(batch)} songs...",
            flush=True,
        )
        rc = subprocess.run(
            ["spotdl", "save"] + batch + ["--save-file", str(temp)],
            cwd=str(MUSIC_DIR),
        ).returncode

        if rc != 0:
            print(
                f"WARNING: spotdl save failed for batch {batch_num} (rc={rc}), skipping this batch",
                flush=True,
            )
            temp.unlink(missing_ok=True)
            continue

        if temp.exists():
            try:
                raw2 = json.loads(temp.read_text())
                batch_songs: list = (
                    raw2 if isinstance(raw2, list) else raw2.get("songs", [])
                )
                for s in batch_songs:
                    if s["song_id"] not in known_ids:
                        all_songs.append(s)
                        known_ids.add(s["song_id"])
                temp.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError) as exc:
                print(
                    f"WARNING: could not parse batch {batch_num} temp file: {exc}",
                    flush=True,
                )
                temp.unlink(missing_ok=True)
                continue

    print(f"Writing {len(all_songs)} entries to liked.spotdl...", flush=True)
    # Atomic write
    tmp = SAVE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(all_songs))
    tmp.replace(SAVE_FILE)

    print(
        f"Done. liked.spotdl now has {len(all_songs)} / {len(ids)} songs tracked.",
        flush=True,
    )


if __name__ == "__main__":
    main()
