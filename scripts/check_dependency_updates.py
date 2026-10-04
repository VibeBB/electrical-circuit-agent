#!/usr/bin/env python3
"""Report dependency updates without modifying repository source files.

Surfaces checked: KiCad nightly package pins from the kicad-dev-nightly PPA
(resolute Packages.gz index), Dockerfile ARG pins checked against GitHub
releases (Konnect, FreeRouting, Semeru JRE), GitLab commit pins (KiCad Library
Utils), unpinned apt packages, the CERN KiCad libraries submodule, direct PyPI
dependencies (compared against resolved versions in uv.lock), plus uv.lock
transitive drift via `uv lock --upgrade --dry-run`, the uv required-version pin, Python minor
pins against the latest stable CPython minor, GitHub Actions `uses:` pins
(including subpath actions like github/codeql-action/upload-sarif), direct
download pins inside workflows (the actionlint tarball and zizmor wheel in
workflow-lint.yml, and the Trivy binary `version:` inputs on aquasecurity
actions), `git clone --branch` pins inside workflows (e.g. the pinned Lynis
checkout in container-audit.yml), the upstream SDK build.py cache-tag layout
the publish workflow patches, and the Docker base image tags.

Renders a markdown report (and optionally JSON). Deferrals live in
scripts/dependency_update_deferrals.json; see docs/operations.md for the
update procedure.
"""

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

ROOT = Path(__file__).resolve().parents[1]

SUBPROCESS_TIMEOUT = 300

Fetch = Callable[[str], bytes]
FetchJson = Callable[[str], Any]
RunUv = Callable[[list[str], Path], str]
ListRemoteTags = Callable[[str], list[str]]
ListRemoteTagCommits = Callable[[str], dict[str, str]]

# ---------------------------------------------------------------------------
# Repo-specific targets: the only block that differs between sibling repos.
# ---------------------------------------------------------------------------

_DOCKERFILES = ["circuit-tools.Dockerfile"]

KICAD_PPA_URL = (
    "https://ppa.launchpadcontent.net/kicad/kicad-dev-nightly/ubuntu/"
    "dists/resolute/main/binary-amd64/Packages.gz"
)
KICAD_PPA_SOURCE = "KiCad PPA resolute"
KICAD_PACKAGES = (
    ("kicad-nightly", "KICAD_NIGHTLY_VERSION"),
    ("kicad-nightly-footprints", "KICAD_NIGHTLY_FOOTPRINTS_VERSION"),
    ("kicad-nightly-symbols", "KICAD_NIGHTLY_SYMBOLS_VERSION"),
)

# (status name, Dockerfile ARG, github repo, release tag prefix)
_DOCKER_ARG_UPSTREAMS = (
    ("Konnect", "KONNECT_VERSION", "mixelpixx/Konnect", "v"),
    ("FreeRouting", "FREEROUTING_VERSION", "freerouting/freerouting", "v"),
    (
        "Semeru JRE (OpenJ9)",
        "SEMERU_JRE_VERSION",
        "ibmruntimes/semeru{major}-binaries",
        "jdk-",
    ),
)

APT_PACKAGES = ("poppler-utils", "librsvg2-bin")
APT_PACKAGE_NOTE = "P4 rasterizer dep; version tracking deferred to the Ubuntu archive"

PYPI_DIRECT = ("openhands-sdk", "openhands-tools", "mcp", "pydantic")

# (status name, path inside the repo, upstream remote URL)
SUBMODULES = (
    (
        "CERN KiCad libraries",
        "libraries/cern-kicad-libs",
        "https://gitlab.com/ohwr/cern-kicad-libs.git",
    ),
)

GIT_COMMIT_UPSTREAMS = (
    (
        "KiCad Library Utils",
        "KICAD_LIBRARY_UTILS_COMMIT",
        "https://gitlab.com/kicad/libraries/kicad-library-utils.git",
    ),
)

REPORT_FOOTER = (
    "When the KiCad nightly package updates, re-test the "
    "`_cvpcb.kiface` ERC failure recorded in `docs/operations.md`."
)

USER_AGENT = "circuit-agent-dependency-check"

# ---------------------------------------------------------------------------

_ACTION = re.compile(
    r"uses:\s*([\w.-]+/[\w.-]+(?:/[\w./-]+)?)@([0-9a-f]{40})(?:\s*#\s*(v[\w.-]+))?"
)
_ACTIONLINT_TARBALL = re.compile(r"actionlint_(\d+\.\d+\.\d+)_linux_amd64\.tar\.gz")
_ZIZMOR_WHEEL = re.compile(r"zizmor-(\d+\.\d+\.\d+)-py3-none")
_TRIVY_USES = re.compile(r"uses:\s*aquasecurity/(?:trivy-action|setup-trivy)@")
_TRIVY_VERSION = re.compile(r"version:\s*['\"]?(v\d+\.\d+\.\d+)")
_STEP_BOUNDARY = re.compile(r"\n {0,8}- ")
_SDK_BUILD_PY_URL = (
    "https://raw.githubusercontent.com/OpenHands/software-agent-sdk/"
    "v{version}/openhands-agent-server/openhands/agent_server/docker/build.py"
)
SDK_CACHE_NEEDLE = "buildcache-{self.target}-{self.base_image_slug}{self.flavor_suffix}"
_ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)=(\S+)\s*$", re.MULTILINE)
_FROM = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
_PYTHON_TAG = re.compile(r"v(\d+)\.(\d+)\.\d+")
_DOCKERHUB_TAGS = (
    "https://hub.docker.com/v2/repositories/library/{image}/tags"
    "?page_size=100&ordering=last_updated"
)
_LOCK_PATTERNS = (
    (re.compile(r"^Update (\S+) v(\S+) -> v(\S+)$"), "update"),
    (re.compile(r"^Add (\S+) v(\S+)$"), "add"),
    (re.compile(r"^Remove (\S+) v(\S+)$"), "remove"),
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
    fetch_failed: bool = False


@dataclass(frozen=True)
class ProjectDependency:
    specifier: str
    floor: str


def request(url: str) -> Request:
    headers = {"User-Agent": USER_AGENT}
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


def _default_run_uv(command: list[str], cwd: Path) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        timeout=SUBPROCESS_TIMEOUT,
    )
    return result.stdout


def _default_list_remote_tags(url: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", url],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT,
    )
    return [
        line.rsplit("\t", 1)[-1].removeprefix("refs/tags/").removesuffix("^{}")
        for line in result.stdout.splitlines()
    ]


def _default_list_remote_tag_commits(url: str) -> dict[str, str]:
    """Tag name -> commit SHA the tag points at.

    Annotated tags resolve through the peeled `^{}` line to the tagged
    commit; lightweight tags resolve to the object they name directly.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--tags", url],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT,
    )
    commits: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            commits[name[: -len("^{}")]] = sha
        else:
            commits.setdefault(name, sha)
    return commits


def normalize_name(name: str) -> str:
    return name.lower().replace("_", "-")


def _dict(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return cast(dict[str, Any], value)


def project_data(repo_root: Path) -> dict[str, Any]:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return _dict(data, "pyproject.toml is not an object")


def project_pins(repo_root: Path) -> dict[str, ProjectDependency]:
    data = project_data(repo_root)
    project = _dict(data.get("project"), "pyproject.toml has no project table")
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
            name = normalize_name(match.group(1))
            specifier = match.group(2).replace(" ", "")
            floor_match = re.search(r"(?:>=|>|~=|==)\s*([0-9][^,\s;]*)", specifier)
            floor = floor_match.group(1) if floor_match else ""
            values[name] = ProjectDependency(specifier, floor)
    return values


def lock_versions(repo_root: Path) -> dict[str, str]:
    lock_path = repo_root / "uv.lock"
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
            versions[normalize_name(name)] = version
    return versions


def _pypi_latest(name: str, fetch_json: FetchJson) -> str:
    payload = fetch_json(f"https://pypi.org/pypi/{name}/json")
    if not isinstance(payload, dict):
        raise ValueError(f"PyPI response is malformed for {name}")
    info = cast(dict[str, Any], payload).get("info")
    version = cast(dict[str, Any], info).get("version") if isinstance(info, dict) else None
    if not isinstance(version, str):
        raise ValueError(f"PyPI response is malformed for {name}")
    return version


def version_tuple(value: str) -> tuple[int, ...]:
    values = re.findall(r"\d+", value)
    if not values:
        raise ValueError(f"version has no numeric components: {value}")
    return tuple(int(item) for item in values)


def release_version(tag_name: str, prefix: str) -> str:
    if not tag_name.startswith(prefix):
        raise ValueError(f"release tag does not start with {prefix!r}: {tag_name}")
    return tag_name.removeprefix(prefix)


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
        if normalize_name(deferral["target"]) != name:
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


def check_pypi(
    repo_root: Path,
    deferrals: list[dict[str, str]],
    *,
    fetch_json: FetchJson = _default_json,
) -> list[Status]:
    pins = project_pins(repo_root)
    locked = lock_versions(repo_root)
    statuses: list[Status] = []
    for name in PYPI_DIRECT:
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
    return statuses


def check_pypi_lock(
    repo_root: Path,
    direct_names: set[str],
    *,
    run_uv: RunUv = _default_run_uv,
) -> list[Status]:
    """Transitive drift per `uv lock --upgrade --dry-run` (Update/Add/Remove lines)."""
    output = run_uv(["uv", "lock", "--upgrade", "--dry-run"], repo_root)
    statuses: list[Status] = []
    for line in output.splitlines():
        for pattern, kind in _LOCK_PATTERNS:
            match = pattern.fullmatch(line.strip())
            if match is None:
                continue
            groups = match.groups()
            name = normalize_name(groups[0])
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


def uv_version_pin(repo_root: Path) -> str:
    data = project_data(repo_root)
    tool = data.get("tool")
    uv = cast(dict[str, Any], tool).get("uv") if isinstance(tool, dict) else None
    if not isinstance(uv, dict):
        return ""
    value = cast(dict[str, Any], uv).get("required-version")
    return value.removeprefix("==") if isinstance(value, str) else ""


def check_uv_pin(repo_root: Path, *, fetch_json: FetchJson = _default_json) -> list[Status]:
    current = uv_version_pin(repo_root)
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
            fetch_failed=latest == "?",
        )
    ]


def workflow_files(repo_root: Path) -> list[Path]:
    return sorted((repo_root / ".github" / "workflows").glob("*.yml"))


def _github_latest_tag(repo: str, list_remote_tags: ListRemoteTags) -> str:
    """Highest semver git tag of a GitHub repo ("" on fetch failure)."""
    try:
        tags = list_remote_tags(f"https://github.com/{repo}")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    versioned = sorted(
        (t for t in tags if re.fullmatch(r"v?\d+\.\d+\.\d+", t)),
        key=lambda t: tuple(int(p) for p in t.removeprefix("v").split(".")),
    )
    return versioned[-1] if versioned else ""


def check_github_actions(
    repo_root: Path,
    *,
    list_remote_tag_commits: ListRemoteTagCommits = _default_list_remote_tag_commits,
) -> list[Status]:
    """`uses: <repo>@<sha> # <tag>` pins.

    The pinned SHA is compared against the dereferenced commit the commented
    tag points at: annotated tags peel to a commit via `^{}`, so a tag-object
    SHA never produces a false outdated result.
    """
    statuses: list[Status] = []
    tag_cache: dict[str, dict[str, str] | None] = {}
    for workflow in workflow_files(repo_root):
        for match in _ACTION.finditer(workflow.read_text(encoding="utf-8")):
            action, current, tag = match.groups()
            if tag is None:
                continue
            # Subpath actions (e.g. github/codeql-action/upload-sarif) share
            # the owning repository's tags, so look up the first two segments.
            repo = "/".join(action.split("/")[:2])
            if repo not in tag_cache:
                try:
                    tag_cache[repo] = list_remote_tag_commits(f"https://github.com/{repo}")
                except (
                    OSError,
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                ):
                    tag_cache[repo] = None
            commits = tag_cache[repo]
            latest = commits.get(tag) if commits else None
            statuses.append(
                Status(
                    f"Action {action}",
                    current,
                    latest or "?",
                    f"{workflow.name}:{tag}",
                    bool(latest) and current != latest,
                    "" if latest else "fetch failed",
                    fetch_failed=latest is None,
                )
            )
    return statuses


_GIT_CLONE = re.compile(
    r"git\s+clone[\s\S]{0,200}?--branch\s+(\S+)\s*(?:\\\s*\n\s*)?"
    r"\s*(https://github\.com/([\w.-]+/[\w.-]+))"
)


def check_git_clones(
    repo_root: Path, *, list_remote_tags: ListRemoteTags = _default_list_remote_tags
) -> list[Status]:
    """`git clone --branch <ref> <github-url>` pins inside workflows
    (e.g. the pinned Lynis checkout in container-audit.yml)."""
    statuses: list[Status] = []
    seen: set[tuple[str, str]] = set()
    for workflow in workflow_files(repo_root):
        for raw_ref, _url, repo in _GIT_CLONE.findall(workflow.read_text(encoding="utf-8")):
            ref = raw_ref.strip("'\"")
            # A ref resolved from a shell variable (e.g. "v${SDK_VERSION}")
            # is not a literal pin.
            if "$" in ref or "{" in ref or "}" in ref:
                continue
            if (repo, ref) in seen:
                continue
            seen.add((repo, ref))
            latest = _github_latest_tag(repo, list_remote_tags)
            outdated = bool(latest) and latest != ref
            statuses.append(
                Status(
                    repo,
                    ref,
                    latest or "?",
                    f"git clone ({workflow.name})",
                    outdated,
                    "" if latest else "fetch failed",
                    fetch_failed=not latest,
                )
            )
    return statuses


def _github_release_tag(repo: str, fetch_json: FetchJson) -> str:
    """Latest release tag of a GitHub repo ("" on fetch failure)."""
    try:
        release = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except (OSError, ValueError):
        return ""
    if not isinstance(release, dict):
        return ""
    tag = cast(dict[str, Any], release).get("tag_name")
    return tag if isinstance(tag, str) else ""


def check_workflow_downloads(
    repo_root: Path, *, fetch_json: FetchJson = _default_json
) -> list[Status]:
    """Direct-download pins inside workflows that no `uses:` line tracks.

    Covers the actionlint tarball and the zizmor wheel fetched by
    workflow-lint.yml plus the Trivy binary `version:` inputs on
    aquasecurity/trivy-action and aquasecurity/setup-trivy steps; a trivy
    step without an explicit `version:` is reported as implicit so the
    drift risk stays visible.
    """
    findings: set[tuple[str, str, str]] = set()
    unpinned_trivy: set[str] = set()
    for workflow in workflow_files(repo_root):
        text = workflow.read_text(encoding="utf-8")
        for version in set(_ACTIONLINT_TARBALL.findall(text)):
            findings.add(("Actionlint", version, workflow.name))
        for version in set(_ZIZMOR_WHEEL.findall(text)):
            findings.add(("Zizmor", version, workflow.name))
        for uses in _TRIVY_USES.finditer(text):
            segment = text[uses.end() : uses.end() + 1500]
            boundary = _STEP_BOUNDARY.search(segment)
            if boundary is not None:
                segment = segment[: boundary.start()]
            version_match = _TRIVY_VERSION.search(segment)
            if version_match is None:
                unpinned_trivy.add(workflow.name)
            else:
                findings.add(
                    ("Trivy binary", version_match.group(1).removeprefix("v"), workflow.name)
                )
    latest_cache: dict[str, str] = {}

    def latest(name: str) -> str:
        if name in latest_cache:
            return latest_cache[name]
        if name == "Actionlint":
            value = _github_release_tag("rhysd/actionlint", fetch_json).removeprefix("v")
        elif name == "Zizmor":
            try:
                value = _pypi_latest("zizmor", fetch_json)
            except (OSError, ValueError):
                value = ""
        else:
            value = _github_release_tag("aquasecurity/trivy", fetch_json).removeprefix("v")
        latest_cache[name] = value
        return value

    statuses: list[Status] = []
    for name, current, workflow_name in sorted(findings):
        remote = latest(name)
        statuses.append(
            Status(
                f"{name} ({workflow_name})",
                current,
                remote or "?",
                f"{workflow_name} direct pin",
                bool(remote) and version_tuple(remote) > version_tuple(current),
                "" if remote else "fetch failed",
                fetch_failed=not remote,
            )
        )
    for workflow_name in sorted(unpinned_trivy):
        statuses.append(
            Status(
                f"Trivy binary ({workflow_name})",
                "implicit",
                "?",
                f"{workflow_name} trivy-action",
                False,
                "no explicit version pin; the binary version is implicit in the pinned action",
            )
        )
    return statuses


def check_sdk_build_layout(repo_root: Path, *, fetch: Fetch = _default_fetch) -> list[Status]:
    """Whether the pinned SDK's agent-server build.py still carries the
    cache_tags expression publish-circuit-images.yml patches. Layout drift
    there silently costs the stable registry cache tag on every server
    build, so it is tracked as an update-class finding."""
    sdk = lock_versions(repo_root).get("openhands-sdk")
    if not sdk:
        return []
    try:
        body = fetch(_SDK_BUILD_PY_URL.format(version=sdk)).decode("utf-8")
    except (OSError, UnicodeError):
        return [
            Status(
                "SDK build.py cache-tag layout",
                f"v{sdk}",
                "?",
                "openhands-agent-server build.py",
                False,
                "fetch failed",
                fetch_failed=True,
            )
        ]
    intact = SDK_CACHE_NEEDLE in body
    return [
        Status(
            "SDK build.py cache-tag layout",
            f"v{sdk}",
            "intact" if intact else "changed",
            "openhands-agent-server build.py",
            not intact,
            "" if intact else "upstream layout changed; server builds lose the stable cache tag",
        )
    ]


def docker_arg_pins(repo_root: Path) -> dict[str, str]:
    """ARG name -> default value across docker/*.Dockerfile."""
    values: dict[str, str] = {}
    for name in _DOCKERFILES:
        path = repo_root / "docker" / name
        if not path.is_file():
            continue
        values.update(dict(_ARG.findall(path.read_text(encoding="utf-8"))))
    return values


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


def check_docker_base(repo_root: Path, *, fetch_json: FetchJson = _default_json) -> list[Status]:
    statuses: list[Status] = []
    for name in _DOCKERFILES:
        dockerfile = repo_root / "docker" / name
        if not dockerfile.is_file():
            continue
        for reference in _FROM.findall(dockerfile.read_text(encoding="utf-8")):
            reference = reference.split("@", 1)[0]
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
                        fetch_failed=latest is None,
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
                        fetch_failed=latest_uv == "?",
                    )
                )
            else:
                statuses.append(
                    Status(
                        f"Docker base {image}",
                        tag,
                        "?",
                        name,
                        False,
                        "unhandled image",
                    )
                )
    return statuses


def check_python_versions(
    repo_root: Path,
    *,
    list_remote_tags: ListRemoteTags = _default_list_remote_tags,
) -> list[Status]:
    """Compare the repo's Python minor pins against the latest stable CPython minor."""
    values: list[tuple[str, str]] = []
    data = project_data(repo_root)
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
    for name in _DOCKERFILES:
        dockerfile = repo_root / "docker" / name
        if not dockerfile.is_file():
            continue
        text = dockerfile.read_text(encoding="utf-8")
        for arg, arg_value in _ARG.findall(text):
            if arg == "PYTHON_VERSION":
                values.append((arg_value, name))
        for minor in re.findall(r"uv\s+python\s+install\s+(\d+\.\d+)", text):
            values.append((minor, name))
        for minor in re.findall(r"uv\s+venv\s+--python\s+(\d+\.\d+)", text):
            values.append((minor, name))
        for minor in re.findall(r"python3\.(\d+)", text):
            values.append((f"3.{minor}", name))
    dotfile = repo_root / ".python-version"
    if dotfile.is_file():
        match = re.search(r"(\d+\.\d+)", dotfile.read_text(encoding="utf-8"))
        if match is not None:
            values.append((match.group(1), ".python-version"))
    for workflow in workflow_files(repo_root):
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


def load_deferrals(repo_root: Path) -> list[dict[str, str]]:
    path = repo_root / "scripts" / "dependency_update_deferrals.json"
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
            target = normalize_name(deferral["target"])
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


# ---------------------------------------------------------------------------
# Repo-specific probes.
# ---------------------------------------------------------------------------


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
    for package_name, _arg_name in KICAD_PACKAGES:
        values = versions.get(package_name)
        if not values:
            raise ValueError(f"PPA metadata has no {package_name}")
        result[package_name] = max(values)
    return result


def check_kicad_ppa(repo_root: Path, *, fetch: Fetch = _default_fetch) -> list[Status]:
    args = docker_arg_pins(repo_root)
    ppa = parse_kicad_packages(fetch(KICAD_PPA_URL))
    return [
        Status(
            package,
            args.get(arg, ""),
            ppa[package],
            KICAD_PPA_SOURCE,
            version_tuple(ppa[package]) > version_tuple(args.get(arg, "")),
        )
        for package, arg in KICAD_PACKAGES
    ]


def check_docker_args(repo_root: Path, *, fetch_json: FetchJson = _default_json) -> list[Status]:
    args = docker_arg_pins(repo_root)
    semeru_major = args.get("SEMERU_JRE_VERSION", "0").split(".")[0]
    statuses: list[Status] = []
    for name, arg_name, repo, prefix in _DOCKER_ARG_UPSTREAMS:
        release_value = fetch_json(
            f"https://api.github.com/repos/{repo.format(major=semeru_major)}/releases/latest"
        )
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
    return statuses


def check_apt_packages() -> list[Status]:
    return [
        Status(
            package,
            "unpinned",
            "(Ubuntu 26.04 archive)",
            "apt",
            False,
            APT_PACKAGE_NOTE,
        )
        for package in APT_PACKAGES
    ]


def check_submodules(repo_root: Path) -> list[Status]:
    statuses: list[Status] = []
    for name, path, url in SUBMODULES:
        head = subprocess.run(
            ["git", "-C", str(repo_root / path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=SUBPROCESS_TIMEOUT,
        ).stdout.strip()
        upstream = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=SUBPROCESS_TIMEOUT,
        ).stdout.split()[0]
        statuses.append(Status(name, head, upstream, "GitLab HEAD", head != upstream))
    return statuses


def check_git_commit_pins(repo_root: Path) -> list[Status]:
    args = docker_arg_pins(repo_root)
    statuses: list[Status] = []
    for name, arg_name, url in GIT_COMMIT_UPSTREAMS:
        latest = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=SUBPROCESS_TIMEOUT,
        ).stdout.split()[0]
        current = args.get(arg_name, "")
        statuses.append(Status(name, current, latest, "GitLab HEAD", current != latest))
    return statuses


def check_dependency_updates(
    repo_root: Path,
    *,
    fetch: Fetch = _default_fetch,
    fetch_json: FetchJson = _default_json,
    run_uv: RunUv = _default_run_uv,
    list_remote_tags: ListRemoteTags = _default_list_remote_tags,
    list_remote_tag_commits: ListRemoteTagCommits = _default_list_remote_tag_commits,
) -> list[Status]:
    deferrals = load_deferrals(repo_root)
    tag_cache: dict[str, list[str]] = {}

    def cached_tags(url: str) -> list[str]:
        if url not in tag_cache:
            tag_cache[url] = list_remote_tags(url)
        return tag_cache[url]

    statuses = [
        *check_kicad_ppa(repo_root, fetch=fetch),
        *check_docker_args(repo_root, fetch_json=fetch_json),
        *check_git_commit_pins(repo_root),
        *check_apt_packages(),
        *check_submodules(repo_root),
        *check_pypi(repo_root, deferrals, fetch_json=fetch_json),
        *check_pypi_lock(repo_root, set(project_pins(repo_root)), run_uv=run_uv),
        *check_uv_pin(repo_root, fetch_json=fetch_json),
        *check_python_versions(repo_root, list_remote_tags=cached_tags),
        *check_github_actions(repo_root, list_remote_tag_commits=list_remote_tag_commits),
        *check_git_clones(repo_root, list_remote_tags=cached_tags),
        *check_workflow_downloads(repo_root, fetch_json=fetch_json),
        *check_sdk_build_layout(repo_root, fetch=fetch),
        *check_docker_base(repo_root, fetch_json=fetch_json),
    ]
    return apply_deferrals(statuses, deferrals)


def report(
    repo_root: Path,
    *,
    fetch: Fetch = _default_fetch,
    fetch_json: FetchJson = _default_json,
    run_uv: RunUv = _default_run_uv,
    list_remote_tags: ListRemoteTags = _default_list_remote_tags,
    list_remote_tag_commits: ListRemoteTagCommits = _default_list_remote_tag_commits,
) -> dict[str, Any]:
    statuses = check_dependency_updates(
        repo_root,
        fetch=fetch,
        fetch_json=fetch_json,
        run_uv=run_uv,
        list_remote_tags=list_remote_tags,
        list_remote_tag_commits=list_remote_tag_commits,
    )
    return {
        "outdated_count": sum(item.outdated for item in statuses),
        "unknown_count": sum(item.fetch_failed for item in statuses),
        "statuses": [asdict(item) for item in statuses],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dependency update check report",
        "",
        "| dependency | current | latest | state | reference |",
        "|---|---|---|---|---|",
    ]
    for item in payload["statuses"]:
        state = item["decision"] or (
            "unknown"
            if item["fetch_failed"]
            else "update available"
            if item["outdated"]
            else "up to date"
        )
        lines.append(
            f"| {item['name']} | `{item['current']}` | `{item['latest']}` | "
            f"{state} | {item['source']} |"
        )
        if item["note"]:
            lines.append(f"| note | {item['note']} |  |  |  |")
    lines.extend(["", REPORT_FOOTER])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = report(args.repo_root)
        text = render_markdown(payload)
        if args.markdown:
            args.markdown.write_text(text, encoding="utf-8")
        if args.json:
            args.json.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.dry_run or not args.markdown:
            print(text, end="")
    except (
        OSError,
        UnicodeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        IndexError,
    ) as exc:
        print(f"FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
