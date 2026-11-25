#!/usr/bin/env bash
# Tests for francesca-pull.sh. A fake "Mac" is a local directory: stub ssh runs
# the remote command with HOME set to it, stub rsync maps host:path onto it.
# Run: bash backup/test-francesca-pull.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/francesca-pull.sh"
fails=0

setup() {
  T="$(mktemp -d)"
  MAC="$T/mac"; mkdir -p "$MAC/mempalace-gateway/backups" "$MAC/.mempalace" "$T/bin"
  cat > "$T/bin/ssh" <<EOF
#!/usr/bin/env bash
while [[ \$# -gt 0 && \$1 == -* ]]; do shift 2; done
shift   # host
HOME="$MAC" bash -c "\$*"
EOF
  cat > "$T/bin/rsync" <<EOF
#!/usr/bin/env bash
args=()
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    -e) shift 2; continue ;;
    *:*) p="\${1#*:}"; [[ \$p == /* ]] || p="$MAC/\$p"; args+=("\$p") ;;
    *) args+=("\$1") ;;
  esac
  shift
done
exec /usr/bin/rsync "\${args[@]}"
EOF
  chmod +x "$T/bin/ssh" "$T/bin/rsync"
  printf 'MAC_HOST=fake@mac\n' > "$T/env"
}

make_kg() { sqlite3 "$MAC/.mempalace/knowledge_graph.sqlite3" \
  "CREATE TABLE triples(s,p,o); INSERT INTO triples VALUES('a','b','c');"; }
make_snap() { # name age-in-days
  local f="$MAC/mempalace-gateway/backups/mempalace-backup-$1.tar.gz"
  echo x > "$f"; touch -d "$2 days ago" "$f"; }

run() {
  PATH="$T/bin:$PATH" ENV_FILE="$T/env" DEST="$T/dest" \
    TELEGRAM_CREDS="$T/none" bash "$SCRIPT" > "$T/out" 2>&1
  rc=$?
}

check() { # description condition...
  local d="$1"; shift
  if "$@"; then echo "ok   - $d"; else echo "FAIL - $d"; cat "$T/out"; fails=$((fails + 1)); fi
}

# 1) happy path
setup; make_kg; make_snap new 0; run
check "happy: exit 0" test "$rc" -eq 0
check "happy: snapshot pulled" test -f "$T/dest/snapshots/mempalace-backup-new.tar.gz"
check "happy: KG copy has data" test "$(sqlite3 "$T"/dest/kg/*.sqlite3 'select count(*) from triples')" = 1
check "happy: no temp file left on the Mac" test -z "$(ls -A "$MAC" | grep francesca-kg-pull)"
rm -rf "$T"

# 2) missing KG on the Mac: alert, no empty copy, no empty db created there
setup; make_snap new 0; run
check "no KG: exit 1" test "$rc" -ne 0
check "no KG: alert names it" grep -q "knowledge graph copy FAILED" "$T/out"
check "no KG: no copy kept" test -z "$(ls -A "$T/dest/kg")"
check "no KG: source not created" test ! -e "$MAC/.mempalace/knowledge_graph.sqlite3"
rm -rf "$T"

# 3) Mac backup stopped: alert, and old copies are NOT pruned
setup; make_kg; make_snap old 10; run
check "stale: exit 1" test "$rc" -ne 0
check "stale: alert" grep -q "no Mac snapshot newer" "$T/out"
check "stale: old snapshot kept" test -f "$T/dest/snapshots/mempalace-backup-old.tar.gz"
rm -rf "$T"

# 4) retention runs behind a fresh copy
setup; make_kg; make_snap old 10; make_snap new 0; run
check "retention: exit 0" test "$rc" -eq 0
check "retention: old snapshot pruned" test ! -e "$T/dest/snapshots/mempalace-backup-old.tar.gz"
check "retention: new snapshot kept" test -f "$T/dest/snapshots/mempalace-backup-new.tar.gz"
rm -rf "$T"

# 5) Mac unreachable (rsync fails): alert
setup; make_kg; make_snap new 0; printf '#!/bin/sh\nexit 255\n' > "$T/bin/rsync"; run
check "rsync down: exit 1" test "$rc" -ne 0
check "rsync down: alert" grep -q "rsync of Mac snapshots FAILED" "$T/out"
rm -rf "$T"

[[ $fails -eq 0 ]] && echo "all passed" || { echo "$fails failed"; exit 1; }
