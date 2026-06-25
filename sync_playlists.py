#!/usr/bin/env python3
"""
Sync Mia's private Spotify playlists to /music/Playlists/ via sp_dc cookie.

Each playlist has its own subfolder and .spotdl save file, treated as a
state snapshot (not a download log). Per playlist, every run:
  1. spotapi  → current playlist song IDs (authenticated)
  2. diff     → added = current - saved, removed = saved - current
  3. added    → spotdl save (metadata) then spotdl download (audio)
  4. removed  → prune the .spotdl then delete the files from this folder

Runs inside the spotdl-local Docker container with /music mounted.
"""
import json
import pathlib
import subprocess

import mutagen
import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
OUTPUT_BASE = "Playlists"
BATCH_SIZE = 20

# Refuse to delete more than this many files from one playlist in a single run.
# A few removals are real; dozens signal an incomplete snapshot (expired cookie
# / partial pagination), so we skip rather than wipe the folder.
MAX_DELETIONS = 30

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


def load_save_file(save_file: pathlib.Path) -> tuple[list, set[str]]:
    if not save_file.exists():
        return [], set()
    try:
        data = json.loads(save_file.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"WARNING: {save_file.name} corrupted, starting fresh", flush=True)
        return [], set()
    songs: list = data if isinstance(data, list) else data.get("songs", [])
    return songs, {s["song_id"] for s in songs if "song_id" in s}


def write_save_file(save_file: pathlib.Path, songs: list) -> None:
    tmp = save_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(songs))
    tmp.replace(save_file)  # atomic rename — never leaves a half-written file


def spotdl(*args: str, cwd: pathlib.Path) -> int:
    return subprocess.run(["spotdl", *args], cwd=str(cwd)).returncode


def song_id_from_file(mp3: pathlib.Path) -> str:
    """Read the Spotify track ID spotDL embeds in the WOAS ID3 frame."""
    try:
        tags = mutagen.File(mp3)
        woas = tags.get("WOAS") if tags else None
    except Exception:
        return ""
    if not woas:
        return ""
    return str(woas).rstrip("/").split("/")[-1]


def delete_files_for_ids(folder: pathlib.Path, removed_ids: set[str]) -> int:
    """Delete files in this playlist folder whose embedded Spotify ID was removed."""
    deleted = 0
    for mp3 in folder.rglob("*.mp3"):
        if song_id_from_file(mp3) in removed_ids:
            mp3.unlink()
            deleted += 1
            for parent in (mp3.parent, mp3.parent.parent):
                if parent != folder and folder in parent.parents and not any(parent.iterdir()):
                    parent.rmdir()
    return deleted


def sync_playlist(login: spotapi.Login, pl: dict) -> None:
    folder = MUSIC_DIR / OUTPUT_BASE / pl["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    save_file = folder / f"{pl['folder']}.spotdl"
    batch_file = folder / f"{pl['folder']}.batch.spotdl"
    output_template = f"{OUTPUT_BASE}/{pl['folder']}/{{artists}}/{{album}}/{{title}}"

    print(f"\n--- {pl['name']} ---", flush=True)

    current_ids = get_playlist_song_ids(login, pl["id"])
    songs, saved_ids = load_save_file(save_file)
    added_ids = current_ids - saved_ids
    removed_ids = saved_ids - current_ids
    print(f"Current: {len(current_ids)}  Added: {len(added_ids)}  Removed: {len(removed_ids)}", flush=True)

    # --- add new songs ---
    if added_ids:
        id_list = list(added_ids)
        total = (len(id_list) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(id_list), BATCH_SIZE):
            batch_ids = id_list[i : i + BATCH_SIZE]
            urls = [f"https://open.spotify.com/track/{sid}" for sid in batch_ids]
            n = i // BATCH_SIZE + 1
            print(f"Batch {n}/{total}: {len(batch_ids)} songs", flush=True)

            # Save metadata first so the .spotdl tracks even already-on-disk songs
            batch_file.unlink(missing_ok=True)
            rc = spotdl("save", *urls, "--save-file", str(batch_file), cwd=folder)
            if rc != 0 or not batch_file.exists():
                print(f"Batch {n}: save failed (rc={rc}), skipping — will retry next run", flush=True)
                batch_file.unlink(missing_ok=True)
                continue
            raw = json.loads(batch_file.read_text())
            batch_songs: list = raw if isinstance(raw, list) else raw.get("songs", [])
            known = {s["song_id"] for s in songs}
            songs = songs + [s for s in batch_songs if s.get("song_id") not in known]
            batch_file.unlink(missing_ok=True)
            write_save_file(save_file, songs)

            spotdl("download", *urls, "--output", output_template, cwd=folder)

    # --- remove songs no longer in the playlist ---
    # Guard wraps both prune and delete: an implausibly large count means the
    # snapshot is broken, so we touch nothing and let the next run self-heal.
    if removed_ids:
        if len(removed_ids) > MAX_DELETIONS:
            print(
                f"WARNING: {len(removed_ids)} removals exceeds limit ({MAX_DELETIONS}) — "
                "snapshot likely incomplete, skipping deletion this run",
                flush=True,
            )
        else:
            songs = [s for s in songs if s.get("song_id") not in removed_ids]
            write_save_file(save_file, songs)
            deleted = delete_files_for_ids(folder, removed_ids)
            print(f"Removed {len(removed_ids)} from DB, deleted {deleted} files", flush=True)


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
