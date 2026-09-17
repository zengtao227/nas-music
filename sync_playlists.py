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
import time
from typing import Any

import mutagen
from mutagen.id3 import WOAS
import spotapi
from playlist_discovery import (
    DiscoveryOutcome,
    update_discovery_state,
)
from shared import (
    deezer_fallback,
    jellyfin_api as _jellyfin_api,
    load_fallback_map,
    load_jellyfin_api_key as _load_jellyfin_api_key,
    load_retry_state,
    make_login,
    notify_exhausted_retries,
    record_retry_outcome,
    retry_due,
    save_retry_state,
    song_id_from_file,
    song_label,
)
from lyrics import process_changed, snapshot

MUSIC_DIR = pathlib.Path("/music")
JELLYFIN_MUSIC_PREFIX = "/media/music"
JELLYFIN_MIA_USER_ID = "9DBDBD21-920F-49E0-86B0-AC5D26D2C63B"
JELLYFIN_PLAYLIST_BATCH = 50
RETRY_STATE_FILE = MUSIC_DIR / ".playlist_retry_state.json"
OUTPUT_BASE = "Playlists"
BATCH_SIZE = 20
LIBRARY_QUERY_LIMIT = 500
MIA_SPOTIFY_OWNER_URI = "spotify:user:31ilqs7huj7wvtguhxhnjmfmlmsi"
PLAYLIST_DISCOVERY_STATE_FILE = MUSIC_DIR / ".spotify_playlist_discovery.json"

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
    {
        "id": "4rYsc7tTRe7UCGY8ajz8k1",
        "name": "calm",
        "folder": "calm",
        "jellyfin_name": "Calm",
    },
    {
        "id": "3pVbUjpOlKzbTXX77UEvnv",
        "name": "katseye_animal",
        "folder": "katseye_animal",
        "jellyfin_name": "Katseye Animal",
    },
    {
        "id": "4fC8ytJHg1fU0ZzoTswuwG",
        "name": "gamma_waves_40hz",
        "folder": "gamma_waves_40hz",
        "jellyfin_name": "Gamma Waves 40 Hz",
    },
]


def discover_new_playlists(
    login: spotapi.Login, configured_playlists: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Return persisted automatic playlists and enroll safe new candidates."""
    try:
        library: dict[str, Any] = dict(
            spotapi.PrivatePlaylist(login).get_library(LIBRARY_QUERY_LIMIT)
        )
        outcome: DiscoveryOutcome = update_discovery_state(
            PLAYLIST_DISCOVERY_STATE_FILE,
            library,
            MIA_SPOTIFY_OWNER_URI,
            configured_playlists,
        )
    except Exception as exc:
        print(
            f"WARNING: playlist discovery skipped ({type(exc).__name__}: {exc})",
            flush=True,
        )
        print(
            "Playlist discovery: fail-closed; only fixed playlists will sync "
            "and automatic playlist files will be preserved",
            flush=True,
        )
        return []

    if outcome.initialized:
        print(
            "Playlist discovery baseline initialized: "
            f"{outcome.library_playlist_count} current playlists recorded; "
            "0 existing playlists auto-enrolled",
            flush=True,
        )
    else:
        if outcome.recovered_from_backup:
            print("Playlist discovery state recovered from backup", flush=True)
        for playlist in outcome.discovered:
            print(
                "Playlist discovery: enrolled new Mia playlist "
                f"'{playlist['jellyfin_name']}' ({playlist['id']})",
                flush=True,
            )
        if outcome.ignored_new:
            print(
                f"Playlist discovery: recorded {outcome.ignored_new} new "
                "unowned/already-configured playlists without enrolling them",
                flush=True,
            )
        if outcome.deferred_unknown_owner:
            print(
                f"Playlist discovery: deferred {outcome.deferred_unknown_owner} "
                "new playlists with unknown owner until a later cycle",
                flush=True,
            )
        if outcome.inactive:
            print(
                f"Playlist discovery: {outcome.inactive} automatic playlists are "
                "not currently in Mia's library; local files were preserved",
                flush=True,
            )
        print(
            f"Playlist discovery summary: {outcome.library_playlist_count} current, "
            f"{len(outcome.playlists)} automatic active, "
            f"{len(outcome.discovered)} enrolled this cycle",
            flush=True,
        )
    return [dict(playlist) for playlist in outcome.playlists]


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
    candidates_by_key: dict[tuple[str, str, str], list[tuple[pathlib.Path, str]]],
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
    candidates_by_key: dict[tuple[str, str, str], list[tuple[pathlib.Path, str]]],
) -> set[str]:
    """Return missing IDs represented by another tracked ID at the same metadata path."""
    if not missing_ids:
        return set()

    tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
    id_to_song = {s["song_id"]: s for s in songs if s.get("song_id") in missing_ids}
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


def build_global_id_to_path() -> dict[str, pathlib.Path]:
    """Map Spotify ID -> MP3 path for files present in ANY playlist folder.

    WHY: the same song is often on more than one of Mia's playlists. Without this,
    each playlist downloads its own independent copy — same song, two files with
    slightly different encodes, which Jellyfin then presents as "alternate
    versions" of one track (the duplicate-source picker Mia sees in Finamp).
    Scanning across all playlist folders lets a playlist reuse a file another
    playlist already has instead of downloading (and storing) a second copy.
    """
    return build_disk_id_to_path(MUSIC_DIR / OUTPUT_BASE)


def build_global_tracked_ids(
    all_playlists: list[dict[str, Any]], exclude_folder: str
) -> set[str]:
    """Union of song IDs currently saved by every playlist except exclude_folder.

    Used to protect a shared file from deletion: a song removed from one
    playlist must not be deleted from disk while another playlist still tracks
    the same Spotify ID there.
    """
    ids: set[str] = set()
    for pl in all_playlists:
        if pl["folder"] == exclude_folder:
            continue
        folder = MUSIC_DIR / OUTPUT_BASE / pl["folder"]
        save_file = folder / f"{pl['folder']}.spotdl"
        _, saved_ids = load_save_file(save_file)
        ids |= saved_ids
    return ids


def delete_files_for_ids(
    folder: pathlib.Path,
    removed_ids: set[str],
    protected_ids: frozenset[str] = frozenset(),
) -> int:
    """Delete files in this playlist folder whose embedded Spotify ID was removed.

    IDs in protected_ids are skipped: another playlist still tracks that song
    and may be the one relying on this exact physical file (cross-playlist reuse).
    """
    deleted = 0
    skipped = 0
    for mp3 in folder.rglob("*.mp3"):
        sid = song_id_from_file(mp3)
        if sid not in removed_ids:
            continue
        if sid in protected_ids:
            skipped += 1
            continue
        mp3.unlink()
        deleted += 1
        for parent in (mp3.parent, mp3.parent.parent):
            if (
                parent != folder
                and folder in parent.parents
                and not any(parent.iterdir())
            ):
                parent.rmdir()
    if skipped:
        print(
            f"Kept {skipped} files still tracked by another playlist", flush=True
        )
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


def _jellyfin_find_playlist_id(jellyfin_name: str, api_key: str) -> str | None:
    """Search for a Jellyfin playlist by exact name.

    Returns the ID if found, "" if confirmed absent, None if the API call failed.
    Callers must treat None as "API error — do not create" to prevent duplicates.
    """
    path = (
        f"/Items?IncludeItemTypes=Playlist&Recursive=true&UserId={JELLYFIN_MIA_USER_ID}"
    )
    result = _jellyfin_api("GET", path, api_key)
    if result is None:
        return None
    for item in result.get("Items", []):
        if item.get("Name") == jellyfin_name:
            return str(item["Id"])
    return ""


def _jellyfin_create_playlist(jellyfin_name: str, api_key: str) -> str:
    """Create a Jellyfin playlist owned by Mia; returns the new ID or "" on failure."""
    body = {
        "Name": jellyfin_name,
        "Ids": [],
        "UserId": JELLYFIN_MIA_USER_ID,
        "MediaType": "Audio",
    }
    result = _jellyfin_api("POST", "/Playlists", api_key, body)
    return str(result["Id"]) if result and result.get("Id") else ""


def get_or_create_jellyfin_id(pl: dict, api_key: str) -> str:
    """Return the Jellyfin playlist ID for pl, creating the playlist if absent.

    Returns "" when the ID cannot be determined this run (API error, just created,
    or no api_key). The caller skips the playlist sync on "" and retries next run.
    Hardcoded jellyfin_id in pl is used as-is with zero network calls.
    """
    hardcoded = pl.get("jellyfin_id", "")
    if hardcoded:
        return hardcoded
    if not api_key:
        return ""
    jellyfin_name = pl.get("jellyfin_name", pl["name"])
    found = _jellyfin_find_playlist_id(jellyfin_name, api_key)
    if found is None:
        # API error — do NOT create (would risk duplicates on transient failures)
        print(
            f"WARNING: Jellyfin playlist lookup failed for '{jellyfin_name}',"
            " skipping rebuild this run",
            flush=True,
        )
        return ""
    if found:
        return found
    # Confirmed absent — create it; populate XML on the next run
    new_id = _jellyfin_create_playlist(jellyfin_name, api_key)
    if new_id:
        print(
            f"Jellyfin playlist '{jellyfin_name}' created ({new_id})"
            " — items will be added on next run",
            flush=True,
        )
    else:
        print(
            f"WARNING: Jellyfin playlist creation failed for '{jellyfin_name}'",
            flush=True,
        )
    return ""


def _add_playlist_items(jellyfin_id: str, item_ids: list[str], api_key: str) -> bool:
    """Append item_ids in order; the API needs the playlist owner's user ID."""
    users = _jellyfin_api("GET", "/Users", api_key) or []
    mia = JELLYFIN_MIA_USER_ID.replace("-", "").lower()
    owner_candidates = [JELLYFIN_MIA_USER_ID] + [
        u["Id"] for u in users if u.get("Id") and u["Id"].replace("-", "").lower() != mia
    ]
    batches = [
        item_ids[i : i + JELLYFIN_PLAYLIST_BATCH]
        for i in range(0, len(item_ids), JELLYFIN_PLAYLIST_BATCH)
    ]
    for owner in owner_candidates:
        path = f"/Playlists/{jellyfin_id}/Items?userId={owner}&ids="
        # WHY: the API exposes no owner to an API key, so a non-owner is expected
        # to be refused here; only the caller warns when every candidate fails.
        if _jellyfin_api("POST", path + ",".join(batches[0]), api_key, warn=False) is None:
            continue
        return all(
            _jellyfin_api("POST", path + ",".join(batch), api_key) is not None
            for batch in batches[1:]
        )
    return False


def sync_jellyfin_playlist_items(
    jellyfin_id: str, jellyfin_name: str, desired_paths: list[str], api_key: str
) -> None:
    """Make a Jellyfin playlist's entries equal desired_paths, in order.

    WHY: since Jellyfin 12 the database is the source of truth for server-managed
    playlists and playlist.xml is no longer read, so entries are changed through
    the Playlists API. Nothing is touched when the playlist already matches.
    """
    current = _jellyfin_api(
        "GET",
        f"/Playlists/{jellyfin_id}/Items?UserId={JELLYFIN_MIA_USER_ID}&Fields=Path",
        api_key,
    )
    if current is None:
        return
    current_items = current.get("Items", [])
    if [item.get("Path") for item in current_items] == desired_paths:
        return

    library = _jellyfin_api(
        "GET",
        "/Items?IncludeItemTypes=Audio&Recursive=true"
        f"&UserId={JELLYFIN_MIA_USER_ID}&Fields=Path",
        api_key,
    )
    if library is None:
        return
    id_by_path = {
        item["Path"]: item["Id"]
        for item in library.get("Items", [])
        if item.get("Path") and item.get("Id")
    }
    desired_ids = [id_by_path[p] for p in desired_paths if p in id_by_path]
    not_indexed = len(desired_paths) - len(desired_ids)
    # WHY: never empty a playlist just because Jellyfin has not indexed the files
    # yet (e.g. during a library scan); new downloads are added on a later run.
    if not_indexed * 2 > len(desired_paths):
        print(
            f"WARNING: Jellyfin playlist '{jellyfin_name}': {not_indexed}/"
            f"{len(desired_paths)} files not indexed yet, skipping update",
            flush=True,
        )
        return
    if [item.get("Id") for item in current_items] == desired_ids:
        print(
            f"Jellyfin playlist '{jellyfin_name}': waiting for {not_indexed}"
            " new files to be indexed",
            flush=True,
        )
        return

    # WHY: entry IDs equal item IDs, so re-adding before removing would let the
    # removal delete the new entries too; remove first, then add in order.
    entry_ids = [item.get("PlaylistItemId") or item["Id"] for item in current_items]
    for i in range(0, len(entry_ids), JELLYFIN_PLAYLIST_BATCH):
        batch = ",".join(entry_ids[i : i + JELLYFIN_PLAYLIST_BATCH])
        path = f"/Playlists/{jellyfin_id}/Items?entryIds={batch}"
        if _jellyfin_api("DELETE", path, api_key) is None:
            return
    if desired_ids and not _add_playlist_items(jellyfin_id, desired_ids, api_key):
        print(
            f"WARNING: Jellyfin playlist '{jellyfin_name}': adding items failed,"
            " will retry next run",
            flush=True,
        )
        return
    after = _jellyfin_api(
        "GET", f"/Playlists/{jellyfin_id}/Items?UserId={JELLYFIN_MIA_USER_ID}", api_key
    )
    after_ids = [item.get("Id") for item in (after or {}).get("Items", [])]
    if after_ids != desired_ids:
        print(
            f"WARNING: Jellyfin playlist '{jellyfin_name}': read-back has"
            f" {len(after_ids)} items, expected {len(desired_ids)}; will retry next run",
            flush=True,
        )
        return
    print(
        f"Jellyfin playlist '{jellyfin_name}' updated:"
        f" {len(current_items)} -> {len(desired_ids)} items",
        flush=True,
    )


def rebuild_jellyfin_playlist(
    pl: dict,
    folder: pathlib.Path,
    songs: list,
    api_key: str,
    candidates_by_key: dict[tuple[str, str, str], list[tuple[pathlib.Path, str]]],
    global_id_to_path: dict[str, pathlib.Path] | None = None,
) -> None:
    """Sync the Jellyfin playlist to the tracked songs' actual MP3 files."""
    jellyfin_name = pl.get("jellyfin_name", pl["name"])
    id_to_path = build_disk_id_to_path(folder)
    global_id_to_path = global_id_to_path or {}
    tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
    paths = []
    missing_ids = []
    collision_reused = 0
    cross_playlist_reused = 0
    for song in songs:
        sid = song.get("song_id")
        if not sid:
            continue
        mp3 = id_to_path.get(sid)
        if not mp3 and sid in global_id_to_path:
            mp3 = global_id_to_path[sid]
            cross_playlist_reused += 1
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

    print(
        f"Jellyfin playlist '{jellyfin_name}': {len(paths)} items on disk"
        f" ({len(missing_ids)} tracked files missing"
        f", {collision_reused} metadata collisions reused"
        f", {cross_playlist_reused} cross-playlist files reused)",
        flush=True,
    )
    jellyfin_id = pl.get("jellyfin_id", "")
    if jellyfin_id and api_key:
        sync_jellyfin_playlist_items(jellyfin_id, jellyfin_name, paths, api_key)


def sync_playlist(
    login: spotapi.Login,
    pl: dict,
    api_key: str,
    all_playlists: list[dict[str, Any]],
) -> bool:
    """Sync one playlist; returns True if new songs were downloaded this run."""
    downloaded_new = False
    folder = MUSIC_DIR / OUTPUT_BASE / pl["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        lyrics_before = snapshot(folder)
    except Exception as exc:
        print(
            f"WARNING: lyrics snapshot unavailable ({type(exc).__name__})", flush=True
        )
        lyrics_before = None
    save_file = folder / f"{pl['folder']}.spotdl"
    batch_file = folder / f"{pl['folder']}.batch.spotdl"
    output_template = f"{OUTPUT_BASE}/{pl['folder']}/{{artists}}/{{album}}/{{title}}"

    jid = get_or_create_jellyfin_id(pl, api_key)
    if jid:
        pl["jellyfin_id"] = jid

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
        global_id_to_path = build_global_id_to_path()
        cross_reused = missing_tracked_ids & set(global_id_to_path)
        if cross_reused:
            print(
                f"Reuse: {len(cross_reused)} files already downloaded by another playlist",
                flush=True,
            )
        missing_tracked_ids -= cross_reused
        candidates_by_key = build_file_key_to_paths(folder)
        missing_tracked_ids -= metadata_collision_satisfied_ids(
            folder, missing_tracked_ids, songs, candidates_by_key
        )
        still_missing_after_repair = repair_stale_woas_matches(
            folder, missing_tracked_ids, songs, candidates_by_key
        )
        if still_missing_after_repair:
            print(
                f"WARNING: {len(still_missing_after_repair)} tracked playlist files"
                " still missing (snapshot incomplete, skipping retry)",
                flush=True,
            )
        if jid:
            rebuild_jellyfin_playlist(
                pl,
                folder,
                songs,
                api_key,
                build_file_key_to_paths(folder),
                global_id_to_path,
            )
        try:
            process_changed(lyrics_before, snapshot(folder))
        except Exception as exc:
            print(
                f"WARNING: lyrics processing failed ({type(exc).__name__})", flush=True
            )
        return False

    # --- add new songs ---
    # WHY: failed new songs are rolled back out of the .spotdl and reappear as
    # "added" next run, so they share the repair path's retry cooldown.
    add_retry_state = load_retry_state(RETRY_STATE_FILE)
    add_now = time.time()
    add_cooling = {
        sid
        for sid in added_ids
        if not retry_due(add_retry_state, f"{folder.name}:{sid}", add_now)
    }
    if add_cooling:
        print(
            f"Added: {len(add_cooling)} previously failed songs in retry cooldown, skipped",
            flush=True,
        )
        added_ids = added_ids - add_cooling
    if added_ids:
        global_id_to_path = build_global_id_to_path()
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

            # WHY: a song already downloaded by another playlist doesn't need a
            # second copy — skip it in the download call, but still track its
            # metadata above so this playlist's .spotdl/XML resolve it correctly.
            reused_ids = {sid for sid in batch_ids if sid in global_id_to_path}
            download_ids = [sid for sid in batch_ids if sid not in reused_ids]
            if reused_ids:
                print(
                    f"Batch {n}: {len(reused_ids)} already downloaded by another"
                    " playlist, reusing",
                    flush=True,
                )

            # cwd=MUSIC_DIR so the "Playlists/{folder}/..." template lands at the
            # right path; cwd=folder would nest a second Playlists/{folder}/ inside.
            if download_ids:
                dl_urls = [
                    f"https://open.spotify.com/track/{sid}" for sid in download_ids
                ]
                dl_rc = spotdl(
                    "download", *dl_urls, "--output", output_template, cwd=MUSIC_DIR
                )
                downloaded_new = True
            else:
                dl_rc = 0
            if dl_rc != 0:
                # WHY: Scan actual downloaded files with WOAS tags to identify which
                # songs truly landed. Only roll back IDs that have no corresponding file.
                # This prevents "one bad song" from blocking 19 good ones in the batch,
                # and avoids orphaned files that would never be cleaned up.
                actually_downloaded = set(build_disk_id_to_path(folder)) | reused_ids

                batch_ids_set = {s["song_id"] for s in batch_new if "song_id" in s}
                failed_ids = batch_ids_set - actually_downloaded

                if failed_ids:
                    _cbk = build_file_key_to_paths(folder)
                    failed_ids -= metadata_collision_satisfied_ids(
                        folder, failed_ids, songs, _cbk
                    )
                    if failed_ids:
                        failed_ids = repair_stale_woas_matches(
                            folder, failed_ids, songs, _cbk
                        )
                if failed_ids:
                    failed_ids = retry_missing_downloads(
                        folder, failed_ids, output_template
                    )
                record_retry_outcome(
                    add_retry_state,
                    {f"{folder.name}:{sid}" for sid in batch_ids_set},
                    {f"{folder.name}:{sid}" for sid in failed_ids},
                    add_now,
                )
                notify_exhausted_retries(
                    add_retry_state,
                    {
                        f"{folder.name}:{s['song_id']}": song_label(s)
                        for s in batch_new
                        if s.get("song_id") in failed_ids
                    },
                    f"歌单 {pl.get('jellyfin_name', pl['name'])}",
                )
                save_retry_state(RETRY_STATE_FILE, add_retry_state)
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
            protected_ids = frozenset(
                removed_ids & build_global_tracked_ids(all_playlists, pl["folder"])
            )
            deleted = delete_files_for_ids(folder, removed_ids, protected_ids)
            print(
                f"Removed {len(removed_ids)} from DB, deleted {deleted} files",
                flush=True,
            )

    tracked_ids = {s.get("song_id") for s in songs if s.get("song_id")}
    disk_ids = set(build_disk_id_to_path(folder))
    missing_tracked_ids = tracked_ids - disk_ids
    global_id_to_path = build_global_id_to_path()
    cross_reused = missing_tracked_ids & set(global_id_to_path)
    if cross_reused:
        print(
            f"Reuse: {len(cross_reused)} files already downloaded by another playlist",
            flush=True,
        )
    missing_tracked_ids -= cross_reused
    candidates_by_key = build_file_key_to_paths(folder)
    missing_tracked_ids -= metadata_collision_satisfied_ids(
        folder, missing_tracked_ids, songs, candidates_by_key
    )
    missing_tracked_ids = repair_stale_woas_matches(
        folder, missing_tracked_ids, songs, candidates_by_key
    )
    retry_state = load_retry_state(RETRY_STATE_FILE)
    now = time.time()
    due_ids = {
        sid
        for sid in missing_tracked_ids
        if retry_due(retry_state, f"{folder.name}:{sid}", now)
    }
    cooling = len(missing_tracked_ids) - len(due_ids)
    if cooling:
        print(
            f"Repair: {cooling} missing files in retry cooldown, skipped this run",
            flush=True,
        )
    still_missing = retry_missing_downloads(folder, due_ids, output_template)
    if due_ids - still_missing:
        downloaded_new = True
    if still_missing:
        # Deezer fallback — last resort for tracks absent from YouTube entirely.
        id_to_meta = {
            s["song_id"]: (s.get("artist", ""), s.get("name", ""))
            for s in songs
            if "song_id" in s
        }
        for sid in sorted(still_missing):
            artist, name = id_to_meta.get(sid, ("", ""))
            print(
                f"  Deezer fallback: trying '{artist} - {name}' ({sid})",
                flush=True,
            )
            if deezer_fallback(sid, artist, name, folder):
                print(f"  ✅ Deezer fallback SUCCESS: {sid}", flush=True)
                still_missing.discard(sid)
                downloaded_new = True
            else:
                print(f"  ❌ Deezer fallback FAILED: {sid}", flush=True)

    record_retry_outcome(
        retry_state,
        {f"{folder.name}:{sid}" for sid in due_ids},
        {f"{folder.name}:{sid}" for sid in still_missing},
        now,
    )
    id_to_label = {s["song_id"]: song_label(s) for s in songs if "song_id" in s}
    notify_exhausted_retries(
        retry_state,
        {
            f"{folder.name}:{sid}": id_to_label.get(sid, sid)
            for sid in missing_tracked_ids
        },
        f"歌单 {pl.get('jellyfin_name', pl['name'])}",
    )
    save_retry_state(RETRY_STATE_FILE, retry_state)
    still_missing |= missing_tracked_ids - due_ids

    if still_missing:
        print(
            f"WARNING: {len(still_missing)} tracked playlist files still missing after repair",
            flush=True,
        )

    if jid:
        rebuild_jellyfin_playlist(
            pl,
            folder,
            songs,
            api_key,
            build_file_key_to_paths(folder),
            build_global_id_to_path(),
        )
    try:
        process_changed(lyrics_before, snapshot(folder))
    except Exception as exc:
        print(f"WARNING: lyrics processing failed ({type(exc).__name__})", flush=True)
    return downloaded_new


def main() -> None:
    print("=== Playlist Sync ===", flush=True)

    api_key = _load_jellyfin_api_key()

    login = make_login()
    playlists: list[dict[str, Any]] = [dict(playlist) for playlist in PLAYLISTS]
    playlists.extend(discover_new_playlists(login, playlists))

    downloaded_new = False
    for pl in playlists:
        try:
            if sync_playlist(login, pl, api_key, playlists):
                downloaded_new = True
        except Exception as exc:
            # WHY: one removed or temporarily unavailable discovered playlist must
            # not prevent the remaining configured playlists from synchronizing.
            print(
                f"WARNING: playlist '{pl['name']}' sync failed "
                f"({type(exc).__name__}: {exc})",
                flush=True,
            )

    # WHY (2026-09-17): Jellyfin's RealtimeMonitor debounces refreshes per watched
    # folder. A playlist sync spanning many minutes across several playlist
    # subfolders keeps resetting that debounce timer, so the automatic scan never
    # gets a quiet gap to run — confirmed via Jellyfin logs when a 94-song first
    # sync left files undiscovered for over an hour. Trigger one explicit scan
    # whenever this run actually downloaded something, instead of relying on the
    # 12-hour scheduled scan or a debounce window that may never open.
    if downloaded_new:
        if _jellyfin_api("POST", "/Library/Refresh", api_key) is not None:
            print("Triggered Jellyfin library refresh (new songs downloaded)", flush=True)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
