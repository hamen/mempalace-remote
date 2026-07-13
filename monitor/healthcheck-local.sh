#!/usr/bin/env bash
# Internal watchdog (runs ON money-maker, every minute via systemd timer).
# Checks the wrapper is actually serving on localhost. If not, restarts the
# service (catches the "process alive but hung" case that Restart=always
# misses) and alerts on state transitions only — not every tick.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:8789/healthz"
STATE="$HOME/.config/mempalace-remote/local_state"

ok() { curl -fs --max-time 8 "$URL" >/dev/null 2>&1; }
prev="$(cat "$STATE" 2>/dev/null || echo up)"
ts="$(date '+%F %T')"

if ok; then
  [[ "$prev" == "down" ]] && "$DIR/notify.sh" "✅ MemPalace: wrapper di nuovo UP su money-maker ($ts)"
  echo up > "$STATE"
  exit 0
fi

# Not serving → try to self-heal.
systemctl --user restart mempalace-remote 2>/dev/null
sleep 6
if ok; then
  "$DIR/notify.sh" "♻️ MemPalace: wrapper era GIÙ su money-maker → riavviato, ora UP ($ts)"
  echo up > "$STATE"
else
  [[ "$prev" != "down" ]] && "$DIR/notify.sh" "🚨 MemPalace: wrapper GIÙ su money-maker e il riavvio NON ha aiutato — serve un'occhiata ($ts)"
  echo down > "$STATE"
fi
