#!/usr/bin/env python3
"""
Sync Mia's Spotify Liked Songs to /music.

liked.spotdl = Spotify state database (not a download log).
Architecture:
  1. spotapi    → current liked song IDs (authenticated via sp_dc)
  2. diff       → added = current - saved, removed = saved - current
  3. disk-path  → songs already on disk: read ID3 tags, zero API calls
  4. dl-path    → songs in downloads.spotdl: copy directly, zero API calls
  5. api-path   → truly new songs: spotdl save (Spotify API) + download
  6. removed    → prune liked.spotdl then delete files
"""
import json
import pathlib
import subprocess
import time

import mutagen
import mutagen.id3
import spotapi

MUSIC_DIR = pathlib.Path("/music")
PLAYLISTS_DIR = MUSIC_DIR / "Playlists"
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
DOWNLOADS_FILE = MUSIC_DIR / "downloads.spotdl"
BATCH_FILE = MUSIC_DIR / "liked_batch.spotdl"
FALLBACK_MAP_FILE = MUSIC_DIR / "youtube_fallback_cache.json"
MISSING_IDS_FILE = MUSIC_DIR / "missing_ids.json"
OUTPUT_TEMPLATE = "{artists}/{album}/{title}"
BATCH_SIZE = 50

MAX_DELETIONS = 30

LOCAL_CACHE_FILE = MUSIC_DIR / ".local_id_cache.json"
CACHE_TTL = 8 * 60  # 8 minutes


def spotdl(*args: str) -> int:
    return subprocess.run(["spotdl", *args], cwd=str(MUSIC_DIR)).returncode


def spotdl_save_with_retry(*args: str, retries: int = 2, backoff: int = 30) -> int:
    for attempt in range(retries + 1):
        rc = spotdl(*args)
        if rc == 0:
            return 0
        if attempt < retries:
            print(f"spotdl save rc={rc}, retrying in {backoff}s ({attempt + 1}/{retries})", flush=True)
            time.sleep(backoff)
    return rc


def song_id_from_file(mp3: pathlib.Path) -> str:
    try:
        tags = mutagen.File(mp3)
        woas = tags.get("WOAS") if tags else None
    except Exception:
        return ""
    if not woas:
        return ""
    return str(woas).rstrip("/").split("/")[-1]


def build_entry_from_disk(mp3: pathlib.Path, song_id: str) -> dict:
    """Build a liked.spotdl entry from on-disk ID3 tags — no Spotify API needed."""
    def tag_str(tags: object, key: str) -> str:
        val = tags.get(key) if tags else None
        if val is None:
            return ""
        return str(val.text[0]) if hasattr(val, "text") else str(val)

    try:
        tags = mutagen.File(mp3)
        name = tag_str(tags, "TIT2") or mp3.stem
        artist = tag_str(tags, "TPE1")
        album = tag_str(tags, "TALB")
        track_raw = tag_str(tags, "TRCK")
        track_num = int(track_raw.split("/")[0]) if track_raw else 0
    except Exception:
        name = mp3.stem
        artist = album = ""
        track_num = 0

    return {
        "song_id": song_id,
        "name": name,
        "artist": artist,
        "album_name": album,
        "track_number": track_num,
        "song_url": f"https://open.spotify.com/track/{song_id}",
    }


def scan_local_spotify_ids() -> set[str]:
    ids: set[str] = set()
    for mp3 in MUSIC_DIR.rglob("*.mp3"):
        if PLAYLISTS_DIR in mp3.parents:
            continue
        sid = song_id_from_file(mp3)
        if sid:
            ids.add(sid)
    return ids


def build_local_id_to_path() -> dict[str, pathlib.Path]:
    """Map Spotify ID → MP3 path for all liked-song files (excludes Playlists/)."""
    result: dict[str, pathlib.Path] = {}
    for mp3 in MUSIC_DIR.rglob("*.mp3"):
        if PLAYLISTS_DIR in mp3.parents:
            continue
        sid = song_id_from_file(mp3)
        if sid:
            result[sid] = mp3
    return result


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
    declared_total: int | None = None
    for chunk in spotapi.PrivatePlaylist(login).paginate_saved_tracks():
        if declared_total is None:
            # WHY: Spotify's API reports the true total in every chunk via
            # 'totalCount'. Reading it once from the first chunk lets us verify
            # completeness after pagination without any extra API calls or
            # heuristic thresholds.
            declared_total = chunk.get("totalCount")
        for item in chunk.get("items", []):
            uri = (item.get("track") or {}).get("_uri", "")
            if uri:
                ids.add(uri.split(":")[-1])
    # Integrity check: if we received fewer IDs than Spotify declared, pagination
    # was interrupted (network cut, API bug, spotapi parsing failure). Raise so
    # the caller aborts the run — prevents spurious removals on a partial snapshot.
    if declared_total is None:
        print("WARNING: Spotify did not return totalCount — integrity check skipped", flush=True)
    elif len(ids) < declared_total:
        raise RuntimeError(
            f"Incomplete Spotify fetch: got {len(ids)}, declared {declared_total} "
            "— aborting to prevent spurious deletions"
        )
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


def load_fallback_map() -> dict[str, str]:
    """Returns spotify_id → youtube_url for entries in the fallback cache."""
    if not FALLBACK_MAP_FILE.exists():
        return {}
    try:
        data = json.loads(FALLBACK_MAP_FILE.read_text())
        return {
            sid: entry["youtube_url"]
            for sid, entry in data.items()
            if isinstance(entry, dict) and entry.get("youtube_url")
        }
    except (json.JSONDecodeError, OSError):
        return {}


def load_downloads_map() -> dict[str, dict]:
    """Build song_id → song-record map from downloads.spotdl."""
    if not DOWNLOADS_FILE.exists():
        return {}
    try:
        data = json.loads(DOWNLOADS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    songs: list = data if isinstance(data, list) else data.get("songs", [])
    return {s["song_id"]: s for s in songs if "song_id" in s}


def write_save_file(songs: list) -> None:
    tmp = SAVE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(songs))
    tmp.replace(SAVE_FILE)


def merge_batch_file(existing: list) -> list:
    if not BATCH_FILE.exists():
        return existing
    raw = json.loads(BATCH_FILE.read_text())
    batch: list = raw if isinstance(raw, list) else raw.get("songs", [])
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

    try:
        current_ids = get_liked_ids()
    except RuntimeError as exc:
        print(f"ABORT: {exc}", flush=True)
        return
    print(f"Spotify liked: {len(current_ids)}", flush=True)

    songs, saved_ids = load_save_file()
    added_ids = current_ids - saved_ids
    removed_ids = saved_ids - current_ids
    print(f"Added: {len(added_ids)}  Removed: {len(removed_ids)}", flush=True)

    if added_ids:
        # --- path 1: disk fast-path (read ID3 tags, zero API calls) ---
        # Scan once; reuse the id→path map for both fast-path and download prefilter.
        id_to_path = build_local_id_to_path()
        disk_hits = {sid: id_to_path[sid] for sid in added_ids if sid in id_to_path}
        if disk_hits:
            known = {s["song_id"] for s in songs}
            new_from_disk = [
                build_entry_from_disk(path, sid)
                for sid, path in disk_hits.items()
                if sid not in known
            ]
            if new_from_disk:
                songs = songs + new_from_disk
                write_save_file(songs)
                print(f"Disk-path: {len(new_from_disk)} songs from ID3 tags (0 API calls)", flush=True)
            added_ids -= set(disk_hits.keys())

        # --- path 2: downloads.spotdl fast-path (copy metadata, zero API calls) ---
        if added_ids:
            downloads_map = load_downloads_map()
            fast_ids = added_ids & set(downloads_map.keys())
            if fast_ids:
                known = {s["song_id"] for s in songs}
                new_from_downloads = [downloads_map[sid] for sid in fast_ids if sid not in known]
                if new_from_downloads:
                    songs = songs + new_from_downloads
                    write_save_file(songs)
                    print(f"DL-path: {len(new_from_downloads)} songs from downloads.spotdl (0 API calls)", flush=True)
                added_ids -= fast_ids

        # --- path 3: API path — truly new songs not on disk or in downloads.spotdl ---
        if added_ids:
            # local_ids reused from id_to_path scan (already done above)
            local_ids = set(id_to_path.keys())
            id_list = list(added_ids)
            total = (len(id_list) + BATCH_SIZE - 1) // BATCH_SIZE
            print(f"API-path: {len(added_ids)} songs need Spotify metadata ({total} batches)", flush=True)

            for i in range(0, len(id_list), BATCH_SIZE):
                batch_ids = id_list[i : i + BATCH_SIZE]
                n = i // BATCH_SIZE + 1
                missing_ids = [sid for sid in batch_ids if sid not in local_ids]
                print(f"Batch {n}/{total}: {len(batch_ids)} songs, missing {len(missing_ids)}", flush=True)

                urls_all = [f"https://open.spotify.com/track/{sid}" for sid in batch_ids]
                BATCH_FILE.unlink(missing_ok=True)
                rc = spotdl_save_with_retry("save", *urls_all, "--save-file", str(BATCH_FILE))
                if rc != 0 or not BATCH_FILE.exists():
                    print(f"Batch {n}: save failed (rc={rc}), skipping", flush=True)
                    BATCH_FILE.unlink(missing_ok=True)
                    continue

                songs = merge_batch_file(songs)
                write_save_file(songs)

                if missing_ids:
                    urls_missing = [f"https://open.spotify.com/track/{sid}" for sid in missing_ids]
                    spotdl("download", *urls_missing, "--output", OUTPUT_TEMPLATE)
                else:
                    print(f"Batch {n}: all already on disk, download skipped", flush=True)

    # --- remove unliked songs ---
    if removed_ids:
        if len(removed_ids) > MAX_DELETIONS:
            print(
                f"WARNING: {len(removed_ids)} removals exceeds limit ({MAX_DELETIONS}) — "
                "snapshot likely incomplete, skipping deletion this run",
                flush=True,
            )
        else:
            songs = [s for s in songs if s.get("song_id") not in removed_ids]
            write_save_file(songs)
            n = delete_files_for_ids(removed_ids)
            print(f"Removed {len(removed_ids)} from DB, deleted {n} files", flush=True)

    # --- fallback consumption path ---
    # Identify songs still missing after primary sync, consume pre-resolved URLs
    local_ids_after_sync = scan_local_spotify_ids()
    liked_ids_all = {s["song_id"] for s in songs if "song_id" in s}
    missing_ids = liked_ids_all - local_ids_after_sync

    fallback_map = load_fallback_map()
    resolved = {sid: fallback_map[sid] for sid in missing_ids if sid in fallback_map}

    if resolved:
        print(f"Fallback: {len(resolved)} pre-resolved URLs found, attempting hybrid download", flush=True)
        for sid, yt_url in resolved.items():
            spotify_url = f"https://open.spotify.com/track/{sid}"
            rc = spotdl("download", f"{yt_url}|{spotify_url}", "--output", OUTPUT_TEMPLATE)
            if rc != 0:
                # WHY: log failure explicitly so the log file is the audit trail.
                # The song remains in missing_ids.json and will be retried on the
                # next cron cycle — no separate retry queue is needed.
                print(f"  ⚠️  Fallback FAILED (rc={rc}): {sid} — {yt_url}", flush=True)

    # Write missing_ids.json snapshot (still-missing after all paths)
    local_ids_final = scan_local_spotify_ids()
    still_missing = list(liked_ids_all - local_ids_final)
    tmp = MISSING_IDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(still_missing))
    tmp.replace(MISSING_IDS_FILE)
    print(f"missing_ids.json: {len(still_missing)} unresolved songs written", flush=True)

    print("Done.", flush=True)


if __name__ == "__main__":
    main()
