#!/usr/bin/env python3
"""
fallback_download.py — YouTube Fallback Downloader

Reads youtube_fallback_map.json and downloads unverified entries using
spotdl's hybrid mode: "YouTubeURL|SpotifyURL"
  - YouTube  = audio source
  - Spotify  = metadata source (Artist / Album / Cover / WOAS / etc.)

运行环境: Docker 容器 spotdl-local:latest，/music 挂载为音乐目录
用法:
  python3 /music/fallback_download.py              # 处理全部 verified=false 的条目
  python3 /music/fallback_download.py --dry-run    # 只打印不下载
  python3 /music/fallback_download.py --ids 6HC7J9YMaRwXme7XNsQkhd,04hmTHK2ddhRjOZxntr01r
"""

import json
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

MUSIC_DIR = pathlib.Path("/music")
PLAYLISTS_DIR = MUSIC_DIR / "Playlists"
FALLBACK_MAP_FILE = MUSIC_DIR / "youtube_fallback_map.json"
DOWNLOADS_FILE = MUSIC_DIR / "downloads.spotdl"
OUTPUT_TEMPLATE = "{artists}/{album}/{title}"

INTER_DOWNLOAD_SLEEP = 3  # seconds between downloads to avoid rate limits


# ── helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: pathlib.Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: could not read {path}: {exc}", flush=True)
        return {}


def save_json(path: pathlib.Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def count_downloads_spotdl() -> int:
    """Count entries in downloads.spotdl (before/after comparison)."""
    data = load_json(DOWNLOADS_FILE)
    songs: list = data if isinstance(data, list) else data.get("songs", [])
    return len(songs)


# ── core logic ───────────────────────────────────────────────────────────────

def run_spotdl_hybrid(youtube_url: str, spotify_url: str) -> bool:
    """
    Run: spotdl download 'YouTubeURL|SpotifyURL' --output ...
    cwd is MUSIC_DIR so spotdl writes downloads.spotdl in the same dir.
    Returns True if exit code == 0.
    """
    query = f"{youtube_url}|{spotify_url}"
    cmd = ["spotdl", "download", query, "--output", OUTPUT_TEMPLATE]
    print(f"  CMD: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=str(MUSIC_DIR))
    return result.returncode == 0


def find_mp3_by_spotify_id(spotify_id: str) -> pathlib.Path | None:
    """Scan /music for an mp3 whose WOAS tag matches spotify_id."""
    try:
        import mutagen
    except ImportError:
        print("WARNING: mutagen not available, skipping WOAS verification", flush=True)
        return None

    for mp3 in MUSIC_DIR.rglob("*.mp3"):
        if PLAYLISTS_DIR in mp3.parents:
            continue
        try:
            tags = mutagen.File(mp3)
            if not tags:
                continue
            woas = tags.get("WOAS")
            if woas:
                found_id = str(woas).rstrip("/").split("/")[-1]
                if found_id == spotify_id:
                    return mp3
        except Exception:
            continue
    return None


def verify_mp3_tags(mp3: pathlib.Path) -> dict:
    """Return a summary of ID3 tag completeness."""
    try:
        import mutagen
        tags = mutagen.File(mp3)
        if not tags:
            return {"ok": False, "reason": "no tags"}

        artist = str(tags.get("TPE1", "")).strip()
        album  = str(tags.get("TALB", "")).strip()
        title  = str(tags.get("TIT2", "")).strip()
        has_cover = any("APIC" in k for k in tags.keys())
        has_woas  = "WOAS" in tags

        return {
            "ok": bool(artist and album and title),
            "artist": artist,
            "album":  album,
            "title":  title,
            "has_cover": has_cover,
            "has_woas":  has_woas,
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    # Optional: --ids=id1,id2,...  to restrict processing
    filter_ids: set[str] | None = None
    for arg in sys.argv[1:]:
        if arg.startswith("--ids="):
            filter_ids = set(arg.split("=", 1)[1].split(","))

    fallback_map: dict = load_json(FALLBACK_MAP_FILE)  # type: ignore[assignment]
    if not fallback_map:
        print("ERROR: youtube_fallback_map.json is empty or missing.", flush=True)
        sys.exit(1)

    targets = {
        sid: entry
        for sid, entry in fallback_map.items()
        if not entry.get("verified", False)
        and (filter_ids is None or sid in filter_ids)
    }

    print("=== Fallback Download ===", flush=True)
    print(f"Map total : {len(fallback_map)}", flush=True)
    print(f"To process: {len(targets)}", flush=True)
    if dry_run:
        print("[DRY RUN mode — no actual downloads]", flush=True)

    dl_before = count_downloads_spotdl()
    print(f"downloads.spotdl before: {dl_before} entries", flush=True)
    print("", flush=True)

    success_count = 0
    fail_count    = 0

    for spotify_id, entry in targets.items():
        song_name   = entry["song_name"]
        youtube_url = entry["youtube_url"]
        spotify_url = entry["spotify_url"]

        print(f"[{'DRY' if dry_run else 'DL'}] {song_name}", flush=True)
        print(f"  Spotify: {spotify_url}", flush=True)
        print(f"  YouTube: {youtube_url}", flush=True)

        if dry_run:
            print("  → skipped (dry run)", flush=True)
            continue

        ok = run_spotdl_hybrid(youtube_url, spotify_url)

        if ok:
            mp3 = find_mp3_by_spotify_id(spotify_id)
            if mp3:
                tag_info = verify_mp3_tags(mp3)
                print(f"  ✅ File   : {mp3.relative_to(MUSIC_DIR)}", flush=True)
                print(f"     Artist : {tag_info.get('artist', '?')}", flush=True)
                print(f"     Album  : {tag_info.get('album',  '?')}", flush=True)
                print(f"     Title  : {tag_info.get('title',  '?')}", flush=True)
                print(f"     Cover  : {'✓' if tag_info.get('has_cover') else '✗'}", flush=True)
                print(f"     WOAS   : {'✓' if tag_info.get('has_woas')  else '✗'}", flush=True)

                # Mark as verified in fallback_map (only after confirmed on disk)
                fallback_map[spotify_id]["verified"]    = True
                fallback_map[spotify_id]["verified_at"] = now_iso()
                save_json(FALLBACK_MAP_FILE, fallback_map)
                success_count += 1
            else:
                print(
                    "  ⚠️  spotdl exit 0 but file not found by WOAS scan — "
                    "may need manual check",
                    flush=True,
                )
                fail_count += 1
        else:
            print("  ❌ spotdl returned non-zero exit code", flush=True)
            fail_count += 1

        print("", flush=True)
        time.sleep(INTER_DOWNLOAD_SLEEP)

    dl_after = count_downloads_spotdl()

    print("=== Summary ===", flush=True)
    print(f"Success : {success_count}", flush=True)
    print(f"Failed  : {fail_count}", flush=True)
    print(
        f"downloads.spotdl: {dl_before} → {dl_after}  "
        f"(delta: {dl_after - dl_before})",
        flush=True,
    )


if __name__ == "__main__":
    main()
