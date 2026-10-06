import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import shared
from spotapi.client import BaseClient
from spotapi.http.request import TLSClient


HASHES = "\n".join(
    f'"{name}","query","{"a" * 64}"' for name in sorted(shared.SPOTIFY_REQUIRED_QUERIES)
)


class SpotifyHashCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cache.json"
        self.path_patch = patch.object(shared, "SPOTIFY_HASH_CACHE_FILE", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.base = SimpleNamespace(
            js_pack="https://open.spotifycdn.com/web-player.old.js",
            raw_hashes=None,
        )

        def download(base: SimpleNamespace) -> None:
            base.raw_hashes = HASHES

        self.loader = Mock(side_effect=download)

    def test_miss_then_hit_keeps_original_hash_lookup_format(self) -> None:
        shared._spotify_hash_cache(self.base, self.loader)
        self.assertEqual(self.loader.call_count, 1)
        first = self.path.read_bytes()
        self.base.raw_hashes = None
        shared._spotify_hash_cache(self.base, self.loader)
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(self.path.read_bytes(), first)
        for name in shared.SPOTIFY_REQUIRED_QUERIES:
            self.assertEqual(BaseClient.part_hash(self.base, name), "a" * 64)

    def test_version_change_downloads_once_and_replaces_cache(self) -> None:
        shared._spotify_hash_cache(self.base, self.loader)
        self.base.js_pack = "https://open.spotifycdn.com/web-player.new.js"
        shared._spotify_hash_cache(self.base, self.loader)
        self.assertEqual(self.loader.call_count, 2)
        self.assertIn(self.base.js_pack, json.loads(self.path.read_text())["bundles"])
        self.base.js_pack = "https://open.spotifycdn.com/web-player.old.js"
        shared._spotify_hash_cache(self.base, self.loader)
        self.assertEqual(self.loader.call_count, 2)

    def test_four_rotating_versions_do_not_evict_each_other(self) -> None:
        urls = [f"https://open.spotifycdn.com/web-player.v{i}.js" for i in range(4)]
        for url in urls + urls:
            self.base.js_pack = url
            shared._spotify_hash_cache(self.base, self.loader)
        self.assertEqual(self.loader.call_count, 4)

    def test_separate_clients_share_cache(self) -> None:
        shared._spotify_hash_cache(self.base, self.loader)
        second = SimpleNamespace(js_pack=self.base.js_pack, raw_hashes=None)
        shared._spotify_hash_cache(second, self.loader)
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(second.raw_hashes, self.base.raw_hashes)

    def test_valid_cache_missing_one_query_is_refreshed(self) -> None:
        shared._spotify_hash_cache(self.base, self.loader)
        cached = json.loads(self.path.read_text())
        cached["bundles"][self.base.js_pack] = f'"fetchPlaylist","query","{"a" * 64}"'
        self.path.write_text(json.dumps(cached))
        shared._spotify_hash_cache(self.base, self.loader)
        self.assertEqual(self.loader.call_count, 2)

    def test_two_processes_cold_start_download_only_once(self) -> None:
        context = multiprocessing.get_context("fork")
        downloads = context.Value("i", 0)

        def worker() -> None:
            def loader(base: SimpleNamespace) -> None:
                with downloads.get_lock():
                    downloads.value += 1
                base.raw_hashes = HASHES

            shared._spotify_hash_cache(self.base, loader)

        processes = [context.Process(target=worker) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(downloads.value, 1)
        self.assertEqual(len(list(self.path.parent.glob(".spotify-query-*"))), 0)

    def test_corrupt_or_missing_required_hash_uses_normal_loader(self) -> None:
        for invalid in (
            "{broken",
            json.dumps({"hashes": '"fetchPlaylist","query","bad"'}),
        ):
            with self.subTest(invalid=invalid):
                self.path.write_text(invalid)
                before = self.loader.call_count
                shared._spotify_hash_cache(self.base, self.loader)
                self.assertEqual(self.loader.call_count, before + 1)
                self.assertEqual(self.base.raw_hashes, HASHES)

    def test_write_failure_preserves_successful_download(self) -> None:
        with patch.object(
            shared.tempfile, "NamedTemporaryFile", side_effect=PermissionError
        ):
            shared._spotify_hash_cache(self.base, self.loader)
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(self.base.raw_hashes, HASHES)

    def test_original_failure_is_not_swallowed_or_repeated(self) -> None:
        loader = Mock(side_effect=OSError("upstream"))
        with self.assertRaisesRegex(OSError, "upstream"):
            shared._spotify_hash_cache(self.base, loader)
        self.assertEqual(loader.call_count, 1)

    def test_malformed_request_preserves_response_without_refresh(self) -> None:
        rejected = SimpleNamespace(
            response={"errors": [{"message": "PersistedQueryNotFound"}]}
        )
        client = SimpleNamespace(_nas_hash_base=SimpleNamespace(get_session=Mock()))
        for params in (
            {},
            {"operationName": "fetchPlaylist", "extensions": "broken"},
            {"operationName": "fetchPlaylist", "extensions": "[]"},
            {
                "operationName": "fetchPlaylist",
                "extensions": '{"persistedQuery": null}',
            },
        ):
            with (
                self.subTest(params=params),
                patch.object(BaseClient, "get_sha256_hash", self.loader),
                patch.object(
                    BaseClient, "_nas_hash_cache_installed", False, create=True
                ),
                patch.object(
                    TLSClient, "post", Mock(return_value=rejected)
                ) as original_post,
                patch.object(shared, "_spotify_hash_cache") as cache,
            ):
                shared._install_spotify_hash_cache()
                result = TLSClient.post(
                    client,
                    "https://api-partner.spotify.com/pathfinder/v1/query",
                    params=params,
                )
                self.assertIs(result, rejected)
                self.assertEqual(original_post.call_count, 1)
                cache.assert_not_called()

    def test_server_rejection_refreshes_and_retries_only_once(self) -> None:
        rejected = SimpleNamespace(
            response={"errors": [{"message": "PersistedQueryNotFound"}]}
        )
        original_post = Mock(return_value=rejected)
        client = SimpleNamespace()
        base = SimpleNamespace(
            client=client, get_session=Mock(), part_hash=Mock(return_value="b" * 64)
        )
        client._nas_hash_base = base
        with (
            patch.object(BaseClient, "get_sha256_hash", self.loader),
            patch.object(BaseClient, "_nas_hash_cache_installed", False, create=True),
            patch.object(TLSClient, "post", original_post),
            patch.object(shared, "_spotify_hash_cache") as cache,
        ):
            shared._install_spotify_hash_cache()
            params = {
                "operationName": "fetchPlaylist",
                "extensions": json.dumps({"persistedQuery": {"sha256Hash": "a" * 64}}),
            }
            TLSClient.post(
                client,
                "https://api-partner.spotify.com/pathfinder/v1/query",
                params=params,
            )
            self.assertEqual(original_post.call_count, 2)
            cache.assert_called_once_with(base, self.loader, refresh=True)
            retry = original_post.call_args.kwargs["params"]
            self.assertEqual(
                json.loads(retry["extensions"])["persistedQuery"]["sha256Hash"],
                "b" * 64,
            )
            self.assertEqual(
                json.loads(params["extensions"])["persistedQuery"]["sha256Hash"],
                "a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
