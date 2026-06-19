#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Export the local data stores to pre-built artifacts in dumps/ so a fresh
# environment (local OR Neon/Qdrant Cloud) can be restored fast — no re-ingest.
#
#   bash scripts/export_data.sh
#
# Produces:
#   dumps/travel.dump        Postgres custom-format dump (pg_dump -Fc)
#   dumps/<collection>.snapshot   one Qdrant snapshot per live collection
#
# Reads from the running docker-compose stores (postgres container + Qdrant on
# localhost:6333). Run `docker compose up -d postgres qdrant` first if they're down.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

DUMPS_DIR="${DUMPS_DIR:-dumps}"
QDRANT_URL_LOCAL="${QDRANT_URL_LOCAL:-http://localhost:6333}"
PG_USER="${PG_USER:-travel}"
PG_DB="${PG_DB:-travel}"

mkdir -p "$DUMPS_DIR"

echo "==> Exporting Postgres ($PG_DB) via pg_dump (custom format)…"
docker compose exec -T postgres pg_dump -U "$PG_USER" -Fc "$PG_DB" > "$DUMPS_DIR/travel.dump"
echo "    wrote $DUMPS_DIR/travel.dump ($(du -h "$DUMPS_DIR/travel.dump" | cut -f1))"

echo "==> Discovering Qdrant collections at $QDRANT_URL_LOCAL…"
# tr -d '\r': Python on Windows writes CRLF to stdout, which would corrupt the
# collection name (and the URL it's spliced into) under Git Bash.
collections=$(curl -fsS "$QDRANT_URL_LOCAL/collections" \
  | python -c "import sys,json;print('\n'.join(c['name'] for c in json.load(sys.stdin)['result']['collections']))" \
  | tr -d '\r')

if [ -z "$collections" ]; then
  echo "    !! no Qdrant collections found — is the data loaded?" >&2
  exit 1
fi

while IFS= read -r c; do
  [ -z "$c" ] && continue
  echo "==> Snapshotting Qdrant collection '$c'…"
  snap_name=$(curl -fsS -X POST "$QDRANT_URL_LOCAL/collections/$c/snapshots" \
    | python -c "import sys,json;print(json.load(sys.stdin)['result']['name'])" \
    | tr -d '\r')
  echo "    created $snap_name — downloading…"
  curl -fsS "$QDRANT_URL_LOCAL/collections/$c/snapshots/$snap_name" -o "$DUMPS_DIR/$c.snapshot"
  echo "    wrote $DUMPS_DIR/$c.snapshot ($(du -h "$DUMPS_DIR/$c.snapshot" | cut -f1))"
done <<< "$collections"

echo ""
echo "✓ Export complete. Artifacts in $DUMPS_DIR/:"
ls -lh "$DUMPS_DIR"
echo ""
echo "Next: publish with scripts/publish_artifacts.sh, or restore with"
echo "scripts/restore_local.sh / scripts/restore_remote.sh."
