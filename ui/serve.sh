#!/usr/bin/env bash
# Build the dashboard from the repo records and serve it on localhost.
# Rebuilds every 60 s; with PULL=1 it also pulls the records the cron pushes.
#   ui/serve.sh            -> http://localhost:8765
#   PORT=9000 PULL=1 ui/serve.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"
PY="${PYTHON:-python3}"

rebuild() {
  if [ "${PULL:-0}" = "1" ]; then
    git pull --ff-only -q || echo "git pull non riuscito: uso i record locali" >&2
  fi
  "$PY" -m ui.build >/dev/null || echo "costruzione della dashboard fallita" >&2
}

"$PY" -m ui.build
( while sleep 60; do rebuild; done ) &
LOOP=$!
trap 'kill "$LOOP" 2>/dev/null' EXIT INT TERM
echo "Dashboard su http://localhost:$PORT  (Ctrl+C per fermare)"
"$PY" -m http.server "$PORT" --bind 127.0.0.1 --directory ui/site
