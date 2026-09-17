#!/usr/bin/env python3
"""Shared utilities for sync_liked.py, sync_playlists.py, and rebuild_liked_save.py.

Place this file in /music/ alongside the sync scripts so Docker's working-directory
import path resolves correctly when scripts run as:
    python3 /music/sync_liked.py
"""

import datetime
import json
import pathlib
import re
import subprocess
import urllib.parse
import urllib.request
from typing import Any, Callable

import mutagen
import mutagen.id3
import spotapi

MUSIC_DIR = pathlib.Path("/music")
SP_DC_FILE = MUSIC_DIR / ".spotify_sp_dc"
DEEZER_ARL_FILE = MUSIC_DIR / ".deezer_arl"
FALLBACK_MAP_FILE = MUSIC_DIR / "youtube_fallback_cache.json"
TELEGRAM_CONFIG_FILE = MUSIC_DIR / ".telegram_config"


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


def _normalize_match_text(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _core_title(title: str) -> str:
    """Drop bracketed and ' - ' suffixes such as '(Stacks from All Sides)' or '- Remix'."""
    core = re.sub(r"[(\[（【].*?[)\]）】]", "", title)
    core = core.split(" - ")[0]
    return _normalize_match_text(core)


def _loosely_equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a in b or b in a)


def deezer_result_matches(
    want_artist: str, want_title: str, got_artist: str, got_title: str
) -> bool:
    """True when a Deezer search hit plausibly is the requested Spotify track."""
    artist_ok = _loosely_equal(
        _normalize_match_text(want_artist), _normalize_match_text(got_artist)
    )
    title_ok = _loosely_equal(_core_title(want_title), _core_title(got_title))
    return artist_ok and title_ok


# WHY: songs that fail every source (YouTube 403, absent on Deezer) used to be
# retried on every 5-minute run forever; each attempt costs ~5 MB of overhead in
# a fresh container, which added ~1 GB/h during the 2026-09-14 incident.
RETRY_FREE_ATTEMPTS = 3
RetryState = dict[str, dict[str, Any]]
RETRY_COOLDOWN_SECONDS = 24 * 3600


def load_retry_state(path: pathlib.Path) -> RetryState:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_retry_state(path: pathlib.Path, state: RetryState) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(path)


def retry_due(state: RetryState, key: str, now: float) -> bool:
    entry = state.get(key)
    if not entry:
        return True
    if entry.get("failures", 0) < RETRY_FREE_ATTEMPTS:
        return True
    return now - entry.get("last_attempt", 0) >= RETRY_COOLDOWN_SECONDS


def record_retry_outcome(
    state: RetryState,
    attempted: set[str],
    still_missing: set[str],
    now: float,
) -> None:
    for key in attempted:
        if key in still_missing:
            entry = state.setdefault(key, {"failures": 0, "last_attempt": 0})
            entry["failures"] = entry.get("failures", 0) + 1
            entry["last_attempt"] = now
        else:
            state.pop(key, None)


def send_telegram(text: str) -> bool:
    """Send via the same bot as check_cookie.sh; never raises into the sync."""
    try:
        conf = dict(
            line.split("=", 1)
            for line in TELEGRAM_CONFIG_FILE.read_text().splitlines()
            if "=" in line
        )
        token = conf["BOT_TOKEN"].strip().strip('"')
        chat_id = conf["CHAT_ID"].strip().strip('"')
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return bool(json.loads(resp.read().decode()).get("ok"))
    except Exception as exc:
        print(f"WARNING: Telegram notify failed ({type(exc).__name__})", flush=True)
        return False


def song_label(song: dict[str, Any]) -> str:
    """'Artist - Title [m:ss]' — the length is what manual YouTube matching needs."""
    label = f"{song.get('artist', '')} - {song.get('name', '')}"
    duration = song.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        label += f" [{int(duration) // 60}:{int(duration) % 60:02d}]"
    return label


# WHY: YouTube changes its player/signature scheme every few weeks; a stale
# yt-dlp still reads metadata but every audio download gets HTTP 403, which is
# what caused the 2026-09-14 incident. The alert names the likely cause so the
# message alone is enough to start the fix.
YTDLP_STALE_DAYS = 60
RUNBOOK_HINT = "处理手册：nas-music CONTEXT.md「来源顺序、重试上限与人工通知」"


def _version_date(version: str) -> datetime.date | None:
    try:
        year, month, day = (int(part) for part in version.split(".")[:3])
        return datetime.date(year, month, day)
    except (ValueError, TypeError):
        return None


def diagnose_download_failures(
    count: int, installed: str, latest: str, today: datetime.date
) -> list[str]:
    installed_date = _version_date(installed)
    latest_date = _version_date(latest)
    status = f"yt-dlp {installed or '版本未知'}"
    if installed_date:
        status += f"（发布于 {(today - installed_date).days} 天前）"
    status += f"，PyPI 最新 {latest}" if latest else "，PyPI 最新版本查询失败"

    outdated = bool(installed_date and latest_date and latest_date > installed_date)
    stale = bool(installed_date and (today - installed_date).days > YTDLP_STALE_DAYS)
    if outdated or stale:
        cause = (
            "最可能原因：YouTube 改了播放器/签名算法，镜像里的 yt-dlp 已过旧"
            "（日志表现为 YT-DLP download error / HTTP 403）。"
            "处理：重建 spotdl-local 镜像升级 yt-dlp，再清空 retry state 让这些歌重试。"
        )
    elif count >= 3:
        cause = (
            "多首同时失败但 yt-dlp 已是最新：可能 YouTube 刚改版而 yt-dlp 还没跟上，"
            "或网络/Deezer 登录异常。处理：先看 .spotdl_*_sync.log 里这些歌的具体报错。"
        )
    else:
        cause = (
            "最可能原因：YouTube/Deezer 上没有同名同歌手的版本"
            "（TikTok remix、改名上传、翻唱等），自动匹配找不到。"
            "处理：按歌名和时长手动搜 YouTube，把链接写入 youtube_fallback_cache.json（source=manual）。"
        )
    return [status, cause, RUNBOOK_HINT]


def current_download_diagnosis(count: int) -> list[str]:
    try:
        from yt_dlp.version import __version__ as installed
    except Exception:
        installed = ""
    try:
        with urllib.request.urlopen(
            "https://pypi.org/pypi/yt-dlp/json", timeout=10
        ) as resp:
            latest = str(json.loads(resp.read().decode())["info"]["version"])
    except Exception:
        latest = ""
    return diagnose_download_failures(count, installed, latest, datetime.date.today())


def notify_exhausted_retries(
    state: RetryState,
    labels: dict[str, str],
    where: str,
    send: Callable[[str], bool] = send_telegram,
    diagnose: Callable[[int], list[str]] | None = None,
) -> None:
    """Tell a human once per song after every source failed RETRY_FREE_ATTEMPTS times.

    labels maps retry keys owned by this caller to "Artist - Title"; keys outside
    it are left for their own sync to report. The flag is set only after a
    successful send so a Telegram outage retries the alert next run.
    """
    pending = sorted(
        key
        for key in labels
        if state.get(key, {}).get("failures", 0) >= RETRY_FREE_ATTEMPTS
        and not state[key].get("notified")
    )
    if not pending:
        return
    lines = [f"• {labels[key]} ({key.rsplit(':', 1)[-1]})" for key in pending]
    diagnosis = (diagnose or current_download_diagnosis)(len(pending))
    text = (
        f"🎵 NAS 音乐同步（{where}）：{len(pending)} 首歌所有来源都下载失败"
        "（YouTube Music/YouTube、缓存链接、Deezer），"
        "已改为每 24 小时重试一次，需要人工处理：\n"
        + "\n".join(lines)
        + "\n\n🔎 诊断\n"
        + "\n".join(diagnosis)
    )
    if send(text):
        for key in pending:
            state[key]["notified"] = True
        print(f"Notified: {len(pending)} exhausted songs sent to Telegram", flush=True)


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

    # WHY: blindly taking tracks[0] once mapped two different Spotify IDs onto the
    # same unrelated Deezer track; both wrote WOAS on one file and kept evicting
    # each other, re-downloading every 5 minutes (2026-09-14 traffic incident).
    track = next(
        (
            t
            for t in tracks[:5]
            if deezer_result_matches(
                artist, title, t.get("artist", {}).get("name", ""), t.get("title", "")
            )
        ),
        None,
    )
    if track is None:
        first = tracks[0]
        print(
            f"    Deezer: rejected mismatched results for '{query}'"
            f" (top: '{first.get('artist', {}).get('name', '')} - {first.get('title', '')}')",
            flush=True,
        )
        return False
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
        # WHY: a separate cover.jpg lands in the library folder and Jellyfin uses
        # it as the folder image; the cover is already embedded in the MP3.
        text = text.replace("save_artwork = true", "save_artwork = false")
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
        move_to_artist_album_folder(mp3s[0], base_dir)
    return True


def _safe_path_part(value: str) -> str:
    return re.sub(r'[/\\:*?"<>|\x00]', "_", value).strip().strip(".")


def move_to_artist_album_folder(mp3: pathlib.Path, base_dir: pathlib.Path) -> pathlib.Path:
    """Move a flat streamrip download to base_dir/{artists}/{album}/{title}.mp3.

    WHY: streamrip saves singles flat as "01. Artist - Title.mp3" in base_dir,
    unlike spotDL's artist/album/title layout; loose files in the library root
    and playlist folders confuse Jellyfin's folder-based album detection.
    Leaves the file in place when tags are incomplete or the target exists.
    """
    try:
        tags = mutagen.id3.ID3(mp3)
    except Exception:
        return mp3
    artist = ", ".join(
        part.strip()
        for frame in tags.getall("TPE1")
        for text in frame.text
        for part in str(text).split("/")
        if part.strip()
    )
    album = str(tags["TALB"].text[0]) if tags.get("TALB") else ""
    title = str(tags["TIT2"].text[0]) if tags.get("TIT2") else ""
    parts = [_safe_path_part(v) for v in (artist, album, title)]
    if not all(parts):
        return mp3
    target = base_dir / parts[0] / parts[1] / f"{parts[2]}.mp3"
    if target.exists():
        return mp3
    target.parent.mkdir(parents=True, exist_ok=True)
    mp3.replace(target)
    return target
