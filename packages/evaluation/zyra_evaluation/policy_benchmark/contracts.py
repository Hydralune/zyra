from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


P2_BASE_COMMIT = "e207b46ca690171139a718b8b85d808cb5a79c1e"
EXPECTED_ADR_IDS = tuple(f"P2-ADR-{index:03d}" for index in range(1, 8))
EXPECTED_CONFIG_IDS = ("source_roles", "state_owners", "activation_gates")
EXPECTED_REQUIRED_MECHANISMS = (
    "loopx",
    "arg_designer",
    "card",
    "agentprune",
    "maas",
)
ALLOWED_SOURCE_ROLES = {
    "primary_implementation",
    "supplementary_implementation",
    "zyra_owned_only",
}
ALLOWED_PROFILE_LIFECYCLES = {
    "baseline",
    "default",
    "validation",
    "diagnostic",
    "retired",
}
ALLOWED_METRIC_DIRECTIONS = {
    "equal",
    "minimum",
    "maximum",
    "non_decreasing",
    "non_increasing",
}
ALLOWED_EMPTY_SAMPLE_SEMANTICS = {
    "fail_closed",
    "not_applicable",
    "zero",
}
FORBIDDEN_TRAINING_TOKENS = {
    "train",
    "training",
    "fine-tune",
    "finetune",
    "rl",
    "policy-gradient",
    "policy_gradient",
    "textual-gradient",
    "textual_gradient",
    "online-learning",
    "online_learning",
}


class ContractViolation(ValueError):
    """A deterministic, machine-readable Phase 2 contract failure."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": str(self),
            "path": self.path,
        }


class MechanismReadinessStatus(str, Enum):
    DETERMINISTIC_READY = "deterministic_ready"
    EVIDENCE_ONLY = "evidence_only"
    UNAVAILABLE = "unavailable"


def parse_mechanism_status(value: object) -> MechanismReadinessStatus:
    try:
        return MechanismReadinessStatus(str(value))
    except ValueError as exc:
        raise ContractViolation(
            "readiness-status-invalid",
            f"Unsupported mechanism readiness status: {value!r}.",
            path="activation_gates.readiness.mechanisms",
        ) from exc


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractViolation(
            "mapping-required",
            f"{label} must be a mapping.",
            path=label,
        )
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractViolation(
            "sequence-required",
            f"{label} must be a sequence.",
            path=label,
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(
            "text-required",
            f"{label} must be non-empty text.",
            path=label,
        )
    return value.strip()


def _commit(value: object, label: str) -> str:
    selected = _text(value, label)
    if len(selected) != 40 or any(char not in "0123456789abcdef" for char in selected):
        raise ContractViolation(
            "commit-invalid",
            f"{label} must be a lowercase 40-character Git commit.",
            path=label,
        )
    return selected


def _digest(value: object, label: str) -> str:
    selected = _text(value, label)
    if len(selected) != 64 or any(char not in "0123456789abcdef" for char in selected):
        raise ContractViolation(
            "digest-invalid",
            f"{label} must be a lowercase SHA-256 digest.",
            path=label,
        )
    return selected


def _relative_path(value: object, label: str) -> str:
    selected = _text(value, label).replace("\\", "/")
    path = Path(selected)
    if path.is_absolute() or ".." in path.parts:
        raise ContractViolation(
            "path-outside-repository",
            f"{label} must remain repository-relative.",
            path=label,
        )
    return selected


def _require_base_commit(value: Mapping[str, Any], label: str) -> None:
    observed = _commit(value.get("p2_base_commit"), f"{label}.p2_base_commit")
    if observed != P2_BASE_COMMIT:
        raise ContractViolation(
            "p2-base-commit-mismatch",
            f"{label} is bound to {observed}, expected {P2_BASE_COMMIT}.",
            path=f"{label}.p2_base_commit",
        )


def _unique(values: Sequence[str], *, code: str, label: str) -> None:
    if len(values) != len(set(values)):
        raise ContractViolation(
            code,
            f"{label} contains a duplicate identity.",
            path=label,
        )


def _entry_by_source(
    registry: Mapping[str, Any],
    source_id: str,
) -> Mapping[str, Any]:
    for domain_value in _sequence(
        registry.get("capability_domains"),
        "source_roles.capability_domains",
    ):
        domain = _mapping(domain_value, "source role domain")
        for entry_value in _sequence(domain.get("entries"), "source role entries"):
            entry = _mapping(entry_value, "source role entry")
            if entry.get("source_id") == source_id:
                return entry
    raise ContractViolation(
        "source-required",
        f"Required source entry is missing: {source_id}.",
        path="source_roles.capability_domains",
    )


def validate_source_role_registry(registry: Mapping[str, Any]) -> dict[str, Any]:
    selected = _mapping(registry, "source_roles")
    _require_base_commit(selected, "source_roles")
    if selected.get("frozen") is not True:
        raise ContractViolation(
            "source-registry-not-frozen",
            "The Phase 2 source-role registry must be frozen.",
            path="source_roles.frozen",
        )

    domains = [
        _mapping(item, "source role domain")
        for item in _sequence(
            selected.get("capability_domains"),
            "source_roles.capability_domains",
        )
    ]
    domain_ids = [_text(item.get("domain_id"), "source role domain id") for item in domains]
    _unique(
        domain_ids,
        code="source-domain-duplicate",
        label="source_roles.capability_domains",
    )

    all_source_ids: list[str] = []
    topology_summary: dict[str, list[str]] | None = None
    for domain in domains:
        domain_id = _text(domain.get("domain_id"), "source role domain id")
        maximum_primary = int(domain.get("maximum_primary_implementation", 0))
        maximum_supplementary = int(
            domain.get("maximum_supplementary_implementation", 0)
        )
        entries = [
            _mapping(item, f"{domain_id} source entry")
            for item in _sequence(domain.get("entries"), f"{domain_id}.entries")
        ]
        roles: dict[str, list[str]] = {
            "primary_implementation": [],
            "supplementary_implementation": [],
            "zyra_owned_only": [],
        }
        for entry in entries:
            source_id = _text(entry.get("source_id"), f"{domain_id}.source_id")
            source_name = _text(entry.get("source_name"), f"{source_id}.source_name")
            role = _text(entry.get("role"), f"{source_id}.role")
            if role not in ALLOWED_SOURCE_ROLES:
                raise ContractViolation(
                    "source-role-invalid",
                    f"{source_id} uses unsupported source role {role}.",
                    path=f"source_roles.{source_id}.role",
                )
            if source_name.casefold() == "openclaw":
                raise ContractViolation(
                    "openclaw-forward-role-forbidden",
                    "OpenClaw cannot appear in a Phase 2 source-role entry.",
                    path=f"source_roles.{source_id}",
                )
            _commit(entry.get("source_commit"), f"{source_id}.source_commit")
            source_languages = [
                _text(item, f"{source_id}.source_languages")
                for item in _sequence(
                    entry.get("source_languages"),
                    f"{source_id}.source_languages",
                )
            ]
            target_languages = [
                _text(item, f"{source_id}.target_languages")
                for item in _sequence(
                    entry.get("target_languages"),
                    f"{source_id}.target_languages",
                )
            ]
            if not source_languages or not target_languages:
                raise ContractViolation(
                    "source-language-custody-missing",
                    f"{source_id} must declare source and target languages.",
                    path=f"source_roles.{source_id}",
                )
            _text(entry.get("migration_mode"), f"{source_id}.migration_mode")
            target_paths = [
                _relative_path(item, f"{source_id}.target_paths")
                for item in _sequence(
                    entry.get("target_paths"),
                    f"{source_id}.target_paths",
                )
            ]
            if not target_paths:
                raise ContractViolation(
                    "source-target-missing",
                    f"{source_id} must declare at least one Zyra target path.",
                    path=f"source_roles.{source_id}.target_paths",
                )
            if entry.get("proposal_only") is not True:
                raise ContractViolation(
                    "proposal-boundary-missing",
                    f"{source_id} must be proposal-only in Phase 2.",
                    path=f"source_roles.{source_id}.proposal_only",
                )
            roles[role].append(source_id)
            all_source_ids.append(source_id)
        if len(roles["primary_implementation"]) > maximum_primary:
            raise ContractViolation(
                "source-primary-limit-exceeded",
                f"{domain_id} exceeds its primary implementation limit.",
                path=f"source_roles.{domain_id}",
            )
        if len(roles["supplementary_implementation"]) > maximum_supplementary:
            raise ContractViolation(
                "source-supplementary-limit-exceeded",
                f"{domain_id} exceeds its supplementary implementation limit.",
                path=f"source_roles.{domain_id}",
            )
        if domain_id == "topology_proposal":
            topology_summary = roles

    _unique(
        all_source_ids,
        code="source-entry-duplicate",
        label="source_roles.capability_domains.entries",
    )
    if topology_summary is None:
        raise ContractViolation(
            "topology-source-domain-missing",
            "The topology proposal source domain is required.",
            path="source_roles.capability_domains",
        )
    if topology_summary["primary_implementation"] != ["arg_designer"]:
        raise ContractViolation(
            "topology-primary-invalid",
            "ARG-Designer must be the sole topology proposal primary.",
            path="source_roles.topology_proposal",
        )
    if set(topology_summary["supplementary_implementation"]) != {
        "card",
        "agentprune",
    }:
        raise ContractViolation(
            "topology-supplementary-invalid",
            "CARD and AgentPrune must be the only topology supplementary sources.",
            path="source_roles.topology_proposal",
        )

    loopx = _entry_by_source(selected, "loopx")
    if (
        loopx.get("role") != "supplementary_implementation"
        or loopx.get("source_version") != "0.2.13"
        or loopx.get("migration_mode")
        != "pinned_embedded_source_integration"
        or loopx.get("source_commit")
        != "a2c072d412d90839132e1cf39c23dd431c394175"
        or loopx.get("source_tree_commit")
        != "7232dca45ec2ca996edc43b2d3558edc802c844e"
    ):
        raise ContractViolation(
            "loopx-source-contract-invalid",
            "LoopX must remain the pinned v0.2.13 embedded supplementary source integration.",
            path="source_roles.loopx",
        )
    maas = _entry_by_source(selected, "maas")
    if maas.get("role") != "supplementary_implementation":
        raise ContractViolation(
            "maas-source-role-invalid",
            "MaAS must remain supplementary to the Zyra scheduler.",
            path="source_roles.maas",
        )

    excluded_sources = [
        _mapping(item, "excluded source")
        for item in _sequence(
            selected.get("excluded_sources"),
            "source_roles.excluded_sources",
        )
    ]
    if not any(
        item.get("source_name") == "OpenClaw"
        and item.get("status") == "excluded_forward_only"
        for item in excluded_sources
    ):
        raise ContractViolation(
            "openclaw-exclusion-missing",
            "The forward-only OpenClaw exclusion must remain explicit.",
            path="source_roles.excluded_sources",
        )

    return {
        "domain_count": len(domains),
        "source_count": len(all_source_ids),
        "topology_primary": topology_summary["primary_implementation"],
        "topology_supplementary": topology_summary[
            "supplementary_implementation"
        ],
    }


def validate_state_owner_registry(
    registry: Mapping[str, Any],
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    selected = _mapping(registry, "state_owners")
    _require_base_commit(selected, "state_owners")
    if selected.get("registry_mode") != "phase2_constraint_overlay":
        raise ContractViolation(
            "owner-registry-mode-invalid",
            "Phase 2 owner data must be a constraint overlay, not a new state owner.",
            path="state_owners.registry_mode",
        )

    domains = [
        _mapping(item, "state owner domain")
        for item in _sequence(selected.get("state_domains"), "state_owners.state_domains")
    ]
    domain_ids = [_text(item.get("domain_id"), "owner domain id") for item in domains]
    _unique(
        domain_ids,
        code="canonical-owner-domain-duplicate",
        label="state_owners.state_domains",
    )
    required_domains = {
        "task_session_attempt",
        "permission",
        "memory_facts",
        "memory_retrieval_compact",
        "graph_state",
        "resource_placement",
        "scheduler_recovery",
        "worker_lease_physical_attempt",
        "artifact",
        "event_trace",
        "provider_model_cost",
        "execution_budget",
        "loopx_private_control",
    }
    missing_domains = sorted(required_domains - set(domain_ids))
    if missing_domains:
        raise ContractViolation(
            "canonical-owner-domain-missing",
            f"Required canonical owner domains are missing: {', '.join(missing_domains)}.",
            path="state_owners.state_domains",
        )

    owner_by_domain: dict[str, str] = {}
    owner_source_by_domain: dict[str, str] = {}
    for domain in domains:
        domain_id = _text(domain.get("domain_id"), "owner domain id")
        if "additional_canonical_owners" in domain:
            raise ContractViolation(
                "canonical-owner-duplicate",
                f"{domain_id} declares additional canonical owners.",
                path=f"state_owners.{domain_id}",
            )
        owner = _mapping(domain.get("canonical_owner"), f"{domain_id}.canonical_owner")
        owner_id = _text(owner.get("owner_id"), f"{domain_id}.owner_id")
        owner_source = _text(owner.get("source_id"), f"{domain_id}.source_id")
        _text(owner.get("symbol"), f"{domain_id}.symbol")
        implementation_state = _text(
            owner.get("implementation_state"),
            f"{domain_id}.implementation_state",
        )
        target_path = _relative_path(owner.get("path"), f"{domain_id}.path")
        if (
            repository_root is not None
            and implementation_state == "existing"
            and not (repository_root / target_path).exists()
        ):
            raise ContractViolation(
                "canonical-owner-path-missing",
                f"Existing owner path does not exist: {target_path}.",
                path=f"state_owners.{domain_id}.canonical_owner.path",
            )
        if domain.get("proposal_writes_allowed") is not False:
            raise ContractViolation(
                "proposal-write-boundary-invalid",
                f"{domain_id} must reject direct proposal writes.",
                path=f"state_owners.{domain_id}.proposal_writes_allowed",
            )
        owner_by_domain[domain_id] = owner_id
        owner_source_by_domain[domain_id] = owner_source

    mechanism_constraints = {
        _text(item.get("mechanism_id"), "mechanism owner constraint id"): _mapping(
            item,
            "mechanism owner constraint",
        )
        for item in _sequence(
            selected.get("mechanism_constraints"),
            "state_owners.mechanism_constraints",
        )
    }
    for mechanism_id in ("arg_designer", "card", "agentprune", "maas", "loopx"):
        if mechanism_id not in mechanism_constraints:
            raise ContractViolation(
                "mechanism-owner-constraint-missing",
                f"Missing owner constraint for {mechanism_id}.",
                path="state_owners.mechanism_constraints",
            )

    maas_forbidden = set(
        _sequence(
            mechanism_constraints["maas"].get("forbidden_domains"),
            "state_owners.maas.forbidden_domains",
        )
    )
    if not {
        "resource_placement",
        "worker_lease_physical_attempt",
        "execution_budget",
    }.issubset(maas_forbidden):
        raise ContractViolation(
            "maas-owner-boundary-invalid",
            "MaAS must be forbidden from scheduler, lease, and execution budget ownership.",
            path="state_owners.maas",
        )
    loopx_forbidden = set(
        _sequence(
            mechanism_constraints["loopx"].get("forbidden_domains"),
            "state_owners.loopx.forbidden_domains",
        )
    )
    if not {
        "task_session_attempt",
        "graph_state",
        "permission",
        "worker_lease_physical_attempt",
        "execution_budget",
    }.issubset(loopx_forbidden):
        raise ContractViolation(
            "loopx-owner-boundary-invalid",
            "LoopX must be forbidden from Zyra task, graph, permission, lease, and budget ownership.",
            path="state_owners.loopx",
        )

    for domain_id, source_id in owner_source_by_domain.items():
        if source_id in {"arg_designer", "card", "agentprune", "maas"}:
            raise ContractViolation(
                "proposal-source-became-owner",
                f"{source_id} cannot own canonical domain {domain_id}.",
                path=f"state_owners.{domain_id}",
            )
        if source_id == "loopx" and domain_id != "loopx_private_control":
            raise ContractViolation(
                "loopx-owner-scope-exceeded",
                f"LoopX cannot own canonical domain {domain_id}.",
                path=f"state_owners.{domain_id}",
            )

    if owner_source_by_domain["graph_state"] != "zyra":
        raise ContractViolation(
            "graph-owner-invalid",
            "Graph state must remain Zyra-owned.",
            path="state_owners.graph_state",
        )
    if owner_by_domain["resource_placement"] != "resource_scheduler":
        raise ContractViolation(
            "scheduler-owner-invalid",
            "ResourceScheduler must remain the physical placement owner.",
            path="state_owners.resource_placement",
        )
    if owner_source_by_domain["loopx_private_control"] != "loopx":
        raise ContractViolation(
            "loopx-private-owner-invalid",
            "LoopX may own only its private control state.",
            path="state_owners.loopx_private_control",
        )

    evidence_catalog = _mapping(
        selected.get("frozen_owner_evidence_catalog"),
        "state_owners.frozen_owner_evidence_catalog",
    )
    evidence_path = _relative_path(
        evidence_catalog.get("path"),
        "state_owners.frozen_owner_evidence_catalog.path",
    )
    expected_digest = _digest(
        evidence_catalog.get("sha256"),
        "state_owners.frozen_owner_evidence_catalog.sha256",
    )
    if repository_root is not None:
        target = repository_root / evidence_path
        if not target.is_file() or file_digest(target) != expected_digest:
            raise ContractViolation(
                "frozen-owner-catalog-tampered",
                "The frozen first-stage owner evidence catalog is missing or changed.",
                path=evidence_path,
            )
        catalog = _mapping(
            json.loads(target.read_text(encoding="utf-8")),
            "frozen owner evidence catalog",
        )
        actual_bindings = {
            str(item.get("domain")): str(
                _mapping(item.get("owner"), "frozen owner").get("symbol")
            )
            for item in _sequence(catalog.get("owners"), "frozen owner catalog owners")
        }
        for binding_value in _sequence(
            evidence_catalog.get("required_bindings"),
            "state_owners.frozen_owner_evidence_catalog.required_bindings",
        ):
            binding = _mapping(binding_value, "frozen owner binding")
            domain = _text(binding.get("domain"), "frozen owner binding domain")
            symbol = _text(binding.get("symbol"), "frozen owner binding symbol")
            if actual_bindings.get(domain) != symbol:
                raise ContractViolation(
                    "frozen-owner-binding-mismatch",
                    f"Frozen owner binding changed for {domain}.",
                    path=evidence_path,
                )

    return {
        "domain_count": len(domains),
        "canonical_owner_count": len(owner_by_domain),
        "loopx_private_domains": [
            domain for domain, source in owner_source_by_domain.items() if source == "loopx"
        ],
        "proposal_owner_count": sum(
            source in {"arg_designer", "card", "agentprune", "maas"}
            for source in owner_source_by_domain.values()
        ),
    }


def compute_frozen_gate_digest(contract: Mapping[str, Any]) -> str:
    selected = _mapping(contract, "activation_gates")
    frozen_payload = {
        "schema": selected.get("schema"),
        "p2_base_commit": selected.get("p2_base_commit"),
        "gate_revision": selected.get("gate_revision"),
        "readiness": selected.get("readiness"),
        "profiles": selected.get("profiles"),
        "activation_rules": selected.get("activation_rules"),
        "no_policy_training": selected.get("no_policy_training"),
        "evidence_contracts": selected.get("evidence_contracts"),
        "metrics": selected.get("metrics"),
    }
    return canonical_digest(frozen_payload)


def _validate_no_policy_training(contract: Mapping[str, Any]) -> None:
    policy = _mapping(
        contract.get("no_policy_training"),
        "activation_gates.no_policy_training",
    )
    for flag in (
        "training_allowed",
        "fine_tuning_allowed",
        "online_learning_allowed",
        "policy_gradient_allowed",
        "textual_gradient_allowed",
        "variable_learned_parameters_allowed",
    ):
        if policy.get(flag) is not False:
            raise ContractViolation(
                "no-policy-training-flag-invalid",
                f"{flag} must remain false.",
                path=f"activation_gates.no_policy_training.{flag}",
            )
    for field in (
        "runtime_entry_points",
        "datasets",
        "checkpoints",
        "mutable_learned_parameters",
    ):
        values = _sequence(
            policy.get(field),
            f"activation_gates.no_policy_training.{field}",
        )
        if values:
            raise ContractViolation(
                "no-policy-training-artifact-present",
                f"{field} must remain empty.",
                path=f"activation_gates.no_policy_training.{field}",
            )
    declared_tokens = {
        str(item).casefold()
        for item in _sequence(
            policy.get("forbidden_entry_tokens"),
            "activation_gates.no_policy_training.forbidden_entry_tokens",
        )
    }
    if not FORBIDDEN_TRAINING_TOKENS.issubset(declared_tokens):
        raise ContractViolation(
            "no-policy-training-token-set-weakened",
            "The forbidden training entry token set was weakened.",
            path="activation_gates.no_policy_training.forbidden_entry_tokens",
        )


def validate_activation_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    selected = _mapping(contract, "activation_gates")
    _require_base_commit(selected, "activation_gates")
    if selected.get("frozen") is not True:
        raise ContractViolation(
            "activation-contract-not-frozen",
            "Activation gates must remain frozen.",
            path="activation_gates.frozen",
        )

    expected_gate_digest = _digest(
        selected.get("frozen_gate_digest"),
        "activation_gates.frozen_gate_digest",
    )
    observed_gate_digest = compute_frozen_gate_digest(selected)
    if observed_gate_digest != expected_gate_digest:
        supersession = _mapping(
            selected.get("supersession"),
            "activation_gates.supersession",
        )
        if not supersession.get("superseding_adr"):
            raise ContractViolation(
                "frozen-gate-modified-without-adr",
                "A frozen gate changed without a superseding ADR.",
                path="activation_gates.metrics",
            )
        raise ContractViolation(
            "frozen-gate-digest-stale",
            "A superseding ADR was declared, but the frozen gate digest was not updated.",
            path="activation_gates.frozen_gate_digest",
        )

    readiness = _mapping(selected.get("readiness"), "activation_gates.readiness")
    allowed_statuses = set(
        _sequence(
            readiness.get("allowed_statuses"),
            "activation_gates.readiness.allowed_statuses",
        )
    )
    if allowed_statuses != {status.value for status in MechanismReadinessStatus}:
        raise ContractViolation(
            "readiness-status-set-invalid",
            "Readiness statuses must be deterministic_ready, evidence_only, and unavailable.",
            path="activation_gates.readiness.allowed_statuses",
        )
    mechanism_values = [
        _mapping(item, "mechanism readiness")
        for item in _sequence(
            readiness.get("mechanisms"),
            "activation_gates.readiness.mechanisms",
        )
    ]
    mechanism_ids = [
        _text(item.get("mechanism_id"), "mechanism readiness id")
        for item in mechanism_values
    ]
    _unique(
        mechanism_ids,
        code="mechanism-readiness-duplicate",
        label="activation_gates.readiness.mechanisms",
    )
    if set(mechanism_ids) != set(EXPECTED_REQUIRED_MECHANISMS):
        raise ContractViolation(
            "mechanism-readiness-set-invalid",
            "Readiness must be declared exactly for LoopX, ARG, CARD, AgentPrune, and MaAS.",
            path="activation_gates.readiness.mechanisms",
        )
    statuses = {
        _text(item.get("mechanism_id"), "mechanism id"): parse_mechanism_status(
            item.get("status")
        )
        for item in mechanism_values
    }
    for item in mechanism_values:
        _text(item.get("report_ref"), "mechanism readiness report ref")
        _text(item.get("fallback_profile"), "mechanism fallback profile")

    profiles = [
        _mapping(item, "activation profile")
        for item in _sequence(selected.get("profiles"), "activation_gates.profiles")
    ]
    profile_ids = [_text(item.get("profile_id"), "profile id") for item in profiles]
    _unique(
        profile_ids,
        code="activation-profile-duplicate",
        label="activation_gates.profiles",
    )
    by_profile = {str(item["profile_id"]): item for item in profiles}
    for profile in profiles:
        lifecycle = _text(profile.get("lifecycle"), "profile lifecycle")
        if lifecycle not in ALLOWED_PROFILE_LIFECYCLES:
            raise ContractViolation(
                "profile-lifecycle-invalid",
                f"Unsupported profile lifecycle: {lifecycle}.",
                path=f"activation_gates.{profile.get('profile_id')}",
            )
    if "phase1_deterministic_baseline" not in by_profile:
        raise ContractViolation(
            "baseline-profile-missing",
            "The frozen Phase 1 deterministic baseline profile is required.",
            path="activation_gates.profiles",
        )
    strongest = _mapping(
        by_profile.get("phase2_strongest_v1"),
        "activation_gates.phase2_strongest_v1",
    )
    strongest_required = list(
        _sequence(
            strongest.get("required_mechanisms"),
            "activation_gates.phase2_strongest_v1.required_mechanisms",
        )
    )
    if strongest_required != list(EXPECTED_REQUIRED_MECHANISMS):
        raise ContractViolation(
            "strongest-mechanism-order-invalid",
            "The strongest profile must preserve the fixed mechanism order.",
            path="activation_gates.phase2_strongest_v1.required_mechanisms",
        )
    _text(
        strongest.get("fallback_profile"),
        "activation_gates.phase2_strongest_v1.fallback_profile",
    )
    strongest_requests_activation = (
        strongest.get("activation_state") == "active"
        or strongest.get("lifecycle") == "default"
    )
    not_ready = [
        mechanism_id
        for mechanism_id in strongest_required
        if statuses[mechanism_id] is not MechanismReadinessStatus.DETERMINISTIC_READY
    ]
    if strongest_requests_activation and not_ready:
        raise ContractViolation(
            "strongest-profile-readiness-failed",
            "The strongest profile cannot activate while required mechanisms are "
            f"not deterministic_ready: {', '.join(not_ready)}.",
            path="activation_gates.phase2_strongest_v1",
        )

    _validate_no_policy_training(selected)

    evidence_contracts = [
        _mapping(item, "evidence contract")
        for item in _sequence(
            selected.get("evidence_contracts"),
            "activation_gates.evidence_contracts",
        )
    ]
    evidence_contract_ids = [
        _text(item.get("contract_id"), "evidence contract id")
        for item in evidence_contracts
    ]
    _unique(
        evidence_contract_ids,
        code="evidence-contract-duplicate",
        label="activation_gates.evidence_contracts",
    )
    for evidence_contract in evidence_contracts:
        contract_id = _text(
            evidence_contract.get("contract_id"),
            "evidence contract id",
        )
        _text(evidence_contract.get("evidence_unit"), f"{contract_id}.evidence_unit")
        required_fields = _sequence(
            evidence_contract.get("required_fields"),
            f"{contract_id}.required_fields",
        )
        if not required_fields:
            raise ContractViolation(
                "evidence-contract-fields-missing",
                f"{contract_id} has no required fields.",
                path=f"activation_gates.evidence_contracts.{contract_id}",
            )

    metrics = [
        _mapping(item, "metric contract")
        for item in _sequence(selected.get("metrics"), "activation_gates.metrics")
    ]
    metric_ids = [_text(item.get("metric_id"), "metric id") for item in metrics]
    _unique(
        metric_ids,
        code="metric-duplicate",
        label="activation_gates.metrics",
    )
    hard_gate_count = 0
    for metric in metrics:
        metric_id = _text(metric.get("metric_id"), "metric id")
        for field in ("unit", "evidence_unit", "sample_unit"):
            _text(metric.get(field), f"{metric_id}.{field}")
        direction = _text(metric.get("direction"), f"{metric_id}.direction")
        if direction not in ALLOWED_METRIC_DIRECTIONS:
            raise ContractViolation(
                "metric-direction-invalid",
                f"{metric_id} uses unsupported direction {direction}.",
                path=f"activation_gates.metrics.{metric_id}.direction",
            )
        empty_semantics = _text(
            metric.get("empty_sample_semantics"),
            f"{metric_id}.empty_sample_semantics",
        )
        if empty_semantics not in ALLOWED_EMPTY_SAMPLE_SEMANTICS:
            raise ContractViolation(
                "metric-empty-sample-invalid",
                f"{metric_id} uses unsupported empty-sample semantics.",
                path=f"activation_gates.metrics.{metric_id}",
            )
        if "threshold" not in metric:
            raise ContractViolation(
                "metric-threshold-missing",
                f"{metric_id} has no threshold.",
                path=f"activation_gates.metrics.{metric_id}",
            )
        requirement_ids = list(
            _sequence(
                metric.get("requirement_ids"),
                f"{metric_id}.requirement_ids",
            )
        )
        metric_evidence_contracts = list(
            _sequence(
                metric.get("evidence_contract_ids"),
                f"{metric_id}.evidence_contract_ids",
            )
        )
        unknown_contracts = sorted(
            set(metric_evidence_contracts) - set(evidence_contract_ids)
        )
        if unknown_contracts:
            raise ContractViolation(
                "metric-evidence-contract-unknown",
                f"{metric_id} references unknown evidence contracts: "
                f"{', '.join(unknown_contracts)}.",
                path=f"activation_gates.metrics.{metric_id}",
            )
        if metric.get("hard_gate") is True:
            hard_gate_count += 1
            if not requirement_ids or not metric_evidence_contracts:
                raise ContractViolation(
                    "hard-gate-traceability-missing",
                    f"{metric_id} must trace to requirement IDs and evidence contracts.",
                    path=f"activation_gates.metrics.{metric_id}",
                )
            if empty_semantics != "fail_closed":
                raise ContractViolation(
                    "hard-gate-empty-sample-not-closed",
                    f"{metric_id} must fail closed on an empty sample.",
                    path=f"activation_gates.metrics.{metric_id}",
                )

    return {
        "mechanism_statuses": {
            mechanism_id: status.value for mechanism_id, status in statuses.items()
        },
        "strongest_activation_eligible": not not_ready,
        "strongest_activation_blockers": not_ready,
        "profile_count": len(profiles),
        "metric_count": len(metrics),
        "hard_gate_count": hard_gate_count,
        "frozen_gate_digest": observed_gate_digest,
    }


@dataclass(frozen=True)
class Phase2PolicyContractPaths:
    repository_root: Path
    source_roles: Path
    state_owners: Path
    activation_gates: Path
    digest_manifest: Path

    @classmethod
    def for_repository(
        cls,
        repository_root: Path,
        *,
        config_root: Path | None = None,
    ) -> "Phase2PolicyContractPaths":
        root = repository_root.resolve()
        selected_config_root = (
            config_root.resolve() if config_root is not None else root / "config" / "phase2"
        )
        return cls(
            repository_root=root,
            source_roles=selected_config_root / "source-roles.yaml",
            state_owners=selected_config_root / "state-owners.yaml",
            activation_gates=selected_config_root / "activation-gates.yaml",
            digest_manifest=selected_config_root / "contract-digests.json",
        )


@dataclass(frozen=True)
class PolicyContractValidationReport:
    valid: bool
    p2_base_commit: str
    source_roles: Mapping[str, Any]
    state_owners: Mapping[str, Any]
    activation_gates: Mapping[str, Any]
    digests: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-policy-contract-validation/v1",
            "valid": self.valid,
            "p2_base_commit": self.p2_base_commit,
            "source_roles": dict(self.source_roles),
            "state_owners": dict(self.state_owners),
            "activation_gates": dict(self.activation_gates),
            "digests": dict(self.digests),
        }


class Phase2PolicyContractBundle:
    """Load and validate immutable Phase 2 policy governance contracts."""

    def __init__(
        self,
        paths: Phase2PolicyContractPaths,
        *,
        source_roles: Mapping[str, Any],
        state_owners: Mapping[str, Any],
        activation_gates: Mapping[str, Any],
        digest_manifest: Mapping[str, Any],
    ) -> None:
        self.paths = paths
        self.source_roles = source_roles
        self.state_owners = state_owners
        self.activation_gates = activation_gates
        self.digest_manifest = digest_manifest

    @staticmethod
    def _load_json_yaml(path: Path) -> Mapping[str, Any]:
        if not path.is_file():
            raise ContractViolation(
                "contract-file-missing",
                f"Phase 2 policy contract file is missing: {path}.",
                path=str(path),
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractViolation(
                "contract-file-invalid",
                f"Phase 2 policy contract is not valid JSON-compatible YAML: {path}.",
                path=str(path),
            ) from exc
        return _mapping(value, str(path))

    @classmethod
    def load(
        cls,
        repository_root: Path,
        *,
        config_root: Path | None = None,
    ) -> "Phase2PolicyContractBundle":
        paths = Phase2PolicyContractPaths.for_repository(
            repository_root,
            config_root=config_root,
        )
        return cls(
            paths,
            source_roles=cls._load_json_yaml(paths.source_roles),
            state_owners=cls._load_json_yaml(paths.state_owners),
            activation_gates=cls._load_json_yaml(paths.activation_gates),
            digest_manifest=cls._load_json_yaml(paths.digest_manifest),
        )

    def _validate_digest_manifest(self) -> dict[str, str]:
        manifest = _mapping(self.digest_manifest, "contract_digests")
        _require_base_commit(manifest, "contract_digests")
        document_values = [
            _mapping(item, "ADR digest entry")
            for item in _sequence(manifest.get("documents"), "contract_digests.documents")
        ]
        document_ids = [_text(item.get("adr_id"), "ADR id") for item in document_values]
        if tuple(document_ids) != EXPECTED_ADR_IDS:
            raise ContractViolation(
                "adr-set-invalid",
                "The digest manifest must contain P2-ADR-001 through P2-ADR-007 in order.",
                path="contract_digests.documents",
            )
        config_values = [
            _mapping(item, "config digest entry")
            for item in _sequence(manifest.get("configs"), "contract_digests.configs")
        ]
        config_ids = [_text(item.get("config_id"), "config id") for item in config_values]
        if tuple(config_ids) != EXPECTED_CONFIG_IDS:
            raise ContractViolation(
                "config-digest-set-invalid",
                "The digest manifest must contain all three Phase 2 registries.",
                path="contract_digests.configs",
            )

        observed: dict[str, str] = {}
        config_targets = {
            "source_roles": self.paths.source_roles,
            "state_owners": self.paths.state_owners,
            "activation_gates": self.paths.activation_gates,
        }
        for item in (*document_values, *config_values):
            identity = str(item.get("adr_id") or item.get("config_id"))
            relative = _relative_path(item.get("path"), f"{identity}.path")
            expected = _digest(item.get("sha256"), f"{identity}.sha256")
            target = config_targets.get(identity, self.paths.repository_root / relative)
            if not target.is_file():
                raise ContractViolation(
                    "digest-target-missing",
                    f"Digest target is missing: {relative}.",
                    path=relative,
                )
            actual = file_digest(target)
            if actual != expected:
                raise ContractViolation(
                    "contract-digest-mismatch",
                    f"Contract digest mismatch for {identity}.",
                    path=relative,
                )
            observed[identity] = actual
        return observed

    def validate(self) -> PolicyContractValidationReport:
        source_summary = validate_source_role_registry(self.source_roles)
        owner_summary = validate_state_owner_registry(
            self.state_owners,
            repository_root=self.paths.repository_root,
        )
        activation_summary = validate_activation_contract(self.activation_gates)
        digests = self._validate_digest_manifest()
        return PolicyContractValidationReport(
            valid=True,
            p2_base_commit=P2_BASE_COMMIT,
            source_roles=source_summary,
            state_owners=owner_summary,
            activation_gates=activation_summary,
            digests=digests,
        )

    def require_strongest_activation(self) -> PolicyContractValidationReport:
        report = self.validate()
        if not report.activation_gates["strongest_activation_eligible"]:
            blockers = ", ".join(
                report.activation_gates["strongest_activation_blockers"]
            )
            raise ContractViolation(
                "strongest-profile-readiness-failed",
                f"phase2_strongest_v1 is blocked by: {blockers}.",
                path="activation_gates.phase2_strongest_v1",
            )
        return report
