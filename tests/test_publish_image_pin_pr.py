from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY = f"VibeBB/{Path(__file__).parents[1].name}"
PR_URL = f"https://github.com/{REPOSITORY}/pull/123"
BRANCH = "bot/update-image-digests-test"


@pytest.fixture
def publish_pin_pr(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$GH_STUB_CALLS"
case "$1 $2" in
  "pr view")
    if [[ "$*" == *"headRefOid"* ]]; then
      printf '%s\\n' "${GH_STUB_HEAD_SHA:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
    else
      case "$GH_STUB_CASE" in
        merged) printf 'MERGED\\n' ;;
        closed) printf 'CLOSED\\n' ;;
        *) printf 'OPEN\\n' ;;
      esac
    fi
    ;;
  "run list")
    if [[ "${GH_STUB_RUN_COVERS_HEAD:-0}" == "1" ]]; then
      printf '%s\\n' "${GH_STUB_HEAD_SHA:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
    fi
    ;;
  "workflow run")
    ;;
  "pr checks")
    check_call=$(grep -c '^pr checks ' "$GH_STUB_CALLS")
    case "$GH_STUB_CASE" in
      unreported-then-green)
        case "$check_call" in
          1)
            printf "Error: no checks reported on the '%s' branch\\n" \
              'bot/update-image-digests-test' >&2
            exit 1
            ;;
          2)
            printf "Error: no required checks reported on the '%s' branch\\n" \
              'bot/update-image-digests-test' >&2
            exit 1
            ;;
          3)
            printf '[{"name":"verify","state":"PENDING","bucket":"pending"}]\\n'
            exit 8
            ;;
          *)
            printf '[{"name":"verify","state":"SUCCESS","bucket":"pass"}]\\n'
            ;;
        esac
        ;;
      no-required-checks-always)
        printf "Error: no required checks reported on the '%s' branch\\n" \
          'bot/update-image-digests-test' >&2
        exit 1
        ;;
      unexpected-check-error)
        printf 'stub transport error: permission denied\\n' >&2
        exit 1
        ;;
      required-failure)
        printf '[{"name":"verify","state":"FAILURE","bucket":"fail"}]\\n'
        ;;
      action-required)
        printf '[{"name":"verify","state":"ACTION_REQUIRED","bucket":null}]\\n'
        ;;
      pending-timeout)
        printf '[{"name":"verify","state":"PENDING","bucket":"pending"}]\\n'
        ;;
      *)
        printf '[{"name":"verify","state":"SUCCESS","bucket":"pass"}]\\n'
        ;;
    esac
    ;;
  "pr merge")
    ;;
  api\\ *)
    if [[ "$*" == *"-X POST"*"/approve"* ]]; then
      printf 'approval response noise\\n'
    else
      printf '77\\n'
    fi
    ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    summary = tmp_path / "summary.md"
    calls = tmp_path / "calls.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_STEP_SUMMARY": str(summary),
            "GH_STUB_CALLS": str(calls),
            "PUBLISH_PIN_PR_REQUIRED_WAIT_ATTEMPTS": "1",
            "PUBLISH_PIN_PR_REQUIRED_WAIT_SECONDS": "0",
            "PUBLISH_PIN_PR_MERGE_WAIT_ATTEMPTS": "1",
            "PUBLISH_PIN_PR_MERGE_WAIT_SECONDS": "0",
            "PUBLISH_PIN_PR_RETRY_ATTEMPTS": "1",
            "PUBLISH_PIN_PR_RETRY_DELAY_SECONDS": "0",
            "PUBLISH_PIN_PR_POST_MERGE_WORKFLOWS": "ci.yml locked-image-check.yml",
        }
    )
    return Path(__file__).parents[1] / "scripts/publish_image_pin_pr.sh", env, calls


def run_helper(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), PR_URL, BRANCH, "", "ci.yml locked-image-check.yml"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.parametrize(
    ("case", "returncode", "expected_summary"),
    [
        ("merged", 0, "is merged; dispatching"),
        ("closed", 1, "was closed without being merged"),
        ("action-required", 0, "auto-merge armed; required checks still running"),
        ("pending-timeout", 0, "auto-merge armed; required checks still running"),
        ("required-failure", 1, "A required check concluded non-success"),
    ],
)
def test_pin_pr_state_and_required_checks(
    publish_pin_pr: tuple[Path, dict[str, str], Path],
    tmp_path: Path,
    case: str,
    returncode: int,
    expected_summary: str,
) -> None:
    script, env, calls = publish_pin_pr
    env["GH_STUB_CASE"] = case
    result = run_helper(script, env)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    call_log = calls.read_text(encoding="utf-8")

    assert result.returncode == returncode
    assert expected_summary in summary
    assert "--json state,mergedAt" in call_log
    print(f"{case}: exit={result.returncode}")
    if result.stdout:
        print(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr:
        print(f"stderr:\n{result.stderr.rstrip()}")
    print(f"summary:\n{summary.rstrip()}")
    if case == "merged":
        assert f"workflow run ci.yml --repo {REPOSITORY} --ref main" in call_log
        assert f"--ref {BRANCH}" not in call_log
    if case == "closed":
        assert "workflow run" not in call_log
    if case in ("action-required", "pending-timeout"):
        assert "--required --json name,state,bucket" in call_log
        assert "--auto --squash --delete-branch" in call_log
        assert "approval response noise" not in result.stdout + result.stderr
    if case == "action-required":
        assert "api -X POST repos/" in call_log
        assert f"--ref {BRANCH}" in call_log
    if case == "required-failure":
        assert "--auto --squash --delete-branch" not in call_log


def test_unreported_checks_transition_to_green_json(
    publish_pin_pr: tuple[Path, dict[str, str], Path],
    tmp_path: Path,
) -> None:
    script, env, calls = publish_pin_pr
    env.update(
        {
            "GH_STUB_CASE": "unreported-then-green",
            "PUBLISH_PIN_PR_REQUIRED_WAIT_ATTEMPTS": "4",
        }
    )

    result = run_helper(script, env)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    call_log = calls.read_text(encoding="utf-8")

    assert result.returncode == 0
    assert call_log.count("pr checks ") == 5
    assert "--auto --squash --delete-branch" in call_log
    assert "--squash --delete-branch" in call_log
    assert "Auto-merge remains armed" in summary
    assert "no checks reported" not in result.stderr
    assert "no required checks reported" not in result.stderr


def test_no_required_checks_arms_auto_merge(
    publish_pin_pr: tuple[Path, dict[str, str], Path],
    tmp_path: Path,
) -> None:
    script, env, calls = publish_pin_pr
    env["GH_STUB_CASE"] = "no-required-checks-always"

    result = run_helper(script, env)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    call_log = calls.read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "auto-merge armed; required checks still running" in summary
    assert call_log.count("pr checks ") == 1
    assert "--auto --squash --delete-branch" in call_log


def test_unexpected_required_check_error_fails_with_stderr(
    publish_pin_pr: tuple[Path, dict[str, str], Path],
) -> None:
    script, env, calls = publish_pin_pr
    env["GH_STUB_CASE"] = "unexpected-check-error"

    result = run_helper(script, env)

    assert result.returncode == 1
    assert (
        f"::error::Could not determine required checks for pin PR {PR_URL}: "
        "stub transport error: permission denied"
    ) in result.stderr
    assert calls.read_text(encoding="utf-8").count("pr checks ") == 1


def test_dispatch_skipped_when_pull_request_run_covers_head(
    publish_pin_pr: tuple[Path, dict[str, str], Path],
    tmp_path: Path,
) -> None:
    script, env, calls = publish_pin_pr
    env["GH_STUB_CASE"] = ""
    env["GH_STUB_RUN_COVERS_HEAD"] = "1"

    result = run_helper(script, env)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    call_log = calls.read_text(encoding="utf-8")

    assert result.returncode == 0
    assert f"--ref {BRANCH}" not in call_log
    assert "already covers pin PR head" in summary
    # Only the redundant dispatch is skipped; the merge path still runs.
    assert "--squash --delete-branch" in call_log


def test_publish_workflow_uses_pin_helper_and_sbom_guard() -> None:
    root = Path(__file__).parents[1]
    workflows = list((root / ".github/workflows").glob("publish-*-images.yml"))
    assert len(workflows) == 1
    workflow = workflows[0].read_text(encoding="utf-8")
    helper = (root / "scripts/publish_image_pin_pr.sh").read_text(encoding="utf-8")

    assert "scripts/publish_image_pin_pr.sh" in workflow
    assert "timeout-minutes: 120" in workflow
    assert "REQUIRED_WAIT_ATTEMPTS=" in helper and ":-40}" in helper
    assert "REQUIRED_WAIT_SECONDS=" in helper and ":-15}" in helper
    assert 'gh pr checks "$PR_URL" --repo "$GITHUB_REPOSITORY" --required' in helper
    assert "SYFT_SOURCE_IMAGE_DEFAULT_PULL_SOURCE: registry" in workflow
    assert "TMPDIR: ${{ runner.temp }}" in workflow
    assert "SYFT_FILE_METADATA_SELECTION: none" in workflow
    assert "Guard tools SPDX SBOM size" in workflow
    generate = workflow.index("Generate tools SPDX SBOM")
    guard = workflow.index("Guard tools SPDX SBOM size")
    validate = workflow.index("Validate tools SPDX SBOM")
    assert generate < guard < validate
    assert 'df -h /tmp "$RUNNER_TEMP"' in workflow
    assert "16777216" in workflow
