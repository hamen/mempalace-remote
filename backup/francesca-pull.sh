#!/usr/bin/env bash
# Pull Francesca's MemPalace backups from the Mac mini to this machine.
# The Mac makes its own hourly snapshots (Qdrant dump + config) and mirrors them
# to Google Drive. This job keeps a second copy here, so one dead disk or one
# broken Drive login does not lose her palace:
#   - rsync the Mac's snapshot tarballs (no --delete: a wipe on the Mac must not
#     wipe this copy; we prune here by age instead)
#   - a consistent sqlite3 .backup of her knowledge graph, taken on the Mac
# Telegram alert only on failure or a stale Mac snapshot.
set -uo pipefail

ENV_FILE="$HOME/.config/mempalace-remote/francesca-pull.env"
# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
MAC_HOST="${MAC_HOST:?set MAC_HOST in $ENV_FILE}"   # e.g. user@<tailscale-ip>
MAC_BACKUPS="${MAC_BACKUPS:-mempalace-gateway/backups}"   # relative to the Mac home
MAC_KG="${MAC_KG:-.mempalace/knowledge_graph.sqlite3}"
DEST="${DEST:-$HOME/.mempalace-backups-francesca}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
STALE_MIN="${STALE_MIN:-180}"       # Mac snapshots are hourly
TELEGRAM_CREDS="$HOME/.config/app-tools/telegram.env"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20)

mkdir -p "$DEST/snapshots" "$DEST/kg"
exec 9>"$DEST/.pull.lock"
if ! flock -n 9; then
  echo "[skip] another pull is running"
  exit 0
fi

stamp="$(date +%Y-%m-%d_%H%M%S)"
alert=""
add_alert() { alert="${alert:+$alert. }$1"; }

notify() {
  [[ -f "$TELEGRAM_CREDS" ]] || return 0
  # shellcheck disable=SC1090
  set -a; source "$TELEGRAM_CREDS"; set +a
  curl -s --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null
}

# --- 1) snapshot tarballs from the Mac ---
if ! rsync -a --timeout=120 -e "${SSH[*]}" \
     --include='mempalace-backup-*.tar.gz' --exclude='*' \
     "$MAC_HOST:$MAC_BACKUPS/" "$DEST/snapshots/"; then
  add_alert "rsync of Mac snapshots FAILED"
fi

# --- 2) knowledge graph: consistent copy taken on the Mac, then pulled ---
remote_tmp="/tmp/francesca-kg-pull.sqlite3"
kg_out="$DEST/kg/knowledge_graph-$stamp.sqlite3"
if "${SSH[@]}" "$MAC_HOST" "rm -f $remote_tmp && sqlite3 \"\$HOME/$MAC_KG\" '.backup $remote_tmp'" \
   && rsync -a --timeout=120 -e "${SSH[*]}" "$MAC_HOST:$remote_tmp" "$kg_out" \
   && [[ -s "$kg_out" ]] \
   && [[ "$(sqlite3 "$kg_out" 'PRAGMA integrity_check;')" == "ok" ]]; then
  :
else
  rm -f "$kg_out"
  add_alert "knowledge graph copy FAILED"
fi

# --- 3) freshness: the newest Mac snapshot must be recent ---
newest="$(find "$DEST/snapshots" -name 'mempalace-backup-*.tar.gz' -mmin -"$STALE_MIN" -print -quit)"
if [[ -z "$newest" ]]; then
  add_alert "no Mac snapshot newer than ${STALE_MIN} min (Mac backup job stopped?)"
fi

# --- 4) retention ---
find "$DEST/snapshots" -name 'mempalace-backup-*.tar.gz' -mtime +"$RETAIN_DAYS" -delete 2>/dev/null || true
find "$DEST/kg" -name 'knowledge_graph-*.sqlite3' -mtime +"$RETAIN_DAYS" -delete 2>/dev/null || true

[[ -n "$alert" ]] && notify "🚨 MemPalace Francesca pull: $alert ($stamp)"
echo "[$stamp] pull done${alert:+ — ALERT: $alert}"
[[ -z "$alert" ]]
