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

echo ">>> Starting Gunicorn..."
exec /app/.local/bin/gunicorn \
  --bind "0.0.0.0:5004" \
  --workers "${GUNICORN_WORKERS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-1800}" \
  --max-requests "${GUNICORN_MAX_REQUESTS:-50}" \
  --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-10}" \
  --forwarded-allow-ips "${GUNICORN_FORWARDED_ALLOW_IPS:-*}" \
  --log-level "${GUNICORN_LOG_LEVEL:-info}" \
  "app:create_app()"
