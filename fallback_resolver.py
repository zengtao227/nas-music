#!/usr/bin/env python3
"""
fallback_resolver.py — spotdl Fallback System Resolver

1. Identifies missing songs (exists in liked.spotdl but lacks physical file on disk).
2. For each missing song:
   - Queries youtube_fallback_cache.json.
   - If not cached, runs `yt-dlp` to search top 3 results for "{artists} - {title}".
   - Filters candidates by duration tolerance (Spotify duration ±15%).
   - Rates matches, filters noise (cover, karaoke, live), and selects the Best Match URL.
   - Triggers `spotdl download "YouTubeURL|SpotifyURL"`.
   - On success, verifies the file tags and caches the result.
"""

import json
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

MUSIC_DIR = pathlib.Path("/music")
PLAYLISTS_DIR = MUSIC_DIR / "Playlists"
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
CACHE_FILE = MUSIC_DIR / "youtube_fallback_cache.json"
OUTPUT_TEMPLATE = "{artists}/{album}/{title}"
INTER_DOWNLOAD_SLEEP = 3


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


def get_mp3_woas_id(mp3: pathlib.Path) -> str:
    try:
        import mutagen
        tags = mutagen.File(mp3)
        woas = tags.get("WOAS") if tags else None
        if woas:
            return str(woas).rstrip("/").split("/")[-1]
    except Exception:
        pass
    return ""


def scan_local_spotify_ids() -> dict[str, pathlib.Path]:
    """Returns spotify_id -> mp3 path mapping for existing local files."""
    local_map = {}
    for mp3 in MUSIC_DIR.rglob("*.mp3"):
        if PLAYLISTS_DIR in mp3.parents:
            continue
        sid = get_mp3_woas_id(mp3)
        if sid:
            local_map[sid] = mp3
    return local_map


def get_spotify_track_metadata(spotify_id: str, liked_songs: list) -> dict | None:
    """Finds song metadata inside liked.spotdl without calling Spotify API."""
    for s in liked_songs:
        if s.get("song_id") == spotify_id:
            return s
    return None


def search_youtube_candidates(query: str) -> list[dict]:
    """Search YouTube using yt-dlp, returning top 3 candidates with title, url, duration."""
    cmd = [
        "yt-dlp",
        f"ytsearch3:{query}",
        "--dump-json",
        "--skip-download",
        "--ignore-errors",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        candidates = []
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                info = json.loads(line)
                candidates.append({
                    "title": info.get("title", ""),
                    "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={info.get('id')}",
                    "duration": info.get("duration", 0),
                })
            except json.JSONDecodeError:
                continue
        return candidates
    except Exception as exc:
        print(f"  ⚠️ yt-dlp search failed for '{query}': {exc}", flush=True)
        return []


def score_candidate(candidate: dict, spotify_title: str, spotify_duration: float) -> float:
    """
    Scores a YouTube candidate (0.0 to 1.0).
    Filters/penalizes if duration is out of ±15% tolerance.
    Penalizes keyword noise if Spotify title doesn't suggest them.
    """
    yt_title = candidate["title"].lower()
    yt_duration = float(candidate["duration"])
    
    if yt_duration <= 0 or spotify_duration <= 0:
        return 0.0
        
    # 1. Duration Tolerance Filter (±15%)
    ratio = yt_duration / spotify_duration
    if not (0.85 <= ratio <= 1.15):
        return 0.0
        
    # Base score on duration closeness
    score = 1.0 - abs(ratio - 1.0)
    
    # 2. Penalize Noise Keywords unless present in Spotify Title
    spotify_title_lower = spotify_title.lower()
    noise_keywords = ["cover", "karaoke", "remix", "live", "slowed", "reverb", "8d"]
    for kw in noise_keywords:
        if kw in yt_title and kw not in spotify_title_lower:
            score -= 0.35  # Significant penalty
            
    return max(0.0, score)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    filter_ids = None
    for arg in sys.argv[1:]:
        if arg.startswith("--ids="):
            filter_ids = set(arg.split("=", 1)[1].split(","))

    print("=== Fallback Resolver ===", flush=True)
    if dry_run:
        print("[DRY RUN mode — no downloads or cache updates]", flush=True)

    # Load liked database (L1 output)
    liked_songs = load_json(SAVE_FILE)
    if not isinstance(liked_songs, list):
        liked_songs = liked_songs.get("songs", []) if isinstance(liked_songs, dict) else []
    
    spotify_liked_ids = {s["song_id"] for s in liked_songs if "song_id" in s}
    print(f"Total liked songs in database: {len(spotify_liked_ids)}", flush=True)

    # Scan physical files on disk
    local_tracks = scan_local_spotify_ids()
    print(f"Total verified local MP3 tracks: {len(local_tracks)}", flush=True)

    # missing_ids are liked but not on disk
    missing_ids = spotify_liked_ids - set(local_tracks.keys())
    if filter_ids is not None:
        missing_ids = missing_ids & filter_ids

    print(f"Total missing tracks: {len(missing_ids)}", flush=True)
    if not missing_ids:
        print("No missing tracks to process. Done.", flush=True)
        return

    # Load history cache (L3)
    cache = load_json(CACHE_FILE)
    if not isinstance(cache, dict):
        cache = {}

    success_count = 0
    fail_count = 0

    for idx, sid in enumerate(missing_ids, 1):
        metadata = get_spotify_track_metadata(sid, liked_songs)
        if not metadata:
            print(f"[{idx}/{len(missing_ids)}] ID {sid}: metadata missing in liked.spotdl, skipping", flush=True)
            fail_count += 1
            continue

        title = metadata.get("name", "Unknown Title")
        artist = metadata.get("artist", "Unknown Artist")
        spotify_duration = float(metadata.get("duration", 0))

        print(f"\n[{idx}/{len(missing_ids)}] Resolving: {artist} - {title} (ID: {sid})", flush=True)

        youtube_url = None
        cached = cache.get(sid)

        # Step 1: Check Cache
        if cached and cached.get("verified") and cached.get("youtube_url"):
            print(f"  -> Cache Hit: {cached['youtube_url']}", flush=True)
            youtube_url = cached["youtube_url"]
        else:
            # Step 2: Auto Search YouTube
            search_query = f"{artist} - {title}"
            print(f"  -> Searching YouTube for: '{search_query}' (target duration: {spotify_duration:.1f}s)", flush=True)
            candidates = search_youtube_candidates(search_query)
            
            scored_candidates = []
            for cand in candidates:
                score = score_candidate(cand, title, spotify_duration)
                scored_candidates.append((score, cand))
                print(f"     Score {score:.2f} | Dur: {cand['duration']}s | Title: {cand['title'][:60]}", flush=True)

            # Sort by score descending
            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            
            if scored_candidates and scored_candidates[0][0] > 0.4:
                best_score, best_cand = scored_candidates[0]
                youtube_url = best_cand["url"]
                print(f"  -> Selected Best Match (Score {best_score:.2f}): {youtube_url}", flush=True)
            else:
                print("  ❌ No candidate passed duration tolerance & filtering thresholds", flush=True)
                fail_count += 1
                continue

        if dry_run:
            print("  [DRY RUN] skipped downloading", flush=True)
            continue

        # Step 3: Trigger spotdl download
        spotify_url = f"https://open.spotify.com/track/{sid}"
        query = f"{youtube_url}|{spotify_url}"
        cmd = ["spotdl", "download", query, "--output", OUTPUT_TEMPLATE]
        
        print(f"  -> Triggering spotdl download...", flush=True)
        res = subprocess.run(cmd, cwd=str(MUSIC_DIR))
        
        if res.returncode == 0:
            # Scan again to verify it is now physically on disk and matches ID
            time.sleep(1)
            updated_tracks = scan_local_spotify_ids()
            if sid in updated_tracks:
                print(f"  ✅ Verified on disk: {updated_tracks[sid].relative_to(MUSIC_DIR)}", flush=True)
                
                # Step 4: Write Cache
                cache[sid] = {
                    "youtube_url": youtube_url,
                    "song_name": f"{artist} - {title}",
                    "spotify_url": spotify_url,
                    "source": "auto",
                    "verified": True,
                    "verified_at": now_iso()
                }
                save_json(CACHE_FILE, cache)
                success_count += 1
            else:
                print("  ⚠️ spotdl finished but verified file not found on disk", flush=True)
                fail_count += 1
        else:
            print(f"  ❌ spotdl failed with exit code {res.returncode}", flush=True)
            fail_count += 1

        time.sleep(INTER_DOWNLOAD_SLEEP)

    print(f"\n=== Resolver Summary ===")
    print(f"Successfully processed : {success_count}")
    print(f"Failed to process      : {fail_count}")
