#!/usr/bin/env bash
# Send a Telegram alert. Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from an env
# file (default ~/.config/mempalace-remote/telegram.env — same bot Ivan uses for
# his apps). Usage: notify.sh "message text"
set -uo pipefail
ENVF="${TELEGRAM_ENV:-$HOME/.config/mempalace-remote/telegram.env}"
if [[ -f "$ENVF" ]]; then set -a; # shellcheck disable=SC1090
  source "$ENVF"; set +a; fi
MSG="${1:-(mensaje vacío)}"
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "notify.sh: missing TELEGRAM_BOT_TOKEN/CHAT_ID in $ENVF" >&2; exit 1
fi
curl -s --max-time 15 \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  -d "disable_web_page_preview=true" >/dev/null
