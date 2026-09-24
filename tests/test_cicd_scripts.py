from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts.check_dependency_updates import (
    ProjectDependency,
    Status,
    apply_deferrals,
    check_docker_base,
    check_pypi_lock,
    check_python_versions,
    check_uv_pin,
    load_deferrals,
    locked_versions,
    parse_kicad_packages,
    pypi_status,
    release_version,
    request,
    version_tuple,
)
from scripts.measure_image_tools import measure
from scripts.print_locked_image import locked_image
from scripts.update_image_digest_lock import update_lock


def test_update_and_read_image_lock(tmp_path: Path) -> None:
    lock = tmp_path / "image-digests.json"
    tools = {"kicad-cli": "10.99.0", "konnect": "0.12.1"}
    assert update_lock(
        lock,
        entry="circuit_tools",
        image="ghcr.io/example/circuit-tools",
        tag="abc-tools",
        digest=f"sha256:{'a' * 64}",
        published_at="2026-09-20T00:00:00Z",
        workflow_run="https://example.invalid/run/1",
        dockerfile="docker/circuit-tools.Dockerfile",
        tools=tools,
    )
    assert locked_image(lock, "circuit_tools") == f"ghcr.io/example/circuit-tools@sha256:{'a' * 64}"
    assert not update_lock(
        lock,
        entry="circuit_tools",
        image="ghcr.io/example/circuit-tools",
        tag="abc-tools",
        digest=f"sha256:{'a' * 64}",
        published_at="2026-09-20T00:00:00Z",
        workflow_run="https://example.invalid/run/1",
        dockerfile="docker/circuit-tools.Dockerfile",
        tools=tools,
    )


def test_image_lock_read_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        locked_image(tmp_path / "missing.json", "circuit_tools")
    (tmp_path / "image-digests.json").write_text(
        json.dumps({"circuit_tools": {"image": "x", "digest": "latest"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest-pinned"):
        locked_image(tmp_path / "image-digests.json", "circuit_tools")
    with pytest.raises(ValueError, match="digest"):
        update_lock(
            tmp_path / "image-digests.json",
            entry="circuit_server",
            image="ghcr.io/example/circuit-server",
            tag="abc-server",
            digest=f"sha256:{'0' * 64}",
            published_at="2026-09-20T00:00:00Z",
            workflow_run="https://example.invalid/run/1",
            dockerfile="docker/circuit-tools.Dockerfile",
            tools={"python": "3.14"},
        )


def test_dependency_version_parsing() -> None:
    assert version_tuple("202609190245+6d837080a5") > version_tuple("202609190000")


def test_release_version_strips_tag_prefix() -> None:
    assert release_version("v2.4.1", "v") == "2.4.1"
    assert release_version("jdk-27.0.0.0", "jdk-") == "27.0.0.0"
    with pytest.raises(ValueError, match="does not start with"):
        release_version("2.4.1", "v")


def test_dependency_checker_reads_resolved_versions_from_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "mcp"\nversion = "1.30.0"\n',
        encoding="utf-8",
    )
    assert locked_versions(tmp_path)["mcp"] == "1.30.0"


def test_dependency_checker_uses_latest_version_within_specifier() -> None:
    status = pypi_status(
        "mcp",
        ProjectDependency(">=1.29,<2", "1.29"),
        "1.30.0",
        {
            "info": {"version": "2.2.0"},
            "releases": {"1.29.0": [], "1.30.0": [], "2.2.0": []},
        },
        [],
    )
    assert status.latest == "1.30.0"
    assert not status.outdated
    assert status.note == "上限外: 2.2.0"


def test_dependency_checker_deferral_suppresses_until_expiry(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    deferral = {
        "deferrals": [
            {
                "target": "mcp",
                "version": "2.x",
                "reason": "SDK constraint",
                "recheck_by": "2099-12-31",
            }
        ]
    }
    (scripts / "dependency_update_deferrals.json").write_text(
        json.dumps(deferral),
        encoding="utf-8",
    )
    loaded = load_deferrals(tmp_path)
    deferred = pypi_status(
        "mcp",
        ProjectDependency(">=1.29,<2", "1.29"),
        "1.30.0",
        {"info": {"version": "2.2.0"}, "releases": {"1.30.0": [], "2.2.0": []}},
        loaded,
    )
    assert deferred.decision == "保留\uff08記録済み\uff09"
    assert not deferred.outdated
    expired = pypi_status(
        "mcp",
        ProjectDependency(">=1.29,<2", "1.29"),
        "1.30.0",
        {"info": {"version": "2.2.0"}, "releases": {"1.30.0": [], "2.2.0": []}},
        [{**loaded[0], "recheck_by": "2000-01-01"}],
    )
    assert expired.outdated
    assert "保留期限切れ" in expired.note


def test_dependency_checker_rejects_invalid_deferrals(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "dependency_update_deferrals.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid shape"):
        load_deferrals(tmp_path)


def test_dependency_request_uses_github_token_only_for_github_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    github_request = request("https://api.github.com/repos/example/project")
    other_request = request("https://pypi.org/pypi/example/json")

    assert github_request.get_header("Authorization") == "Bearer test-token"
    assert github_request.get_header("X-github-api-version") == "2022-11-28"
    assert other_request.get_header("Authorization") is None
    assert other_request.get_header("X-github-api-version") is None


def test_parse_kicad_packages() -> None:
    import gzip

    payload = gzip.compress(
        b"Package: kicad-nightly\nVersion: 1\n\n"
        b"Package: kicad-nightly\nVersion: 2\n\n"
        b"Package: kicad-nightly-footprints\nVersion: 3\n\n"
        b"Package: kicad-nightly-symbols\nVersion: 4\n"
    )
    assert parse_kicad_packages(payload) == {
        "kicad-nightly": "2",
        "kicad-nightly-footprints": "3",
        "kicad-nightly-symbols": "4",
    }


def test_measure_image_tools_reads_cern_commit_file(monkeypatch: pytest.MonkeyPatch) -> None:
    output = "\n".join(
        [
            "10.99.0",
            "konnect 0.12.1",
            "Python 3.14.4",
            "circuit=0.0.1",
            "semeru_jre=27.0.0.0",
            "freerouting=2.4.1",
            "9dba1850616da7fb1a4834531a3a1f0fff7c8666",
            "kicad-nightly=202609190245+6d837080a5~189~ubuntu26.04.1",
            "kicad-nightly-footprints=202609171319+1df46f29b~14~ubuntu26.04.1",
            "kicad-nightly-symbols=202609181937+a82391d3d~12~ubuntu26.04.1",
        ]
    )

    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("scripts.measure_image_tools.subprocess.run", fake_run)
    result = measure("circuit-tools:dev")
    assert result["cern_commit"] == "9dba1850616da7fb1a4834531a3a1f0fff7c8666"


def test_measure_image_tools_rejects_unknown_cern_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "\n".join(
        [
            "10.99.0",
            "konnect 0.12.1",
            "Python 3.14.4",
            "circuit=0.0.1",
            "semeru_jre=27.0.0.0",
            "freerouting=2.4.1",
            "unknown",
            "kicad-nightly=202609190245+6d837080a5~189~ubuntu26.04.1",
            "kicad-nightly-footprints=202609171319+1df46f29b~14~ubuntu26.04.1",
            "kicad-nightly-symbols=202609181937+a82391d3d~12~ubuntu26.04.1",
        ]
    )

    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("scripts.measure_image_tools.subprocess.run", fake_run)
    with pytest.raises(ValueError, match="metadata probe omitted"):
        measure("ghcr.io/example/circuit-tools@sha256:" + "a" * 64)


def test_check_pypi_lock_reports_transitive_drift(tmp_path: Path) -> None:
    output = (
        "Resolved 42 packages in 1.23s\n"
        "Update transitive-dep v1.0.0 -> v1.1.0\n"
        "Add new-dep v2.0.0\n"
        "Remove old-dep v3.0.0\n"
        "Update pydantic v2.0.0 -> v2.1.0\n"
    )
    statuses = check_pypi_lock(
        tmp_path,
        {"pydantic"},
        run_uv=lambda command, cwd: output,
    )
    by_name = {status.name: status for status in statuses}
    assert set(by_name) == {
        "transitive-dep (transitive)",
        "new-dep (transitive)",
        "old-dep (transitive)",
    }
    assert by_name["transitive-dep (transitive)"].current == "1.0.0"
    assert by_name["transitive-dep (transitive)"].latest == "1.1.0"
    assert by_name["new-dep (transitive)"].latest == "2.0.0"
    assert by_name["new-dep (transitive)"].note == "would be added"
    assert by_name["old-dep (transitive)"].current == "3.0.0"
    assert by_name["old-dep (transitive)"].latest == "-"
    assert all(status.outdated for status in statuses)


def test_check_uv_pin_compares_required_version(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n[tool.uv]\nrequired-version = "==0.12.18"\n',
        encoding="utf-8",
    )
    statuses = check_uv_pin(
        tmp_path,
        fetch_json=lambda url: {"info": {"version": "0.12.18"}},
    )
    assert len(statuses) == 1
    assert statuses[0].current == "0.12.18"
    assert not statuses[0].outdated
    statuses = check_uv_pin(
        tmp_path,
        fetch_json=lambda url: {"info": {"version": "0.13.0"}},
    )
    assert statuses[0].outdated


def test_check_python_versions_flags_older_minors(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - uses: actions/setup-python@x\n"
        '        with:\n          python-version: "3.12"\n',
        encoding="utf-8",
    )
    statuses = check_python_versions(
        tmp_path,
        list_remote_tags=lambda url: ["v3.11.9", "v3.12.1", "v3.13.0", "v3.14.0"],
    )
    assert statuses
    assert all(status.latest == "3.14" for status in statuses)
    assert all(status.outdated for status in statuses)
    assert {status.source for status in statuses} == {"pyproject.toml", "ci.yml"}


def test_check_docker_base_tracks_ubuntu_and_uv_images(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    docker.mkdir()
    (docker / "circuit-tools.Dockerfile").write_text(
        "FROM ghcr.io/astral-sh/uv:0.12.18 AS uv\nFROM ubuntu:26.04\nFROM debian:13\n",
        encoding="utf-8",
    )

    def fetch_json(url: str) -> Any:
        if "hub.docker.com" in url:
            return {"results": [{"name": "26.04"}, {"name": "24.10"}], "next": None}
        if "pypi.org" in url:
            return {"info": {"version": "0.13.0"}}
        raise ValueError(url)

    statuses = check_docker_base(tmp_path, fetch_json=fetch_json)
    by_name = {status.name: status for status in statuses}
    assert by_name["Docker base ubuntu"].latest == "26.04"
    assert not by_name["Docker base ubuntu"].outdated
    assert by_name["uv base image"].latest == "0.13.0"
    assert by_name["uv base image"].outdated
    assert by_name["Docker base debian"].note == "unhandled image"


def test_check_python_versions_fails_closed_without_stable_tags(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no stable CPython"):
        check_python_versions(tmp_path, list_remote_tags=lambda url: [])


def test_apply_deferrals_suppresses_matching_statuses(tmp_path: Path) -> None:
    statuses = [
        Status("Python minor (ci.yml)", "3.12", "3.14", "ci.yml", True),
        Status("uv", "0.12.18", "0.13.0", "pyproject", True),
        Status("Konnect", "0.12.1", "0.12.1", "GitHub", False),
    ]
    deferrals = [
        {
            "target": "python minor",
            "version": "3.x",
            "reason": "pending SDK verification",
            "recheck_by": "2099-12-31",
        }
    ]
    applied = apply_deferrals(statuses, deferrals)
    assert applied[0].decision == "保留\uff08記録済み\uff09"
    assert not applied[0].outdated
    assert "pending SDK verification" in applied[0].note
    assert applied[1].outdated
    assert applied[1].decision == ""
    expired = apply_deferrals(
        statuses,
        [{**deferrals[0], "recheck_by": "2000-01-01"}],
    )
    assert expired[0].outdated
    assert "保留期限切れ" in expired[0].note


def test_apply_deferrals_skips_version_mismatch(tmp_path: Path) -> None:
    statuses = [Status("Python minor (ci.yml)", "3.12", "4.0", "ci.yml", True)]
    deferrals = [
        {
            "target": "python minor",
            "version": "3.x",
            "reason": "pending",
            "recheck_by": "2099-12-31",
        }
    ]
    applied = apply_deferrals(statuses, deferrals)
    assert applied[0].outdated
    assert applied[0].decision == ""
