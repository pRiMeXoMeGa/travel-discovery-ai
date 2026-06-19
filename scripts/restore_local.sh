#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Restore the pre-built artifacts in dumps/ into the LOCAL docker-compose stores.
# This is what makes `docker compose up` + this script a fast one-command run
# (no 60–120 min re-ingest).
#
#   bash scripts/restore_local.sh
#
# Expects (download from the GitHub Release first if missing — see README):
#   dumps/travel.dump
#   dumps/<collection>.snapshot
#
# Requires the postgres + qdrant containers to be up:
#   docker compose up -d postgres qdrant
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

DUMPS_DIR="${DUMPS_DIR:-dumps}"
QDRANT_URL_LOCAL="${QDRANT_URL_LOCAL:-http://localhost:6333}"
PG_USER="${PG_USER:-travel}"
PG_DB="${PG_DB:-travel}"

[ -f "$DUMPS_DIR/travel.dump" ] || { echo "!! $DUMPS_DIR/travel.dump not found" >&2; exit 1; }

echo "==> Restoring Postgres ($PG_DB) from $DUMPS_DIR/travel.dump…"
# --clean --if-exists makes the restore idempotent (drops objects first if re-run).
docker compose exec -T postgres pg_restore -U "$PG_USER" -d "$PG_DB" \
  --clean --if-exists --no-owner --no-acl < "$DUMPS_DIR/travel.dump"
echo "    Postgres restored."

shopt -s nullglob
for snap in "$DUMPS_DIR"/*.snapshot; do
  c=$(basename "$snap" .snapshot)
  echo "==> Recovering Qdrant collection '$c' from $snap…"
  curl -fsS -X POST "$QDRANT_URL_LOCAL/collections/$c/snapshots/upload?priority=snapshot" \
    -H "Content-Type:multipart/form-data" \
    -F "snapshot=@$snap" > /dev/null
  echo "    '$c' recovered."
done

echo ""
echo "✓ Local restore complete. Verify:"
echo "    curl -s $QDRANT_URL_LOCAL/collections | python -m json.tool"
echo "    docker compose exec -T postgres psql -U $PG_USER -d $PG_DB -c 'SELECT count(*) FROM listings;'"
