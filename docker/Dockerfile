FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip && apt-get clean
RUN pip install --no-cache-dir spotdl streamrip
# WHY: spotdl's resolved yt-dlp lagged ~3 months behind; stale extractors got
# HTTP 403 from YouTube on every download (2026-09-14 traffic incident).
RUN pip install --no-cache-dir --upgrade yt-dlp yt-dlp-ejs
COPY patch_spotipyfree.py /tmp/patch.py
RUN python3 /tmp/patch.py
COPY patch_format_logging.py /tmp/patch_format_logging.py
RUN python3 /tmp/patch_format_logging.py

# Deno runtime — required by yt-dlp/spotdl for YouTube JS challenges (nsig/poToken).
# Without it, some YouTube videos fail with "Some YouTube downloads require Deno".
RUN curl -fsSL -o /tmp/deno.zip \
      https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip && \
    unzip -q /tmp/deno.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/deno && \
    rm /tmp/deno.zip

WORKDIR /music
ENTRYPOINT ["spotdl"]
