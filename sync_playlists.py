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

import datetime
import json
import pathlib
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

import mutagen
from mutagen.id3 import WOAS
import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
FALLBACK_MAP_FILE = MUSIC_DIR / "youtube_fallback_cache.json"
JELLYFIN_PLAYLISTS_DIR = pathlib.Path("/jellyfin_playlists")
JELLYFIN_MUSIC_PREFIX = "/media/music"
JELLYFIN_URL = "http://192.168.68.68:8096"
JELLYFIN_API_KEY_FILE = MUSIC_DIR / ".jellyfin_api_key"
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
        "jellyfin_name": "Summer 26",
        "jellyfin_id": "ed82387a29c7bf3d4703b7d964d94c54",
    },
    {
        "id": "2Rx94JQDRIft0V4Fd9rMq5",
        "name": "can_dances",
        "folder": "can_dances",
        "jellyfin_name": "Can Dances",
        "jellyfin_id": "313dc8185ed60db38a6a6b42e2321835",
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


def load_fallback_map() -> dict[str, str]:
    """Returns spotify_id -> youtube_url using the same trust rules as liked sync."""
    if not FALLBACK_MAP_FILE.exists():
        return {}
    try:
        data = json.loads(FALLBACK_MAP_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    now = datetime.datetime.now(datetime.timezone.utc)
    result: dict[str, str] = {}
    for sid, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("youtube_url"):
            continue
        if entry.get("verified") is False:
            continue
        source = entry.get("source", "manual")
        if source == "manual":
            result[sid] = entry["youtube_url"]
            continue

        has_new_fields = "confidence" in entry and bool(entry.get("resolved_at"))
        if has_new_fields:
            if float(entry.get("confidence") or 0) < 0.35:
                continue
            try:
                ts = datetime.datetime.fromisoformat(
                    entry["resolved_at"].replace("Z", "+00:00")
                )
                if (now - ts).days >= 90:
                    continue
            except ValueError:
                continue
            result[sid] = entry["youtube_url"]
        elif entry.get("verified") is True:
            result[sid] = entry["youtube_url"]
    return result


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


def tag_text(tags: Any, key: str) -> str:
    val = tags.get(key) if tags else None
    if val is None:
        return ""
    return str(val.text[0]) if hasattr(val, "text") and val.text else str(val)


def normalize_text(value: object) -> str:
    return " ".join(str(value).casefold().split())


def normalized_song_key(song: dict) -> tuple[str, str, str]:
    artists = song.get("artists") or []
    artist = ", ".join(artists) if artists else (song.get("artist") or "")
    return (
        normalize_text(artist),
        normalize_text(song.get("album_name") or ""),
        normalize_text(song.get("name") or ""),
    )


def normalized_file_key(mp3: pathlib.Path) -> tuple[str, str, str] | None:
    try:
        tags = mutagen.File(mp3)
    except Exception:
        return None
    if not tags:
        return None
    values = (tag_text(tags, "TPE1"), tag_text(tags, "TALB"), tag_text(tags, "TIT2"))
    if not (values[0] and values[2]):
        return None
    return (
        normalize_text(values[0]),
        normalize_text(values[1]),
        normalize_text(values[2]),
    )


def set_file_song_id(mp3: pathlib.Path, song_id: str) -> bool:
    try:
        audio = mutagen.File(mp3)
        if not audio or not getattr(audio, "tags", None):
            return False
        audio.tags.setall(
            "WOAS", [WOAS(url=f"https://open.spotify.com/track/{song_id}")]
        )
        audio.save()
    except Exception as exc:
        print(f"Repair: failed to update WOAS for {mp3.name}: {exc}", flush=True)
        return False
    return True


def build_file_key_to_paths(
    folder: pathlib.Path,
) -> dict[tuple[str, str, str], list[tuple[pathlib.Path, str]]]:
    candidates_by_key: dict[tuple[str, str, str], list[tuple[pathlib.Path, str]]] = {}
    for mp3 in folder.rglob("*.mp3"):
        key = normalized_file_key(mp3)
        if key is None:
            continue
        candidates_by_key.setdefault(key, []).append((mp3, song_id_from_file(mp3)))
    return candidates_by_key


def repair_stale_woas_matches(
    folder: pathlib.Path,
    missing_ids: set[str],
    songs: list,
) -> set[str]:
    """Fix same-song files whose old WOAS prevents them from satisfying .spotdl.

    WHY: spotDL skips downloads by output filename. If Spotify relinks a track or
    returns a sibling ID for the same title/album/artist, the MP3 can already be
    present at the exact target path while its WOAS still points at an old ID.
    That creates an infinite repair loop: spotDL skips the file, then our ID scan
    still marks it missing. We only retag when there is one exact metadata match
    and the file's current WOAS is not another tracked song in this playlist.
    """
    if not missing_ids:
        return set()

    tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
    id_to_song = {s["song_id"]: s for s in songs if s.get("song_id") in missing_ids}
    candidates_by_key = build_file_key_to_paths(folder)

    # WHY: track retagged paths so two missing IDs sharing the same normalized key
    # don't both claim the same file in one pass. The second ID stays missing and
    # falls through to metadata_collision_satisfied_ids instead.
    retagged_paths: set[pathlib.Path] = set()
    for sid in sorted(missing_ids):
        song = id_to_song.get(sid)
        if not song:
            continue
        candidates = [
            (mp3, current_id)
            for mp3, current_id in candidates_by_key.get(normalized_song_key(song), [])
            if current_id != sid
            and current_id not in tracked_ids
            and mp3 not in retagged_paths
        ]
        if len(candidates) != 1:
            continue

        mp3, old_id = candidates[0]
        if set_file_song_id(mp3, sid):
            retagged_paths.add(mp3)
            old_label = old_id or "no-WOAS"
            print(
                f"Repair: corrected playlist WOAS for {mp3.name} ({old_label} -> {sid})",
                flush=True,
            )

    disk_ids = set(build_disk_id_to_path(folder))
    return missing_ids - disk_ids


def metadata_collision_satisfied_ids(
    folder: pathlib.Path,
    missing_ids: set[str],
    songs: list,
) -> set[str]:
    """Return missing IDs represented by another tracked ID at the same metadata path."""
    if not missing_ids:
        return set()

    tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
    id_to_song = {s["song_id"]: s for s in songs if s.get("song_id") in missing_ids}
    candidates_by_key = build_file_key_to_paths(folder)
    satisfied: set[str] = set()

    for sid in sorted(missing_ids):
        song = id_to_song.get(sid)
        if not song:
            continue
        candidates = [
            current_id
            for _, current_id in candidates_by_key.get(normalized_song_key(song), [])
            if current_id != sid and current_id in tracked_ids
        ]
        if candidates:
            satisfied.add(sid)

    if satisfied:
        print(
            f"Repair: {len(satisfied)} playlist metadata collisions satisfied by existing files",
            flush=True,
        )
    return satisfied


def build_disk_id_to_path(folder: pathlib.Path) -> dict[str, pathlib.Path]:
    """Map Spotify ID -> MP3 path for files actually present in one playlist folder."""
    result: dict[str, pathlib.Path] = {}
    for mp3 in folder.rglob("*.mp3"):
        sid = song_id_from_file(mp3)
        if sid and sid not in result:
            result[sid] = mp3
    return result


def delete_files_for_ids(folder: pathlib.Path, removed_ids: set[str]) -> int:
    """Delete files in this playlist folder whose embedded Spotify ID was removed."""
    deleted = 0
    for mp3 in folder.rglob("*.mp3"):
        if song_id_from_file(mp3) in removed_ids:
            mp3.unlink()
            deleted += 1
            for parent in (mp3.parent, mp3.parent.parent):
                if (
                    parent != folder
                    and folder in parent.parents
                    and not any(parent.iterdir())
                ):
                    parent.rmdir()
    return deleted


def retry_missing_downloads(
    folder: pathlib.Path,
    missing_ids: set[str],
    output_template: str,
) -> set[str]:
    """Retry playlist files that are tracked in .spotdl but absent on disk."""
    if not missing_ids:
        return set()

    fallback_map = load_fallback_map()
    fallback_ids = missing_ids & set(fallback_map)
    primary_ids = missing_ids - fallback_ids

    if primary_ids:
        urls = [f"https://open.spotify.com/track/{sid}" for sid in sorted(primary_ids)]
        print(
            f"Repair: primary retry for {len(urls)} missing playlist files", flush=True
        )
        rc = spotdl("download", *urls, "--output", output_template, cwd=MUSIC_DIR)
        if rc != 0:
            print(f"Repair: primary retry returned rc={rc}", flush=True)

    if fallback_ids:
        print(
            f"Repair: fallback retry for {len(fallback_ids)} missing playlist files",
            flush=True,
        )
        for sid in sorted(fallback_ids):
            spotify_url = f"https://open.spotify.com/track/{sid}"
            rc = spotdl(
                "download",
                f"{fallback_map[sid]}|{spotify_url}",
                "--output",
                output_template,
                cwd=MUSIC_DIR,
            )
            if rc != 0:
                print(f"Repair: fallback failed (rc={rc}): {sid}", flush=True)

    disk_ids = set(build_disk_id_to_path(folder))
    return missing_ids - disk_ids


def indent_xml(elem: ET.Element, level: int = 0) -> None:
    """Pretty-print ElementTree output in Jellyfin's simple playlist XML shape."""
    pad = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "  "
        for child in elem:
            indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = pad
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = pad


def notify_jellyfin_refresh(jellyfin_id: str) -> None:
    """Tell Jellyfin to reload the playlist so Finamp sees the new item count immediately."""
    if not JELLYFIN_API_KEY_FILE.exists():
        print(
            f"WARNING: Jellyfin refresh skipped: key file missing ({JELLYFIN_API_KEY_FILE})",
            flush=True,
        )
        return
    try:
        api_key = JELLYFIN_API_KEY_FILE.read_text().strip()
    except OSError as exc:
        print(
            f"WARNING: Jellyfin refresh skipped: cannot read key file: {exc}",
            flush=True,
        )
        return
    if not api_key:
        print("WARNING: Jellyfin refresh skipped: key file is empty", flush=True)
        return
    url = (
        f"{JELLYFIN_URL}/Items/{jellyfin_id}/Refresh"
        f"?MetadataRefreshMode=Default"
        f"&ImageRefreshMode=Default"
        f"&ReplaceAllImages=false"
        f"&ReplaceAllMetadata=false"
    )
    try:
        req = urllib.request.Request(url, method="POST")
        req.add_header("X-MediaBrowser-Token", api_key)
        req.add_header("Content-Length", "0")
        urllib.request.urlopen(req, timeout=5)
        print(f"Jellyfin refresh triggered: {jellyfin_id}", flush=True)
    except Exception as exc:
        print(f"WARNING: Jellyfin refresh failed for {jellyfin_id}: {exc}", flush=True)


def rebuild_jellyfin_playlist(pl: dict, folder: pathlib.Path, songs: list) -> None:
    """Rewrite Jellyfin's playlist XML from actual MP3 files every sync run."""
    jellyfin_name = pl.get("jellyfin_name", pl["name"])
    xml_path = JELLYFIN_PLAYLISTS_DIR / jellyfin_name / "playlist.xml"
    if not xml_path.exists():
        print(
            f"WARNING: Jellyfin playlist XML not mounted/found: {xml_path}",
            flush=True,
        )
        return

    id_to_path = build_disk_id_to_path(folder)
    tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
    candidates_by_key = build_file_key_to_paths(folder)
    paths = []
    missing_ids = []
    collision_reused = 0
    for song in songs:
        sid = song.get("song_id")
        if not sid:
            continue
        mp3 = id_to_path.get(sid)
        if not mp3:
            candidates = [
                path
                for path, current_id in candidates_by_key.get(
                    normalized_song_key(song), []
                )
                if current_id != sid and current_id in tracked_ids
            ]
            if candidates:
                mp3 = sorted(candidates)[0]
                collision_reused += 1
        if mp3:
            rel = mp3.relative_to(MUSIC_DIR).as_posix()
            paths.append(f"{JELLYFIN_MUSIC_PREFIX}/{rel}")
        else:
            missing_ids.append(sid)

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        print(
            f"WARNING: Jellyfin playlist XML corrupt for '{jellyfin_name}', skipping rebuild: {exc}",
            flush=True,
        )
        return
    root = tree.getroot()
    old_items = root.find("PlaylistItems")
    if old_items is not None:
        root.remove(old_items)
    playlist_items = ET.SubElement(root, "PlaylistItems")
    for path in paths:
        item = ET.SubElement(playlist_items, "PlaylistItem")
        path_node = ET.SubElement(item, "Path")
        path_node.text = path

    indent_xml(root)
    tmp = xml_path.with_suffix(".tmp")
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    tmp.replace(xml_path)
    print(
        f"Jellyfin playlist '{jellyfin_name}': {len(paths)} items written"
        f" ({len(missing_ids)} tracked files missing"
        f", {collision_reused} metadata collisions reused)",
        flush=True,
    )
    jellyfin_id = pl.get("jellyfin_id", "")
    if jellyfin_id:
        notify_jellyfin_refresh(jellyfin_id)


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
    print(
        f"Current: {len(current_ids)}  Added: {len(added_ids)}  Removed: {len(removed_ids)}",
        flush=True,
    )

    # WHY: if pagination was truncated (network error, API rate limit), current_ids
    # will be far smaller than saved_ids. The MAX_DELETIONS guard protects deletion,
    # but an incomplete snapshot would also silently skip adding songs already on the
    # server. Skip the entire run rather than operate on a broken snapshot.
    if saved_ids and len(current_ids) < int(0.5 * len(saved_ids)):
        print(
            f"WARNING: snapshot too small ({len(current_ids)} vs {len(saved_ids)} saved) — "
            "possible pagination failure, skipping this run",
            flush=True,
        )
        tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
        disk_ids = set(build_disk_id_to_path(folder))
        missing_tracked_ids = tracked_ids - disk_ids
        missing_tracked_ids -= metadata_collision_satisfied_ids(
            folder, missing_tracked_ids, songs
        )
        still_missing_after_repair = repair_stale_woas_matches(
            folder, missing_tracked_ids, songs
        )
        if still_missing_after_repair:
            print(
                f"WARNING: {len(still_missing_after_repair)} tracked playlist files"
                " still missing (snapshot incomplete, skipping retry)",
                flush=True,
            )
        rebuild_jellyfin_playlist(pl, folder, songs)
        return

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
            rc = spotdl("save", *urls, "--save-file", str(batch_file), cwd=MUSIC_DIR)
            if rc != 0 or not batch_file.exists():
                print(
                    f"Batch {n}: save failed (rc={rc}), skipping — will retry next run",
                    flush=True,
                )
                batch_file.unlink(missing_ok=True)
                continue
            raw = json.loads(batch_file.read_text())
            batch_songs: list = raw if isinstance(raw, list) else raw.get("songs", [])
            known = {s["song_id"] for s in songs}
            batch_new = [s for s in batch_songs if s.get("song_id") not in known]
            songs = songs + batch_new
            batch_file.unlink(missing_ok=True)
            write_save_file(save_file, songs)

            # cwd=MUSIC_DIR so the "Playlists/{folder}/..." template lands at the
            # right path; cwd=folder would nest a second Playlists/{folder}/ inside.
            dl_rc = spotdl(
                "download", *urls, "--output", output_template, cwd=MUSIC_DIR
            )
            if dl_rc != 0:
                # WHY: Scan actual downloaded files with WOAS tags to identify which
                # songs truly landed. Only roll back IDs that have no corresponding file.
                # This prevents "one bad song" from blocking 19 good ones in the batch,
                # and avoids orphaned files that would never be cleaned up.
                actually_downloaded = set(build_disk_id_to_path(folder))

                batch_ids_set = {s["song_id"] for s in batch_new if "song_id" in s}
                failed_ids = batch_ids_set - actually_downloaded

                if failed_ids:
                    failed_ids -= metadata_collision_satisfied_ids(
                        folder, failed_ids, songs
                    )
                if failed_ids:
                    failed_ids = repair_stale_woas_matches(folder, failed_ids, songs)
                if failed_ids:
                    failed_ids = retry_missing_downloads(
                        folder, failed_ids, output_template
                    )
                if failed_ids:
                    songs = [s for s in songs if s.get("song_id") not in failed_ids]
                    write_save_file(save_file, songs)
                    print(
                        f"Batch {n}: download partial (rc={dl_rc}), "
                        f"rolled back {len(failed_ids)}/{len(batch_ids_set)} failed — will retry next run",
                        flush=True,
                    )
                else:
                    print(
                        f"Batch {n}: download reported error (rc={dl_rc}) but all files landed — continuing",
                        flush=True,
                    )

    # --- remove songs no longer in the playlist ---
    # Guard wraps both prune and delete: an implausibly large count means the
    # snapshot is broken, so we touch nothing and let the next run self-heal.
    if removed_ids:
        deletion_limit = max(MAX_DELETIONS, int(0.3 * len(saved_ids)))
        if len(removed_ids) > deletion_limit:
            print(
                f"WARNING: {len(removed_ids)} removals exceeds limit ({deletion_limit}) — "
                "snapshot likely incomplete, skipping deletion this run",
                flush=True,
            )
        else:
            songs = [s for s in songs if s.get("song_id") not in removed_ids]
            write_save_file(save_file, songs)
            deleted = delete_files_for_ids(folder, removed_ids)
            print(
                f"Removed {len(removed_ids)} from DB, deleted {deleted} files",
                flush=True,
            )

    tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
    disk_ids = set(build_disk_id_to_path(folder))
    missing_tracked_ids = tracked_ids - disk_ids
    missing_tracked_ids -= metadata_collision_satisfied_ids(
        folder, missing_tracked_ids, songs
    )
    missing_tracked_ids = repair_stale_woas_matches(folder, missing_tracked_ids, songs)
    still_missing = retry_missing_downloads(
        folder, missing_tracked_ids, output_template
    )
    if still_missing:
        print(
            f"WARNING: {len(still_missing)} tracked playlist files still missing after repair",
            flush=True,
        )

    rebuild_jellyfin_playlist(pl, folder, songs)


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
