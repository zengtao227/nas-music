"""Incremental, non-fatal LRCLIB sidecar support for music sync jobs."""

from __future__ import annotations

import json
import math
import os
import secrets
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3
except ModuleNotFoundError:
    EasyID3 = None
    MP3 = None

API = "https://lrclib.net/api/get"


@dataclass(frozen=True)
class Fingerprint:
    inode: int
    size: int
    mtime_ns: int


@dataclass
class Summary:
    changed: int = 0
    existing: int = 0
    metadata_missing: int = 0
    queried: int = 0
    cache_hits: int = 0
    synced_written: int = 0
    plain_written: int = 0
    no_match: int = 0
    rejected: int = 0
    error: int = 0


def snapshot(root: Path, exclude_playlists: bool = False) -> dict[Path, Fingerprint]:
    result: dict[Path, Fingerprint] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() != ".mp3":
            continue
        if exclude_playlists and (root / "Playlists") in path.parents:
            continue
        stat = path.stat()
        result[path] = Fingerprint(stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return result


def changed_files(
    before: dict[Path, Fingerprint] | None, after: dict[Path, Fingerprint] | None
) -> set[Path]:
    if before is None or after is None:
        return set()
    return {path for path, value in after.items() if before.get(path) != value}


def _metadata(path: Path) -> tuple[str, str, str, float] | None:
    if MP3 is None or EasyID3 is None:
        return None
    try:
        audio = MP3(path, ID3=EasyID3)
        tags = audio.tags or {}
        title, artist, album = (
            tags.get("title", [""])[0].strip(),
            tags.get("artist", [""])[0].strip(),
            tags.get("album", [""])[0].strip(),
        )
        if not (title and artist and album):
            return None
        duration = float(audio.info.length)
        return (
            (title, artist, album, duration)
            if math.isfinite(duration) and duration > 0
            else None
        )
    except Exception:
        return None


def _write(path: Path, text: str) -> bool:
    temporary = path.parent / f".{path.name}.tmp-{secrets.token_hex(8)}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((text.rstrip("\n") + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        os.chmod(path, 0o644)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _fetch(
    meta: tuple[str, str, str, float], request: Callable[..., Any] = urlopen
) -> tuple[str, str | None]:
    title, artist, album, duration = meta
    if not math.isfinite(duration) or duration <= 0:
        return "rejected", None
    url = (
        API
        + "?"
        + urlencode(
            {
                "track_name": title,
                "artist_name": artist,
                "album_name": album,
                "duration": round(duration),
            }
        )
    )
    for attempt in range(2):
        try:
            with request(
                Request(url, headers={"User-Agent": "nas-music-lyrics-sync/1.0"}),
                timeout=10,
            ) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                return "rejected", None
            response_duration = payload.get("duration")
            if (
                isinstance(response_duration, bool)
                or not isinstance(response_duration, (int, float))
                or not math.isfinite(float(response_duration))
                or abs(float(response_duration) - duration) > 3
            ):
                return "rejected", None
            synced, plain = payload.get("syncedLyrics"), payload.get("plainLyrics")
            if isinstance(synced, str) and synced.strip():
                return "synced", synced
            if isinstance(plain, str) and plain.strip():
                return "plain", plain
            return "rejected", None
        except HTTPError as error:
            if error.code == 404:
                return "no_match", None
            if error.code != 429 and error.code < 500:
                return "error", None
            try:
                wait = (
                    int(error.headers.get("Retry-After", "1"))
                    if error.code == 429
                    else 1
                )
            except (TypeError, ValueError):
                wait = 1
            wait = max(0, min(10, wait))
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            wait = 1
        if attempt:
            return "error", None
        time.sleep(wait)
    return "error", None


def process_changed(
    before: dict[Path, Fingerprint] | None, after: dict[Path, Fingerprint] | None
) -> Summary:
    summary = Summary()
    cache: dict[tuple[str, str, str, int], tuple[str, str | None]] = {}
    last = 0.0
    for path in sorted(changed_files(before, after)):
        summary.changed += 1
        if any(
            path.with_suffix(suffix).exists() for suffix in (".lrc", ".elrc", ".txt")
        ):
            summary.existing += 1
            continue
        try:
            meta = _metadata(path)
        except Exception:
            summary.error += 1
            continue
        if meta is None:
            summary.metadata_missing += 1
            continue
        key = (
            meta[0].casefold(),
            meta[1].casefold(),
            meta[2].casefold(),
            round(meta[3]),
        )
        result = cache.get(key)
        if result is None:
            time.sleep(max(0, 1 - (time.monotonic() - last)))
            last = time.monotonic()
            summary.queried += 1
            try:
                result = _fetch(meta)
            except Exception:
                summary.error += 1
                continue
            cache[key] = result
        else:
            summary.cache_hits += 1
        kind, text = result
        if kind in ("synced", "plain") and text is not None:
            try:
                written = _write(
                    path.with_suffix(".lrc" if kind == "synced" else ".txt"), text
                )
            except Exception:
                summary.error += 1
                continue
            if written:
                setattr(
                    summary, f"{kind}_written", getattr(summary, f"{kind}_written") + 1
                )
            else:
                summary.existing += 1
        else:
            setattr(summary, kind, getattr(summary, kind) + 1)
    print("Lyrics: " + json.dumps(asdict(summary), sort_keys=True), flush=True)
    return summary
