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

try:
    import spotapi  # noqa: F401  (real package, when fully installed)
except ModuleNotFoundError:
    sys.modules["spotapi"] = types.ModuleType("spotapi")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_playlists as sp


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
            # A second, untouched file keeps the directory non-empty after the
            # deletion below — isolates this test to delete_files_for_ids'
            # id-matching behavior rather than its (separate, pre-existing)
            # empty-parent rmdir cleanup.
            other = folder / "Artist" / "stays.mp3"
            _touch(f, "GONE")
            _touch(other, "UNRELATED")

            with patch.object(sp, "song_id_from_file", _fake_song_id_from_file):
                deleted = sp.delete_files_for_ids(folder, removed_ids={"GONE"})

            self.assertEqual(deleted, 1)
            self.assertFalse(f.exists())
            self.assertTrue(other.exists())


class RebuildJellyfinPlaylistCrossReuseTest(unittest.TestCase):
    def _rebuild(self, root, folder, songs, global_id_to_path):
        synced = []
        with patch.object(sp, "MUSIC_DIR", root), patch.object(
            sp, "song_id_from_file", _fake_song_id_from_file
        ), patch.object(
            sp,
            "sync_jellyfin_playlist_items",
            lambda jid, name, paths, key: synced.append(paths),
        ):
            sp.rebuild_jellyfin_playlist(
                pl={"name": "calm", "jellyfin_name": "Calm", "jellyfin_id": "PL"},
                folder=folder,
                songs=songs,
                api_key="key",
                candidates_by_key={},
                global_id_to_path=global_id_to_path,
            )
        return synced

    def test_uses_global_path_when_not_in_own_folder(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Playlists" / "calm"
            folder.mkdir(parents=True)
            elsewhere = root / "Playlists" / "summer26" / "SZA" / "Kill Bill.mp3"
            elsewhere.parent.mkdir(parents=True)
            elsewhere.write_text("SID1")

            synced = self._rebuild(
                root, folder, [{"song_id": "SID1"}], {"SID1": elsewhere}
            )

            self.assertEqual(
                synced,
                [[f"{sp.JELLYFIN_MUSIC_PREFIX}/Playlists/summer26/SZA/Kill Bill.mp3"]],
            )

    def test_missing_everywhere_stays_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Playlists" / "calm"
            folder.mkdir(parents=True)

            synced = self._rebuild(root, folder, [{"song_id": "SID_GONE"}], {})

            self.assertEqual(synced, [[]])


class FakeJellyfin:
    """Records Playlists API calls against an in-memory playlist and library."""

    def __init__(self, entries, library, owner=sp.JELLYFIN_MIA_USER_ID):
        self.entries = list(entries)
        self.library = library
        self.owner = owner
        self.writes = []
        self.add_limit = None

    def __call__(self, method, path, api_key, body=None, warn=True):
        if method == "GET" and path.startswith("/Playlists/"):
            return {
                "Items": [
                    {"Id": i, "PlaylistItemId": i, "Path": self.library[i]}
                    for i in self.entries
                ]
            }
        if method == "GET" and path.startswith("/Items"):
            return {"Items": [{"Id": i, "Path": p} for i, p in self.library.items()]}
        if method == "GET" and path == "/Users":
            return [{"Id": sp.JELLYFIN_MIA_USER_ID}, {"Id": "ADMIN"}]
        self.writes.append((method, path))
        query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
        if method == "DELETE":
            gone = set(query["entryIds"].split(","))
            self.entries = [i for i in self.entries if i not in gone]
            return {}
        if method == "POST":
            if query["userId"] != self.owner:
                return None
            self.entries.extend(query["ids"].split(",")[: self.add_limit])
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


class SyncJellyfinPlaylistItemsTest(unittest.TestCase):
    LIBRARY = {"A": "/media/music/a.mp3", "B": "/media/music/b.mp3", "C": "/media/music/c.mp3"}

    def _sync(self, fake, desired_ids):
        desired_paths = [self.LIBRARY[i] for i in desired_ids]
        with patch.object(sp, "_jellyfin_api", fake):
            sp.sync_jellyfin_playlist_items("PL", "Calm", desired_paths, "key")

    def test_matching_playlist_is_not_touched(self):
        fake = FakeJellyfin(["A", "B"], self.LIBRARY)
        self._sync(fake, ["A", "B"])
        self.assertEqual(fake.writes, [])

    def test_changed_playlist_is_replaced_in_order(self):
        fake = FakeJellyfin(["A", "B"], self.LIBRARY)
        self._sync(fake, ["C", "A"])
        self.assertEqual(fake.entries, ["C", "A"])
        self.assertEqual(fake.writes[0][0], "DELETE")

    def test_does_not_empty_playlist_when_files_not_indexed(self):
        fake = FakeJellyfin(["A", "B"], self.LIBRARY)
        with patch.object(sp, "_jellyfin_api", fake):
            sp.sync_jellyfin_playlist_items(
                "PL",
                "Calm",
                ["/media/music/new1.mp3", "/media/music/new2.mp3", self.LIBRARY["A"]],
                "key",
            )
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.entries, ["A", "B"])

    def test_waits_for_unindexed_new_file_without_rewriting(self):
        fake = FakeJellyfin(["A", "B"], self.LIBRARY)
        with patch.object(sp, "_jellyfin_api", fake):
            sp.sync_jellyfin_playlist_items(
                "PL",
                "Calm",
                [self.LIBRARY["A"], self.LIBRARY["B"], "/media/music/new.mp3"],
                "key",
            )
        self.assertEqual(fake.writes, [])

    def test_partial_add_is_reported(self):
        fake = FakeJellyfin(["A"], self.LIBRARY)
        fake.add_limit = 1
        with patch("builtins.print") as printed:
            self._sync(fake, ["A", "B"])
        messages = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("read-back has 1 items, expected 2", messages)

    def test_adds_with_playlist_owner_when_not_owned_by_mia(self):
        fake = FakeJellyfin(["A"], self.LIBRARY, owner="ADMIN")
        self._sync(fake, ["A", "B"])
        self.assertEqual(fake.entries, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
