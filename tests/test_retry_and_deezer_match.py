import datetime
import tempfile
import unittest
from pathlib import Path

import shared


def NO_DIAG(count):
    return ["DIAG"]


class DeezerMatchTest(unittest.TestCase):
    def test_rejects_unrelated_top_hit_from_incident(self):
        self.assertFalse(
            shared.deezer_result_matches(
                "攬佬SKAI ISYOURGOD",
                "八方來財(Stacks from All Sides)",
                "张大炮",
                "HIT IT",
            )
        )
        self.assertFalse(
            shared.deezer_result_matches(
                "攬佬SKAI ISYOURGOD", "求签", "张大炮", "HIT IT"
            )
        )

    def test_accepts_same_track_with_decorations(self):
        self.assertTrue(
            shared.deezer_result_matches(
                "BABYMONSTER", "LIKE THAT", "BABYMONSTER", "LIKE THAT"
            )
        )
        self.assertTrue(
            shared.deezer_result_matches(
                "攬佬SKAI ISYOURGOD",
                "八方來財(Stacks from All Sides)",
                "SKAI ISYOURGOD",
                "八方來財",
            )
        )

    def test_same_artist_different_title_rejected(self):
        self.assertFalse(
            shared.deezer_result_matches("Adele", "Hello", "Adele", "Skyfall")
        )

    def test_empty_values_never_match(self):
        self.assertFalse(shared.deezer_result_matches("", "", "", ""))


class RetryBackoffTest(unittest.TestCase):
    def test_free_attempts_then_cooldown_then_due_again(self):
        state: dict = {}
        now = 1_000_000.0
        for _ in range(shared.RETRY_FREE_ATTEMPTS):
            self.assertTrue(shared.retry_due(state, "a", now))
            shared.record_retry_outcome(state, {"a"}, {"a"}, now)
        self.assertFalse(shared.retry_due(state, "a", now + 60))
        self.assertTrue(
            shared.retry_due(state, "a", now + shared.RETRY_COOLDOWN_SECONDS)
        )

    def test_success_clears_entry(self):
        state = {"a": {"failures": 5, "last_attempt": 1.0}}
        shared.record_retry_outcome(state, {"a"}, set(), 2.0)
        self.assertEqual(state, {})

    def test_unattempted_entries_untouched(self):
        state = {"b": {"failures": 4, "last_attempt": 1.0}}
        shared.record_retry_outcome(state, {"a"}, {"a"}, 2.0)
        self.assertEqual(state["b"], {"failures": 4, "last_attempt": 1.0})
        self.assertEqual(state["a"], {"failures": 1, "last_attempt": 2.0})

    def test_state_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "state.json"
            self.assertEqual(shared.load_retry_state(path), {})
            shared.save_retry_state(path, {"a": {"failures": 1, "last_attempt": 3.0}})
            self.assertEqual(
                shared.load_retry_state(path),
                {"a": {"failures": 1, "last_attempt": 3.0}},
            )
            path.write_text("{broken")
            self.assertEqual(shared.load_retry_state(path), {})


class ExhaustedNotifyTest(unittest.TestCase):
    def _state(self):
        return {
            "pl:a": {"failures": shared.RETRY_FREE_ATTEMPTS, "last_attempt": 1.0},
            "pl:b": {"failures": 1, "last_attempt": 1.0},
            "other:c": {"failures": 9, "last_attempt": 1.0},
        }

    def test_notifies_only_exhausted_owned_keys_once(self):
        state = self._state()
        sent: list[str] = []
        labels = {"pl:a": "Artist - A", "pl:b": "Artist - B"}
        shared.notify_exhausted_retries(
            state,
            labels,
            "歌单 X",
            send=lambda t: sent.append(t) or True,
            diagnose=NO_DIAG,
        )
        self.assertEqual(len(sent), 1)
        self.assertIn("Artist - A", sent[0])
        self.assertNotIn("Artist - B", sent[0])
        self.assertTrue(state["pl:a"]["notified"])
        self.assertNotIn("notified", state["other:c"])
        shared.notify_exhausted_retries(
            state,
            labels,
            "歌单 X",
            send=lambda t: sent.append(t) or True,
            diagnose=NO_DIAG,
        )
        self.assertEqual(len(sent), 1)

    def test_failed_send_keeps_alert_pending(self):
        state = self._state()
        shared.notify_exhausted_retries(
            state, {"pl:a": "A"}, "x", send=lambda t: False, diagnose=NO_DIAG
        )
        self.assertNotIn("notified", state["pl:a"])

    def test_notified_flag_survives_later_failure(self):
        state = {"pl:a": {"failures": 3, "last_attempt": 1.0, "notified": True}}
        shared.record_retry_outcome(state, {"pl:a"}, {"pl:a"}, 2.0)
        self.assertTrue(state["pl:a"]["notified"])
        self.assertEqual(state["pl:a"]["failures"], 4)


class DiagnosisTest(unittest.TestCase):
    today = datetime.date(2026, 9, 14)

    def test_outdated_ytdlp_points_to_image_rebuild(self):
        lines = shared.diagnose_download_failures(
            1, "2026.08.19", "2026.10.02", self.today
        )
        self.assertIn("PyPI 最新 2026.10.02", lines[0])
        self.assertIn("升级 yt-dlp", lines[1])
        self.assertEqual(lines[2], shared.RUNBOOK_HINT)

    def test_stale_ytdlp_flagged_even_if_pypi_lookup_failed(self):
        lines = shared.diagnose_download_failures(1, "2026.6.9", "", self.today)
        self.assertIn("查询失败", lines[0])
        self.assertIn("升级 yt-dlp", lines[1])

    def test_fresh_ytdlp_single_song_points_to_manual_link(self):
        lines = shared.diagnose_download_failures(
            1, "2026.08.19", "2026.08.19", self.today
        )
        self.assertIn("手动搜 YouTube", lines[1])

    def test_fresh_ytdlp_many_songs_points_to_logs(self):
        lines = shared.diagnose_download_failures(
            5, "2026.08.19", "2026.08.19", self.today
        )
        self.assertIn("sync.log", lines[1])

    def test_alert_text_contains_diagnosis(self):
        state = {"pl:a": {"failures": 3, "last_attempt": 1.0}}
        sent: list[str] = []
        shared.notify_exhausted_retries(
            state,
            {"pl:a": "A - B"},
            "x",
            send=lambda t: sent.append(t) or True,
            diagnose=NO_DIAG,
        )
        self.assertIn("🔎 诊断\nDIAG", sent[0])

    def test_song_label_includes_length(self):
        self.assertEqual(
            shared.song_label({"artist": "SVBE", "name": "GAMBLING", "duration": 125}),
            "SVBE - GAMBLING [2:05]",
        )
        self.assertEqual(shared.song_label({"artist": "A", "name": "B"}), "A - B")


class MoveToArtistAlbumFolderTest(unittest.TestCase):
    def _tagged(self, path, artist, album, title):
        import mutagen.id3 as id3

        path.write_bytes(b"\xff\xfb" + b"\x00" * 64)
        tags = id3.ID3()
        tags.add(id3.TPE1(encoding=3, text=[artist]))
        tags.add(id3.TALB(encoding=3, text=[album]))
        tags.add(id3.TIT2(encoding=3, text=[title]))
        tags.save(path)

    def test_moves_flat_download_into_artist_album_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            flat = base / "01. LISA - Rockstar.mp3"
            self._tagged(flat, "LISA/Tyla", "Alter: Ego", "Rock?star")
            moved = shared.move_to_artist_album_folder(flat, base)
            self.assertEqual(moved, base / "LISA, Tyla" / "Alter_ Ego" / "Rock_star.mp3")
            self.assertTrue(moved.exists())
            self.assertFalse(flat.exists())

    def test_keeps_file_when_target_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            flat = base / "01. A - T.mp3"
            self._tagged(flat, "A", "B", "T")
            (base / "A" / "B").mkdir(parents=True)
            (base / "A" / "B" / "T.mp3").write_text("existing")
            self.assertEqual(shared.move_to_artist_album_folder(flat, base), flat)
            self.assertTrue(flat.exists())


if __name__ == "__main__":
    unittest.main()
