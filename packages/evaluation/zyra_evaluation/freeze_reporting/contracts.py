from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import (
    require_commit,
    require_digest,
    require_identity,
    require_integer,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
)
from .errors import blocker, require_no_blockers


SCORE_DIMENSIONS = {
    "works-completeness": 40,
    "application-innovation": 25,
    "technical-innovation": 20,
    "performance-efficiency": 15,
}

SCORE_ITEMS = {
    "REQ-COMP-001": ("works-completeness", 6, "perception-plan-execute-feedback"),
    "REQ-COMP-002": ("works-completeness", 8, "long-horizon-effective-transitions"),
    "REQ-COMP-003": ("works-completeness", 6, "dynamic-heterogeneous-organization"),
    "REQ-COMP-004": ("works-completeness", 6, "sparse-role-coordination"),
    "REQ-COMP-005": ("works-completeness", 6, "dual-domain-live-delivery"),
    "REQ-COMP-006": ("works-completeness", 5, "fault-change-node-loss-recovery"),
    "REQ-COMP-007": ("works-completeness", 3, "causal-trajectory-navigation"),
    "REQ-APP-001": ("application-innovation", 7, "software-delivery-case"),
    "REQ-APP-002": ("application-innovation", 5, "cross-source-research-case"),
    "REQ-APP-003": ("application-innovation", 5, "cross-industry-generalization"),
    "REQ-APP-004": ("application-innovation", 4, "auditable-social-economic-value"),
    "REQ-APP-005": ("application-innovation", 4, "reviewer-usable-control-surface"),
    "REQ-TECH-001": ("technical-innovation", 8, "dynamic-sparse-low-entropy"),
    "REQ-TECH-002": ("technical-innovation", 5, "distributed-memory-compact-restore"),
    "REQ-TECH-003": ("technical-innovation", 4, "neuro-symbolic-control"),
    "REQ-TECH-004": ("technical-innovation", 3, "algorithm-complexity-linkage"),
    "REQ-PERF-001": ("performance-efficiency", 6, "robustness-recovery-success"),
    "REQ-PERF-002": ("performance-efficiency", 5, "token-time-compute-efficiency"),
    "REQ-PERF-003": ("performance-efficiency", 4, "provider-model-role-extensibility"),
}

REQUIRED_REFERENCE_KINDS = (
    "default-entry",
    "live-mutation",
    "artifact",
    "metric",
    "test",
    "config",
    "commit",
    "checksum",
)

REQUIRED_MATERIAL_TOPICS = (
    "architecture",
    "dynamic-topology",
    "low-entropy-communication",
    "memory-compact-restore",
    "neuro-symbolic-control",
    "edge-cloud-model-split",
    "fault-recovery",
)

REQUIRED_ABLATION_VARIANTS = (
    "dynamic-heterogeneous-swarm",
    "single-agent",
    "static-full-connect-multi-agent",
    "no-scheduler",
    "no-memory-compact",
    "no-recovery",
    "no-low-entropy-communication",
)

ACTIVE_ROLES = {
    "primary_implementation",
    "supplementary_implementation",
}

INACTIVE_ROLES = {
    "conformance_only",
    "reference_only",
    "experimental",
    "deferred",
    "rejected",
}

ALL_SOURCE_ROLES = ACTIVE_ROLES | INACTIVE_ROLES


def validate_contract_totals() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    dimension_scores = {dimension: 0 for dimension in SCORE_DIMENSIONS}
    for requirement_id, (dimension, score, title) in SCORE_ITEMS.items():
        if dimension not in dimension_scores:
            findings.append(
                blocker(
                    "score-dimension-unknown",
                    "Score item uses an unknown dimension.",
                    requirement_id=requirement_id,
                    dimension=dimension,
                )
            )
            continue
        if not title:
            findings.append(
                blocker(
                    "score-title-missing",
                    "Score item title is missing.",
                    requirement_id=requirement_id,
                )
            )
        dimension_scores[dimension] += score
    for dimension, maximum in SCORE_DIMENSIONS.items():
        if dimension_scores[dimension] != maximum:
            findings.append(
                blocker(
                    "score-dimension-total-invalid",
                    "Score dimension does not sum to its declared maximum.",
                    dimension=dimension,
                    expected=maximum,
                    observed=dimension_scores[dimension],
                )
            )
    if sum(dimension_scores.values()) != 100:
        findings.append(
            blocker(
                "score-total-invalid",
                "Score contract does not sum to 100.",
                observed=sum(dimension_scores.values()),
            )
        )
    require_no_blockers(
        findings,
        code="freeze-score-contract-invalid",
        message="Freeze score contract is internally inconsistent.",
        phase="contract",
    )
    return {
        "dimensions": dimension_scores,
        "score": sum(dimension_scores.values()),
        "requirement_count": len(SCORE_ITEMS),
    }


def validate_evidence_reference(
    value: Mapping[str, Any],
    *,
    repository_root: Path,
) -> dict[str, Any]:
    selected = require_mapping(value, "evidence reference")
    kind = require_text(selected.get("kind"), "reference kind", maximum=64)
    if kind not in REQUIRED_REFERENCE_KINDS:
        raise ValueError(f"unsupported evidence reference kind: {kind}")
    reference = {
        "reference_id": require_identity(
            selected.get("reference_id"),
            "reference id",
        ),
        "kind": kind,
        "path": safe_relative_path(selected.get("path"), "reference path"),
        "sha256": require_digest(selected.get("sha256"), "reference sha256"),
        "label": require_text(selected.get("label"), "reference label", maximum=512),
    }
    if "selector" in selected:
        reference["selector"] = require_text(
            selected.get("selector"),
            "reference selector",
            maximum=1024,
        )
    if "commit" in selected:
        reference["commit"] = require_commit(
            selected.get("commit"),
            "reference commit",
        )
    target = repository_root / reference["path"]
    reference["exists"] = target.is_file() or target.is_dir()
    return reference


def validate_score_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    selected = require_mapping(value, "score entry")
    requirement_id = require_text(
        selected.get("requirement_id"),
        "requirement id",
        maximum=64,
    )
    if requirement_id not in SCORE_ITEMS:
        raise ValueError(f"unknown score requirement: {requirement_id}")
    dimension, points, title = SCORE_ITEMS[requirement_id]
    references = [
        require_mapping(item, f"{requirement_id} reference")
        for item in require_sequence(selected.get("references"), "score references")
    ]
    observed_kinds = {str(item.get("kind") or "") for item in references}
    missing = sorted(set(REQUIRED_REFERENCE_KINDS) - observed_kinds)
    if missing:
        raise ValueError(
            f"{requirement_id} is missing evidence kinds: {', '.join(missing)}"
        )
    return {
        "requirement_id": requirement_id,
        "dimension": dimension,
        "points": points,
        "title": title,
        "status": require_text(selected.get("status"), "score status", maximum=32),
        "references": references,
    }


def validate_archive_member(value: Mapping[str, Any]) -> dict[str, Any]:
    selected = require_mapping(value, "archive member")
    return {
        "path": safe_relative_path(selected.get("path"), "archive member path"),
        "sha256": require_digest(selected.get("sha256"), "archive member sha256"),
        "bytes": require_integer(
            selected.get("bytes"),
            "archive member bytes",
            minimum=0,
            maximum=16 * 1024 * 1024 * 1024,
        ),
        "kind": require_text(
            selected.get("kind"),
            "archive member kind",
            maximum=64,
        ),
        "previous_digest": require_digest(
            selected.get("previous_digest"),
            "archive previous digest",
        ),
        "chain_digest": require_digest(
            selected.get("chain_digest"),
            "archive chain digest",
        ),
    }


def validate_source_row(value: Mapping[str, Any]) -> dict[str, Any]:
    selected = require_mapping(value, "source row")
    role = require_text(selected.get("role"), "source role", maximum=64)
    if role not in ALL_SOURCE_ROLES and role != "excluded_forward_only":
        raise ValueError(f"unsupported source role: {role}")
    languages = [
        require_text(item, "source language", maximum=64)
        for item in require_sequence(selected.get("source_languages"), "source languages")
    ]
    if not languages:
        raise ValueError("source row must retain at least one concrete language")
    return {
        "source_id": require_identity(selected.get("source_id"), "source id"),
        "capability": require_text(
            selected.get("capability"),
            "source capability",
            maximum=512,
        ),
        "role": role,
        "status": require_text(selected.get("status"), "source status", maximum=64),
        "source_languages": sorted(set(languages)),
        "target_language": require_text(
            selected.get("target_language"),
            "target language",
            maximum=64,
        ),
        "owner": str(selected.get("owner") or "").strip(),
        "target_paths": [
            safe_relative_path(item, "source target path")
            for item in require_sequence(
                selected.get("target_paths"),
                "source target paths",
            )
        ],
        "test_paths": [
            safe_relative_path(item, "source test path")
            for item in require_sequence(
                selected.get("test_paths"),
                "source test paths",
            )
        ],
        "decision": require_text(
            selected.get("decision"),
            "source decision",
            maximum=4096,
        ),
    }


def source_row_blockers(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    selected = validate_source_row(row)
    findings: list[dict[str, Any]] = []
    if selected["role"] in ACTIVE_ROLES:
        if not selected["owner"]:
            findings.append(
                blocker(
                    "active-source-owner-missing",
                    "Active source row has no canonical owner.",
                    source_id=selected["source_id"],
                    capability=selected["capability"],
                )
            )
        if not selected["target_paths"]:
            findings.append(
                blocker(
                    "active-source-target-missing",
                    "Active source row has no Zyra target path.",
                    source_id=selected["source_id"],
                )
            )
        if not selected["test_paths"]:
            findings.append(
                blocker(
                    "active-source-test-missing",
                    "Active source row has no behavior test path.",
                    source_id=selected["source_id"],
                )
            )
    if selected["source_id"].lower() == "openclaw":
        if selected["role"] != "excluded_forward_only":
            findings.append(
                blocker(
                    "openclaw-forward-role-forbidden",
                    "OpenClaw must remain excluded_forward_only in M3.",
                    role=selected["role"],
                )
            )
        if selected["target_paths"] or selected["test_paths"]:
            findings.append(
                blocker(
                    "openclaw-forward-work-forbidden",
                    "OpenClaw cannot acquire new target or test obligations.",
                )
            )
    return findings


validate_contract_totals()
