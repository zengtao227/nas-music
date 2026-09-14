import json
import tempfile
import unittest
from pathlib import Path

import playlist_discovery

OWNER_URI = "spotify:user:mia"
OTHER_OWNER_URI = "spotify:user:other"


def library_response(
    playlists: list[tuple[str, str, str]],
    extra_items: int = 0,
    reported_total: int | None = None,
) -> dict:
    items: list[dict] = []
    for playlist_id, name, owner_uri in playlists:
        items.append(
            {
                "item": {
                    "data": {
                        "__typename": "Playlist",
                        "uri": f"spotify:playlist:{playlist_id}",
                        "name": name,
                        "ownerV2": {"data": {"uri": owner_uri}},
                    }
                }
            }
        )
    for index in range(extra_items):
        items.append(
            {
                "item": {
                    "data": {
                        "__typename": "Album",
                        "uri": f"spotify:album:{index}",
                    }
                }
            }
        )
    total: int = len(items) if reported_total is None else reported_total
    return {
        "data": {
            "me": {
                "libraryV3": {
                    "items": items,
                    "totalCount": total,
                }
            }
        }
    }


class PlaylistDiscoveryTest(unittest.TestCase):
    def test_missing_owner_is_deferred_then_enrolled_when_owner_resolves(self):
        with tempfile.TemporaryDirectory() as value:
            state_path = Path(value) / "state.json"
            initial = library_response([("old", "Old", OWNER_URI)])
            playlist_discovery.update_discovery_state(
                state_path, initial, OWNER_URI, []
            )
            next_response = library_response(
                [("old", "Old", OWNER_URI), ("unknown", "Unknown", "")]
            )
            unknown_data = next_response["data"]["me"]["libraryV3"]["items"][1]["item"][
                "data"
            ]
            unknown_data["ownerV2"] = {"data": None}
            outcome = playlist_discovery.update_discovery_state(
                state_path, next_response, OWNER_URI, []
            )
            state = json.loads(state_path.read_text())
            self.assertEqual(outcome.playlists, ())
            self.assertEqual(outcome.deferred_unknown_owner, 1)
            self.assertNotIn("unknown", state["seen_playlist_ids"])

            resolved = playlist_discovery.update_discovery_state(
                state_path,
                library_response(
                    [("old", "Old", OWNER_URI), ("unknown", "Unknown", OWNER_URI)]
                ),
                OWNER_URI,
                [],
            )
            self.assertEqual(resolved.playlists[0]["id"], "unknown")

    def test_rejects_incomplete_library_before_state_write(self):
        with tempfile.TemporaryDirectory() as value:
            state_path = Path(value) / "state.json"
            with self.assertRaisesRegex(
                playlist_discovery.DiscoveryError, "incomplete library snapshot"
            ):
                playlist_discovery.update_discovery_state(
                    state_path,
                    library_response([("old", "Old", OWNER_URI)], reported_total=2),
                    OWNER_URI,
                    [],
                )
            self.assertFalse(state_path.exists())

    def test_first_run_baselines_every_current_playlist_without_enrollment(self):
        with tempfile.TemporaryDirectory() as value:
            state_path = Path(value) / "state.json"
            outcome = playlist_discovery.update_discovery_state(
                state_path,
                library_response(
                    [
                        ("owned-old", "Owned Old", OWNER_URI),
                        ("followed-old", "Followed Old", OTHER_OWNER_URI),
                    ],
                    extra_items=3,
                ),
                OWNER_URI,
                [],
            )
            state = json.loads(state_path.read_text())
            self.assertTrue(outcome.initialized)
            self.assertEqual(outcome.library_playlist_count, 2)
            self.assertEqual(outcome.playlists, ())
            self.assertEqual(state["seen_playlist_ids"], ["followed-old", "owned-old"])
            self.assertEqual(state["auto_playlists"], [])
            self.assertEqual(
                json.loads(state_path.with_suffix(".json.bak").read_text()), state
            )

    def test_only_new_owned_playlist_is_enrolled_after_baseline(self):
        with tempfile.TemporaryDirectory() as value:
            state_path = Path(value) / "state.json"
            initial = [("old", "Old", OWNER_URI)]
            playlist_discovery.update_discovery_state(
                state_path, library_response(initial), OWNER_URI, []
            )
            outcome = playlist_discovery.update_discovery_state(
                state_path,
                library_response(
                    initial
                    + [
                        ("new-owned-123", "Mía's New Mix", OWNER_URI),
                        ("new-followed", "Official Mix", OTHER_OWNER_URI),
                    ]
                ),
                OWNER_URI,
                [],
            )
            state = json.loads(state_path.read_text())
            self.assertFalse(outcome.initialized)
            self.assertEqual(len(outcome.discovered), 1)
            self.assertEqual(outcome.ignored_new, 1)
            self.assertEqual(outcome.playlists[0]["id"], "new-owned-123")
            self.assertEqual(outcome.playlists[0]["folder"], "mia_s_new_mix_new-owne")
            self.assertEqual(
                state["seen_playlist_ids"],
                ["new-followed", "new-owned-123", "old"],
            )

    def test_duplicate_jellyfin_name_gets_id_suffix(self):
        with tempfile.TemporaryDirectory() as value:
            state_path = Path(value) / "state.json"
            playlist_discovery.update_discovery_state(
                state_path,
                library_response([("old", "Old", OWNER_URI)]),
                OWNER_URI,
                [],
            )
            outcome = playlist_discovery.update_discovery_state(
                state_path,
                library_response(
                    [
                        ("old", "Old", OWNER_URI),
                        ("abcdef123", "Calm", OWNER_URI),
                    ]
                ),
                OWNER_URI,
                [{"id": "static", "name": "calm", "jellyfin_name": "Calm"}],
            )
            self.assertEqual(outcome.playlists[0]["jellyfin_name"], "Calm [abcdef]")

    def test_removed_auto_playlist_becomes_inactive_without_state_deletion(self):
        with tempfile.TemporaryDirectory() as value:
            state_path = Path(value) / "state.json"
            playlist_discovery.update_discovery_state(
                state_path,
                library_response([("old", "Old", OWNER_URI)]),
                OWNER_URI,
                [],
            )
            playlist_discovery.update_discovery_state(
                state_path,
                library_response(
                    [("old", "Old", OWNER_URI), ("new", "New", OWNER_URI)]
                ),
                OWNER_URI,
                [],
            )
            outcome = playlist_discovery.update_discovery_state(
                state_path,
                library_response([("old", "Old", OWNER_URI)]),
                OWNER_URI,
                [],
            )
            state = json.loads(state_path.read_text())
            self.assertEqual(outcome.playlists, ())
            self.assertEqual(outcome.inactive, 1)
            self.assertEqual(state["auto_playlists"][0]["id"], "new")

    def test_corrupt_primary_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as value:
            state_path = Path(value) / "state.json"
            playlist_discovery.update_discovery_state(
                state_path,
                library_response([("old", "Old", OWNER_URI)]),
                OWNER_URI,
                [],
            )
            playlist_discovery.update_discovery_state(
                state_path,
                library_response(
                    [("old", "Old", OWNER_URI), ("new", "New", OWNER_URI)]
                ),
                OWNER_URI,
                [],
            )
            state_path.write_text("not json")
            outcome = playlist_discovery.update_discovery_state(
                state_path,
                library_response(
                    [("old", "Old", OWNER_URI), ("new", "New", OWNER_URI)]
                ),
                OWNER_URI,
                [],
            )
            self.assertTrue(outcome.recovered_from_backup)
            self.assertEqual(outcome.playlists[0]["id"], "new")
            self.assertEqual(json.loads(state_path.read_text())["version"], 1)


if __name__ == "__main__":
    unittest.main()
