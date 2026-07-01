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

import datetime
import json
import pathlib
import subprocess
import time
from collections import defaultdict
from typing import Any

import mutagen
import spotapi

MUSIC_DIR = pathlib.Path("/music")
PLAYLISTS_DIR = MUSIC_DIR / "Playlists"
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
DOWNLOADS_FILE = MUSIC_DIR / "downloads.spotdl"
BATCH_FILE = MUSIC_DIR / "liked_batch.spotdl"
FALLBACK_MAP_FILE = MUSIC_DIR / "youtube_fallback_cache.json"
MISSING_IDS_FILE = MUSIC_DIR / "missing_ids.json"
COLLISION_AUDIT_FILE = MUSIC_DIR / ".collision_audit.jsonl"
DEEZER_ARL_FILE = MUSIC_DIR / ".deezer_arl"
OUTPUT_TEMPLATE = "{artists}/{album}/{title}"
BATCH_SIZE = 50

MAX_DELETIONS = 30


def spotdl(*args: str) -> int:
    return subprocess.run(["spotdl", *args], cwd=str(MUSIC_DIR)).returncode


def spotdl_save_with_retry(*args: str, retries: int = 2, backoff: int = 30) -> int:
    for attempt in range(retries + 1):
        rc = spotdl(*args)
        if rc == 0:
            return 0
        if attempt < retries:
            print(
                f"spotdl save rc={rc}, retrying in {backoff}s ({attempt + 1}/{retries})",
                flush=True,
            )
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

    def tag_str(tags: Any, key: str) -> str:
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


# ─── COLLISION RESOLUTION CONTRACT ───────────────────────────────────────────
# Three invariants that ALL collision-related code MUST uphold.
# Breaking any one silently corrupts convergence guarantees.
#
# I1. IDENTITY
#     WOAS tag == canonical Spotify Track ID for a file.
#     A file is "satisfied" only when its WOAS ∈ liked_ids_all.
#
# I2. DETERMINISTIC OWNERSHIP
#     For any collision group (2+ liked IDs → same {artist}/{album}/{title}),
#     canonical owner = min(sorted sibling IDs).
#     This rule MUST NOT depend on filesystem state, cache order, or timing.
#     Corollary: replay with the same liked.spotdl always yields the same owner.
#
# I3. NON-OWNER SUPPRESSION
#     Non-owner IDs are NEVER added to the fallback download queue.
#     Non-owner IDs are ALWAYS marked collision-satisfied in missing_ids.json.
#     A non-owner file on disk is a transitional artifact; cleanup removes it.
#
# Functions in scope: build_collision_groups, cleanup_stale_conflicts,
#                     resolve_path_collisions, and the fallback section of main.
# Adding or changing any of these four touch-points requires re-verifying I1–I3.
# ─────────────────────────────────────────────────────────────────────────────


def build_collision_groups(songs: list[dict]) -> dict[str, list[str]]:
    """Map stable path key → sorted sibling IDs for all collision groups.

    A collision group exists when 2+ liked songs share the same deterministic
    filesystem path (same artist/album/title metadata).  The key is normalised
    (lowercase, stripped) for stability across minor metadata variations.
    Sorting IDs ensures [0] is always the canonical owner on every replay.
    """
    path_map: defaultdict[str, list[str]] = defaultdict(list)
    for s in songs:
        if "song_id" not in s:
            continue
        artists = s.get("artists") or []
        artist_str = ", ".join(artists) if artists else (s.get("artist") or "")
        album = (s.get("album_name") or "").strip()
        title = (s.get("name") or "").strip()
        if not (artist_str and title):
            continue
        key = f"{artist_str.lower().strip()}::{album.lower()}::{title.lower()}"
        path_map[key].append(s["song_id"])
    return {k: sorted(v) for k, v in path_map.items() if len(v) > 1}


def cleanup_stale_conflicts(
    missing_ids: set[str],
    songs: list[dict],
    liked_ids_all: set[str],
    id_to_canonical_owner: dict[str, str],
) -> None:
    """Remove stale-WOAS placeholder files that would cause spotdl to skip a download.

    WHY: spotdl's skip-existing check is filename-based, not Spotify-ID-based.
    When a track's Spotify ID changes (re-release / label re-upload), the old
    file keeps its old WOAS tag and occupies the filename spotdl would write for
    the new ID.  spotdl sees the file → skips → new ID never lands → infinite
    cron loop.

    Ownership rule (deterministic):
    - Canonical owner of a collision group = min(sorted sibling IDs).
    - A file whose WOAS IS the canonical owner → keep (correct long-term file).
    - A file whose WOAS is a non-owner sibling → delete (let owner replace it).
    - A file whose WOAS is a unique liked song (no collision group) → keep.
    - A file whose WOAS is not liked → delete (true stale).
    """
    id_to_meta: dict[str, dict] = {s["song_id"]: s for s in songs if "song_id" in s}

    for sid in missing_ids:
        meta = id_to_meta.get(sid)
        if not meta:
            continue

        artists = meta.get("artists") or []
        artist_str = ", ".join(artists) if artists else (meta.get("artist") or "")
        album = meta.get("album_name") or ""
        title = meta.get("name") or ""
        if not (artist_str and title):
            continue

        expected: pathlib.Path = MUSIC_DIR / artist_str / album / f"{title}.mp3"
        if not expected.exists():
            continue

        try:
            tags = mutagen.File(expected)
            if not tags:
                continue
            woas = tags.get("WOAS")
            current_id = str(woas).rstrip("/").split("/")[-1] if woas else None
            if not (current_id and current_id != sid):
                continue

            canonical_of_current = id_to_canonical_owner.get(current_id)
            is_canonical_owner = canonical_of_current == current_id
            # Unique liked song: in liked_ids_all but has no collision group.
            is_unique_liked = (
                current_id in liked_ids_all and current_id not in id_to_canonical_owner
            )

            if is_canonical_owner or is_unique_liked:
                continue

            label = "non-owner sibling" if current_id in liked_ids_all else "stale"
            print(
                f"[CLEAN] {label} removed: {expected.name}"
                f" (WOAS={current_id}, want={sid})",
                flush=True,
            )
            expected.unlink()
        except Exception:
            continue


def _log_collision_decision(
    group: str, owner_id: str, competing_ids: list[str], reason: str
) -> None:
    record = {
        "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "group": group,
        "owner_id": owner_id,
        "competing_ids": competing_ids,
        "reason": reason,
    }
    try:
        with COLLISION_AUDIT_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def resolve_path_collisions(missing_ids: set[str], songs: list[dict]) -> set[str]:
    """Return non-owner IDs that are always collision-satisfied (never downloaded).

    Ownership is deterministic: min(sorted sibling IDs) is the canonical owner.
    Non-owner IDs are satisfied regardless of filesystem state, so behaviour is
    identical on every replay.  Each decision is appended to COLLISION_AUDIT_FILE.
    """
    collision_groups = build_collision_groups(songs)
    non_owner_ids: set[str] = {
        sid for ids in collision_groups.values() for sid in ids[1:]
    }
    satisfied = missing_ids & non_owner_ids

    for group_key, group_ids in collision_groups.items():
        owner = group_ids[0]
        competing = sorted(set(group_ids[1:]) & missing_ids)
        if competing:
            _log_collision_decision(group_key, owner, competing, "deterministic_min_id")
            title = group_key.split("::")[-1]
            for sid in competing:
                print(
                    f"[COLLISION] {title} ({sid}) non-owner, canonical={owner}",
                    flush=True,
                )

    return satisfied


def get_liked_ids() -> tuple[set[str], bool]:
    """Fetch liked song IDs from Spotify.

    Returns (ids, total_count_absent) where total_count_absent=True means Spotify
    did not include totalCount in the response (caller should apply heuristic guard).
    """
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
        print(
            "WARNING: Spotify did not return totalCount — heuristic guard active",
            flush=True,
        )
    elif len(ids) < declared_total:
        raise RuntimeError(
            f"Incomplete Spotify fetch: got {len(ids)}, declared {declared_total} "
            "— aborting to prevent spurious deletions"
        )
    return ids, declared_total is None


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
    """Returns spotify_id → youtube_url, filtered by source trust rules.

    Trust rules (all five must be considered before accepting an entry):
    1. verified=false          → reject always, regardless of source
    2. source=manual           → accept if verified absent or true
    3. source=auto, new format → accept if confidence >= 0.35 AND resolved_at < 90 days
    4. source=auto, old format → accept if verified=true (legacy allowlist)
    5. source=auto, old format, no verified=true → reject
    """
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
        if entry.get("verified") is False:  # rule 1: explicit false always rejects
            continue
        source = entry.get("source", "manual")
        if source == "manual":
            result[sid] = entry["youtube_url"]  # rule 2
            continue
        # auto source
        has_new_fields = "confidence" in entry and bool(entry.get("resolved_at"))
        if has_new_fields:
            # rule 3: new resolver format — apply confidence + TTL
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
            result[sid] = entry["youtube_url"]  # rule 4: old auto + verified=true
        # rule 5: old auto, no confidence/resolved_at, no verified=true → skip
    return result


def deezer_fallback(spotify_id: str, artist: str, title: str) -> bool:
    """Search Deezer API and download 128kbps MP3 via streamrip.

    Called as a last-resort fallback when both spotDL (YT Music) and
    yt-dlp (YouTube web) have failed.  Returns True if a usable mp3
    landed on disk, False otherwise.
    """
    import urllib.request
    import urllib.parse

    if not DEEZER_ARL_FILE.exists():
        return False

    query = f"{artist} {title}".strip()
    if not query:
        return False

    # 1. Search Deezer public API (free, no auth required)
    api_url = f"https://api.deezer.com/search?q={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(api_url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"    Deezer API error: {exc}", flush=True)
        return False

    tracks = data.get("data", [])
    if not tracks:
        print(f"    Deezer: no results for '{query}'", flush=True)
        return False

    track = tracks[0]
    deezer_url: str = track["link"]
    deezer_title: str = track["title"]
    deezer_artist: str = track["artist"]["name"]
    print(
        f"    Deezer: found '{deezer_artist} - {deezer_title}' ({deezer_url})",
        flush=True,
    )

    # 2. Bootstrap streamrip config: generate defaults, inject ARL + settings
    arl = DEEZER_ARL_FILE.read_text().strip()
    config_path = pathlib.Path("/root/.config/streamrip/config.toml")
    subprocess.run(
        ["rip", "config", "reset"],
        input=b"y\n",
        capture_output=True,
        timeout=10,
    )
    if config_path.exists():
        text = config_path.read_text()
        text = text.replace('arl = ""', f'arl = "{arl}"')
        text = text.replace(
            'folder = "/root/StreamripDownloads"', 'folder = "."'
        )
        # Set deezer quality to 0 (128 kbps MP3, free-tier); skip comment lines
        lines = text.split("\n")
        in_deezer = False
        for i, line in enumerate(lines):
            if line == "[deezer]":
                in_deezer = True
            elif in_deezer and line.startswith("["):
                break
            elif in_deezer and "quality" in line and not line.strip().startswith("#"):
                lines[i] = line.replace("quality = 2", "quality = 0")
                break
        config_path.write_text("\n".join(lines))

    # 3. Download via streamrip (128 kbps MP3, free-tier quality)
    rc = subprocess.run(
        ["rip", "url", deezer_url],
        cwd=str(MUSIC_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    ).returncode
    if rc != 0:
        print(f"    Deezer download FAILED (rc={rc})", flush=True)
        return False

    # 4. Write Spotify ID as WOAS tag so scan_local_spotify_ids() finds it
    mp3s = sorted(
        (p for p in MUSIC_DIR.glob("*.mp3")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if mp3s:
        try:
            tags = mutagen.File(mp3s[0])
            if tags:
                tags["WOAS"] = mutagen.id3.WOAS(
                    encoding=3, url=f"https://open.spotify.com/track/{spotify_id}"
                )
                tags.save()
        except Exception:
            pass  # file is still playable without the tag

    return True


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
                if (
                    parent != MUSIC_DIR
                    and parent.is_dir()
                    and not any(parent.iterdir())
                ):
                    parent.rmdir()
    return deleted


def main() -> None:
    print("=== Liked Songs Sync ===", flush=True)

    try:
        current_ids, total_count_absent = get_liked_ids()
    except RuntimeError as exc:
        print(f"ABORT: {exc}", flush=True)
        return
    print(f"Spotify liked: {len(current_ids)}", flush=True)

    songs, saved_ids = load_save_file()
    # WHY: only apply heuristic guard when totalCount was absent.  When Spotify
    # returned totalCount, get_liked_ids() already verified completeness via the
    # declared count — applying a second 80% check would reject legitimate large
    # unlikes (e.g. Mia removes 25% of her library in one go).
    if (
        total_count_absent
        and saved_ids
        and len(current_ids) < int(0.8 * len(saved_ids))
    ):
        print(
            f"WARNING: liked snapshot too small ({len(current_ids)} vs {len(saved_ids)} saved) — "
            "possible pagination failure, aborting",
            flush=True,
        )
        return
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
                print(
                    f"Disk-path: {len(new_from_disk)} songs from ID3 tags (0 API calls)",
                    flush=True,
                )
            added_ids -= set(disk_hits.keys())

        # --- path 2: downloads.spotdl fast-path (copy metadata, zero API calls) ---
        # WHY: We can reuse metadata from downloads.spotdl, BUT we must verify the
        # file actually exists in the root directory before skipping download. An ID
        # in downloads.spotdl might come from a playlist download or be a stale entry.
        if added_ids:
            downloads_map = load_downloads_map()
            fast_ids_candidates = added_ids & set(downloads_map.keys())
            # Only use downloads.spotdl metadata if the file ALSO exists on disk in root
            fast_ids = fast_ids_candidates & set(id_to_path.keys())
            if fast_ids:
                known = {s["song_id"] for s in songs}
                new_from_downloads = [
                    downloads_map[sid] for sid in fast_ids if sid not in known
                ]
                if new_from_downloads:
                    songs = songs + new_from_downloads
                    write_save_file(songs)
                    print(
                        f"DL-path: {len(new_from_downloads)} songs from downloads.spotdl (0 API calls)",
                        flush=True,
                    )
                added_ids -= fast_ids

            # IDs in downloads.spotdl but not on disk still need download
            stale_in_global = fast_ids_candidates - fast_ids
            if stale_in_global:
                print(
                    f"DL-path: {len(stale_in_global)} IDs in downloads.spotdl but file missing, will download",
                    flush=True,
                )

        # --- path 3: API path — truly new songs not on disk or in downloads.spotdl ---
        if added_ids:
            # local_ids reused from id_to_path scan (already done above)
            local_ids = set(id_to_path.keys())
            id_list = list(added_ids)
            total = (len(id_list) + BATCH_SIZE - 1) // BATCH_SIZE
            print(
                f"API-path: {len(added_ids)} songs need Spotify metadata ({total} batches)",
                flush=True,
            )

            for i in range(0, len(id_list), BATCH_SIZE):
                batch_ids = id_list[i : i + BATCH_SIZE]
                n = i // BATCH_SIZE + 1
                batch_missing_ids = [sid for sid in batch_ids if sid not in local_ids]
                print(
                    f"Batch {n}/{total}: {len(batch_ids)} songs, missing {len(batch_missing_ids)}",
                    flush=True,
                )

                urls_all = [
                    f"https://open.spotify.com/track/{sid}" for sid in batch_ids
                ]
                BATCH_FILE.unlink(missing_ok=True)
                rc = spotdl_save_with_retry(
                    "save", *urls_all, "--save-file", str(BATCH_FILE)
                )
                if rc != 0 or not BATCH_FILE.exists():
                    print(f"Batch {n}: save failed (rc={rc}), skipping", flush=True)
                    BATCH_FILE.unlink(missing_ok=True)
                    continue

                songs = merge_batch_file(songs)
                write_save_file(songs)

                if batch_missing_ids:
                    urls_missing = [
                        f"https://open.spotify.com/track/{sid}"
                        for sid in batch_missing_ids
                    ]
                    spotdl("download", *urls_missing, "--output", OUTPUT_TEMPLATE)
                else:
                    print(
                        f"Batch {n}: all already on disk, download skipped", flush=True
                    )

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

    # Build collision groups once — shared by cleanup and fallback filter.
    # id_to_canonical_owner: every ID in a collision group → its canonical owner (min sorted ID).
    collision_groups = build_collision_groups(songs)
    id_to_canonical_owner: dict[str, str] = {
        sid: ids[0] for ids in collision_groups.values() for sid in ids
    }
    non_owner_ids: set[str] = {
        sid for ids in collision_groups.values() for sid in ids[1:]
    }

    # WHY: remove stale-WOAS placeholders that cause spotdl to skip downloads.
    # With deterministic ownership, non-owner siblings are also removed to let
    # the canonical owner's file land at the shared path.
    cleanup_stale_conflicts(missing_ids, songs, liked_ids_all, id_to_canonical_owner)

    fallback_map = load_fallback_map()
    # Non-owners are never downloaded — canonical owner represents the entire group.
    download_candidates = missing_ids - non_owner_ids
    resolved = {
        sid: fallback_map[sid] for sid in download_candidates if sid in fallback_map
    }

    if resolved:
        print(
            f"Fallback: {len(resolved)} pre-resolved URLs found, attempting hybrid download",
            flush=True,
        )
        for sid, yt_url in resolved.items():
            spotify_url = f"https://open.spotify.com/track/{sid}"
            rc = spotdl(
                "download", f"{yt_url}|{spotify_url}", "--output", OUTPUT_TEMPLATE
            )
            if rc != 0:
                print(f"  ⚠️  Fallback FAILED (rc={rc}): {sid} — {yt_url}", flush=True)

    # Retry: direct spotdl download for songs without a cached fallback URL.
    # Transient failures (Spotify metadata None, YT Music rate-limit, etc.) often
    # resolve on the next attempt — don't leave them stuck in missing_ids.json.
    retry_candidates = download_candidates - set(resolved)
    if retry_candidates:
        print(
            f"Retry: {len(retry_candidates)} missing songs without cache,"
            " re-running spotdl download",
            flush=True,
        )
        urls = [
            f"https://open.spotify.com/track/{sid}"
            for sid in sorted(retry_candidates)
        ]
        spotdl("download", *urls, "--output", OUTPUT_TEMPLATE)

        # Deezer fallback — last resort when both YT Music and YouTube web fail.
        # Some tracks (e.g. Calluna by Quiescente) simply don't exist on YouTube
        # but are available on Deezer at 128 kbps.
        after_retry = scan_local_spotify_ids()
        still_missing = retry_candidates - after_retry
        if still_missing:
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
                if deezer_fallback(sid, artist, name):
                    print(f"  ✅ Deezer fallback SUCCESS: {sid}", flush=True)
                else:
                    print(f"  ❌ Deezer fallback FAILED: {sid}", flush=True)

    # Write missing_ids.json snapshot (still-missing after all paths)
    local_ids_final = scan_local_spotify_ids()
    truly_missing = liked_ids_all - local_ids_final
    collision_satisfied = resolve_path_collisions(truly_missing, songs)
    still_missing = list(truly_missing - collision_satisfied)
    if collision_satisfied:
        print(
            f"Collision-satisfied: {len(collision_satisfied)} IDs covered by canonical owners",
            flush=True,
        )
    tmp = MISSING_IDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(still_missing))
    tmp.replace(MISSING_IDS_FILE)
    print(
        f"missing_ids.json: {len(still_missing)} unresolved songs written", flush=True
    )

    print("Done.", flush=True)


if __name__ == "__main__":
    main()
