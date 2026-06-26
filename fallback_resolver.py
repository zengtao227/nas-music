#!/usr/bin/env python3
"""
fallback_resolver.py — Pure URL Resolver (no downloads)

Reads missing_ids.json (produced by sync_liked.py), searches YouTube
via yt-dlp for each missing Spotify track, and writes resolved
youtube_url mappings to youtube_fallback_cache.json.

Does NOT call spotdl. Does NOT modify music files.
The next sync_liked.py run will consume the cache for hybrid download.

Usage:
    python3 fallback_resolver.py [--dry-run]
"""

import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
MUSIC_DIR = pathlib.Path("/music")
SAVE_FILE = MUSIC_DIR / "liked.spotdl"
CACHE_FILE = MUSIC_DIR / "youtube_fallback_cache.json"
MISSING_IDS_FILE = MUSIC_DIR / "missing_ids.json"

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
DURATION_TOLERANCE = 0.15       # ±15% duration tolerance
MIN_SCORE_THRESHOLD = 0.4       # candidates below this score are rejected
CACHE_TTL_DAYS = 90             # entries older than this are re-resolved
NOISE_KEYWORDS = [              # penalise these unless in the Spotify title
    "cover", "karaoke", "remix", "live", "slowed", "reverb", "8d",
]
NOISE_PENALTY = 0.35            # score deducted per noise keyword matched


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
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


def is_cache_valid(entry: dict | None) -> bool:
    """Return True if the cache entry exists and is not older than CACHE_TTL_DAYS."""
    if not entry or not entry.get("youtube_url"):
        return False
    resolved_at = entry.get("resolved_at", "")
    if not resolved_at:
        return False
    try:
        ts = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ts).days
        return age_days < CACHE_TTL_DAYS
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# YouTube search + scoring
# ---------------------------------------------------------------------------
def search_youtube_candidates(query: str) -> list[dict]:
    """Search YouTube using yt-dlp, returning top 3 candidates."""
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
                    "url": (
                        info.get("webpage_url")
                        or f"https://www.youtube.com/watch?v={info.get('id')}"
                    ),
                    "duration": info.get("duration", 0),
                })
            except json.JSONDecodeError:
                continue
        return candidates
    except Exception as exc:
        print(f"  ⚠️  yt-dlp search failed for '{query}': {exc}", flush=True)
        return []


def score_candidate(
    candidate: dict, spotify_title: str, spotify_duration: float
) -> float:
    """Score a YouTube candidate in [0.0, 1.0]. Returns 0.0 if duration is out of tolerance."""
    yt_duration = float(candidate["duration"])
    if yt_duration <= 0 or spotify_duration <= 0:
        return 0.0

    ratio = yt_duration / spotify_duration
    if not (1 - DURATION_TOLERANCE <= ratio <= 1 + DURATION_TOLERANCE):
        return 0.0

    # Base score: closeness to target duration
    score = 1.0 - abs(ratio - 1.0)

    # Penalise noise keywords not present in the Spotify title
    yt_title_lower = candidate["title"].lower()
    spotify_title_lower = spotify_title.lower()
    for kw in NOISE_KEYWORDS:
        if kw in yt_title_lower and kw not in spotify_title_lower:
            score -= NOISE_PENALTY

    return max(0.0, score)


def resolve_url(
    spotify_id: str, liked_songs: list
) -> tuple[str | None, float, dict]:
    """
    Pure function: Spotify ID → (youtube_url | None, best_score, best_candidate).
    No side effects beyond returning values.
    """
    metadata = next(
        (s for s in liked_songs if s.get("song_id") == spotify_id), None
    )
    if not metadata:
        return None, 0.0, {}

    artist = metadata.get("artist", "")
    title = metadata.get("name", "")
    spotify_duration = float(metadata.get("duration", 0))

    query = f"{artist} - {title}"
    print(
        f"  → Searching: '{query}' (target: {spotify_duration:.0f}s)",
        flush=True,
    )
    candidates = search_youtube_candidates(query)

    scored = sorted(
        [(score_candidate(c, title, spotify_duration), c) for c in candidates],
        key=lambda x: x[0],
        reverse=True,
    )

    for score, cand in scored:
        print(
            f"     Score {score:.2f} | {cand['duration']}s | {cand['title'][:60]}",
            flush=True,
        )

    if scored and scored[0][0] >= MIN_SCORE_THRESHOLD:
        best_score, best = scored[0]
        print(f"  ✓ Best match (score {best_score:.2f}): {best['url']}", flush=True)
        return best["url"], best_score, best

    print("  ✗ No candidate passed thresholds", flush=True)
    return None, 0.0, {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    dry_run = "--dry-run" in sys.argv

    print("=== Fallback Resolver (pure resolver — no downloads) ===", flush=True)
    if dry_run:
        print("[DRY RUN — cache will not be written]", flush=True)

    # 1. Read missing_ids.json (produced by sync_liked.py)
    if not MISSING_IDS_FILE.exists():
        print(
            "missing_ids.json not found. Run sync_liked.py first to generate it.",
            flush=True,
        )
        return

    missing_ids: list[str] = load_json(MISSING_IDS_FILE)  # type: ignore[assignment]
    if not isinstance(missing_ids, list) or not missing_ids:
        print("No missing IDs found in missing_ids.json. Nothing to resolve.", flush=True)
        return

    print(f"Missing IDs to resolve: {len(missing_ids)}", flush=True)

    # 2. Load liked.spotdl for metadata
    liked_songs = load_json(SAVE_FILE)
    if isinstance(liked_songs, dict):
        liked_songs = liked_songs.get("songs", [])

    # 3. Load existing cache
    cache: dict = load_json(CACHE_FILE)  # type: ignore[assignment]
    if not isinstance(cache, dict):
        cache = {}

    resolved_count = 0
    skipped_count = 0
    failed_count = 0

    for idx, sid in enumerate(missing_ids, 1):
        metadata = next((s for s in liked_songs if s.get("song_id") == sid), None)
        label = (
            f"{metadata.get('artist','')} - {metadata.get('name','')}"
            if metadata
            else sid
        )
        print(f"\n[{idx}/{len(missing_ids)}] {label}", flush=True)

        # Check cache validity first
        if is_cache_valid(cache.get(sid)):
            print("  → Cache hit (still valid)", flush=True)
            skipped_count += 1
            continue

        youtube_url, best_score, best_cand = resolve_url(sid, liked_songs)

        if youtube_url:
            if not dry_run:
                # Store confidence + duration_delta for future auditability.
                # Low-confidence entries (score < 0.7) are probabilistic guesses.
                spotify_dur = float(
                    next(
                        (s.get("duration", 0) for s in liked_songs if s.get("song_id") == sid),
                        0,
                    )
                )
                duration_delta = abs(best_cand.get("duration", 0) - spotify_dur)
                cache[sid] = {
                    "youtube_url": youtube_url,
                    "song_name": label,
                    "spotify_url": f"https://open.spotify.com/track/{sid}",
                    "source": "auto",
                    "confidence": round(best_score, 3),
                    "duration_delta_s": round(duration_delta, 1),
                    "resolved_at": now_iso(),
                }
                save_json(CACHE_FILE, cache)
            resolved_count += 1
        else:
            failed_count += 1

    print("\n=== Resolver Summary ===", flush=True)
    print(f"Resolved  : {resolved_count}", flush=True)
    print(f"Skipped   : {skipped_count} (valid cache)", flush=True)
    print(f"Failed    : {failed_count}", flush=True)
    print(
        "\nRun sync_liked.py next to consume resolved URLs via hybrid download.",
        flush=True,
    )


if __name__ == "__main__":
    main()
