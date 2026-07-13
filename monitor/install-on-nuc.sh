#!/usr/bin/env bash
# Deploy the external sentinel onto the NUC, NATIVELY (no Docker): copy 2 scripts
# + the Telegram config, then install a 1-minute cron entry. Run FROM money-maker.
#   NUC_HOST=nuc ./install-on-nuc.sh
set -euo pipefail
NUC="${NUC_HOST:-nuc}"
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST=".local/share/mempalace-sentinel"

echo "== prepare dirs on $NUC =="
ssh "$NUC" "mkdir -p ~/$DEST ~/.config/mempalace-remote"

echo "== copy scripts + telegram config =="
scp "$DIR/sentinel.sh" "$DIR/notify.sh" "$NUC:~/$DEST/"
scp "$HOME/.config/mempalace-remote/telegram.env" "$NUC:~/.config/mempalace-remote/telegram.env"
ssh "$NUC" "chmod +x ~/$DEST/*.sh && chmod 600 ~/.config/mempalace-remote/telegram.env"

echo "== push public URL as NUC-local config (keeps the hostname out of git) =="
# Single source of truth: PUBLIC_BASE_URL in money-maker's gitignored env.
BASE="$(grep -E '^PUBLIC_BASE_URL=' "$HOME/.mempalace/remote/env" | cut -d= -f2- | tr -d '"' | sed 's#:8443##; s#/*$##')"
[[ -n "$BASE" ]] || { echo "!! PUBLIC_BASE_URL non trovato in ~/.mempalace/remote/env"; exit 1; }
ssh "$NUC" "printf 'MP_PUBLIC_URL=%s/healthz\n' '$BASE' > ~/.config/mempalace-remote/env && chmod 600 ~/.config/mempalace-remote/env"

echo "== install 1-minute cron (idempotent) =="
ssh "$NUC" '( crontab -l 2>/dev/null | grep -v mempalace-sentinel ; echo "* * * * * \$HOME/.local/share/mempalace-sentinel/sentinel.sh >/dev/null 2>&1 # mempalace-sentinel" ) | crontab -'

echo "== test alert from the NUC =="
ssh "$NUC" "~/$DEST/notify.sh '👁️ Sentinella MemPalace installata sul NUC — controllo money-maker via Funnel ogni 60s.'"
echo "DONE."
