from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts.check_dependency_updates import (
    ProjectDependency,
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
