#!/usr/bin/env python3
"""
Sync Spotify Liked Songs.

liked.spotdl is treated as a Spotify state snapshot, not a download log.
Every run:
1. Read existing liked.spotdl.
2. Generate a fresh snapshot from Spotify.
3. Compute added/removed song IDs.
4. Download only added songs.
5. Replace liked.spotdl with the fresh snapshot.
6. Run spotdl sync when removals exist.
"""
import json
import pathlib
import shutil
import subprocess

MUSIC_DIR = pathlib.Path('/music')
SAVE_FILE = MUSIC_DIR / 'liked.spotdl'
TEMP_FILE = MUSIC_DIR / 'liked_current.spotdl'
OUTPUT_TEMPLATE = '{artists}/{album}/{title}'
LIKED_URL = 'https://open.spotify.com/collection/tracks'


def spotdl(*args: str) -> int:
    return subprocess.run(['spotdl', *args], cwd=str(MUSIC_DIR)).returncode


def load_song_ids(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    songs = data if isinstance(data, list) else data.get('songs', [])
    return {s['song_id'] for s in songs if 'song_id' in s}


def main() -> None:
    print('=== Liked Songs Sync ===', flush=True)

    TEMP_FILE.unlink(missing_ok=True)

    rc = spotdl('save', LIKED_URL, '--save-file', str(TEMP_FILE))
    if rc != 0 or not TEMP_FILE.exists():
        raise RuntimeError('spotdl save failed')

    old_ids = load_song_ids(SAVE_FILE)
    new_ids = load_song_ids(TEMP_FILE)

    added = new_ids - old_ids
    removed = old_ids - new_ids

    print(f'Spotify snapshot: {len(new_ids)} songs', flush=True)
    print(f'Added: {len(added)} Removed: {len(removed)}', flush=True)

    if added:
        urls = [f'https://open.spotify.com/track/{sid}' for sid in added]
        spotdl('download', *urls, '--output', OUTPUT_TEMPLATE)

    shutil.move(str(TEMP_FILE), str(SAVE_FILE))

    if removed:
        spotdl('sync', str(SAVE_FILE), '--output', OUTPUT_TEMPLATE)

    print('Done.', flush=True)


if __name__ == '__main__':
    main()
