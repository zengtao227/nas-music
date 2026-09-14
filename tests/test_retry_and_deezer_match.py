import tempfile
import unittest
from pathlib import Path

import shared


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


if __name__ == "__main__":
    unittest.main()
