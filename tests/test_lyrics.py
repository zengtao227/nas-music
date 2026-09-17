import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lyrics


class LyricsTest(unittest.TestCase):
    def test_snapshot_changed_and_playlist_exclusion(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            song = root / "a.mp3"
            playlist = root / "Playlists" / "p.mp3"
            playlist.parent.mkdir()
            song.write_bytes(b"a")
            playlist.write_bytes(b"a")
            before = lyrics.snapshot(root, True)
            song.write_bytes(b"bb")
            after = lyrics.snapshot(root, True)
            self.assertEqual(lyrics.changed_files(before, after), {song})
            self.assertNotIn(playlist, after)

    def test_missing_before_never_scans_all(self):
        with tempfile.TemporaryDirectory() as value:
            song = Path(value) / "a.mp3"
            song.write_bytes(b"a")
            self.assertEqual(
                lyrics.changed_files(None, lyrics.snapshot(Path(value))), set()
            )

    def test_atomic_no_clobber(self):
        with tempfile.TemporaryDirectory() as value:
            target = Path(value) / "a.lrc"
            self.assertTrue(lyrics._write(target, "one"))
            self.assertFalse(lyrics._write(target, "two"))
            self.assertEqual(target.read_text(), "one\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(target.parent.glob(".a.lrc.tmp-*")), [])

    def test_process_outcomes_and_existing(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            synced = root / "a.mp3"
            plain = root / "b.mp3"
            rejected = root / "c.mp3"
            missing = root / "d.mp3"
            existing = root / "e.mp3"
            for path in (synced, plain, rejected, missing, existing):
                path.write_bytes(b"x")
            existing.with_suffix(".lrc").write_text("old")
            before = {}
            after = lyrics.snapshot(root)

            def meta(path):
                return (path.stem, "artist", "album", 100.0)

            def fetch(meta):
                return {
                    "a": ("synced", "[00:00]x"),
                    "b": ("plain", "x"),
                    "c": ("rejected", None),
                    "d": ("no_match", None),
                }[meta[0]]

            with (
                patch.object(lyrics, "_metadata", meta),
                patch.object(lyrics, "_fetch", fetch),
                patch.object(lyrics.time, "sleep"),
            ):
                result = lyrics.process_changed(before, after)
            self.assertEqual(
                (
                    result.synced_written,
                    result.plain_written,
                    result.rejected,
                    result.no_match,
                    result.existing,
                ),
                (1, 1, 1, 1, 1),
            )

    def test_fetch_exception_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "a.mp3"
            path.write_bytes(b"x")
            with (
                patch.object(lyrics, "_metadata", return_value=("a", "b", "c", 1.0)),
                patch.object(lyrics, "_fetch", return_value=("error", None)),
                patch.object(lyrics.time, "sleep"),
            ):
                result = lyrics.process_changed({}, lyrics.snapshot(Path(value)))
            self.assertEqual(result.error, 1)

    def test_fetch_runtime_error_and_write_error_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "a.mp3"
            path.write_bytes(b"x")
            with (
                patch.object(lyrics, "_metadata", return_value=("a", "b", "c", 1.0)),
                patch.object(lyrics, "_fetch", side_effect=RuntimeError),
                patch.object(lyrics.time, "sleep"),
            ):
                self.assertEqual(
                    lyrics.process_changed({}, lyrics.snapshot(Path(value))).error, 1
                )
            with (
                patch.object(lyrics, "_metadata", return_value=("a", "b", "c", 1.0)),
                patch.object(lyrics, "_fetch", return_value=("synced", "x")),
                patch.object(lyrics, "_write", side_effect=OSError),
                patch.object(lyrics.time, "sleep"),
            ):
                self.assertEqual(
                    lyrics.process_changed({}, lyrics.snapshot(Path(value))).error, 1
                )

    def test_duration_and_payload_rejections(self):
        meta = ("a", "b", "c", 100.0)

        class Response:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.value).encode()

        for payload in (
            {"duration": 104, "syncedLyrics": "x"},
            {"duration": float("nan")},
            [],
        ):
            self.assertEqual(
                lyrics._fetch(
                    meta, lambda *args, payload=payload, **kwargs: Response(payload)
                )[0],
                "rejected",
            )

    def test_cache_reuse(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            for name in ("a.mp3", "b.mp3"):
                (root / name).write_bytes(b"x")
            with (
                patch.object(lyrics, "_metadata", return_value=("a", "b", "c", 1.0)),
                patch.object(
                    lyrics, "_fetch", return_value=("no_match", None)
                ) as fetch,
                patch.object(lyrics.time, "sleep"),
            ):
                result = lyrics.process_changed({}, lyrics.snapshot(root))
            self.assertEqual(
                (result.queried, result.cache_hits, fetch.call_count), (1, 1, 1)
            )


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


def _get_404_then(search_payload):
    from urllib.error import HTTPError

    def request(req, timeout):
        if req.full_url.startswith(lyrics.API_SEARCH):
            return _Response(search_payload)
        raise HTTPError(req.full_url, 404, "not found", {}, None)

    return request


class LyricsSearchStageTest(unittest.TestCase):
    META = ("lovely (with Khalid)", "Billie Eilish, Khalid", "Best of Glasto", 200.0)

    def test_search_accepts_other_album_with_same_title_artist_duration(self):
        payload = [
            {"trackName": "lovely", "artistName": "Billie Eilish & Khalid",
             "duration": 201, "plainLyrics": "plain"},
            {"trackName": "lovely (with Khalid)", "artistName": "Billie Eilish",
             "duration": 200, "syncedLyrics": "[00:01]synced"},
        ]
        self.assertEqual(
            lyrics._fetch(self.META, _get_404_then(payload)), ("synced", "[00:01]synced")
        )

    def test_search_rejects_duration_or_title_mismatch(self):
        payload = [
            {"trackName": "lovely", "artistName": "Billie Eilish",
             "duration": 196, "syncedLyrics": "[00:01]other version"},
            {"trackName": "ocean eyes", "artistName": "Billie Eilish",
             "duration": 200, "syncedLyrics": "[00:01]other song"},
        ]
        self.assertEqual(lyrics._fetch(self.META, _get_404_then(payload)), ("no_match", None))

    def test_search_skips_instrumental_without_text(self):
        payload = [{"trackName": "lovely", "artistName": "Billie Eilish",
                    "duration": 200, "instrumental": True}]
        self.assertEqual(lyrics._fetch(self.META, _get_404_then(payload)), ("no_match", None))

    def test_embedded_lyrics_are_not_duplicated(self):
        import mutagen.id3 as id3

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            song = root / "a.mp3"
            song.write_bytes(b"\xff\xfb" + b"\x00" * 64)
            tags = id3.ID3()
            tags.add(id3.USLT(encoding=3, lang="eng", desc="", text="embedded"))
            tags.save(song)
            with (
                patch.object(lyrics, "_metadata", return_value=("a", "b", "c", 1.0)),
                patch.object(lyrics, "_fetch", return_value=("synced", "x")) as fetch,
            ):
                result = lyrics.process_changed({}, lyrics.snapshot(root))
            self.assertEqual((result.existing, fetch.call_count), (1, 0))
            self.assertFalse(song.with_suffix(".lrc").exists())


if __name__ == "__main__":
    unittest.main()
