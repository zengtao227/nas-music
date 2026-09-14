"""Tests for cross-playlist file reuse (2026-09-14 duplicate-download fix).

WHY: the same song often sits on more than one of Mia's playlists. Before this
change each playlist downloaded (and kept) its own independent copy, so
Jellyfin saw two files with identical tags for one song and presented them to
Finamp as "alternate versions" to choose from. These tests cover the new
global lookup used to skip a duplicate download/keep, and the deletion guard
that stops one playlist from deleting a file another playlist still needs.

sync_playlists.py imports `spotapi`, which pulls in optional dependencies
(pymongo, redis) this repo does not otherwise need. We stub the module before
import so these tests only need `mutagen` (already a hard runtime dependency).
"""

import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

if "spotapi" not in sys.modules:
    sys.modules["spotapi"] = types.ModuleType("spotapi")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_playlists as sp  # noqa: E402


def _fake_song_id_from_file(mp3: Path) -> str:
    """Stand-in for shared.song_id_from_file: reads the ID from file content.

    Keeps these tests independent of real WOAS/ID3 tag writing (already
    exercised in production) and focused on the new reuse/protection logic.
    """
    try:
        return mp3.read_text().strip()
    except OSError:
        return ""


def _touch(path: Path, song_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(song_id)


class BuildGlobalIdToPathTest(unittest.TestCase):
    def test_finds_file_in_any_playlist_folder(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(sp, "MUSIC_DIR", root), patch.object(
                sp, "song_id_from_file", _fake_song_id_from_file
            ):
                _touch(root / "Playlists" / "summer26" / "a.mp3", "SID1")
                _touch(root / "Playlists" / "calm" / "b.mp3", "SID2")

                result = sp.build_global_id_to_path()

                self.assertEqual(
                    result["SID1"], root / "Playlists" / "summer26" / "a.mp3"
                )
                self.assertEqual(
                    result["SID2"], root / "Playlists" / "calm" / "b.mp3"
                )

    def test_missing_song_id_not_present(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(sp, "MUSIC_DIR", root), patch.object(
                sp, "song_id_from_file", _fake_song_id_from_file
            ):
                _touch(root / "Playlists" / "summer26" / "a.mp3", "")
                result = sp.build_global_id_to_path()
                self.assertEqual(result, {})


class BuildGlobalTrackedIdsTest(unittest.TestCase):
    def test_unions_other_playlists_excluding_current(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(sp, "MUSIC_DIR", root):
                a_folder = root / "Playlists" / "summer26"
                a_folder.mkdir(parents=True)
                sp.write_save_file(
                    a_folder / "summer26.spotdl", [{"song_id": "SID1"}]
                )
                b_folder = root / "Playlists" / "calm"
                b_folder.mkdir(parents=True)
                sp.write_save_file(b_folder / "calm.spotdl", [{"song_id": "SID2"}])

                playlists = [
                    {"folder": "summer26"},
                    {"folder": "calm"},
                ]

                self.assertEqual(
                    sp.build_global_tracked_ids(playlists, "calm"), {"SID1"}
                )
                self.assertEqual(
                    sp.build_global_tracked_ids(playlists, "summer26"), {"SID2"}
                )
                self.assertEqual(
                    sp.build_global_tracked_ids(playlists, "unknown"),
                    {"SID1", "SID2"},
                )


class DeleteFilesForIdsTest(unittest.TestCase):
    def test_protected_id_is_kept_unprotected_is_deleted(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            kept = folder / "Artist" / "keep.mp3"
            removed = folder / "Artist" / "remove.mp3"
            _touch(kept, "PROTECTED")
            _touch(removed, "GONE")

            with patch.object(sp, "song_id_from_file", _fake_song_id_from_file):
                deleted = sp.delete_files_for_ids(
                    folder,
                    removed_ids={"PROTECTED", "GONE"},
                    protected_ids=frozenset({"PROTECTED"}),
                )

            self.assertEqual(deleted, 1)
            self.assertTrue(kept.exists())
            self.assertFalse(removed.exists())

    def test_default_protected_ids_deletes_everything_removed(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            f = folder / "Artist" / "gone.mp3"
            _touch(f, "GONE")

            with patch.object(sp, "song_id_from_file", _fake_song_id_from_file):
                deleted = sp.delete_files_for_ids(folder, removed_ids={"GONE"})

            self.assertEqual(deleted, 1)
            self.assertFalse(f.exists())


class RebuildJellyfinPlaylistCrossReuseTest(unittest.TestCase):
    def test_uses_global_path_when_not_in_own_folder(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Playlists" / "calm"
            folder.mkdir(parents=True)
            xml_dir = root / "jellyfin" / "Calm"
            xml_dir.mkdir(parents=True)
            xml_path = xml_dir / "playlist.xml"
            xml_path.write_text(
                '<?xml version="1.0" encoding="utf-8"?><Item><PlaylistItems />'
                "</Item>"
            )

            elsewhere = root / "Playlists" / "summer26" / "SZA" / "Kill Bill.mp3"
            elsewhere.parent.mkdir(parents=True)
            elsewhere.write_text("SID1")

            with patch.object(sp, "MUSIC_DIR", root), patch.object(
                sp, "JELLYFIN_PLAYLISTS_DIR", root / "jellyfin"
            ), patch.object(sp, "song_id_from_file", _fake_song_id_from_file):
                sp.rebuild_jellyfin_playlist(
                    pl={"name": "calm", "jellyfin_name": "Calm"},
                    folder=folder,
                    songs=[{"song_id": "SID1"}],
                    api_key="",
                    candidates_by_key={},
                    global_id_to_path={"SID1": elsewhere},
                )

            written = xml_path.read_text()
            self.assertIn(
                f"{sp.JELLYFIN_MUSIC_PREFIX}/Playlists/summer26/SZA/Kill Bill.mp3",
                written,
            )

    def test_missing_everywhere_stays_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Playlists" / "calm"
            folder.mkdir(parents=True)
            xml_dir = root / "jellyfin" / "Calm"
            xml_dir.mkdir(parents=True)
            xml_path = xml_dir / "playlist.xml"
            xml_path.write_text(
                '<?xml version="1.0" encoding="utf-8"?><Item><PlaylistItems />'
                "</Item>"
            )

            with patch.object(sp, "MUSIC_DIR", root), patch.object(
                sp, "JELLYFIN_PLAYLISTS_DIR", root / "jellyfin"
            ), patch.object(sp, "song_id_from_file", _fake_song_id_from_file):
                sp.rebuild_jellyfin_playlist(
                    pl={"name": "calm", "jellyfin_name": "Calm"},
                    folder=folder,
                    songs=[{"song_id": "SID_GONE"}],
                    api_key="",
                    candidates_by_key={},
                    global_id_to_path={},
                )

            written = xml_path.read_text()
            self.assertNotIn(sp.JELLYFIN_MUSIC_PREFIX, written)


if __name__ == "__main__":
    unittest.main()
