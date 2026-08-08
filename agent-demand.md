# Agent Demand Gate: Jellyfin Playlist Auto-Rebuild & Auto-Create

## 1. Friction Point
- Current user friction: Finamp's playlist count can silently drift from downloaded playlist files because Jellyfin playlist XML is not rebuilt automatically.
- Who experiences it: Mia using Finamp, and the maintainer diagnosing NAS sync failures.
- Why fixed rules or normal automation are insufficient: They are sufficient; the workflow is deterministic and does not need an AI agent.
- Evidence source: Can Dances had 110 MP3 files on disk while Jellyfin playlist XML exposed 95 items to Finamp.

## 2. Quantified Gap
- Baseline metric: Can Dances playlist XML lagged by 15 playable files; Summer 26 also had stale XML entries.
- Target metric: Jellyfin playlist XML item count equals the number of playable MP3 files tracked for that playlist after every sync run.
- Failure or exit point: Any sync run that finishes without refreshing playlist XML after download/removal/repair.
- Acceptable error / misclassification rate: 0 stale playable files in the Jellyfin playlist XML; unavailable tracks may remain logged as missing.
- Measurement window: Every 5-minute playlist sync cycle.

## 3. Solution Choice
- Recommended path: non-agent-automation
- Why this path fits current data and change frequency: Playlist membership, local files, and XML output are all deterministic state transitions.
- Why the rejected paths are weaker: A prompt-chain or agent would add nondeterminism and operational risk without improving correctness.
- Smallest useful prototype: Mount Jellyfin playlist XML into the playlist sync container and rewrite the XML from actual MP3 files at the end of each run.

## 4. Success Preview And Risk Plan
- Success standard: NAS cron updates both downloaded files and Jellyfin playlist XML without manual rebuilds. Adding a new PLAYLISTS entry without a jellyfin_id automatically creates the playlist in Jellyfin and populates its XML on the second run.
- Pause / kill signal: XML rebuild corrupts playlist files, drops valid entries, blocks normal downloads, or creates duplicate Jellyfin playlists.
- Degraded fallback: Restore the timestamped Jellyfin playlist XML backup and run the previous sync script without the Jellyfin mount. Delete any duplicate Jellyfin playlists via Web UI.
- No-duplicate guarantee: transient API lookup failure returns None → no creation; only a confirmed empty result triggers POST /Playlists.
- Owner and review cadence: zengtao227 reviews sync logs and playlist counts after the initial deployment, then relies on cron logs for ongoing monitoring.

---

# Agent Demand Gate: Automatic Lyrics For New Music

## 1. Friction Point
- Current user friction: Songs added through Mia's Spotify Liked Songs or configured playlists arrive in Finamp without lyrics unless the maintainer runs a separate batch downloader.
- Who experiences it: Mia during playback and the maintainer performing manual lyric backfills.
- Why fixed rules or normal automation are insufficient: They are sufficient; ID3 metadata plus LRCLIB's deterministic exact-match endpoint provides a rule-based workflow and does not need an AI agent.
- Evidence source: The 2026-08-08 one-time strict backfill created lyrics for 992 of 1,172 MP3 files, while newly downloaded songs currently have no automatic lyric step.

## 2. Quantified Gap
- Baseline metric: 0% of future MP3 downloads automatically trigger a lyric lookup; backfilling requires a separate manual run.
- Target metric: 100% of newly created MP3 files are checked once per sync run, and every exact LRCLIB match receives one same-basename `.lrc` or `.txt` sidecar before the run finishes.
- Failure or exit point: A lyric lookup blocks or changes a successful music download, overwrites an existing sidecar, or writes a response whose duration differs from the local audio by more than 3 seconds.
- Acceptable error / misclassification rate: 0 overwritten or knowingly mismatched lyrics; LRCLIB misses and transient network failures may remain without lyrics and must be logged without failing music sync.
- Measurement window: Every 5-minute Liked Songs and playlist sync cycle, reviewed across the next 20 new MP3 files or 7 days, whichever comes first.

## 3. Solution Choice
- Recommended path: non-agent-automation
- Why this path fits current data and change frequency: The inputs, exact-match rules, retry limits, sidecar formats, and failure behavior are deterministic and already validated on the current library.
- Why the rejected paths are weaker: Prompt chains, workflow agents, or fine-tuning add nondeterminism and cost without improving exact metadata matching.
- Smallest useful prototype: Extract the validated LRCLIB lookup and atomic sidecar writer into one shared module, then invoke it only for MP3 files created or repaired by `sync_liked.py` and `sync_playlists.py`.

## 4. Success Preview And Risk Plan
- Success standard: A new Like or configured-playlist addition downloads its MP3 and, when an exact LRCLIB result exists, its lyric sidecar in the same sync run; existing lyrics and music files remain untouched.
- Pause / kill signal: Any lyric exception escapes into the main sync, download duration materially increases beyond the bounded per-track lookup budget, wrong-version lyrics are observed, or existing sidecars change.
- Degraded fallback: Disable the lyric call sites while leaving music synchronization unchanged; keep the standalone strict batch downloader for manual backfills.
- Owner and review cadence: zengtao227 checks the Liked Songs and playlist logs after deployment and reviews matched/missed/error counters after 7 days or 20 new MP3 files.
