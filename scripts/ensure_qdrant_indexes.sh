#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Ensure the payload indexes the retrieval/itinerary agents need exist on the
# Qdrant `listings` collection. Qdrant Cloud runs in STRICT mode: filtering on
# an un-indexed payload field 400s ("Index required but not found for <field>").
# Local Qdrant allows un-indexed filtering (full scan), so this is mandatory in
# the cloud and harmless (idempotent) locally.
#
#   QDRANT_URL=http://localhost:6333 bash scripts/ensure_qdrant_indexes.sh
#   QDRANT_URL=https://xxx.cloud.qdrant.io:6333 QDRANT_API_KEY=… bash scripts/ensure_qdrant_indexes.sh
#
# Called automatically by restore_local.sh and restore_remote.sh.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_API_KEY="${QDRANT_API_KEY:-}"
COLLECTION="${QDRANT_COLLECTION_LISTINGS:-listings}"

auth=()
[ -n "$QDRANT_API_KEY" ] && auth=(-H "api-key: $QDRANT_API_KEY")

# field:schema pairs the agents filter on (see backend/app/agents/retrieval.py).
indexes="city:keyword type:keyword neighbourhood:keyword amenities:keyword base_price:float beds:integer"

echo "==> Ensuring payload indexes on '$COLLECTION' at $QDRANT_URL…"
for pair in $indexes; do
  field="${pair%%:*}"; schema="${pair##*:}"
  printf "    %-14s %-8s -> " "$field" "$schema"
  curl -fsS -X PUT "$QDRANT_URL/collections/$COLLECTION/index?wait=true" \
    "${auth[@]}" -H "Content-Type: application/json" \
    -d "{\"field_name\":\"$field\",\"field_schema\":\"$schema\"}" \
    | python -c "import sys,json;r=json.load(sys.stdin);print(r.get('result',{}).get('status','?'))" | tr -d '\r'
done
echo "✓ Payload indexes ensured."
