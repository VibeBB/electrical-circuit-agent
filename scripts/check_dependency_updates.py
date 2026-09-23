#!/usr/bin/env python3
"""Report dependency updates without modifying repository source files."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

Fetch = Callable[[str], bytes]
FetchJson = Callable[[str], Any]

_ACTION = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@([0-9a-f]{40})(?:\s*#\s*(v[\w.-]+))?")
_ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)=(\S+)\s*$", re.MULTILINE)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class Status:
    name: str
    current: str
    latest: str
    source: str
    outdated: bool
    note: str = ""
    decision: str = ""


@dataclass(frozen=True)
class ProjectDependency:
    specifier: str
    floor: str


def parse_kicad_packages(payload: bytes) -> dict[str, str]:
    text = gzip.decompress(payload).decode("utf-8")
    versions: dict[str, list[str]] = {}
    package: str | None = None
    for line in text.splitlines():
        if line.startswith("Package: "):
            package = line.removeprefix("Package: ").strip()
        elif line.startswith("Version: ") and package is not None:
            versions.setdefault(package, []).append(line.removeprefix("Version: ").strip())
    result: dict[str, str] = {}
    for package in ("kicad-nightly", "kicad-nightly-footprints", "kicad-nightly-symbols"):
        values = versions.get(package)
        if not values:
            raise ValueError(f"PPA metadata has no {package}")
        result[package] = max(values)
    return result


def request(url: str) -> Request:
    headers = {"User-Agent": "circuit-agent-dependency-check"}
    if urlsplit(url).hostname == "api.github.com":
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
    return Request(url, headers=headers)


def _default_fetch(url: str) -> bytes:
    with urlopen(request(url), timeout=30) as response:
        return response.read()


def _default_json(url: str) -> Any:
    return json.loads(_default_fetch(url).decode("utf-8"))


def version_tuple(value: str) -> tuple[int, ...]:
    values = re.findall(r"\d+", value)
    if not values:
        raise ValueError(f"version has no numeric components: {value}")
    return tuple(int(item) for item in values)


def _project_pins(root: Path) -> dict[str, ProjectDependency]:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_value = data.get("project")
    project = cast(dict[str, Any], project_value) if isinstance(project_value, dict) else None
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml has no project table")
    values: dict[str, ProjectDependency] = {}
    dependencies_value = project.get("dependencies", [])
    if not isinstance(dependencies_value, list):
        raise ValueError("project.dependencies must be a string list")
    dependencies = cast(list[object], dependencies_value)
    if not all(isinstance(item, str) for item in dependencies):
        raise ValueError("project.dependencies must be a string list")
    for raw in cast(list[str], dependencies):
        match = re.match(
            r"^\s*([A-Za-z0-9_.-]+)\s*((?:[<>=!~]=?\s*[0-9][^,\s;]*"
            r"(?:\s*,\s*[<>=!~]=?\s*[0-9][^,\s;]*)*)?)",
            raw,
        )
        if match:
            name = match.group(1).lower().replace("_", "-")
            specifier = match.group(2).replace(" ", "")
            floor_match = re.search(r"(?:>=|>|~=|==)\s*([0-9][^,\s;]*)", specifier)
            floor = floor_match.group(1) if floor_match else ""
            values[name] = ProjectDependency(specifier, floor)
    return values


def locked_versions(root: Path) -> dict[str, str]:
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        return {}
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages_value = data.get("package")
    if not isinstance(packages_value, list):
        raise ValueError("uv.lock has no package list")
    versions: dict[str, str] = {}
    packages = cast(list[Any], packages_value)
    for package_value in packages:
        if not isinstance(package_value, dict):
            raise ValueError("uv.lock package entry is malformed")
        package = cast(dict[str, Any], package_value)
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            versions[name.lower().replace("_", "-")] = version
    return versions


def load_deferrals(root: Path) -> list[dict[str, str]]:
    path = root / "scripts" / "dependency_update_deferrals.json"
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("dependency_update_deferrals.json has invalid shape")
    mapping = cast(dict[str, Any], value)
    deferrals_value = mapping.get("deferrals")
    if not isinstance(deferrals_value, list):
        raise ValueError("dependency_update_deferrals.json has invalid shape")
    result: list[dict[str, str]] = []
    required = {"target", "version", "reason", "recheck_by"}
    entries = cast(list[Any], deferrals_value)
    for raw_item in entries:
        if not isinstance(raw_item, dict):
            raise ValueError("dependency_update_deferrals.json entry has invalid shape")
        item = cast(dict[str, Any], raw_item)
        if set(item) != required:
            raise ValueError("dependency_update_deferrals.json entry has invalid shape")
        if not all(isinstance(item[key], str) for key in required):
            raise ValueError("dependency_update_deferrals.json entry has non-string value")
        try:
            date.fromisoformat(cast(str, item["recheck_by"]))
        except ValueError as exc:
            raise ValueError("dependency_update_deferrals.json has invalid recheck_by") from exc
        result.append(cast(dict[str, str], item))
    return result


def _deferred_version(version: str, prefix: str) -> bool:
    if prefix.endswith(".x"):
        return version.startswith(prefix[:-2] + ".")
    return version.startswith(prefix)


def pypi_status(
    name: str,
    dependency: ProjectDependency,
    current: str,
    payload: dict[str, Any],
    deferrals: list[dict[str, str]],
    *,
    today: date | None = None,
) -> Status:
    info = payload.get("info")
    if not isinstance(info, dict):
        raise ValueError(f"PyPI response is malformed for {name}")
    info_mapping = cast(dict[str, Any], info)
    if not isinstance(info_mapping.get("version"), str):
        raise ValueError(f"PyPI response is malformed for {name}")
    latest_external = cast(str, info_mapping["version"])
    try:
        specifier = SpecifierSet(dependency.specifier)
    except ValueError as exc:
        raise ValueError(f"invalid specifier for {name}: {dependency.specifier}") from exc
    releases = payload.get("releases")
    candidates: list[Version] = []
    if isinstance(releases, dict):
        release_map = cast(dict[str, Any], releases)
        for raw_version in release_map:
            try:
                parsed = Version(raw_version)
            except InvalidVersion:
                continue
            if parsed.is_prerelease and not specifier.prereleases:
                continue
            if parsed in specifier:
                candidates.append(parsed)
    if not candidates:
        try:
            parsed_latest = Version(latest_external)
        except InvalidVersion as exc:
            raise ValueError(f"PyPI response has no valid release for {name}") from exc
        if parsed_latest in specifier:
            candidates.append(parsed_latest)
    if not candidates:
        raise ValueError(f"PyPI response has no release within specifier for {name}")
    latest_in_range = str(max(candidates))
    note_parts: list[str] = []
    if latest_external != latest_in_range:
        note_parts.append(f"上限外: {latest_external}")
    outdated = Version(latest_in_range) > Version(current)
    decision = ""
    today_value = today or date.today()
    for deferral in deferrals:
        if deferral["target"].lower().replace("_", "-") != name:
            continue
        if not _deferred_version(latest_external, deferral["version"]):
            continue
        if today_value > date.fromisoformat(deferral["recheck_by"]):
            note_parts.append("保留期限切れ")
            outdated = True
        else:
            decision = "保留\uff08記録済み\uff09"
            if current == latest_in_range:
                outdated = False
            note_parts.append(f"保留理由: {deferral['reason']}")
    return Status(
        name,
        current,
        latest_in_range,
        f"pyproject: {dependency.specifier or '(任意)'}",
        outdated,
        "、".join(note_parts),
        decision,
    )


def _docker_args(root: Path) -> dict[str, str]:
    text = (root / "docker" / "circuit-tools.Dockerfile").read_text(encoding="utf-8")
    return dict(_ARG.findall(text))


def _statuses(root: Path, fetch: Fetch, fetch_json: FetchJson) -> list[Status]:
    args = _docker_args(root)
    ppa_url = (
        "https://ppa.launchpadcontent.net/kicad/kicad-dev-nightly/ubuntu/"
        "dists/resolute/main/binary-amd64/Packages.gz"
    )
    ppa = parse_kicad_packages(fetch(ppa_url))
    statuses = [
        Status(
            package,
            args.get(arg, ""),
            ppa[package],
            "KiCad PPA resolute",
            version_tuple(ppa[package]) > version_tuple(args.get(arg, "")),
        )
        for package, arg in (
            ("kicad-nightly", "KICAD_NIGHTLY_VERSION"),
            ("kicad-nightly-footprints", "KICAD_NIGHTLY_FOOTPRINTS_VERSION"),
            ("kicad-nightly-symbols", "KICAD_NIGHTLY_SYMBOLS_VERSION"),
        )
    ]
    release_value = fetch_json("https://api.github.com/repos/mixelpixx/Konnect/releases/latest")
    if not isinstance(release_value, dict):
        raise ValueError("Konnect release response is malformed")
    release = cast(dict[str, Any], release_value)
    if not isinstance(release.get("tag_name"), str):
        raise ValueError("Konnect release response is malformed")
    latest_konnect = str(release["tag_name"]).removeprefix("v")
    current_konnect = args.get("KONNECT_VERSION", "")
    statuses.append(
        Status(
            "Konnect",
            current_konnect,
            latest_konnect,
            "GitHub release",
            version_tuple(latest_konnect) > version_tuple(current_konnect),
        )
    )
    for package in ("poppler-utils", "librsvg2-bin"):
        statuses.append(
            Status(
                package,
                "unpinned",
                "(Ubuntu 26.04 archive)",
                "apt",
                False,
                "P4 rasterizer dep; version tracking deferred to the Ubuntu archive",
            )
        )
    head = subprocess.run(
        ["git", "-C", str(root / "libraries" / "cern-kicad-libs"), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    upstream = subprocess.run(
        ["git", "ls-remote", "https://gitlab.com/ohwr/cern-kicad-libs.git", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.split()[0]
    statuses.append(Status("CERN KiCad libraries", head, upstream, "GitLab HEAD", head != upstream))
    pins = _project_pins(root)
    locked = locked_versions(root)
    deferrals = load_deferrals(root)
    for name in ("openhands-sdk", "openhands-tools", "mcp", "pydantic"):
        dependency = pins.get(name)
        if dependency is None:
            raise ValueError(f"pyproject.toml has no pin for {name}")
        current = locked.get(name, dependency.floor)
        if not current:
            raise ValueError(f"dependency {name} has no lock version or specifier floor")
        payload_value = fetch_json(f"https://pypi.org/pypi/{name}/json")
        if not isinstance(payload_value, dict):
            raise ValueError(f"PyPI response is malformed for {name}")
        statuses.append(
            pypi_status(
                name,
                dependency,
                current,
                cast(dict[str, Any], payload_value),
                deferrals,
            )
        )
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    for workflow in workflows:
        for match in _ACTION.finditer(workflow.read_text(encoding="utf-8")):
            repo, current, tag = match.groups()
            if tag is None:
                continue
            ref_value = fetch_json(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}")
            if not isinstance(ref_value, dict):
                raise ValueError(f"GitHub action tag response is malformed: {repo}@{tag}")
            ref = cast(dict[str, Any], ref_value)
            obj = ref.get("object")
            latest = cast(dict[str, Any], obj).get("sha") if isinstance(obj, dict) else None
            if not isinstance(latest, str):
                raise ValueError(f"GitHub action tag response is malformed: {repo}@{tag}")
            statuses.append(
                Status(
                    f"Action {repo}",
                    current,
                    latest,
                    f"{workflow.name}:{tag}",
                    current != latest,
                )
            )
    statuses.append(
        Status(
            "Ubuntu base image",
            "ubuntu:26.04",
            "未取得 (digestはpublish時に確認)",
            "Docker Hub",
            False,
            "digest check is intentionally deferred to image publish",
        )
    )
    return statuses


def report(
    root: Path, *, fetch: Fetch = _default_fetch, fetch_json: FetchJson = _default_json
) -> dict[str, Any]:
    statuses = _statuses(root, fetch, fetch_json)
    return {
        "outdated_count": sum(item.outdated for item in statuses),
        "statuses": [asdict(item) for item in statuses],
    }


def markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 依存アップデート確認レポート",
        "",
        "| 対象 | 現在 | 最新 | 判定 | 出所 |",
        "|---|---|---|---|---|",
    ]
    for item in payload["statuses"]:
        state = item["decision"] or ("更新あり" if item["outdated"] else "一致/保留")
        lines.append(
            f"| {item['name']} | `{item['current']}` | `{item['latest']}` | "
            f"{state} | {item['source']} |"
        )
        if item["note"]:
            lines.append(f"| 注記 | {item['note']} |  |  |  |")
    lines.extend(
        [
            "",
            "KiCad nightly更新時は、`docs/operations.md`に記録された"
            "`_cvpcb.kiface` ERC障害を再テストすること。",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = report(Path(__file__).resolve().parents[1])
        text = markdown(payload)
        if args.markdown:
            args.markdown.write_text(text, encoding="utf-8")
        if args.json:
            args.json.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.dry_run or not args.markdown:
            print(text, end="")
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError, IndexError) as exc:
        print(f"FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
