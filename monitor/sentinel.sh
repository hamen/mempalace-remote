#!/usr/bin/env bash
# EXTERNAL sentinel — runs on the NUC ("Nuke"), every minute. Hits the PUBLIC
# Funnel URL of money-maker. This is the only layer that can notice money-maker
# being fully down (power/kernel/network) or Funnel being broken — the internal
# watchdog can't report what it can't run on. Alerts on transitions, with a
# few-minutes debounce to avoid blips.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
# The public URL is NUC-local config (kept out of git); install-on-nuc.sh writes
# it from money-maker's env. Fallback is a placeholder — set MP_PUBLIC_URL.
[[ -f "$HOME/.config/mempalace-remote/env" ]] && source "$HOME/.config/mempalace-remote/env"
URL="${MP_PUBLIC_URL:-https://your-machine.your-tailnet.ts.net/healthz}"
STATE="$HOME/.config/mempalace-sentinel/state"
THRESHOLD="${MP_FAIL_THRESHOLD:-3}"   # consecutive minutes down before alarming
mkdir -p "$(dirname "$STATE")"

prev="$(cat "$STATE" 2>/dev/null || echo up)"
fails="$(cat "${STATE}.fails" 2>/dev/null || echo 0)"
ts="$(date '+%F %T')"

if curl -fs --max-time 10 "$URL" 2>/dev/null | grep -q ok; then
  [[ "$prev" == "down" ]] && "$DIR/notify.sh" "✅ MemPalace di nuovo RAGGIUNGIBILE (money-maker) — $ts"
  echo up > "$STATE"; echo 0 > "${STATE}.fails"
else
  fails=$((fails + 1)); echo "$fails" > "${STATE}.fails"
  if [[ "$fails" -ge "$THRESHOLD" && "$prev" != "down" ]]; then
    "$DIR/notify.sh" "🚨 MemPalace IRRAGGIUNGIBILE da ${fails} min — money-maker giù o Funnel rotto. Controlla il desktop. ($ts)"
    echo down > "$STATE"
  fi
fi
