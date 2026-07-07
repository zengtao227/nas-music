#!/bin/bash
# Runs as root via cron (daily 08:00). Verifies the liked-sync pipeline is actually
# working by reading its own log, and notifies via Telegram when it is stale or
# failing. Healthy runs are silent.
#
# WHY log-based instead of probing Spotify: since 2025-03 Spotify requires TOTP on
# open.spotify.com/get_access_token, so that endpoint returns 403 even for a valid
# sp_dc cookie — probing it produces false "cookie expired" alarms. The sync log is
# the same source of truth as production: "Spotify liked: N" is printed only after
# a complete, integrity-checked fetch through the sp_dc cookie (sync_liked.py).
#
# Telegram credentials live OUTSIDE git in /volume1/homes/Mia/Music/.telegram_config
# (chmod 600), containing two lines:
#   BOT_TOKEN=...
#   CHAT_ID=...
#
# Env overrides (for testing): MUSIC_DIR, MAX_AGE_MINUTES, DRY_RUN=1 prints the
# alert to stdout instead of sending it.
MUSIC_DIR="${MUSIC_DIR:-/volume1/homes/Mia/Music}"
LOG_FILE="$MUSIC_DIR/.spotdl_liked_sync.log"
TELEGRAM_CONFIG="$MUSIC_DIR/.telegram_config"
MAX_AGE_MINUTES="${MAX_AGE_MINUTES:-30}"

notify() {
  if [ -n "$DRY_RUN" ]; then
    echo "ALERT: $1"
    return
  fi
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    --data-urlencode "text=$1" \
    -d "chat_id=${CHAT_ID}" > /dev/null
}

if [ -z "$DRY_RUN" ]; then
  if [ ! -f "$TELEGRAM_CONFIG" ]; then
    echo "ERROR: $TELEGRAM_CONFIG missing — cannot send alerts" >&2
    exit 1
  fi
  # shellcheck source=/dev/null
  . "$TELEGRAM_CONFIG"
fi

if [ ! -f "$LOG_FILE" ]; then
  notify "⚠️ NAS Music: 同步日志 ${LOG_FILE} 不存在，liked 同步可能从未运行"
  exit 1
fi

# --- Freshness: the shell wrapper stamps "Liked sync done" after every run
# (success or not), so a stale timestamp means cron/Docker/log rotation broke.
LAST_DONE=$(grep 'Liked sync done' "$LOG_FILE" | tail -1 | sed -E 's/^\[([^]]+)\].*/\1/')
if [ -z "$LAST_DONE" ] && [ -f "${LOG_FILE}.bak" ]; then
  # Right after 5 MB rotation the fresh log may not contain a "done" line yet.
  LAST_DONE=$(grep 'Liked sync done' "${LOG_FILE}.bak" | tail -1 | sed -E 's/^\[([^]]+)\].*/\1/')
fi
DONE_EPOCH=$(date -d "$LAST_DONE" +%s 2>/dev/null || echo 0)
AGE_MINUTES=$(( ($(date +%s) - DONE_EPOCH) / 60 ))
if [ "$DONE_EPOCH" -eq 0 ] || [ "$AGE_MINUTES" -gt "$MAX_AGE_MINUTES" ]; then
  notify "⚠️ NAS Music: liked 同步已 ${AGE_MINUTES} 分钟没有完成记录（最后完成：${LAST_DONE:-无}），请检查 NAS cron 和 Docker"
  exit 1
fi

# --- Cookie health: require at least one successful fetch in the last 3 runs,
# so a single transient Spotify 503 does not page anyone.
FIRST_RECENT_START=$(grep -n 'Liked sync started' "$LOG_FILE" | tail -3 | head -1 | cut -d: -f1)
RECENT=$(tail -n +"${FIRST_RECENT_START:-1}" "$LOG_FILE")
if ! printf '%s' "$RECENT" | grep -q 'Spotify liked: '; then
  if printf '%s' "$RECENT" | grep -q 'LoginError'; then
    notify "⚠️ NAS Music: 最近 3 次 liked 同步都因 LoginError 失败，sp_dc cookie 很可能真的失效了——请从浏览器复制新的 sp_dc 并更新 ${MUSIC_DIR}/.spotify_sp_dc"
  else
    notify "⚠️ NAS Music: 最近 3 次 liked 同步都没有取到 Spotify 数据（无 LoginError），请查看 ${LOG_FILE}"
  fi
  exit 1
fi

exit 0
