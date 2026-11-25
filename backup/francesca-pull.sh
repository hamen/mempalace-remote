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

ENV_FILE="${ENV_FILE:-$HOME/.config/mempalace-remote/francesca-pull.env}"
# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
MAC_HOST="${MAC_HOST:?set MAC_HOST in $ENV_FILE}"   # e.g. user@<tailscale-ip>
MAC_BACKUPS="${MAC_BACKUPS:-mempalace-gateway/backups}"   # relative to the Mac home
MAC_KG="${MAC_KG:-.mempalace/knowledge_graph.sqlite3}"    # relative to the Mac home
DEST="${DEST:-$HOME/.mempalace-backups-francesca}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
STALE_MIN="${STALE_MIN:-180}"       # Mac snapshots are hourly
TELEGRAM_CREDS="${TELEGRAM_CREDS:-$HOME/.config/app-tools/telegram.env}"
# ServerAlive bounds a dead link; `timeout` below bounds a hung remote command.
SSH_CMD="ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"

notify() (
  [[ -f "$TELEGRAM_CREDS" ]] || exit 0
  # shellcheck disable=SC1090
  set -a; source "$TELEGRAM_CREDS"; set +a
  curl -s --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null
)

stamp="$(date +%Y-%m-%d_%H%M%S)"
if ! mkdir -p "$DEST/snapshots" "$DEST/kg" || ! exec 9>"$DEST/.pull.lock"; then
  notify "🚨 MemPalace Francesca pull: cannot create $DEST ($stamp)"
  exit 1
fi
if ! flock -n 9; then
  echo "[skip] another pull is running"
  exit 0
fi

alert=""
add_alert() { alert="${alert:+$alert. }$1"; }

# --- 1) snapshot tarballs from the Mac ---
if ! timeout 900 rsync -a --timeout=120 -e "$SSH_CMD" \
     --include='mempalace-backup-*.tar.gz' --exclude='*' \
     "$MAC_HOST:$MAC_BACKUPS/" "$DEST/snapshots/"; then
  add_alert "rsync of Mac snapshots FAILED"
fi

# --- 2) knowledge graph: consistent copy taken on the Mac, then pulled ---
# -readonly: a missing source must fail, not create an empty database.
# The temp file lives in the Mac home (private), never in /tmp, and is removed
# whatever happens.
kg_out="$DEST/kg/knowledge_graph-$stamp.sqlite3"
kg_ok=0
remote_tmp="$(timeout 60 $SSH_CMD "$MAC_HOST" 'umask 077; mktemp "$HOME/.francesca-kg-pull.XXXXXX"')"
if [[ -n "$remote_tmp" ]] \
   && timeout 300 $SSH_CMD "$MAC_HOST" "sqlite3 -readonly \"\$HOME/$MAC_KG\" \".backup '$remote_tmp'\"" \
   && timeout 300 rsync -a --timeout=120 -e "$SSH_CMD" "$MAC_HOST:$remote_tmp" "$kg_out" \
   && [[ "$(sqlite3 "$kg_out" 'PRAGMA integrity_check;' 2>/dev/null)" == "ok" ]] \
   && sqlite3 "$kg_out" 'SELECT count(*) FROM triples;' >/dev/null 2>&1; then
  kg_ok=1
else
  rm -f "$kg_out"
  add_alert "knowledge graph copy FAILED"
fi
[[ -n "$remote_tmp" ]] && timeout 60 $SSH_CMD "$MAC_HOST" "rm -f '$remote_tmp'"

# --- 3) freshness: there must be a recent Mac snapshot ---
# Relies on rsync -a keeping the Mac's mtime.
fresh="$(find "$DEST/snapshots" -name 'mempalace-backup-*.tar.gz' -mmin -"$STALE_MIN" -print -quit)"
if [[ -z "$fresh" ]]; then
  add_alert "no Mac snapshot newer than ${STALE_MIN} min (Mac backup job stopped?)"
fi

# --- 4) retention: prune only behind a fresh, verified copy, so a long outage
# never ages out the last good backups ---
if [[ -n "$fresh" ]]; then
  find "$DEST/snapshots" -name 'mempalace-backup-*.tar.gz' -mtime +"$RETAIN_DAYS" -delete
fi
if [[ $kg_ok -eq 1 ]]; then
  find "$DEST/kg" -name 'knowledge_graph-*.sqlite3' -mtime +"$RETAIN_DAYS" -delete
fi

[[ -n "$alert" ]] && notify "🚨 MemPalace Francesca pull: $alert ($stamp)"
echo "[$stamp] pull done${alert:+ — ALERT: $alert}"
[[ -z "$alert" ]]
