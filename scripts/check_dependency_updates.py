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
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

Fetch = Callable[[str], bytes]
FetchJson = Callable[[str], Any]
RunUv = Callable[[list[str], Path], str]
ListRemoteTags = Callable[[str], list[str]]

_ACTION = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@([0-9a-f]{40})(?:\s*#\s*(v[\w.-]+))?")
_ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)=(\S+)\s*$", re.MULTILINE)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FROM = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
_PYTHON_TAG = re.compile(r"v(\d+)\.(\d+)\.\d+")
_DOCKERHUB_TAGS = (
    "https://hub.docker.com/v2/repositories/library/{image}/tags"
    "?page_size=100&ordering=last_updated"
)


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


def release_version(tag_name: str, prefix: str) -> str:
    if not tag_name.startswith(prefix):
        raise ValueError(f"release tag does not start with {prefix!r}: {tag_name}")
    return tag_name.removeprefix(prefix)


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


def _default_run_uv(command: list[str], cwd: Path) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
    )
    return result.stdout


def _default_list_remote_tags(url: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", url],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [
        line.rsplit("\t", 1)[-1].removeprefix("refs/tags/").removesuffix("^{}")
        for line in result.stdout.splitlines()
    ]


def _pypi_latest(name: str, fetch_json: FetchJson) -> str:
    payload = fetch_json(f"https://pypi.org/pypi/{name}/json")
    if not isinstance(payload, dict):
        raise ValueError(f"PyPI response is malformed for {name}")
    info = cast(dict[str, Any], payload).get("info")
    version = cast(dict[str, Any], info).get("version") if isinstance(info, dict) else None
    if not isinstance(version, str):
        raise ValueError(f"PyPI response is malformed for {name}")
    return version


def uv_version_pin(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    tool = data.get("tool")
    uv = cast(dict[str, Any], tool).get("uv") if isinstance(tool, dict) else None
    if not isinstance(uv, dict):
        return ""
    value = cast(dict[str, Any], uv).get("required-version")
    return value.removeprefix("==") if isinstance(value, str) else ""


def check_uv_pin(root: Path, *, fetch_json: FetchJson = _default_json) -> list[Status]:
    current = uv_version_pin(root)
    try:
        latest = _pypi_latest("uv", fetch_json)
    except (ValueError, OSError):
        latest = "?"
    outdated = latest != "?" and bool(current) and latest != current
    return [
        Status(
            "uv",
            current or "-",
            latest,
            "pyproject.toml [tool.uv] required-version",
            outdated,
            "" if latest != "?" else "fetch failed",
        )
    ]


_LOCK_PATTERNS = (
    (re.compile(r"^Update (\S+) v(\S+) -> v(\S+)$"), "update"),
    (re.compile(r"^Add (\S+) v(\S+)$"), "add"),
    (re.compile(r"^Remove (\S+) v(\S+)$"), "remove"),
)


def check_pypi_lock(
    root: Path,
    direct_names: set[str],
    *,
    run_uv: RunUv = _default_run_uv,
) -> list[Status]:
    """Transitive drift per `uv lock --upgrade --dry-run` (Update/Add/Remove lines)."""
    output = run_uv(["uv", "lock", "--upgrade", "--dry-run"], root)
    statuses: list[Status] = []
    for line in output.splitlines():
        for pattern, kind in _LOCK_PATTERNS:
            match = pattern.fullmatch(line.strip())
            if match is None:
                continue
            groups = match.groups()
            name = groups[0].lower().replace("_", "-")
            if name in direct_names:
                break
            if kind == "update":
                current, latest, note = groups[1], groups[2], ""
            elif kind == "add":
                current, latest, note = "-", groups[1], "would be added"
            else:
                current, latest, note = groups[1], "-", "would be removed"
            statuses.append(Status(f"{name} (transitive)", current, latest, "uv.lock", True, note))
            break
    return statuses


def check_python_versions(
    root: Path,
    *,
    list_remote_tags: ListRemoteTags = _default_list_remote_tags,
) -> list[Status]:
    """Compare the repo's Python minor pins against the latest stable CPython minor."""
    values: list[tuple[str, str]] = []
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project")
    requires_python = (
        cast(dict[str, Any], project).get("requires-python") if isinstance(project, dict) else None
    )
    if not isinstance(requires_python, str):
        raise ValueError("pyproject.toml has no requires-python")
    requires_match = re.search(r"(\d+)\.(\d+)", requires_python)
    if requires_match is None:
        raise ValueError(f"invalid requires-python: {requires_python}")
    values.append((f"{requires_match.group(1)}.{requires_match.group(2)}", "pyproject.toml"))
    dockerfile = root / "docker" / "circuit-tools.Dockerfile"
    if dockerfile.is_file():
        text = dockerfile.read_text(encoding="utf-8")
        for arg, arg_value in _ARG.findall(text):
            if arg == "PYTHON_VERSION":
                values.append((arg_value, "circuit-tools.Dockerfile"))
        for minor in re.findall(r"uv\s+python\s+install\s+(\d+\.\d+)", text):
            values.append((minor, "circuit-tools.Dockerfile"))
    for workflow in sorted((root / ".github" / "workflows").glob("*.yml")):
        for minor in re.findall(
            r'python-version:\s*"?(\d+\.\d+)"?',
            workflow.read_text(encoding="utf-8"),
        ):
            values.append((minor, workflow.name))
    tags = list_remote_tags("https://github.com/python/cpython")
    stable_minors = sorted(
        {
            (int(match.group(1)), int(match.group(2)))
            for tag in tags
            if (match := _PYTHON_TAG.fullmatch(tag))
        }
    )
    if not stable_minors:
        raise ValueError("no stable CPython minor series found")
    latest = ".".join(str(part) for part in stable_minors[-1])
    statuses: list[Status] = []
    seen: set[tuple[str, str]] = set()
    for value, source in values:
        key = (value, source)
        if key in seen:
            continue
        seen.add(key)
        value_match = re.search(r"(\d+)\.(\d+)", value)
        if value_match is None:
            raise ValueError(f"unparseable python version {value!r} in {source}")
        minor = (int(value_match.group(1)), int(value_match.group(2)))
        statuses.append(
            Status(
                f"Python minor ({source})",
                value,
                latest,
                source,
                minor < stable_minors[-1],
            )
        )
    return statuses


def _ubuntu_lts_tags(fetch_json: FetchJson) -> list[str]:
    url: str | None = _DOCKERHUB_TAGS.format(image="ubuntu")
    tags: list[str] = []
    for _page in range(10):
        if url is None:
            break
        try:
            data = fetch_json(url)
        except (ValueError, OSError):
            return []
        results = cast(dict[str, Any], data).get("results") if isinstance(data, dict) else None
        items = cast(list[Any], results) if isinstance(results, list) else []
        for item in items:
            name = cast(dict[str, Any], item).get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and re.fullmatch(r"\d{2}\.\d{2}", name):
                tags.append(name)
        next_url = cast(dict[str, Any], data).get("next") if isinstance(data, dict) else None
        url = next_url if isinstance(next_url, str) and next_url else None
    return tags


def check_docker_base(root: Path, *, fetch_json: FetchJson = _default_json) -> list[Status]:
    dockerfile = root / "docker" / "circuit-tools.Dockerfile"
    if not dockerfile.is_file():
        return []
    statuses: list[Status] = []
    for reference in _FROM.findall(dockerfile.read_text(encoding="utf-8")):
        if ":" not in reference or "$" in reference:
            continue
        image, _, tag = reference.rpartition(":")
        if image == "ubuntu":
            lts_tags = [t for t in _ubuntu_lts_tags(fetch_json) if t.endswith(".04")]
            latest = max(
                lts_tags,
                key=lambda t: tuple(int(part) for part in t.split(".")),
                default=None,
            )
            statuses.append(
                Status(
                    f"Docker base {image}",
                    tag,
                    latest or "?",
                    "Docker Hub",
                    latest is not None and latest != tag,
                    "" if latest else "fetch failed",
                )
            )
        elif image == "ghcr.io/astral-sh/uv":
            try:
                latest_uv = _pypi_latest("uv", fetch_json)
            except (ValueError, OSError):
                latest_uv = "?"
            statuses.append(
                Status(
                    "uv base image",
                    tag,
                    latest_uv,
                    "PyPI",
                    latest_uv != "?" and latest_uv != tag,
                    "" if latest_uv != "?" else "fetch failed",
                )
            )
        else:
            statuses.append(
                Status(
                    f"Docker base {image}",
                    tag,
                    "?",
                    "circuit-tools.Dockerfile",
                    False,
                    "unhandled image",
                )
            )
    return statuses


def _statuses(
    root: Path,
    fetch: Fetch,
    fetch_json: FetchJson,
    *,
    run_uv: RunUv = _default_run_uv,
    list_remote_tags: ListRemoteTags = _default_list_remote_tags,
) -> list[Status]:
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
    semeru_major = args.get("SEMERU_JRE_VERSION", "0").split(".")[0]
    for name, arg_name, repo, prefix in (
        ("FreeRouting", "FREEROUTING_VERSION", "freerouting/freerouting", "v"),
        (
            "Semeru JRE (OpenJ9)",
            "SEMERU_JRE_VERSION",
            f"ibmruntimes/semeru{semeru_major}-binaries",
            "jdk-",
        ),
    ):
        release_value = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
        if not isinstance(release_value, dict):
            raise ValueError(f"{name} release response is malformed")
        release = cast(dict[str, Any], release_value)
        if not isinstance(release.get("tag_name"), str):
            raise ValueError(f"{name} release response is malformed")
        latest = release_version(str(release["tag_name"]), prefix)
        current = args.get(arg_name, "")
        statuses.append(
            Status(
                name,
                current,
                latest,
                "GitHub release",
                version_tuple(latest) > version_tuple(current),
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
    statuses.extend(check_pypi_lock(root, set(pins), run_uv=run_uv))
    statuses.extend(check_uv_pin(root, fetch_json=fetch_json))
    statuses.extend(check_python_versions(root, list_remote_tags=list_remote_tags))
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
    statuses.extend(check_docker_base(root, fetch_json=fetch_json))
    return apply_deferrals(statuses, deferrals)


def apply_deferrals(
    statuses: list[Status],
    deferrals: list[dict[str, str]],
    *,
    today: date | None = None,
) -> list[Status]:
    """Apply deferral entries to non-PyPI statuses.

    `target` matches a status name exactly or as the prefix before ` (`,
    so `python minor` covers `Python minor (ci.yml)` rows.
    """
    today_value = today or date.today()
    applied: list[Status] = []
    for status in statuses:
        updated = status
        for deferral in deferrals:
            target = deferral["target"].lower().replace("_", "-")
            name = status.name.lower()
            if name != target and not name.startswith(f"{target} ("):
                continue
            if not _deferred_version(status.latest, deferral["version"]):
                continue
            notes = [status.note] if status.note else []
            if today_value > date.fromisoformat(deferral["recheck_by"]):
                notes.append("保留期限切れ")
                updated = replace(status, outdated=True, note="、".join(notes))
            else:
                notes.append(f"保留理由: {deferral['reason']}")
                updated = replace(
                    status,
                    outdated=False,
                    note="、".join(notes),
                    decision="保留\uff08記録済み\uff09",
                )
            break
        applied.append(updated)
    return applied


def report(
    root: Path,
    *,
    fetch: Fetch = _default_fetch,
    fetch_json: FetchJson = _default_json,
    run_uv: RunUv = _default_run_uv,
    list_remote_tags: ListRemoteTags = _default_list_remote_tags,
) -> dict[str, Any]:
    statuses = _statuses(
        root,
        fetch,
        fetch_json,
        run_uv=run_uv,
        list_remote_tags=list_remote_tags,
    )
    return {
        "outdated_count": sum(item.outdated for item in statuses),
        "statuses": [asdict(item) for item in statuses],
    }


def markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dependency update check report",
        "",
        "| dependency | current | latest | state | reference |",
        "|---|---|---|---|---|",
    ]
    for item in payload["statuses"]:
        state = item["decision"] or ("update available" if item["outdated"] else "up to date")
        lines.append(
            f"| {item['name']} | `{item['current']}` | `{item['latest']}` | "
            f"{state} | {item['source']} |"
        )
        if item["note"]:
            lines.append(f"| note | {item['note']} |  |  |  |")
    lines.extend(
        [
            "",
            "When the KiCad nightly package updates, re-test the "
            "`_cvpcb.kiface` ERC failure recorded in `docs/operations.md`.",
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
