"""Build-time patch: log the yt-dlp format actually selected for each download.

Why: 2026-09-14 traffic-alert investigation found spotdl's format selector falls
back to "best" (a muxed video+audio stream) when "bestaudio" can't be resolved,
which can be far larger than an audio-only stream. There was no record of which
format was actually chosen per track, only after-the-fact file sizes. This adds
that record so future spikes can be diagnosed without re-deriving it from disk
timestamps and Deco byte counters.

Writes one line per completed download to /music/.ytdlp_format_choice.log
(same directory/convention as the existing .spotdl_*.log files, so it rotates
along with them and survives container restarts via the /music bind mount).
"""

import pathlib

f = pathlib.Path("/usr/local/lib/python3.11/site-packages/spotdl/providers/audio/base.py")
content = f.read_text()

# Insert the hook function once, right after the module logger is created.
old_anchor = 'logger = logging.getLogger(__name__)\n'
if "_log_ytdlp_format_choice" not in content:
    new_anchor = old_anchor + '''

def _log_ytdlp_format_choice(d):
    """Append the actually-selected yt-dlp format to a project log file.

    Best-effort only: any failure here must never break a real download.
    """
    if d.get("status") != "finished":
        return
    try:
        import datetime

        info = d.get("info_dict") or {}
        line = (
            "[{ts}] id={id} format_id={fmt} ext={ext} vcodec={vcodec} "
            "acodec={acodec} bytes={size}\\n"
        ).format(
            ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            id=info.get("id"),
            fmt=info.get("format_id"),
            ext=info.get("ext"),
            vcodec=info.get("vcodec"),
            acodec=info.get("acodec"),
            size=info.get("filesize") or info.get("filesize_approx"),
        )
        with open("/music/.ytdlp_format_choice.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass
'''
    content = content.replace(old_anchor, new_anchor, 1)

# Wire the hook into the yt-dlp options dict built in AudioProvider.__init__.
old_opts = '''        yt_dlp_options = {
            "format": ytdl_format,
            "quiet": True,
            "no_warnings": True,
            "encoding": "UTF-8",
            "logger": YTDLLogger(),
            "cookiefile": self.cookie_file,
            "outtmpl": str((get_temp_path() / "%(id)s.%(ext)s").resolve()),
            "retries": 5,
            "extractor_args": {},
        }'''
new_opts = '''        yt_dlp_options = {
            "format": ytdl_format,
            "quiet": True,
            "no_warnings": True,
            "encoding": "UTF-8",
            "logger": YTDLLogger(),
            "cookiefile": self.cookie_file,
            "outtmpl": str((get_temp_path() / "%(id)s.%(ext)s").resolve()),
            "retries": 5,
            "extractor_args": {},
            "progress_hooks": [_log_ytdlp_format_choice],
        }'''
content = content.replace(old_opts, new_opts)

f.write_text(content)

checks = [
    ("hook function inserted", "_log_ytdlp_format_choice(d)"),
    ("hook wired into options", '"progress_hooks": [_log_ytdlp_format_choice]'),
]
for label, needle in checks:
    status = "OK" if needle in content else "MISSING"
    print(f"{label}: {status}")
