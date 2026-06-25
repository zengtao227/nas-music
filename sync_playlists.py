#!/usr/bin/env python3
"""
Syncs Mia's private Spotify playlists to /music/Playlists/ via sp_dc cookie.
Each playlist has its own subfolder and .spotdl save file.
Runs inside the spotdl-local Docker container with /music mounted.
"""
import json
import subprocess
import pathlib

import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
OUTPUT_BASE = "Playlists"
BATCH_SIZE = 20

PLAYLISTS = [
    {
        "id": "3ebskb0Uy9zbm87SyemHjG",
        "name": "summer26",
        "folder": "summer26",
    },
    {
        "id": "2Rx94JQDRIft0V4Fd9rMq5",
        "name": "can_dances",
        "folder": "can_dances",
    },
]


def get_playlist_song_ids(login: spotapi.Login, playlist_id: str) -> set[str]:
    """Get song IDs from a private playlist using the sp_dc-authenticated client."""
    ids: set[str] = set()
    # Pass login.client so PublicPlaylist's API calls include auth tokens — works for private playlists
    pl = spotapi.PublicPlaylist(playlist_id, client=login.client)
    for chunk in pl.paginate_playlist():
        for item in chunk.get("items", []):
            iv2 = item.get("itemV2") or {}
            data = iv2.get("data") or {}
            uri = data.get("uri", "")
            if uri.startswith("spotify:track:"):
                ids.add(uri.split(":")[-1])
    return ids


def get_saved_ids(save_file: pathlib.Path) -> set[str]:
    if not save_file.exists():
        return set()
    data = json.loads(save_file.read_text())
    songs: list = data if isinstance(data, list) else data.get("songs", [])
    return {s["song_id"] for s in songs}


def spotdl(*args: str, cwd: pathlib.Path) -> int:
    return subprocess.run(["spotdl"] + list(args), cwd=str(cwd)).returncode


def sync_playlist(login: spotapi.Login, pl: dict) -> None:
    folder = MUSIC_DIR / OUTPUT_BASE / pl["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    save_file = folder / f"{pl['folder']}.spotdl"
    output_template = f"{OUTPUT_BASE}/{pl['folder']}/{{artists}}/{{album}}/{{title}}"

    print(f"\n--- {pl['name']} ---", flush=True)

    current_ids = get_playlist_song_ids(login, pl["id"])
    saved_ids = get_saved_ids(save_file)

    new_ids = current_ids - saved_ids
    removed_ids = saved_ids - current_ids

    print(f"Current: {len(current_ids)}  New: {len(new_ids)}  Removed: {len(removed_ids)}", flush=True)

    # spotDL --save-file OVERWRITES on each call — use temp file per batch and merge.
    if new_ids:
        batch_save = save_file.with_suffix(".batch.spotdl")
        id_list = list(new_ids)
        for i in range(0, len(id_list), BATCH_SIZE):
            batch = id_list[i:i + BATCH_SIZE]
            urls = [f"https://open.spotify.com/track/{sid}" for sid in batch]
            print(f"Batch {i // BATCH_SIZE + 1}: {len(batch)} songs...", flush=True)
            spotdl("download", *urls,
                   "--output", output_template,
                   "--save-file", str(batch_save),
                   cwd=folder)
            if batch_save.exists():
                raw = json.loads(batch_save.read_text())
                batch_songs: list = raw if isinstance(raw, list) else raw.get("songs", [])
                existing = json.loads(save_file.read_text()) if save_file.exists() else []
                cur: list = existing if isinstance(existing, list) else existing.get("songs", [])
                known = {s["song_id"] for s in cur}
                new_entries = [s for s in batch_songs if s["song_id"] not in known]
                if new_entries:
                    save_file.write_text(json.dumps(cur + new_entries))
                batch_save.unlink(missing_ok=True)

    if removed_ids and save_file.exists():
        data = json.loads(save_file.read_text())
        songs: list = data if isinstance(data, list) else data.get("songs", [])
        updated = [s for s in songs if s["song_id"] not in removed_ids]
        save_file.write_text(json.dumps(updated))
        print(f"Removed {len(removed_ids)} songs, running sync to clean files...", flush=True)
        spotdl("sync", str(save_file), "--output", output_template, cwd=folder)


def main() -> None:
    print("=== Playlist Sync ===", flush=True)

    sp_dc = SP_DC_FILE.read_text().strip()
    cfg = spotapi.Config(logger=spotapi.NoopLogger())
    dump = {"identifier": "mia", "password": "", "cookies": {"sp_dc": sp_dc}}
    login = spotapi.Login.from_cookies(dump, cfg)

    for pl in PLAYLISTS:
        sync_playlist(login, pl)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
