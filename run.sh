#!/usr/bin/env bash
# Launch mempalace-remote with the SAME interpreter that has mempalace installed.
set -euo pipefail

# Secrets / overrides (passphrase, port, public URL, MEMPALACE_PYTHON). Not committed.
# Sourced BEFORE resolving PY: setting MEMPALACE_PYTHON here is documented as an
# override, so it has to land in the environment before the default below is taken.
ENV_FILE="${MEMPALACE_REMOTE_ENV:-$HOME/.mempalace/remote/env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Interpreter from the uv tool venv that ships mempalace.
PY="${MEMPALACE_PYTHON:-$HOME/.local/share/uv/tools/mempalace/bin/python}"

: "${MEMPALACE_REMOTE_PASSPHRASE:?set MEMPALACE_REMOTE_PASSPHRASE in $ENV_FILE}"

cd "$(dirname "$0")"
exec "$PY" server.py
