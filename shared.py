#!/usr/bin/env python3
"""Shared utilities for sync_liked.py, sync_playlists.py, and rebuild_liked_save.py.

Place this file in /music/ alongside the sync scripts so Docker's working-directory
import path resolves correctly when scripts run as:
    python3 /music/sync_liked.py
"""

import datetime
import json
import pathlib
import subprocess
import urllib.parse
import urllib.request

import mutagen
import mutagen.id3
import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
DEEZER_ARL_FILE = MUSIC_DIR / ".deezer_arl"
FALLBACK_MAP_FILE = MUSIC_DIR / "youtube_fallback_cache.json"


def make_login() -> spotapi.Login:
    """Construct an authenticated spotapi Login from the on-disk sp_dc cookie."""
    sp_dc = SP_DC_FILE.read_text().strip()
    cfg = spotapi.Config(logger=spotapi.NoopLogger())
    dump = {"identifier": "mia", "password": "", "cookies": {"sp_dc": sp_dc}}
    return spotapi.Login.from_cookies(dump, cfg)


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


def load_fallback_map() -> dict[str, str]:
    """Return spotify_id → youtube_url, filtered by source trust rules.

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


def deezer_fallback(
    spotify_id: str, artist: str, title: str, base_dir: pathlib.Path
) -> bool:
    """Search Deezer and download 128 kbps MP3 via streamrip.

    base_dir is the streamrip working directory and the root of the rglob search
    for the newly landed MP3.  Returns True if the file was downloaded and had its
    WOAS tag written so that scan_local_spotify_ids() will recognise it.
    """
    if not DEEZER_ARL_FILE.exists():
        return False

    query = f"{artist} {title}".strip()
    if not query:
        return False

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
    print(
        f"    Deezer: found '{track['artist']['name']} - {track['title']}' ({deezer_url})",
        flush=True,
    )

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
        text = text.replace('folder = "/root/StreamripDownloads"', 'folder = "."')
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

    rc = subprocess.run(
        ["rip", "url", deezer_url],
        cwd=str(base_dir),
        capture_output=True,
        text=True,
        timeout=120,
    ).returncode
    if rc != 0:
        print(f"    Deezer download FAILED (rc={rc})", flush=True)
        return False

    # WHY: streamrip's default path template creates Artist/Album/Track.mp3 inside
    # base_dir, so rglob is needed — glob("*.mp3") would miss subdirectory files.
    mp3s = sorted(
        base_dir.rglob("*.mp3"),
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
            pass
    return True
