#!/bin/bash
# Runs as root via cron (daily 08:00). Checks sp_dc cookie validity and notifies
# via Telegram if expired.
#
# Telegram credentials live OUTSIDE git in /volume1/homes/Mia/Music/.telegram_config
# (chmod 600), containing two lines:
#   BOT_TOKEN=...
#   CHAT_ID=...
MUSIC_DIR="/volume1/homes/Mia/Music"
SP_DC_FILE="$MUSIC_DIR/.spotify_sp_dc"
TELEGRAM_CONFIG="$MUSIC_DIR/.telegram_config"

if [ ! -f "$TELEGRAM_CONFIG" ]; then
  echo "ERROR: $TELEGRAM_CONFIG missing — cannot send alerts" >&2
  exit 1
fi
# shellcheck source=/dev/null
. "$TELEGRAM_CONFIG"

notify() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    --data-urlencode "text=$1" \
    -d "chat_id=${CHAT_ID}" > /dev/null
}

SP_DC=$(cat "$SP_DC_FILE" 2>/dev/null)
if [ -z "$SP_DC" ]; then
  notify "⚠️ NAS Music: sp_dc 文件不存在，音乐同步中断"
  exit 1
fi

HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'User-Agent: Mozilla/5.0' \
  -H "Cookie: sp_dc=${SP_DC}" \
  'https://open.spotify.com/get_access_token?reason=transport&productType=web-player')

if [ "$HTTP_CODE" != "200" ]; then
  notify "⚠️ NAS Music: Spotify cookie 失效（HTTP ${HTTP_CODE}），请重新从浏览器复制 sp_dc 并更新 ${SP_DC_FILE}"
fi
