#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: publish_image_pin_pr.sh PR_URL BRANCH BASE_SHA POST_MERGE_WORKFLOWS" >&2
  exit 2
fi

PR_URL=$1
BRANCH=$2
BASE_SHA=$3
POST_MERGE_WORKFLOWS=$4
REQUIRED_WAIT_ATTEMPTS=${PUBLISH_PIN_PR_REQUIRED_WAIT_ATTEMPTS:-40}
REQUIRED_WAIT_SECONDS=${PUBLISH_PIN_PR_REQUIRED_WAIT_SECONDS:-15}
MERGE_WAIT_ATTEMPTS=${PUBLISH_PIN_PR_MERGE_WAIT_ATTEMPTS:-36}
MERGE_WAIT_SECONDS=${PUBLISH_PIN_PR_MERGE_WAIT_SECONDS:-10}
RETRY_ATTEMPTS=${PUBLISH_PIN_PR_RETRY_ATTEMPTS:-3}
RETRY_DELAY_SECONDS=${PUBLISH_PIN_PR_RETRY_DELAY_SECONDS:-10}
REQUIRED_CHECKS_STDERR_FILE=$(mktemp)
trap 'rm -f "$REQUIRED_CHECKS_STDERR_FILE"' EXIT

write_summary() {
  printf '%s\n' "$1"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
}

read_pin_pr_state() {
  retry gh pr view "$PR_URL" --repo "$GITHUB_REPOSITORY" \
    --json state,mergedAt --jq '.state'
}

retry() {
  local attempt
  for ((attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++)); do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -lt "$RETRY_ATTEMPTS" ]; then
      sleep "$RETRY_DELAY_SECONDS"
    fi
  done
  return 1
}

assert_pin_pr_merged() {
  local state
  if ! state=$(read_pin_pr_state); then
    echo "::error::Could not read pin PR state for ${PR_URL}." >&2
    exit 1
  fi
  if [ "$state" != MERGED ]; then
    echo "::error::Pin PR ${PR_URL} is no longer merged (state: ${state})." >&2
    exit 1
  fi
}

dispatch_main_workflows() {
  local workflow
  local -a workflows=()
  read -r -a workflows <<< "$POST_MERGE_WORKFLOWS"
  for workflow in "${workflows[@]}"; do
    assert_pin_pr_merged
    local -a command=(gh workflow run "$workflow" --repo "$GITHUB_REPOSITORY" --ref main)
    if [ "$workflow" = ci.yml ] && [ -n "$BASE_SHA" ]; then
      command+=(-f "base_sha=$BASE_SHA")
    fi
    if ! retry "${command[@]}"; then
      assert_pin_pr_merged
      write_summary "Post-merge ${workflow} dispatch on main failed; main-ci-failure-issue remains the completion monitor."
    fi
  done
}

handle_pin_pr_state() {
  case "$1" in
    MERGED)
      write_summary "Pin PR ${PR_URL} is merged; dispatching post-merge main workflows."
      dispatch_main_workflows
      exit 0
      ;;
    CLOSED)
      echo "::error::Pin PR ${PR_URL} was closed without being merged." >&2
      write_summary "Pin PR ${PR_URL} was closed without being merged."
      exit 1
      ;;
    OPEN)
      return 0
      ;;
    *)
      echo "::error::Unable to determine pin PR state for ${PR_URL}: '$1'." >&2
      exit 1
      ;;
  esac
}

check_pin_pr_state() {
  local state
  if ! state=$(read_pin_pr_state); then
    echo "::error::Could not read pin PR state for ${PR_URL}." >&2
    exit 1
  fi
  handle_pin_pr_state "$state"
}

approve_gated_runs() {
  local run_ids run_id
  run_ids=$(retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs?status=action_required&per_page=100" \
    --jq ".workflow_runs[] | select(.head_branch==\"${BRANCH}\") | .id" || true)
  while IFS= read -r run_id; do
    [ -n "$run_id" ] || continue
    retry gh api -X POST "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/approve" \
      >/dev/null 2>&1 || true
  done <<< "$run_ids"
}

dispatch_pin_workflow() {
  local workflow=$1
  local -a command=(gh workflow run "$workflow" --repo "$GITHUB_REPOSITORY" --ref "$BRANCH")
  if [ "$workflow" = ci.yml ] && [ -n "$BASE_SHA" ]; then
    command+=(-f "base_sha=$BASE_SHA")
  fi
  check_pin_pr_state
  # Skip the dispatch when a pull_request run on the branch head already
  # covers this workflow. A GITHUB_TOKEN push usually produces no run, so
  # dispatch stays the default; the check only dedupes races with real
  # PR-event runs.
  local head_sha existing
  head_sha=$(retry gh pr view "$PR_URL" --repo "$GITHUB_REPOSITORY" \
    --json headRefOid --jq .headRefOid || true)
  if [ -n "$head_sha" ]; then
    existing=$(retry gh run list --repo "$GITHUB_REPOSITORY" --workflow "$workflow" \
      --branch "$BRANCH" --limit 20 --json headSha --jq '.[].headSha' || true)
    if printf '%s\n' "$existing" | grep -qx "$head_sha"; then
      write_summary "${workflow} already covers pin PR head ${head_sha:0:12}; dispatch skipped."
      return 0
    fi
  fi
  if ! retry "${command[@]}"; then
    check_pin_pr_state
    write_summary "Dispatch of ${workflow} for pin PR ${PR_URL} failed; required PR checks remain authoritative."
  fi
}

required_check_counts() {
  local checks_json=$1
  local failures pending count
  failures=$(jq '[.[] | select(.bucket == "fail" or
    (.bucket == null and (.state | IN("FAILURE", "ERROR", "CANCELLED", "CANCELED", "TIMED_OUT", "STARTUP_FAILURE", "STALE"))))] | length' \
    <<< "$checks_json")
  pending=$(jq '[.[] | select(.bucket == "pending" or
    (.bucket == null and (.state | IN("ACTION_REQUIRED", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"))))] | length' \
    <<< "$checks_json")
  count=$(jq 'length' <<< "$checks_json")
  if [ "$count" -eq 0 ]; then
    pending=1
  fi
  printf '%s %s %s\n' "$failures" "$pending" "$count"
}

read_required_checks() {
  local checks_json checks_error
  : > "$REQUIRED_CHECKS_STDERR_FILE"
  checks_json=$(retry gh pr checks "$PR_URL" --repo "$GITHUB_REPOSITORY" --required \
    --json name,state,bucket 2>"$REQUIRED_CHECKS_STDERR_FILE") || true
  if [ -n "$checks_json" ] &&
    jq -e 'type == "array"' >/dev/null 2>&1 <<< "$checks_json"; then
    printf '%s\n' "$checks_json"
    return 0
  fi
  checks_error=$(<"$REQUIRED_CHECKS_STDERR_FILE")
  if [ -z "$checks_json" ] &&
    [[ "$checks_error" == *"no checks reported on the"* ||
      "$checks_error" == *"no required checks reported on the"* ]]; then
    printf '[]\n'
    return 0
  fi
  return 1
}

report_required_checks_error() {
  local detail
  detail=$(<"$REQUIRED_CHECKS_STDERR_FILE")
  detail=${detail//$'\r'/}
  detail=${detail//$'\n'/ }
  printf '::error::Could not determine required checks for pin PR %s: %s\n' \
    "$PR_URL" "$detail" >&2
}

arm_auto_merge() {
  check_pin_pr_state
  if ! retry gh pr merge --repo "$GITHUB_REPOSITORY" --auto --squash --delete-branch "$PR_URL"; then
    check_pin_pr_state
    echo "::error::Could not arm auto-merge for pin PR ${PR_URL}." >&2
    write_summary "Could not arm auto-merge for pin PR ${PR_URL}."
    exit 1
  fi
  check_pin_pr_state
}

check_pin_pr_state
for workflow in ci.yml workflow-lint.yml; do
  dispatch_pin_workflow "$workflow"
done

checks_complete=false
for ((attempt = 1; attempt <= REQUIRED_WAIT_ATTEMPTS; attempt++)); do
  check_pin_pr_state
  approve_gated_runs
  if ! checks_json=$(read_required_checks); then
    check_pin_pr_state
    report_required_checks_error
    exit 1
  fi
  read -r failures pending total <<< "$(required_check_counts "$checks_json")"
  printf 'Required pin PR checks: %s failed, %s pending (%s total).\n' "$failures" "$pending" "$total"
  if [ "$failures" -gt 0 ]; then
    check_pin_pr_state
    echo "::error::A required check concluded non-success for pin PR ${PR_URL}." >&2
    write_summary "A required check concluded non-success for pin PR ${PR_URL}; the PR was left open."
    exit 1
  fi
  if [ "$pending" -eq 0 ]; then
    checks_complete=true
    break
  fi
  if [ "$attempt" -lt "$REQUIRED_WAIT_ATTEMPTS" ]; then
    sleep "$REQUIRED_WAIT_SECONDS"
  fi
done

if [ "$checks_complete" != true ]; then
  arm_auto_merge
  write_summary "auto-merge armed; required checks still running"
  exit 0
fi

arm_auto_merge
for ((attempt = 1; attempt <= MERGE_WAIT_ATTEMPTS; attempt++)); do
  check_pin_pr_state
  if ! checks_json=$(read_required_checks); then
    check_pin_pr_state
    report_required_checks_error
    exit 1
  fi
  read -r failures pending total <<< "$(required_check_counts "$checks_json")"
  if [ "$failures" -gt 0 ]; then
    check_pin_pr_state
    echo "::error::A required check concluded non-success for pin PR ${PR_URL}." >&2
    write_summary "A required check concluded non-success for pin PR ${PR_URL}; the PR was left open."
    exit 1
  fi
  if [ "$pending" -gt 0 ]; then
    write_summary "auto-merge armed; required checks still running"
    exit 0
  fi
  if [ "$attempt" -lt "$MERGE_WAIT_ATTEMPTS" ]; then
    sleep "$MERGE_WAIT_SECONDS"
  fi
done

check_pin_pr_state
if retry gh pr merge --repo "$GITHUB_REPOSITORY" --squash --delete-branch "$PR_URL"; then
  check_pin_pr_state
fi
write_summary "Auto-merge remains armed for pin PR ${PR_URL}; post-merge main workflows will be dispatched when it lands."
