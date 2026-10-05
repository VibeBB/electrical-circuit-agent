#!/usr/bin/env bash
# Prune orphaned <sha>-tools tags from a ghcr.io package.
#
# A package version is an orphan when ALL of these hold:
#   - it carries a tag matching ^[0-9a-f]{40}-tools$ (a publish-time sha tag)
#   - it is older than GHCR_PRUNE_MIN_AGE_DAYS (default: 14 days)
#   - its digest is not the digest pinned in the image lock file
#   - it has no stored build-provenance attestation (a gated, attested
#     image is kept even while unpinned)
#
# Failed publishes leave exactly this shape: pushed-but-unvetted image
# tags that nothing references. Deletion is irreversible, so the script
# also runs with GHCR_PRUNE_DRY_RUN=1 to report candidates only.
#
# Environment: GITHUB_REPOSITORY_OWNER, GITHUB_REPOSITORY, GH_TOKEN;
#   GHCR_PACKAGE (required in workflows; default: fpga-tools),
#   GHCR_PRUNE_MIN_AGE_DAYS, GHCR_PRUNE_DRY_RUN,
#   LOCK_FILE (default: docker/image-digests.json),
#   LOCK_ENTRY (default: GHCR_PACKAGE with '-' -> '_').
set -euo pipefail

PACKAGE=${GHCR_PACKAGE:-fpga-tools}
ORG=${GITHUB_REPOSITORY_OWNER:?GITHUB_REPOSITORY_OWNER is required}
REPO=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}
LOCK_FILE=${LOCK_FILE:-docker/image-digests.json}
LOCK_ENTRY=${LOCK_ENTRY:-${PACKAGE//-/_}}
MIN_AGE=${GHCR_PRUNE_MIN_AGE_DAYS:-14}
DRY_RUN=${GHCR_PRUNE_DRY_RUN:-0}
export LOCK_ENTRY

summary() { echo "$1" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"; }

lock_digest=$(python3 - "$LOCK_FILE" <<'PY'
import json, os, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    data = {}
# Two lock schemas exist in the family: the standard docker/image-digests.json
# (entry name -> record) and bard's flat plugins/bard/skills/bard-render/
# tools-image.json (the record itself is the document root).
key = os.environ.get("LOCK_ENTRY", "")
if key and isinstance(data.get(key), dict):
    entry = data[key]
elif isinstance(data.get("digest"), str):
    entry = data
else:
    entry = {}
print(entry.get("digest") or "")
PY
)
cutoff=$(date -u -d "$MIN_AGE days ago" +%Y-%m-%dT%H:%M:%SZ)
summary "GHCR orphan-tag prune: package=$PACKAGE min_age=${MIN_AGE}d cutoff=$cutoff lock=${lock_digest:-none} dry_run=$DRY_RUN"

pruned=0
kept=0
while IFS=$'\t' read -r id digest created_at tags_csv; do
  [ -n "$id" ] || continue
  sha_tag=""
  IFS=',' read -ra tags <<< "$tags_csv"
  for tag in "${tags[@]}"; do
    if [[ "$tag" =~ ^[0-9a-f]{40}-tools$ ]]; then
      sha_tag=$tag
      break
    fi
  done
  # Only publish-time <sha>-tools tags are candidates; latest and other
  # curated tags are never touched.
  [ -n "$sha_tag" ] || continue
  if [ "$digest" = "$lock_digest" ]; then
    summary "keep $sha_tag (pinned by $LOCK_FILE)"
    kept=$((kept + 1))
    continue
  fi
  if [[ ! "$created_at" < "$cutoff" ]]; then
    summary "keep $sha_tag (younger than ${MIN_AGE}d)"
    kept=$((kept + 1))
    continue
  fi
  attestations=$(gh api "repos/$REPO/attestations/$digest" \
    --jq '.attestations | length' 2>/dev/null || echo -1)
  if [ "$attestations" != "0" ]; then
    summary "keep $sha_tag (attestation state: ${attestations})"
    kept=$((kept + 1))
    continue
  fi
  if [ "$DRY_RUN" = "1" ]; then
    summary "would delete $sha_tag ($digest, created $created_at, no attestation, not locked)"
  else
    gh api -X DELETE "orgs/$ORG/packages/container/$PACKAGE/versions/$id"
    summary "deleted $sha_tag ($digest, created $created_at)"
  fi
  pruned=$((pruned + 1))
done < <(gh api --paginate "orgs/$ORG/packages/container/$PACKAGE/versions" \
  --jq '.[] | [.id, .name, .created_at, ((.metadata.container.tags // []) | join(","))] | @tsv')

summary "prune complete: ${pruned} removed, ${kept} kept"
