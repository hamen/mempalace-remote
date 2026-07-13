#!/usr/bin/env bash
# Launch mempalace-remote with the SAME interpreter that has mempalace installed.
set -euo pipefail

# Interpreter from the uv tool venv that ships mempalace.
PY="${MEMPALACE_PYTHON:-/home/ivan/.local/share/uv/tools/claude-code-telegram/bin/python}"

# Secrets / overrides (passphrase, port, public URL). Not committed.
ENV_FILE="${MEMPALACE_REMOTE_ENV:-$HOME/.mempalace/remote/env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${MEMPALACE_REMOTE_PASSPHRASE:?set MEMPALACE_REMOTE_PASSPHRASE in $ENV_FILE}"

cd "$(dirname "$0")"
exec "$PY" server.py
