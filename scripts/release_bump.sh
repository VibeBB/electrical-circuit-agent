#!/usr/bin/env bash
# Release workflow's bump-version step: resolve the target version, write the
# version bump, and push it to main — routing through an auto-merged fallback
# PR when the ruleset rejects a direct push. Emits the version/tag/sha step
# outputs the verify, install-smoke, and release jobs consume.
#
# Inputs (env): BUMP, SET_VERSION, DRY_RUN, GH_TOKEN plus the GitHub-provided
# GITHUB_* variables. The RELEASE_BUMP_* knobs tune the polling/retry counts
# and intervals; the workflow defaults match the previous inline step and
# tests shrink them to zero. Covered by tests/test_release_bump.py.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=${RELEASE_BUMP_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}
cd "$REPO_ROOT"

RETRY_ATTEMPTS=${RELEASE_BUMP_RETRY_ATTEMPTS:-5}
RETRY_DELAY_SECONDS=${RELEASE_BUMP_RETRY_DELAY_SECONDS:-10}
GATED_POLLS=${RELEASE_BUMP_GATED_POLLS:-10}
GATED_EMPTY_SECONDS=${RELEASE_BUMP_GATED_EMPTY_SECONDS:-30}
GATED_BATCH_SECONDS=${RELEASE_BUMP_GATED_BATCH_SECONDS:-10}
RUN_WAIT_ATTEMPTS=${RELEASE_BUMP_RUN_WAIT_ATTEMPTS:-30}
RUN_WAIT_SECONDS=${RELEASE_BUMP_RUN_WAIT_SECONDS:-10}
CONCLUSION_ATTEMPTS=${RELEASE_BUMP_CONCLUSION_ATTEMPTS:-6}
CONCLUSION_SECONDS=${RELEASE_BUMP_CONCLUSION_SECONDS:-10}
WATCH_INTERVAL=${RELEASE_BUMP_WATCH_INTERVAL:-30}
MERGE_ATTEMPTS=${RELEASE_BUMP_MERGE_ATTEMPTS:-40}
MERGE_SECONDS=${RELEASE_BUMP_MERGE_SECONDS:-30}

retry() {
  # Transient GitHub API/server errors (HTTP 5xx, TLS resets, truncated
  # responses) should not hard-fail the release: retry gh/git calls with
  # backoff before giving up.
  local attempt
  for ((attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++)); do
    "$@" && return 0
    sleep $((attempt * RETRY_DELAY_SECONDS))
  done
  return 1
}

write_summary() {
  printf '%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
}

SET_VERSION="${SET_VERSION#v}"
CURRENT=$(python3 -c 'import tomllib,sys;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
if [ "$DRY_RUN" = "true" ]; then
  # Rehearsal: compute the would-be version and check the tag is free, but
  # write nothing — downstream jobs still exercise the
  # verify/install-smoke/build path against HEAD.
  if [ -n "$SET_VERSION" ]; then
    if [ "$SET_VERSION" = "$CURRENT" ]; then
      VERSION="$CURRENT"
    else
      VERSION=$(python3 "$SCRIPT_DIR/bump_version.py" --dry-run --set "$SET_VERSION" --root "$REPO_ROOT")
    fi
  else
    VERSION=$(python3 "$SCRIPT_DIR/bump_version.py" --dry-run --bump "$BUMP" --root "$REPO_ROOT")
  fi
  if git ls-remote --exit-code --tags origin "refs/tags/v${VERSION}"; then
    echo "::error::tag v${VERSION} already exists" >&2
    exit 1
  fi
  write_summary "dry-run: v${VERSION} would release at $(git rev-parse HEAD) (bump=${BUMP}, current=${CURRENT})"
  {
    echo "sha=$(git rev-parse HEAD)"
    echo "version=${VERSION}"
    echo "tag=v${VERSION}"
  } >> "$GITHUB_OUTPUT"
  exit 0
fi
SKIP_COMMIT=false
if [ -n "$SET_VERSION" ]; then
  if [ "$SET_VERSION" = "$CURRENT" ]; then
    VERSION="$CURRENT"
    python3 "$SCRIPT_DIR/bump_version.py" --dry-run --bump patch --root "$REPO_ROOT" >/dev/null
    SKIP_COMMIT=true
  else
    VERSION=$(python3 "$SCRIPT_DIR/bump_version.py" --set "$SET_VERSION" --root "$REPO_ROOT")
  fi
else
  VERSION=$(python3 "$SCRIPT_DIR/bump_version.py" --bump "$BUMP" --root "$REPO_ROOT")
fi
if git ls-remote --exit-code --tags origin "refs/tags/v${VERSION}"; then
  echo "::error::tag v${VERSION} already exists" >&2
  exit 1
fi
if [ "$SKIP_COMMIT" = true ]; then
  echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
else
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  # bump_version.py owns the versioned-file list; a new versioned file is
  # staged automatically instead of silently omitted.
  python3 "$SCRIPT_DIR/bump_version.py" --list-files | xargs git add
  git commit -m "Release v${VERSION}: update version files"
  if retry git push origin HEAD:main; then
    echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
  else
    # No bypass actor is configured, so the pull_request rule rejects a
    # direct push. Route the bump commit through a pull request instead —
    # same self-approve + dispatched checks + auto-merge flow as sibling
    # release workflows.
    echo "::warning::direct push to main rejected; routing the version bump through a pull request"
    branch="bot/release-bump-v${VERSION}-${GITHUB_RUN_ID}"
    retry git push origin "HEAD:refs/heads/${branch}"
    body=$(printf '%s\n' \
      "Automated version bump for the Release workflow." \
      "" \
      "version: v${VERSION}" \
      "workflow run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}")
    pr_url=$(retry gh pr create \
      --repo "$GITHUB_REPOSITORY" \
      --base main \
      --head "$branch" \
      --title "Release v${VERSION}: update version files" \
      --body "$body")
    write_summary "version-bump PR: $pr_url"
    # Pull_request runs on the bot branch queue as approval-gated
    # action_required runs; poll and approve. A rejected approval is
    # non-fatal: the dispatched run below still satisfies required checks.
    gated_seen=0
    empty_streak=0
    for _ in $(seq 1 "$GATED_POLLS"); do
      batch=$(retry gh run list --repo "$GITHUB_REPOSITORY" --branch "$branch" \
        --event pull_request --status action_required --json databaseId --jq '.[].databaseId' || true)
      if [ -z "$batch" ]; then
        empty_streak=$((empty_streak + 1))
        if { [ "$gated_seen" -eq 0 ] && [ "$empty_streak" -ge 3 ]; } ||
          { [ "$gated_seen" -eq 1 ] && [ "$empty_streak" -ge 2 ]; }; then
          break
        fi
        sleep "$GATED_EMPTY_SECONDS"
        continue
      fi
      empty_streak=0
      gated_seen=1
      for gated in $batch; do
        retry gh api -X POST "repos/$GITHUB_REPOSITORY/actions/runs/$gated/approve" || true
      done
      sleep "$GATED_BATCH_SECONDS"
    done
    started=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    retry gh workflow run ci.yml --repo "$GITHUB_REPOSITORY" --ref "$branch"
    retry gh workflow run workflow-lint.yml --repo "$GITHUB_REPOSITORY" --ref "$branch"
    failed=0
    for wf in ci.yml workflow-lint.yml; do
      run_id=""
      for _ in $(seq 1 "$RUN_WAIT_ATTEMPTS"); do
        sleep "$RUN_WAIT_SECONDS"
        run_id=$(retry gh run list --repo "$GITHUB_REPOSITORY" --workflow "$wf" --branch "$branch" \
          --event workflow_dispatch --created ">=$started" --json databaseId --jq '.[0].databaseId // empty' || true)
        [ -n "$run_id" ] && break
      done
      if [ -z "$run_id" ]; then
        write_summary "$wf workflow_dispatch for $branch was not observed; the version-bump PR was left open"
        exit 1
      fi
      echo "$wf run for $branch: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${run_id}"
      gh run watch --repo "$GITHUB_REPOSITORY" "$run_id" --interval "$WATCH_INTERVAL" || true
      # An empty conclusion is a transient API failure, not a run verdict:
      # re-query until the API reports the conclusion.
      conclusion=""
      for _ in $(seq 1 "$CONCLUSION_ATTEMPTS"); do
        conclusion=$(retry gh run view --repo "$GITHUB_REPOSITORY" "$run_id" \
          --json conclusion --jq '.conclusion // empty' || true)
        [ -n "$conclusion" ] && break
        sleep "$CONCLUSION_SECONDS"
      done
      conclusion="${conclusion:-unknown}"
      echo "$wf conclusion for $branch: $conclusion"
      if [ "$conclusion" != "success" ]; then
        failed=1
      fi
    done
    if [ "$failed" -ne 0 ]; then
      write_summary "version-bump PR checks failed; the PR was left open for manual review"
      exit 1
    fi
    # Arm auto-merge on the version-bump PR. Checks are already green here
    # so the merge lands immediately; arming failure leaves the PR open for
    # manual merge.
    retry gh pr merge --repo "$GITHUB_REPOSITORY" --auto --squash --delete-branch "$pr_url" || {
      write_summary "version-bump PR auto-merge could not be armed; the PR was left open for manual review"
      echo "::error::version-bump PR $pr_url was not auto-merged; merge it, then re-run Release with version=${VERSION}."
      exit 1
    }
    # Wait for the merge to land on main so the release SHA is the real main
    # tip, not the branch head.
    merged=false
    for _ in $(seq 1 "$MERGE_ATTEMPTS"); do
      merged_at=$(retry gh api "repos/$GITHUB_REPOSITORY/pulls/${pr_url##*/}" --jq '.merged_at // empty' || true)
      [ -n "$merged_at" ] && { merged=true; break; }
      sleep "$MERGE_SECONDS"
    done
    if [ "$merged" != true ]; then
      write_summary "version-bump PR did not merge within the wait; it was left open"
      echo "::error::merge $pr_url, then re-run Release with version=${VERSION}."
      exit 1
    fi
    git fetch -q origin main
    echo "sha=$(git rev-parse origin/main)" >> "$GITHUB_OUTPUT"
  fi
fi
{
  echo "version=${VERSION}"
  echo "tag=v${VERSION}"
} >> "$GITHUB_OUTPUT"
