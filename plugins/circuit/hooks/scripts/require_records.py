#!/usr/bin/env python3
"""Enforce the VibeBB Record Protocol at SessionStart and Stop.

SessionStart (argv ``session-start``) writes a per-session marker under
``<records_dir>/.sessions/``. Stop (argv ``stop``) refuses to let the
agent finish while this session still owes records:

* every vision tool event and every image the model viewed needs a
  ``vision_review`` with a long-form impression;
* every artifact matching ``artifact_globs`` that changed this session
  must be covered by a fresh ``stage_impression`` (its recorded sha256
  equals the current bytes, so the impression was written after the
  final regeneration);
* a session that changed artifacts must record at least one
  ``decision``;
* every log must keep an intact hash chain and every line must validate;
* every sister impression taken in with an import (``reads.jsonl``)
  must be answered in the ``upstream`` field of a later impression;
* images matching ``dual_review_globs`` need a blind second review and a
  reconcile record; a split needs one tiebreak, after which an
  unresolved split is reported as a warning (unknown), never re-asked;
* every bard song delivered to this plugin needs a song receipt.

SessionStart also prints a short recall of sister concerns aimed at this
plugin, unanswered imported impressions and songs waiting, so the agent
starts with them in context. Nothing here waits on another agent: only
records that already exist are checked, so two plugins reading each
other cannot deadlock.

Delegated agents use ``record-context`` instead: native identity is exposed
without recalling earlier review prose that could bias an independent review.

The refusal is bounded by ``max_stop_denials`` per session so a model
that cannot comply still terminates; the last verdict is written to
``<records_dir>/records-status.json`` either way. Hook errors never
block (exit 0 with a stderr note).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _records import (
    DEFAULT_MAX_STOP_DENIALS,
    IMAGE_OBSERVATIONS_FILE,
    LOG_FILES,
    VISION_EVENTS_FILE,
    acknowledged,
    changed_artifacts,
    covers,
    load_jsonl,
    load_policy,
    matches,
    parse_time,
    recall,
    received,
    review_primary,
    sha256_file,
    song_deliveries,
    tree_sha256,
    verify_log,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MAX_LISTED = 20
LEGACY_SESSION_WINDOW_SECONDS = 24 * 60 * 60


def _project_dir(payload: dict[str, Any]) -> Path:
    return Path(
        os.environ.get("OPENHANDS_PROJECT_DIR") or payload.get("working_dir") or "."
    ).resolve()


def _session_key(payload: dict[str, Any]) -> str:
    raw = str(payload.get("session_id") or "default")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:96] or "default"


def _in_session(record: dict[str, Any], session_id: str | None, since: float) -> bool:
    recorded_session = record.get("session_id")
    if recorded_session:
        return bool(session_id and recorded_session == session_id)
    # Old writers omit session identity. Keep a bounded compatibility window;
    # never use time to override an explicit other-session identity.
    if time.time() > since + LEGACY_SESSION_WINDOW_SECONDS:
        return False
    moment = parse_time(record.get("recorded_at"))
    return moment is not None and since <= moment <= time.time()


def _image_key(record: dict[str, Any], root: Path) -> tuple[str, str]:
    path = (root / str(record.get("image_path") or "")).resolve()
    return str(path), str(record.get("image_sha256") or "")


def _original_image(claim: Any, root: Path, *, current: bool = True) -> bool:
    """Validate producer scope against the current workspace revision."""
    if not isinstance(claim, dict):
        return False
    claim = cast(dict[str, Any], claim)
    if set(claim) != {"path", "sha256", "owner_plugin", "origin"}:
        return False
    if claim.get("origin") not in ("renderer", "liaison-response"):
        return False
    if claim.get("owner_plugin") not in (
        "ux",
        "bard",
        "circuit",
        "dashboard",
        "doc",
        "firmware",
        "fpga",
        "mech",
        "prodeng",
        "sim",
        "wire",
    ):
        return False
    return _intact_image(claim, root, current=current)


def _intact_image(claim: dict[str, Any], root: Path, *, current: bool = True) -> bool:
    path, digest = claim.get("path"), claim.get("sha256")
    if (
        not isinstance(path, str)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return False
    try:
        source = Path(path)
        return (
            source.is_absolute()
            and str(source.resolve()) == path
            and source.is_relative_to(root)
            and (not current or (source.is_file() and sha256_file(source) == digest))
        )
    except (OSError, ValueError):
        return False


def _producer_sources(
    observation: dict[str, Any], root: Path, *, current: bool = True
) -> list[dict[str, Any]] | None:
    """Unknown/corrupt ownership never suppresses a review obligation."""
    claim = observation.get("image_identity")
    if not isinstance(claim, dict) or observation.get("identity_status") != "bound":
        return None
    claim = cast(dict[str, Any], claim)
    if (
        claim.get("path") != observation.get("image_path")
        or claim.get("sha256") != observation.get("image_sha256")
        or not _intact_image(claim, root, current=current)
    ):
        return None
    if claim.get("origin") != "derived":
        return [claim] if _original_image(claim, root, current=current) else None
    raw_parents = claim.get("parents")
    if not isinstance(raw_parents, list):
        return None
    parents = cast(list[Any], raw_parents)
    if (
        set(claim)
        != {"path", "sha256", "owner_plugin", "origin", "operation", "parents"}
        or claim.get("owner_plugin") != "ux"
        or claim.get("operation") not in ("crop", "tile", "composite")
        or not 1 <= len(parents) <= 16
        or (claim.get("operation") != "composite" and len(parents) != 1)
        or any(
            not _original_image(p, root, current=current) or p["path"] == claim["path"]
            for p in parents
        )
        or len({p["path"] for p in parents}) != len(parents)
    ):
        return None
    return cast(list[dict[str, Any]], parents)


def _receipt_sources(
    observation: dict[str, Any], root: Path, *, current: bool = True
) -> list[dict[str, Any]] | None:
    content = {
        k: v for k, v in observation.items() if k not in {"event_id", "recorded_at"}
    }
    intact = (
        observation.get("delivery_evidence") == "inline_bytes"
        and observation.get("event_id")
        == hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return _producer_sources(observation, root, current=current) if intact else None


def _current_originals(
    observations: list[dict[str, Any]], root: Path
) -> dict[str, dict[str, Any]]:
    originals: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for observation in observations:
        for source in _receipt_sources(observation, root) or []:
            previous = originals.get(source["path"])
            if previous and previous["owner_plugin"] != source["owner_plugin"]:
                ambiguous.add(source["path"])
            originals[source["path"]] = source
    return {path: source for path, source in originals.items() if path not in ambiguous}


def _superseding_sources(
    observation: dict[str, Any], root: Path, originals: dict[str, dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Retire an old receipt only after its originals were re-delivered intact."""
    sources = _receipt_sources(observation, root, current=False)
    if not sources:
        return None
    replacements: list[dict[str, Any]] = []
    changed = False
    for source in sources:
        replacement = originals.get(source["path"])
        if replacement is None or replacement["owner_plugin"] != source["owner_plugin"]:
            return None
        changed = changed or replacement["sha256"] != source["sha256"]
        replacements.append(replacement)
    return replacements if changed else None


def _observation_keys(
    observation: dict[str, Any],
    root: Path,
    plugin: str,
    originals: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    sources = _receipt_sources(observation, root)
    superseded = False
    if sources is None and originals is not None:
        sources = _superseding_sources(observation, root, originals)
        superseded = sources is not None
    if sources is None:
        key = _image_key(observation, root)
        try:
            current = Path(key[0])
            if current.is_relative_to(root) and current.is_file():
                key = (key[0], sha256_file(current))
        except (OSError, ValueError):
            pass
        return [key], "image owner/ancestry unknown; legacy review scope retained"
    keys = [(p["path"], p["sha256"]) for p in sources if p["owner_plugin"] == plugin]
    if superseded:
        return keys, "earlier image delivery superseded by current original revisions"
    return (
        keys,
        "foreign or auxiliary image scoped to its original owner/revision"
        if not keys
        else None,
    )


def _auxiliary_review(review: dict[str, Any], root: Path) -> bool:
    delivery = review.get("image_delivery")
    if not isinstance(delivery, dict):
        return False
    delivery = cast(dict[str, Any], delivery)
    if delivery.get("status") != "bound":
        return False
    claim = delivery.get("image_identity")
    if not isinstance(claim, dict):
        return False
    claim = cast(dict[str, Any], claim)
    if claim.get("origin") != "derived":
        return False
    return (
        _producer_sources(
            {
                "image_identity": claim,
                "identity_status": delivery.get("producer_identity_status"),
                "image_path": _image_key(review, root)[0],
                "image_sha256": review.get("image_sha256"),
            },
            root,
            current=False,
        )
        is not None
    )


def _marker_path(records_dir: Path, payload: dict[str, Any]) -> Path:
    return records_dir / ".sessions" / f"{_session_key(payload)}.json"


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _create_marker(
    path: Path, payload: dict[str, Any], scope_origin: str
) -> dict[str, Any]:
    marker = {
        "session_id": payload.get("session_id"),
        "started_at": time.time(),
        "denials": 0,
        "scope_origin": scope_origin,
    }
    _write_json(path, marker)
    return marker


def session_start(
    payload: dict[str, Any],
    policy: dict[str, Any],
    root: Path,
    *,
    include_recall: bool = True,
) -> int:
    records_dir = root / str(policy["records_dir"])
    marker = _marker_path(records_dir, payload)
    if _read_marker(marker) is None:
        _create_marker(
            marker, payload, "session_start" if include_recall else "record-context"
        )
    briefing = recall(root, str(policy["plugin"])) if include_recall else ""
    if payload.get("session_id"):
        identity = json.dumps(payload["session_id"], ensure_ascii=False)
        briefing = (
            f"VRP record context: pass session_id={identity} "
            "to this plugin's record tools. "
            "Use the delegated agent's own session context for its records. "
            "A delegated primary must record coordinator_session_id "
            "supplied by its caller; "
            "when delegating a primary, pass your own session_id as that coordinator.\n"
            + briefing
        )
    if briefing:
        print(json.dumps({"additionalContext": briefing}, ensure_ascii=False))
    return 0


def _dual_review_problems(
    policy: dict[str, Any],
    logs: dict[str, list[dict[str, Any]]],
    session_id: str | None,
    root: Path,
) -> tuple[list[str], list[str]]:
    patterns = cast(list[str], policy.get("dual_review_globs") or [])
    problems: list[str] = []
    warnings: list[str] = []
    if not patterns:
        return problems, warnings
    reviews = logs["vision_review"]
    reconciles = logs["review_reconcile"]
    primaries = [
        r
        for r in reviews
        if r.get("reviewer") == "primary"
        and not _auxiliary_review(r, root)
        and (
            not r.get("coordinator_session_id")
            or r.get("coordinator_session_id") == session_id
        )
        and matches(str(r.get("image_path") or ""), patterns)
    ]
    for primary in primaries:
        digest = primary.get("image_sha256")
        image = primary.get("image_path")
        blind = [
            r
            for r in reviews
            if r.get("reviewer") == "blind"
            and (
                not primary.get("session_id")
                or r.get("primary_event_id") == primary.get("event_id")
            )
            and r.get("image_sha256") == digest
            and r.get("image_path") == image
        ]
        if not blind:
            problems.append(
                f"image {image} needs a blind second review: "
                "delegate it with `task` to the"
                " blind vision sub-agent (it must not see the first review)"
            )
            continue
        mine = [
            c
            for c in reconciles
            if primary.get("event_id") in cast(list[str], c.get("reviews") or [])
        ]
        if not mine:
            problems.append(f"image {image}: reconcile the primary and blind reviews")
            continue
        last = max(mine, key=lambda c: int(c.get("round") or 0))
        if last.get("outcome") == "disagree":
            problems.append(
                f"image {image}: the reviews disagree; "
                "get one tiebreak review and reconcile"
                " again (round 2 is the last)"
            )
        elif last.get("outcome") == "unresolved":
            warnings.append(
                f"image {image}: independent reviews stay split; treat it as unknown"
            )
    return problems, warnings


def problems_for(
    payload: dict[str, Any], policy: dict[str, Any], root: Path, since: float
) -> tuple[list[str], list[str]]:
    records_dir = root / str(policy["records_dir"])
    plugin = str(policy["plugin"])
    session_id = cast(str | None, payload.get("session_id"))
    problems: list[str] = []
    warnings: list[str] = []
    logs: dict[str, list[dict[str, Any]]] = {}
    if not session_id:
        warnings.append("session_id is missing; record attribution is unknown")
    for kind, filename in LOG_FILES.items():
        records, issues = verify_log(records_dir / filename, kind)
        problems.extend(issues)
        if any(r.get("plugin") != plugin for r in records):
            problems.append(f"{filename}: record plugin differs from owner {plugin}")
        # Invalid chains are evidence of a gap, never usable review input.
        logs[kind] = [] if issues else [r for r in records if r.get("plugin") == plugin]
        legacy = [
            r
            for r in logs[kind]
            if not r.get("session_id")
            and (parse_time(r.get("recorded_at")) or 0) >= since
        ]
        if legacy:
            warnings.append(
                f"{filename}: {len(legacy)} legacy records lack session_id; "
                "timestamp fallback is limited to 24 hours after session start; "
                "session attribution remains unknown"
            )
    session = {
        kind: [
            r
            for r in items
            if _in_session(r, session_id, since)
            or (
                kind == "vision_review"
                and r.get("reviewer") == "primary"
                and session_id
                and r.get("session_id")
                and r.get("coordinator_session_id") == session_id
            )
        ]
        for kind, items in logs.items()
    }
    for review in logs["vision_review"]:
        if review.get("reviewer") not in {"blind", "tiebreak"}:
            continue
        primary = review_primary(review, logs["vision_review"], root)
        if (
            review.get("session_id") or review.get("primary_event_id")
        ) and primary is None:
            if review in session["vision_review"]:
                session["vision_review"].remove(review)
                problems.append(
                    "scoped independent review has no valid primary_event_id"
                )
            continue
        if (
            primary is not None
            and primary in session["vision_review"]
            and review not in session["vision_review"]
        ):
            session["vision_review"].append(review)

    for primary in session["vision_review"]:
        if primary.get("reviewer") != "primary" or not primary.get(
            "coordinator_session_id"
        ):
            continue
        if primary.get("coordinator_session_id") != session_id:
            warnings.append(
                "independent review round handed to coordinator "
                + str(primary["coordinator_session_id"])
            )
        for reconciliation in logs["review_reconcile"]:
            if (
                reconciliation.get("reviews", [None])[0] == primary.get("event_id")
                and reconciliation.get("session_id") == primary.get("session_id")
                and reconciliation not in session["review_reconcile"]
            ):
                session["review_reconcile"].append(reconciliation)

    reviews = session["vision_review"]
    reviewed_events = {
        str(r.get("source_event_id")) for r in reviews if r.get("source_event_id")
    }
    reviewed_images = {_image_key(r, root) for r in reviews if r.get("image_sha256")}
    events, malformed = load_jsonl(records_dir / VISION_EVENTS_FILE)
    if malformed:
        problems.append(f"{VISION_EVENTS_FILE}: {malformed} malformed lines")
    if any(
        not e.get("session_id") and (parse_time(e.get("recorded_at")) or 0) >= since
        for e in events
    ):
        warnings.append("legacy vision events lack session_id; attribution is unknown")
    for event in events:
        if not _in_session(event, session_id, since):
            continue
        if str(event.get("event_id")) not in reviewed_events:
            problems.append(
                f"vision tool event {event.get('event_id')} (question: "
                f"{str(event.get('question'))[:80]!r}) has no vision_review impression"
            )
    observations, malformed = load_jsonl(records_dir / IMAGE_OBSERVATIONS_FILE)
    if malformed:
        problems.append(f"{IMAGE_OBSERVATIONS_FILE}: {malformed} malformed lines")
    if any(
        not item.get("session_id")
        and (parse_time(item.get("recorded_at")) or 0) >= since
        for item in observations
    ):
        warnings.append(
            "legacy image observations lack session_id; attribution is unknown"
        )
    seen: set[tuple[str, str]] = set()
    originals = _current_originals(observations, root)
    for observation in observations:
        if not _in_session(observation, session_id, since):
            continue
        keys, warning = _observation_keys(observation, root, plugin, originals)
        claim = observation.get("image_identity")
        claim = cast(dict[str, Any], claim) if isinstance(claim, dict) else {}
        if (
            claim.get("origin") == "derived"
            and warning
            and warning.startswith("image owner/ancestry unknown")
        ):
            problems.append(
                "image ancestry unknown: restore and re-deliver the original revisions"
            )
        if warning and warning not in warnings:
            warnings.append(warning)
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            if key not in reviewed_images:
                problems.append(
                    f"viewed image {key[0] if observation.get('image_path') else None} "
                    f"(sha256 {key[1][:12]}) has no vision_review impression"
                )

    for read in session["upstream_read"]:
        after = parse_time(read.get("recorded_at"))
        for ref in cast(list[dict[str, Any]], read.get("refs") or []):
            if not acknowledged(session, ref, after):
                problems.append(
                    f"sister impression {ref.get('system')}:"
                    f"{str(ref.get('event_id'))[:12]} came with "
                    f"{cast(dict[str, Any], read.get('artifact') or {}).get('path')}"
                    "; cite"
                    " it in the upstream field of an impression "
                    "(adopted, deferred, disputed or"
                    " noted, and its effect)"
                )
        for issue in cast(list[str], read.get("unresolved") or []):
            warnings.append(f"import {read.get('producer')}: {issue}")

    dual, split = _dual_review_problems(policy, session, session_id, root)
    problems.extend(dual)
    warnings.extend(split)

    for relative, doc, issues in song_deliveries(root):
        if doc is None or doc.get("to") != plugin:
            continue
        if issues:
            warnings.append(f"bard song {relative} is malformed: {issues[0]}")
            continue
        if not received(logs, relative, sha256_file(root / relative)):
            problems.append(
                f"bard sent you a song ({relative}); "
                "read it and record a song receipt: what"
                " you felt and whether it makes you look at the design again"
            )

    changed = changed_artifacts(root, policy, since)
    if changed:
        fresh: list[str] = []
        for impression in session["stage_impression"]:
            for entry in cast(list[dict[str, Any]], impression.get("artifacts") or []):
                path = str(entry.get("path") or "")
                target = (root / path).resolve()
                try:
                    current = tree_sha256(target) if target.exists() else None
                except OSError:
                    current = None
                if current is not None and current == entry.get("sha256"):
                    fresh.append(path)
        uncovered = [rel for rel in changed if not any(covers(p, rel) for p in fresh)]
        for relative in uncovered:
            problems.append(
                f"artifact {relative} changed this session "
                "but no fresh stage_impression covers it"
            )
        if not session["decision"]:
            problems.append(
                "artifacts changed this session but no decision record explains why"
                " (principles, options, evidence, unknowns, risks)"
            )
    return problems, warnings


def stop(payload: dict[str, Any], policy: dict[str, Any], root: Path) -> int:
    records_dir = root / str(policy["records_dir"])
    marker_path = _marker_path(records_dir, payload)
    marker = _read_marker(marker_path)
    missing_marker = marker is None
    if marker is None:
        marker = _create_marker(marker_path, payload, "stop")
    since = float(marker.get("started_at") or 0.0)
    problems, warnings = problems_for(payload, policy, root, since)
    if missing_marker or marker.get("scope_origin") == "stop":
        warnings.append(
            "session-start marker was missing; prior record scope is unknown"
        )
    limit = max(0, int(policy.get("max_stop_denials", DEFAULT_MAX_STOP_DENIALS)))
    denials = int(marker.get("denials") or 0)
    deny = bool(problems) and denials < limit
    if deny:
        denials += 1
        marker["denials"] = denials
        _write_json(marker_path, marker)
    status = {
        "schema_version": 1,
        "plugin": policy["plugin"],
        "session_id": payload.get("session_id"),
        "checked_at": time.time(),
        "stop_permission": "deny" if deny else "allow",
        "record_compliance": "fail" if problems else "unknown" if warnings else "pass",
        "run_completion": "unknown",
        "acceptance": "unknown",
        "stop_denials": denials,
        "max_stop_denials": limit,
        "checkpoint": str(marker_path.relative_to(root)),
        "verdict": "fail" if problems else "pass",
        "problems": problems,
        "warnings": warnings,
    }
    _write_json(records_dir / "records-status.json", status)
    _write_json(marker_path.with_suffix(".status.json"), status)
    if not problems:
        return 0
    listed = problems[:MAX_LISTED]
    more = len(problems) - len(listed)
    body = "\n".join(f"- {item}" for item in listed)
    if more > 0:
        body += f"\n- ... and {more} more"
    hint = str(policy.get("record_hint") or "")
    if deny:
        message = (
            f"[{policy['plugin']}] VibeBB Record Protocol: "
            "this session still owes records."
            f" Record them before finishing (refusal {denials}/{limit}):\n"
            f"{body}\n{hint}"
        ).strip()
        print(
            json.dumps(
                {"decision": "deny", "reason": message, "additionalContext": message}
            )
        )
        return 2
    message = (
        f"[{policy['plugin']}] VibeBB Record Protocol "
        f"still unmet after {limit} refusals;"
        f" finishing with gaps recorded in {policy['records_dir']}/records-status.json."
        f" Tell the user exactly which records are missing:\n{body}"
    )
    print(json.dumps({"decision": "allow", "additionalContext": message}))
    return 0


def main(argv: list[str]) -> int:
    try:
        raw: Any = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        raw = {}
    payload = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    mode = argv[0] if argv else str(payload.get("event_type") or "")
    try:
        policy = load_policy(PLUGIN_ROOT)
        root = _project_dir(payload)
        if mode in ("session-start", "SessionStart"):
            return session_start(payload, policy, root)
        if mode == "record-context":
            return session_start(payload, policy, root, include_recall=False)
        if mode in ("stop", "Stop"):
            return stop(payload, policy, root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"require-records hook skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
