# Task Eval: Automatic Lyrics For New Music

## Goal
- Every MP3 newly downloaded by the Liked Songs or configured-playlist sync receives a strict LRCLIB lookup in the same run without making music synchronization depend on lyric availability.

## Acceptance Criteria
- [x] Shared lyric code reads ID3 title, artist, album, and duration and creates a same-basename `.lrc` or `.txt` only when no lyric sidecar exists.
- [x] Strict LRCLIB results with a duration difference above 3 seconds are rejected.
- [x] LRCLIB misses, timeouts, rate limits, invalid metadata, and write failures are logged but never change the successful music-sync outcome.
- [x] Both normal downloads and repaired/fallback downloads are detected through before/after MP3 snapshots in Liked Songs and playlist scopes.
- [x] Existing MP3 files, lyric sidecars, playlist XML, cookies, tokens, and unrelated services are not modified.
- [x] Unit tests cover synced, plain, no-match, rejected, error, existing-sidecar, atomic no-clobber, cache reuse, and non-fatal processing behavior.
- [x] NAS deployment uses timestamped backups and both sync paths can import the shared module inside `spotdl-local:latest`.
- [x] A controlled NAS smoke test creates a lyric sidecar for a test fixture or newly introduced copy without affecting the production MP3 source.

## Verification
- Command: `python3 -m py_compile lyrics.py sync_liked.py sync_playlists.py tests/test_lyrics.py`
- Expected: Exit 0.
- Command: `ruff check lyrics.py sync_liked.py sync_playlists.py tests --output-format=concise`
- Expected: Exit 0.
- Command: `ruff format lyrics.py sync_liked.py sync_playlists.py tests --check`
- Expected: Exit 0.
- Command: `mypy lyrics.py sync_liked.py sync_playlists.py --ignore-missing-imports --no-strict-optional --check-untyped-defs`
- Expected: Exit 0.
- Command: `python3 -m unittest discover -s tests -v`
- Expected: All tests pass without adding a runtime or test dependency.
- Command: `ssh nas '<container import and controlled lyric smoke checks>'`
- Expected: Import succeeds, one strict sidecar is created atomically, and Jellyfin health remains HTTP 200.

## Manual Checks
- [x] Liked and playlist logs show bounded lyric matched/missed/rejected/error counters.
- [x] A lyric failure is visibly non-fatal and does not roll back a downloaded track.
- [x] A timestamped rollback copy of the previous sync files exists without touching downloaded music.

## Result
- Status: PASS
- Evidence:
  - Agent-demand gate selected deterministic non-agent automation.
  - Existing one-time strict downloader achieved 992/1,172 Jellyfin lyric coverage with zero batch errors.
  - Local verification passed: Python compilation, 8 unit tests, Ruff lint/format, and mypy.
  - Container import verified `lyrics`, both sync scripts, and mutagen inside `spotdl-local:latest`.
  - Isolated NAS smoke copied `See You Again` into a temporary writable mount and wrote one 2,849-byte synchronized sidecar with mode 0644; MP3 hashes remained unchanged.
  - Production Liked, Summer 26, and Can Dances sync logs each emitted `Lyrics:` with `changed=0` and `error=0`, then completed normally.
  - After rebasing onto upstream's Calm and Katseye Animal playlist additions, five consecutive production `sync_playlists.py` runs (2026-08-08 16:01–16:20) each emitted a clean `Lyrics:` summary with `error=0` for all four playlists — Summer 26, Can Dances, Calm, Katseye Animal — ending in `Done.` with zero traceback/ImportError/lyrics-processing-failure lines anywhere after deployment.
  - Post-deployment state: Jellyfin HTTP 200, MP3=1,172, `.lrc`=935, `.txt`=57, zero empty lyrics, zero lyric temp files, and no residual sync containers (only the actively-running scheduled cron sync observed mid-execution).
  - NAS SHA-256 for `lyrics.py`, `sync_liked.py`, and the rebased `sync_playlists.py` (including Calm/Katseye Animal) match the local commit exactly.
- Remaining risks:
  - LRCLIB exact misses and exhausted transient retries remain without lyrics until the MP3 changes or a manual backfill is run.
  - Official Jellyfin LrcLib provider remains installed but does not supply candidates on Jellyfin 10.11.11; automatic sync uses the direct strict module instead.
