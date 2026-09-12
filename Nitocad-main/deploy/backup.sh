#!/usr/bin/env bash
# Backs up the sqlite DB (job history + API keys) from the running `app`
# container. Uses Python's stdlib sqlite3 `.backup()` (via `docker compose
# exec`) rather than `cp`-ing the file directly - copying a live sqlite
# file while the app is writing to it can grab it mid-write; `.backup()`
# is safe to run against a live database. Runs inside the container so
# this doesn't need to guess the compose-generated volume name (which
# depends on the checkout directory's name) or have sqlite3 installed on
# the host.
#
# Usage (manual):
#   ./deploy/backup.sh
#
# Usage (cron, daily at 3am, keeping the last 14 backups):
#   0 3 * * * cd /home/nitocad/nitocad-pro && ./deploy/backup.sh >> /var/log/nitocad-backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-$HOME/nitocad-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
CONTAINER_BACKUP_PATH="/data/backup-${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

echo "==> Backing up nl_to_cad.db from the running app container"
docker compose exec -T app python -c "
import sqlite3
src = sqlite3.connect('/data/nl_to_cad.db')
dst = sqlite3.connect('${CONTAINER_BACKUP_PATH}')
with dst:
    src.backup(dst)
src.close()
dst.close()
"

docker compose cp "app:${CONTAINER_BACKUP_PATH}" "${BACKUP_DIR}/nl_to_cad-${TIMESTAMP}.db"
docker compose exec -T app rm -f "${CONTAINER_BACKUP_PATH}"

echo "==> Wrote ${BACKUP_DIR}/nl_to_cad-${TIMESTAMP}.db"

echo "==> Pruning backups older than ${RETENTION_DAYS} days"
find "${BACKUP_DIR}" -name "nl_to_cad-*.db" -mtime "+${RETENTION_DAYS}" -delete -print

echo "==> Done. Current backups:"
ls -lh "${BACKUP_DIR}"
