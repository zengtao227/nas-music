# Agent Demand Gate: Jellyfin Playlist Auto-Rebuild

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
- Success standard: NAS cron updates both downloaded files and Jellyfin playlist XML without manual rebuilds.
- Pause / kill signal: XML rebuild corrupts playlist files, drops valid entries, or blocks normal downloads.
- Degraded fallback: Restore the timestamped Jellyfin playlist XML backup and run the previous sync script without the Jellyfin mount.
- Owner and review cadence: zengtao227 reviews sync logs and playlist counts after the initial deployment, then relies on cron logs for ongoing monitoring.
