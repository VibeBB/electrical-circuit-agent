"""Tests for scripts/release_bump.sh (the release workflow's bump step).

A fixture repo with minimal version-bearing files stands in for the checkout
and stub `gh`/`git` executables on PATH record calls and emit canned output,
so the state machine — version resolution, the tag-exists check, the
skip-commit branch, direct-push vs. pull-request fallback, and the dry_run
gate — is exercised without touching GitHub.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_bump.sh"
REPOSITORY = "VibeBB/electrical-circuit-agent"
PR_URL = f"https://github.com/{REPOSITORY}/pull/123"
HEAD_SHA = "aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee"
MAIN_SHA = "ffffffff11111111222222223333333344444444"
BUMP_BRANCH = "bot/release-bump-v0.1.1-4242"

SKILLS = [
    "circuit-brief",
    "circuit-firmware",
    "circuit-konnect",
    "circuit-libraries",
    "circuit-library-guard",
    "circuit-verification",
    "circuit-workflow",
]


def _make_repo(tmp_path: Path, version: str = "0.1.0") -> Path:
    root = tmp_path / "repo"
    (root / "plugins/circuit/.plugin").mkdir(parents=True)
    (root / "plugins/circuit/.plugin/plugin.json").write_text(
        f'{{\n  "name": "circuit",\n  "version": "{version}"\n}}\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "circuit-agent"\nversion = "{version}"\n\n'
        '[tool.ruff]\ntarget-version = "py312"\n',
        encoding="utf-8",
    )
    for skill in SKILLS:
        skill_dir = root / f"plugins/circuit/skills/{skill}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\nversion: {version}\nlicense: BSD-3-Clause\n---\n",
            encoding="utf-8",
        )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "other"\nversion = "9.9.9"\n\n'
        f'[[package]]\nname = "circuit-agent"\nversion = "{version}"\n'
        'source = { virtual = "." }\n',
        encoding="utf-8",
    )
    return root


GIT_STUB = """#!/usr/bin/env bash
set -eu
printf 'git %s\\n' "$*" >> "$GIT_STUB_CALLS"
case "$1" in
  rev-parse)
    if [ "$2" = "HEAD" ]; then
      printf '%s\\n' "$GIT_STUB_HEAD"
    else
      printf '%s\\n' "$GIT_STUB_MAIN_SHA"
    fi
    ;;
  ls-remote)
    tag="${!#}"
    tag="${tag##refs/tags/}"
    case " $GIT_STUB_TAGS " in
      *" $tag "*)
        printf 'deadbeef\\t%s\\n' "${!#}"
        exit 0
        ;;
      *)
        exit 2
        ;;
    esac
    ;;
  push)
    if [ "$3" = "HEAD:main" ] && [ "${GIT_STUB_PUSH_MAIN:-ok}" = "fail" ]; then
      printf 'rejected: pull_request rule\\n' >&2
      exit 1
    fi
    ;;
  config|add|commit|fetch)
    ;;
  *)
    printf 'stub git: unhandled %s\\n' "$*" >&2
    exit 1
    ;;
esac
"""

GH_STUB = """#!/usr/bin/env bash
set -eu
printf 'gh %s\\n' "$*" >> "$GH_STUB_CALLS"
case "$1" in
  pr)
    case "$2" in
      create)
        printf '%s\\n' "$GH_STUB_PR_URL"
        ;;
      merge)
        exit "${GH_STUB_MERGE_RC:-0}"
        ;;
      *)
        printf 'stub gh: unhandled %s\\n' "$*" >&2
        exit 1
        ;;
    esac
    ;;
  run)
    case "$2" in
      list)
        if [[ "$*" == *pull_request* ]]; then
          prior=$(grep -c 'gh run list.*pull_request' "$GH_STUB_CALLS")
          if [ "$prior" -le 1 ]; then
            printf '%s\\n' ${GH_STUB_GATED_IDS:-}
          fi
        else
          printf '%s\\n' "${GH_STUB_RUN_ID:-777}"
        fi
        ;;
      watch)
        ;;
      view)
        printf '%s\\n' "${GH_STUB_CONCLUSION:-success}"
        ;;
      *)
        printf 'stub gh: unhandled %s\\n' "$*" >&2
        exit 1
        ;;
    esac
    ;;
  workflow)
    ;;
  api)
    if [[ "$*" == *"/pulls/"* ]]; then
      if [ -n "${GH_STUB_MERGED_AT:-}" ]; then
        printf '%s\\n' "$GH_STUB_MERGED_AT"
      fi
    elif [[ "$*" != *"-X POST"*"/approve"* ]]; then
      printf 'stub gh: unhandled %s\\n' "$*" >&2
      exit 1
    fi
    ;;
  *)
    printf 'stub gh: unhandled %s\\n' "$*" >&2
    exit 1
    ;;
esac
"""


class Fixture(NamedTuple):
    repo: Path
    env: dict[str, str]
    output: Path
    summary: Path
    git_calls: Path
    gh_calls: Path


@pytest.fixture
def release_bump(tmp_path: Path) -> Fixture:
    repo = _make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("git", GIT_STUB), ("gh", GH_STUB)):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    output = tmp_path / "ghout"
    summary = tmp_path / "summary.md"
    git_calls = tmp_path / "git.log"
    gh_calls = tmp_path / "gh.log"
    env = os.environ.copy()
    # The session exports gh() through BASH_ENV/BASH_FUNC_gh%%; a function
    # shadows the PATH stub, so the test env must not inherit it.
    env.pop("BASH_ENV", None)
    for key in [k for k in env if k.startswith("BASH_FUNC_")]:
        env.pop(key)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "RELEASE_BUMP_REPO_ROOT": str(repo),
            "BUMP": "patch",
            "SET_VERSION": "",
            "DRY_RUN": "false",
            "GH_TOKEN": "stub-token",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "4242",
            "GIT_STUB_CALLS": str(git_calls),
            "GIT_STUB_HEAD": HEAD_SHA,
            "GIT_STUB_MAIN_SHA": MAIN_SHA,
            "GIT_STUB_TAGS": "",
            "GH_STUB_CALLS": str(gh_calls),
            "GH_STUB_PR_URL": PR_URL,
            "GH_STUB_GATED_IDS": "555 556",
            "GH_STUB_MERGED_AT": "2026-10-04T00:00:00Z",
            "RELEASE_BUMP_RETRY_ATTEMPTS": "1",
            "RELEASE_BUMP_RETRY_DELAY_SECONDS": "0",
            "RELEASE_BUMP_GATED_POLLS": "5",
            "RELEASE_BUMP_GATED_EMPTY_SECONDS": "0",
            "RELEASE_BUMP_GATED_BATCH_SECONDS": "0",
            "RELEASE_BUMP_RUN_WAIT_ATTEMPTS": "3",
            "RELEASE_BUMP_RUN_WAIT_SECONDS": "0",
            "RELEASE_BUMP_CONCLUSION_ATTEMPTS": "2",
            "RELEASE_BUMP_CONCLUSION_SECONDS": "0",
            "RELEASE_BUMP_WATCH_INTERVAL": "0",
            "RELEASE_BUMP_MERGE_ATTEMPTS": "2",
            "RELEASE_BUMP_MERGE_SECONDS": "0",
        }
    )
    return Fixture(repo, env, output, summary, git_calls, gh_calls)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )


def _outputs(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    pairs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        pairs[key] = value
    return pairs


def _calls(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _pyproject_version(repo: Path) -> str:
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version = "([^"]+)"', text)
    assert m is not None
    return m.group(1)


def test_dry_run_computes_version_without_writes(release_bump: Fixture) -> None:
    release_bump.env.update({"DRY_RUN": "true", "BUMP": "minor"})
    result = _run(release_bump.env)

    assert result.returncode == 0, result.stderr
    outputs = _outputs(release_bump.output)
    assert outputs == {"sha": HEAD_SHA, "version": "0.2.0", "tag": "v0.2.0"}
    summary = release_bump.summary.read_text(encoding="utf-8")
    assert f"dry-run: v0.2.0 would release at {HEAD_SHA}" in summary
    git_calls = _calls(release_bump.git_calls)
    assert "ls-remote" in git_calls and "rev-parse" in git_calls
    assert "commit" not in git_calls and "push" not in git_calls
    assert _calls(release_bump.gh_calls) == ""
    assert _pyproject_version(release_bump.repo) == "0.1.0"


def test_dry_run_existing_tag_fails(release_bump: Fixture) -> None:
    release_bump.env.update({"DRY_RUN": "true", "GIT_STUB_TAGS": "v0.1.1"})
    result = _run(release_bump.env)

    assert result.returncode == 1
    assert "tag v0.1.1 already exists" in result.stderr
    assert "push" not in _calls(release_bump.git_calls)


def test_dry_run_set_same_as_current(release_bump: Fixture) -> None:
    release_bump.env.update({"DRY_RUN": "true", "SET_VERSION": "v0.1.0"})
    result = _run(release_bump.env)

    assert result.returncode == 0, result.stderr
    outputs = _outputs(release_bump.output)
    assert outputs == {"sha": HEAD_SHA, "version": "0.1.0", "tag": "v0.1.0"}
    assert _pyproject_version(release_bump.repo) == "0.1.0"


def test_real_bump_direct_push(release_bump: Fixture) -> None:
    result = _run(release_bump.env)

    assert result.returncode == 0, result.stderr
    outputs = _outputs(release_bump.output)
    assert outputs == {"sha": HEAD_SHA, "version": "0.1.1", "tag": "v0.1.1"}
    git_calls = _calls(release_bump.git_calls)
    assert "git commit -m Release v0.1.1" in git_calls
    assert "git push origin HEAD:main" in git_calls
    assert "refs/heads/bot/" not in git_calls
    assert _calls(release_bump.gh_calls) == ""
    assert _pyproject_version(release_bump.repo) == "0.1.1"


def test_set_same_as_current_skips_commit(release_bump: Fixture) -> None:
    release_bump.env.update({"SET_VERSION": "0.1.0"})
    result = _run(release_bump.env)

    assert result.returncode == 0, result.stderr
    outputs = _outputs(release_bump.output)
    assert outputs == {"sha": HEAD_SHA, "version": "0.1.0", "tag": "v0.1.0"}
    git_calls = _calls(release_bump.git_calls)
    assert "commit" not in git_calls and "push" not in git_calls
    assert _pyproject_version(release_bump.repo) == "0.1.0"


def test_set_higher_version_writes_and_pushes(release_bump: Fixture) -> None:
    release_bump.env.update({"SET_VERSION": "v0.3.0"})
    result = _run(release_bump.env)

    assert result.returncode == 0, result.stderr
    outputs = _outputs(release_bump.output)
    assert outputs["version"] == "0.3.0" and outputs["tag"] == "v0.3.0"
    assert "git push origin HEAD:main" in _calls(release_bump.git_calls)
    assert _pyproject_version(release_bump.repo) == "0.3.0"


def test_set_lower_version_fails(release_bump: Fixture) -> None:
    release_bump.env.update({"SET_VERSION": "0.0.1"})
    result = _run(release_bump.env)

    assert result.returncode == 1
    assert "must be greater" in result.stderr
    assert _pyproject_version(release_bump.repo) == "0.1.0"


def test_existing_tag_fails(release_bump: Fixture) -> None:
    release_bump.env.update({"GIT_STUB_TAGS": "v0.1.1"})
    result = _run(release_bump.env)

    assert result.returncode == 1
    assert "tag v0.1.1 already exists" in result.stderr
    git_calls = _calls(release_bump.git_calls)
    assert "commit" not in git_calls and "push" not in git_calls


def test_push_rejected_routes_through_pull_request(release_bump: Fixture) -> None:
    release_bump.env.update({"GIT_STUB_PUSH_MAIN": "fail"})
    result = _run(release_bump.env)

    assert result.returncode == 0, result.stderr
    outputs = _outputs(release_bump.output)
    # The release SHA is the merged main tip, not the bump-branch head.
    assert outputs == {"sha": MAIN_SHA, "version": "0.1.1", "tag": "v0.1.1"}
    git_calls = _calls(release_bump.git_calls)
    assert "git push origin HEAD:main" in git_calls
    assert f"git push origin HEAD:refs/heads/{BUMP_BRANCH}" in git_calls
    assert "git fetch -q origin main" in git_calls
    gh_calls = _calls(release_bump.gh_calls)
    assert f"gh pr create --repo {REPOSITORY} --base main --head {BUMP_BRANCH}" in gh_calls
    assert "actions/runs/555/approve" in gh_calls
    assert "actions/runs/556/approve" in gh_calls
    for wf in ("ci.yml", "workflow-lint.yml"):
        assert f"gh workflow run {wf} --repo {REPOSITORY} --ref {BUMP_BRANCH}" in gh_calls
    assert "--event workflow_dispatch" in gh_calls
    assert "--auto --squash --delete-branch" in gh_calls
    assert f"gh pr merge --repo {REPOSITORY}" in gh_calls
    assert f"gh api repos/{REPOSITORY}/pulls/123" in gh_calls
    summary = release_bump.summary.read_text(encoding="utf-8")
    assert f"version-bump PR: {PR_URL}" in summary


def test_pr_fallback_check_failure_leaves_pr_open(release_bump: Fixture) -> None:
    release_bump.env.update({"GIT_STUB_PUSH_MAIN": "fail", "GH_STUB_CONCLUSION": "failure"})
    result = _run(release_bump.env)

    assert result.returncode == 1
    gh_calls = _calls(release_bump.gh_calls)
    assert "pr merge" not in gh_calls
    summary = release_bump.summary.read_text(encoding="utf-8")
    assert "version-bump PR checks failed" in summary


def test_pr_fallback_merge_timeout(release_bump: Fixture) -> None:
    release_bump.env.update({"GIT_STUB_PUSH_MAIN": "fail", "GH_STUB_MERGED_AT": ""})
    result = _run(release_bump.env)

    assert result.returncode == 1
    gh_calls = _calls(release_bump.gh_calls)
    assert "pr merge" in gh_calls
    summary = release_bump.summary.read_text(encoding="utf-8")
    assert "did not merge within the wait" in summary
    assert "rev-parse origin/main" not in _calls(release_bump.git_calls)
