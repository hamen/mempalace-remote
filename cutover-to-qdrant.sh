#!/usr/bin/env bash
# One-shot cutover: switch ALL MemPalace clients from embedded Chroma to the
# shared Qdrant server. Idempotent-ish, with automatic rollback if the migrated
# data doesn't match. The original Chroma store is moved aside (never deleted).
set -uo pipefail

PY="${MEMPALACE_PYTHON:-/home/ivan/.local/share/uv/tools/mempalace/bin/python}"
QURL=http://127.0.0.1:6333
MP="$HOME/.mempalace"
PALACE="$MP/palace"
TS=$(date +%Y%m%d-%H%M%S)
ARCHIVE="$MP/palace.chroma-archive-$TS"
MIGRATE="$HOME/code/mempalace-remote/migrate_chroma_to_qdrant.py"
CFG="$MP/config.json"
BOTS="claude-telegram-bot.service claude-telegram-bot-main.service claude-telegram-bot-ops.service claude-telegram-bot-social.service"

say() { echo -e "\n=== $* ==="; }

say "0. preflight"
curl -fs --max-time 5 "$QURL/healthz" >/dev/null || { echo "Qdrant giù → ABORT"; exit 1; }
if [[ ! -f "$PALACE/chroma.sqlite3" ]]; then echo "chroma.sqlite3 assente (già migrato?) → ABORT"; exit 1; fi
cp "$CFG" "$CFG.pre-qdrant.$TS"   # config backup for rollback

say "1. backup fresco"
"$HOME/code/mempalace-remote/backup/mempalace-backup.sh" || echo "warn: backup non pulito (continuo, ho già il pre-qdrant)"

say "2. quiesce: stop bot + wrapper + monitor, kill mcp_server"
systemctl --user stop $BOTS mempalace-remote.service mempalace-monitor.timer 2>/dev/null
pkill -f "mempalace.mcp_server" 2>/dev/null; sleep 2

say "3. switch globale config.json -> qdrant (prima della move, così ogni respawn usa qdrant)"
"$PY" - "$CFG" "$QURL" <<'EOF'
import json,sys
cfg=json.load(open(sys.argv[1]))
cfg["backend"]="qdrant"; cfg["qdrant_url"]=sys.argv[2]
json.dump(cfg,open(sys.argv[1],"w"),indent=2)
print("config.json: backend=qdrant")
EOF

say "4. reset Qdrant (slate pulita)"
for c in $(curl -s "$QURL/collections" | "$PY" -c "import sys,json;print(' '.join(x['name'] for x in json.load(sys.stdin)['result']['collections']))" 2>/dev/null); do
  curl -s -X DELETE "$QURL/collections/$c" >/dev/null; echo "  drop $c"
done

say "5. sposta Chroma in archivio, crea palace vuota per il marker qdrant"
mv "$PALACE" "$ARCHIVE"; mkdir -p "$PALACE"

say "6. migrazione archive(chroma) -> palace(qdrant), riusando i vettori"
MEMPALACE_QDRANT_URL="$QURL" OMP_NUM_THREADS=2 "$PY" "$MIGRATE" "$ARCHIVE" "$PALACE"; MIG=$?

say "7. verifica parità"
SRC=$(MEMPALACE_QDRANT_URL="$QURL" "$PY" -c "from mempalace.palace import get_collection;print(get_collection('$ARCHIVE',create=False,backend='chroma').count())" 2>/dev/null)
DST=$(MEMPALACE_QDRANT_URL="$QURL" "$PY" -c "from mempalace.palace import get_collection;print(get_collection('$PALACE',create=True,backend='qdrant').count())" 2>/dev/null)
echo "  chroma=$SRC  qdrant=$DST  (migrator exit=$MIG)"
# Gate sulla VERITÀ dei dati = parità dei conteggi drawer. L'exit del migratore
# è informativo (la collezione opzionale closets non deve far fallire il cutover).
if [[ -z "$DST" || "$DST" == "0" || "$SRC" != "$DST" ]]; then
  echo "!! PARITÀ FALLITA → ROLLBACK a Chroma"
  rm -rf "$PALACE"; mv "$ARCHIVE" "$PALACE"
  cp "$CFG.pre-qdrant.$TS" "$CFG"
  systemctl --user start $BOTS mempalace-remote.service mempalace-monitor.timer 2>/dev/null
  echo "rollback completato. ABORT"; exit 1
fi

say "8. env wrapper per healthz profondo"
if ! grep -q MEMPALACE_QDRANT_URL "$MP/remote/env"; then
  printf 'MEMPALACE_BACKEND="qdrant"\nMEMPALACE_QDRANT_URL="%s"\n' "$QURL" >> "$MP/remote/env"
fi

say "9. restart client su qdrant"
systemctl --user start mempalace-remote.service $BOTS mempalace-monitor.timer
sleep 6

say "10. verifica end-to-end"
echo -n "wrapper /healthz (deep): "; curl -s --max-time 8 http://127.0.0.1:8789/healthz
echo "drawer count via qdrant: $DST"
echo "FATTO. Archivio Chroma: $ARCHIVE"
echo "Rollback manuale: stop client; rm -rf $PALACE; mv $ARCHIVE $PALACE; cp $CFG.pre-qdrant.$TS $CFG; restart client"
