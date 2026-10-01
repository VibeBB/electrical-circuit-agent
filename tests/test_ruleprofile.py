import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from circuit.ruleprofile import (
    EvidenceRef,
    GoalOverride,
    PasteRule,
    RuleProfile,
    RuleProfileError,
    load_rules,
    profile_sha256,
)


def _builtin_ipc_profile() -> RuleProfile:
    return RuleProfile(
        artifact_kind="circuit_rule_profile",
        profile_id="builtin:ipc7351b",
        layer="standard",
        parent=None,
        standard="ipc7351b",
        density="nominal",
        fabrication_tolerance=0.05,
        placement_tolerance=0.025,
        min_pad_clearance_mm=0.15,
        min_ep_to_pad_clearance_mm=0.2,
        min_mask_web_mm=0.1,
        paste=PasteRule(
            coverage_min=0.5,
            coverage_max=1.0,
            ep_coverage_min=0.5,
            ep_coverage_max=0.8,
        ),
    )


def _write_profile(
    rules_dir: Path,
    profile_id: str,
    **changes: object,
) -> RuleProfile:
    payload: dict[str, object] = {
        "artifact_kind": "circuit_rule_profile",
        "profile_id": profile_id,
        "layer": "manufacturer",
        "parent": "builtin:ipc7351b",
        "parent_sha256": profile_sha256(_builtin_ipc_profile()),
    }
    payload.update(changes)
    profile = RuleProfile.model_validate(payload)
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / f"{profile_id}.json").write_text(
        json.dumps(profile.model_dump(mode="json")), encoding="utf-8"
    )
    return profile


def test_builtin_profiles_resolve_expected_defaults_and_chain(tmp_path: Path) -> None:
    ipc = load_rules("builtin:ipc7351b", tmp_path / "rules")
    generator = load_rules("builtin:kicad-generator", tmp_path / "rules")

    assert ipc.chain == ["builtin:ipc7351b"]
    assert [(profile.profile_id, profile.layer) for profile in ipc.profile_chain] == [
        ("builtin:ipc7351b", "standard")
    ]
    assert ipc.profile_chain[0].sha256 == profile_sha256(_builtin_ipc_profile())
    assert ipc.fabrication_tolerance == 0.05
    assert ipc.placement_tolerance == 0.025
    assert (
        ipc.min_pad_clearance_mm,
        ipc.min_ep_to_pad_clearance_mm,
        ipc.min_mask_web_mm,
    ) == (0.15, 0.2, 0.1)
    assert ipc.paste.coverage_min == 0.5
    assert ipc.paste.coverage_max == 1.0
    assert ipc.paste.ep_coverage_min == 0.5
    assert ipc.paste.ep_coverage_max == 0.8
    assert generator.fabrication_tolerance == 0.1
    assert generator.placement_tolerance == 0.05
    assert generator.chain_sha256 != ipc.chain_sha256


def test_profile_hash_is_canonical() -> None:
    profile = RuleProfile(
        artifact_kind="circuit_rule_profile",
        profile_id="test-profile",
        layer="manufacturer",
        parent="builtin:ipc7351b",
        parent_sha256="a" * 64,
        rationale="manufacturer drawing",
    )

    assert profile_sha256(profile) == profile_sha256(
        RuleProfile.model_validate(profile.model_dump(mode="json"))
    )


def test_child_profile_overrides_parent_and_records_hash_chain(tmp_path: Path) -> None:
    rules_dir = tmp_path / "library" / "rules"
    _write_profile(
        rules_dir,
        "manufacturer.test",
        density="least",
        fabrication_tolerance=0.07,
        min_pad_clearance_mm=0.18,
        goal_overrides={"gullwing_dual": {"toe": 0.4}},
    )

    effective = load_rules("manufacturer.test", rules_dir)

    assert effective.chain == ["builtin:ipc7351b", "manufacturer.test"]
    assert [(profile.profile_id, profile.layer) for profile in effective.profile_chain] == [
        ("builtin:ipc7351b", "standard"),
        ("manufacturer.test", "manufacturer"),
    ]
    assert effective.layer == "manufacturer"
    assert effective.density == "least"
    assert effective.fabrication_tolerance == 0.07
    assert effective.placement_tolerance == 0.025
    assert effective.min_pad_clearance_mm == 0.18
    assert effective.goal_overrides["gullwing_dual"].toe == 0.4


def test_builtin_file_cannot_override_code_profile(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "builtin:ipc7351b.json").write_text('{"profile_id":"evil"}', encoding="utf-8")

    rules = load_rules("builtin:ipc7351b", rules_dir)
    assert rules.fabrication_tolerance == 0.05


def test_profile_cycle_is_rejected(tmp_path: Path) -> None:
    rules_dir = tmp_path / "project" / "library" / "rules"
    rules_dir.mkdir(parents=True)
    a = RuleProfile(
        artifact_kind="circuit_rule_profile",
        profile_id="a",
        layer="manufacturer",
        parent="b",
        parent_sha256="0" * 64,
    )
    b = RuleProfile(
        artifact_kind="circuit_rule_profile",
        profile_id="b",
        layer="manufacturer",
        parent="a",
        parent_sha256="0" * 64,
    )
    for profile in (a, b):
        (rules_dir / f"{profile.profile_id}.json").write_text(
            json.dumps(profile.model_dump(mode="json")), encoding="utf-8"
        )

    with pytest.raises(RuleProfileError) as error:
        load_rules("a", rules_dir)
    assert error.value.code == "rule_profile_cycle"


def test_missing_parent_and_parent_hash_mismatch_are_rejected(tmp_path: Path) -> None:
    rules_dir = tmp_path / "project" / "library" / "rules"
    _write_profile(rules_dir, "orphan", parent="missing")
    with pytest.raises(RuleProfileError) as missing:
        load_rules("orphan", rules_dir)
    assert missing.value.code == "missing_parent"

    _write_profile(
        rules_dir,
        "bad-hash",
        parent_sha256="0" * 64,
    )
    with pytest.raises(RuleProfileError) as mismatch:
        load_rules("bad-hash", rules_dir)
    assert mismatch.value.code == "parent_hash"


def test_layer_order_cannot_decrease(tmp_path: Path) -> None:
    rules_dir = tmp_path / "project" / "library" / "rules"
    _write_profile(
        rules_dir,
        "organization",
        layer="organization",
        rationale="measured prototype results",
        evidence=[
            {
                "kind": "prototype",
                "path": "prototype.txt",
                "sha256": hashlib.sha256(b"data").hexdigest(),
            }
        ],
    )
    (tmp_path / "project" / "prototype.txt").write_text("data", encoding="utf-8")
    _write_profile(
        rules_dir,
        "decreasing",
        layer="manufacturer",
        parent="organization",
        parent_sha256=profile_sha256(
            RuleProfile.model_validate(
                json.loads((rules_dir / "organization.json").read_text(encoding="utf-8"))
            )
        ),
    )

    with pytest.raises(RuleProfileError) as error:
        load_rules("decreasing", rules_dir)
    assert error.value.code == "layer_order"


def test_evidence_hash_and_project_boundary_are_verified(tmp_path: Path) -> None:
    rules_dir = tmp_path / "project" / "library" / "rules"
    rules_dir.mkdir(parents=True)
    evidence = tmp_path / "project" / "prototype.txt"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("new", encoding="utf-8")
    profile = _write_profile(
        rules_dir,
        "organization",
        layer="organization",
        rationale="prototype yield data",
        evidence=[
            {
                "kind": "prototype",
                "path": "prototype.txt",
                "sha256": hashlib.sha256(b"old").hexdigest(),
            }
        ],
    )
    assert profile.profile_id == "organization"
    with pytest.raises(RuleProfileError) as mismatch:
        load_rules("organization", rules_dir)
    assert mismatch.value.code == "evidence"

    profile = RuleProfile.model_validate(
        {
            **profile.model_dump(mode="json"),
            "evidence": [
                {
                    "kind": "prototype",
                    "path": "../../outside",
                    "sha256": hashlib.sha256(b"data").hexdigest(),
                }
            ],
        }
    )
    (rules_dir / "organization.json").write_text(
        json.dumps(profile.model_dump(mode="json")), encoding="utf-8"
    )
    with pytest.raises(RuleProfileError) as outside:
        load_rules("organization", rules_dir)
    assert outside.value.code == "evidence"


@pytest.mark.parametrize(
    ("layer", "rationale", "evidence", "code"),
    [
        ("organization", "", [], "rationale"),
        ("organization", "why", [], "evidence"),
    ],
)
def test_org_profiles_require_rationale_and_evidence(
    layer: str,
    rationale: str,
    evidence: list[EvidenceRef],
    code: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        RuleProfile(
            artifact_kind="circuit_rule_profile",
            profile_id="org.profile",
            layer=layer,  # type: ignore[arg-type]
            parent="builtin:ipc7351b",
            parent_sha256="a" * 64,
            rationale=rationale,
            evidence=evidence,
        )
    assert code in str(error.value)


def test_rule_values_are_rejected_when_invalid() -> None:
    with pytest.raises(ValidationError):
        RuleProfile(
            artifact_kind="circuit_rule_profile",
            profile_id="bad",
            layer="manufacturer",
            parent="builtin:ipc7351b",
            parent_sha256="a" * 64,
            fabrication_tolerance=float("nan"),
        )
    with pytest.raises(ValidationError):
        RuleProfile(
            artifact_kind="circuit_rule_profile",
            profile_id="bad",
            layer="manufacturer",
            parent="builtin:ipc7351b",
            parent_sha256="a" * 64,
            min_pad_clearance_mm=0,
        )
    with pytest.raises(ValidationError):
        PasteRule(
            coverage_min=0.8,
            coverage_max=0.7,
            ep_coverage_min=0.5,
            ep_coverage_max=0.8,
        )
    with pytest.raises(ValidationError):
        GoalOverride(toe=float("inf"))


def test_evidence_reference_rejects_invalid_hash() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(kind="datasheet", path="parts.pdf", sha256="0" * 63)
