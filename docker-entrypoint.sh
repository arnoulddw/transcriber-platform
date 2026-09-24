#!/usr/bin/env bash
set -euo pipefail

export FLASK_APP="${FLASK_APP:-app}"
export PATH="/app/.local/bin:${PATH}"

if [[ "${SKIP_BOOTSTRAP:-0}" != "1" ]]; then
  echo ">>> Running application bootstrap..."
  flask bootstrap
else
  echo ">>> SKIP_BOOTSTRAP is set. Skipping bootstrap step."
fi

gunicorn_command=(/app/.local/bin/gunicorn \
  --bind "0.0.0.0:5004" \
  --workers "${GUNICORN_WORKERS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-1800}" \
  --max-requests "${GUNICORN_MAX_REQUESTS:-0}" \
  --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-0}" \
  --forwarded-allow-ips "${GUNICORN_FORWARDED_ALLOW_IPS:-*}" \
  --log-level "${GUNICORN_LOG_LEVEL:-info}" \
  "app:create_app()")

if [[ "${START_EMBEDDED_WORKER:-1}" != "1" ]]; then
  echo ">>> Starting Gunicorn..."
  exec "${gunicorn_command[@]}"
fi

echo ">>> Starting background worker..."
/app/.local/bin/flask worker &
worker_pid=$!
echo ">>> Starting Gunicorn..."
"${gunicorn_command[@]}" &
web_pid=$!

stop_children() {
  trap - TERM INT
  kill -TERM "$web_pid" "$worker_pid" 2>/dev/null || true
  wait "$web_pid" "$worker_pid" 2>/dev/null || true
}

trap 'stop_children; exit 143' TERM INT
status=0
wait -n "$web_pid" "$worker_pid" || status=$?
stop_children
exit "$status"
