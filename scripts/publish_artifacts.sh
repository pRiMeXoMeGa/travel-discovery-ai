#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Publish the pre-built data artifacts to a GitHub Release so anyone can restore
# the stack without the raw CSVs. Artifacts are too large for git (and are
# gitignored), so they live as Release assets.
#
#   bash scripts/publish_artifacts.sh [tag]
#       tag  release tag (default: deploy-data-v1)
#
# Requires the GitHub CLI authenticated: `gh auth login`.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

DUMPS_DIR="${DUMPS_DIR:-dumps}"
TAG="${1:-deploy-data-v1}"

command -v gh >/dev/null 2>&1 || { echo "!! gh CLI not found — install + run 'gh auth login'" >&2; exit 1; }

mapfile -t assets < <(ls "$DUMPS_DIR"/travel.dump "$DUMPS_DIR"/*.snapshot 2>/dev/null)
[ "${#assets[@]}" -gt 0 ] || { echo "!! no artifacts in $DUMPS_DIR — run scripts/export_data.sh first" >&2; exit 1; }

echo "==> Publishing to GitHub Release '$TAG':"
printf '    %s\n' "${assets[@]}"

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "    release exists — uploading (clobber)…"
  gh release upload "$TAG" "${assets[@]}" --clobber
else
  echo "    creating release…"
  gh release create "$TAG" "${assets[@]}" \
    --title "Pre-built data ($TAG)" \
    --notes "Postgres dump + Qdrant snapshots for fast restore (50K listings + 200K reviews). See README → Deployment."
fi

echo ""
echo "✓ Published. Download URLs:"
gh release view "$TAG" --json assets --jq '.assets[].url'
