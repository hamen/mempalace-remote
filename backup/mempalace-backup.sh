#!/usr/bin/env bash
# Hourly MemPalace backup, 7-day rolling window, offsite to Google Drive.
# Consistent even with live writers: sqlite3 .backup for both SQLite DBs
# (chroma.sqlite3 + knowledge_graph.sqlite3), tar for the rest of the palace.
# Telegram alert only on failure or anomalous growth.
set -uo pipefail

MEMPALACE_DIR="$HOME/.mempalace"
BACKUP_ROOT="$HOME/.mempalace-backups"
RETAIN_DAYS=7                       # hourly × 7d ≈ 168 snapshots
TELEGRAM_CREDS="$HOME/.config/app-tools/telegram.env"
RCLONE="$HOME/.local/bin/rclone"
RCLONE_REMOTE="gdrive:MemPalace-Backups"

mkdir -p "$BACKUP_ROOT"
# Serialize: never let two backups run at once (they would collide on the same
# minute-stamped dir and fail each other's sqlite .backup). Skip if one is live.
exec 9>"$BACKUP_ROOT/.backup.lock"
if ! flock -n 9; then
  echo "[skip] un altro backup è già in corso"
  exit 0
fi

stamp="$(date +%Y-%m-%d_%H%M%S)"
dest="$BACKUP_ROOT/$stamp"
mkdir -p "$dest"
alert=""

notify() {
  [[ -f "$TELEGRAM_CREDS" ]] || return 0
  # shellcheck disable=SC1090
  set -a; source "$TELEGRAM_CREDS"; set +a
  curl -s --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null
}

# --- consistent snapshots ---
if ! sqlite3 "$MEMPALACE_DIR/knowledge_graph.sqlite3" ".backup '$dest/knowledge_graph.sqlite3'"; then
  alert="KG .backup FALLITO"
fi
if [[ -f "$MEMPALACE_DIR/palace/chroma.sqlite3" ]]; then
  sqlite3 "$MEMPALACE_DIR/palace/chroma.sqlite3" ".backup '$dest/chroma.sqlite3'" \
    || alert="${alert:+$alert. }chroma .backup FALLITO"
fi
# Qdrant era (post-cutover): snapshot the qdrant storage dir if present.
# Qdrant writes live (segment rotation, WAL, .deleted), so tar can hit a file
# that changes/disappears mid-read → GNU tar exits 1 ("file changed as we read
# it"). That's benign (qdrant rebuilds from WAL on load, next snapshot is clean);
# only exit >=2 is a real failure. Suppress the noise, keep the fatal check.
if [[ -d "$MEMPALACE_DIR/qdrant" ]]; then
  tar -C "$MEMPALACE_DIR" --warning=no-file-changed --warning=no-file-removed \
      -czf "$dest/qdrant-storage.tar.gz" qdrant
  rc=$?
  [[ $rc -ge 2 ]] && alert="${alert:+$alert. }qdrant tar FALLITO (rc=$rc)"
fi
# Segment files + small configs (chroma.sqlite3 already captured above).
tar -C "$MEMPALACE_DIR" -czf "$dest/palace-rest.tar.gz" \
  --exclude='palace/chroma.sqlite3' palace config.json tunnels.json 2>/dev/null || true

# Empty/failed snapshot guard.
if [[ ! -s "$dest/knowledge_graph.sqlite3" && ! -s "$dest/chroma.sqlite3" ]]; then
  alert="${alert:+$alert. }snapshot VUOTO ($stamp)"
fi

# --- retention (7 days rolling) ---
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +$RETAIN_DAYS -exec rm -rf {} + 2>/dev/null || true

# --- offsite mirror to Google Drive (mirrors post-retention window) ---
if [[ -x "$RCLONE" ]]; then
  "$RCLONE" sync "$BACKUP_ROOT" "$RCLONE_REMOTE" \
    --exclude ".*" --transfers 4 --checkers 8 >/dev/null 2>&1 \
    || alert="${alert:+$alert. }offsite gdrive sync FALLITO"
fi

[[ -n "$alert" ]] && notify "🚨 MemPalace backup: $alert ($stamp)"
echo "[$stamp] backup done${alert:+ — ALERT: $alert}"
