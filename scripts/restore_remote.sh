#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Restore the pre-built artifacts in dumps/ into the MANAGED cloud stores
# (Neon Postgres + Qdrant Cloud). Run this once after provisioning, instead of
# re-ingesting against a remote DB (which is far slower).
#
#   export DATABASE_URL='postgresql://USER:PASS@ep-xxx.neon.tech/neondb?sslmode=require'
#   export QDRANT_URL='https://xxx.cloud.qdrant.io:6333'
#   export QDRANT_API_KEY='your-qdrant-api-key'
#   bash scripts/restore_remote.sh
#
# Postgres is restored via the local postgres:16 container's pg_restore (so you
# don't need a host Postgres client and the server versions match). Qdrant
# snapshots are uploaded to the cloud cluster over HTTPS with the API key.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

DUMPS_DIR="${DUMPS_DIR:-dumps}"

: "${DATABASE_URL:?set DATABASE_URL to the Neon connection string}"
: "${QDRANT_URL:?set QDRANT_URL to the Qdrant Cloud cluster URL}"
: "${QDRANT_API_KEY:?set QDRANT_API_KEY to the Qdrant Cloud API key}"

[ -f "$DUMPS_DIR/travel.dump" ] || { echo "!! $DUMPS_DIR/travel.dump not found (download from the Release first)" >&2; exit 1; }

echo "==> Restoring Postgres into Neon…"
# --clean --if-exists keeps it re-runnable; harmless 'does not exist' notices on a fresh DB.
docker compose exec -T postgres pg_restore -d "$DATABASE_URL" \
  --clean --if-exists --no-owner --no-acl < "$DUMPS_DIR/travel.dump"
echo "    Neon Postgres restored."

shopt -s nullglob
for snap in "$DUMPS_DIR"/*.snapshot; do
  c=$(basename "$snap" .snapshot)
  echo "==> Uploading Qdrant snapshot for '$c' to $QDRANT_URL…"
  curl -fsS -X POST "$QDRANT_URL/collections/$c/snapshots/upload?priority=snapshot" \
    -H "api-key: $QDRANT_API_KEY" \
    -H "Content-Type:multipart/form-data" \
    -F "snapshot=@$snap" > /dev/null
  echo "    '$c' uploaded."
done

echo ""
echo "==> Verifying counts…"
echo -n "    Postgres listings: "
docker compose exec -T postgres psql -d "$DATABASE_URL" -tAc "SELECT count(*) FROM listings;" || true
echo "    Qdrant collections:"
curl -fsS "$QDRANT_URL/collections" -H "api-key: $QDRANT_API_KEY" \
  | python -c "import sys,json;[print('     ',c['name']) for c in json.load(sys.stdin)['result']['collections']]" || true

echo ""
echo "✓ Remote restore complete."
