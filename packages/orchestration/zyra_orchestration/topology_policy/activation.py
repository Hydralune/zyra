from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import FrozenDict, canonical_digest, freeze_json, thaw_json
from .registry import (
    MechanismLifecycle,
    MechanismRegistration,
    MechanismRegistry,
    NoPolicyTrainingDeclaration,
    PolicyRegistryError,
    ReadinessStage,
    ReadinessStatus,
    RegistryTransition,
    ValidationManifest,
)


_FORBIDDEN_DEPENDENCIES = {
    "torch",
    "tensorflow",
    "jax",
    "trl",
    "datasets",
    "stable-baselines3",
}
_FORBIDDEN_PATH_PARTS = {
    "train",
    "trainer",
    "training",
    "dataset",
    "datasets",
    "checkpoint",
    "checkpoints",
    "learned_parameter",
    "learned_parameters",
    "policy_gradient",
    "textual_gradient",
}
_FORBIDDEN_FILE_NAMES = {
    "train.py",
    "trainer.py",
    "training.py",
}


@dataclass(frozen=True, slots=True)
class ActivationEvidence:
    evidence_id: str
    family: str
    version: str
    schema_digest: str
    config_digest: str
    source_digest: str
    implementation_commit: str
    evidence_commit: str
    readiness_digest: str
    hard_gates: FrozenDict
    audits: FrozenDict
    rollback_family: str
    rollback_version: str
    no_policy_training_passed: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ActivationEvidence:
        hard_gates = freeze_json(_mapping(value.get("hard_gates"), "hard_gates"))
        audits = freeze_json(_mapping(value.get("audits"), "audits"))
        if not isinstance(hard_gates, FrozenDict) or not isinstance(
            audits,
            FrozenDict,
        ):
            raise PolicyRegistryError(
                "activation-evidence-invalid",
                "Activation gates and audits must be objects.",
            )
        return cls(
            evidence_id=_text(value.get("evidence_id"), "evidence_id"),
            family=_text(value.get("family"), "family"),
            version=_text(value.get("version"), "version"),
            schema_digest=_text(
                value.get("schema_digest"),
                "schema_digest",
            ),
            config_digest=_text(
                value.get("config_digest"),
                "config_digest",
            ),
            source_digest=_text(
                value.get("source_digest"),
                "source_digest",
            ),
            implementation_commit=_text(
                value.get("implementation_commit"),
                "implementation_commit",
            ),
            evidence_commit=_text(
                value.get("evidence_commit"),
                "evidence_commit",
            ),
            readiness_digest=_text(
                value.get("readiness_digest"),
                "readiness_digest",
            ),
            hard_gates=hard_gates,
            audits=audits,
            rollback_family=_text(
                value.get("rollback_family"),
                "rollback_family",
            ),
            rollback_version=_text(
                value.get("rollback_version"),
                "rollback_version",
            ),
            no_policy_training_passed=(
                value.get("no_policy_training_passed") is True
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "family": self.family,
            "version": self.version,
            "schema_digest": self.schema_digest,
            "config_digest": self.config_digest,
            "source_digest": self.source_digest,
            "implementation_commit": self.implementation_commit,
            "evidence_commit": self.evidence_commit,
            "readiness_digest": self.readiness_digest,
            "hard_gates": thaw_json(self.hard_gates),
            "audits": thaw_json(self.audits),
            "rollback_family": self.rollback_family,
            "rollback_version": self.rollback_version,
            "no_policy_training_passed": self.no_policy_training_passed,
        }


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    eligible: bool
    family: str
    version: str
    registration_digest: str
    schema_digest: str
    config_digest: str
    source_digest: str
    evidence_digest: str
    blockers: tuple[str, ...]
    passed_gates: tuple[str, ...]

    @property
    def decision_digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-policy-activation-decision/v1",
            "eligible": self.eligible,
            "family": self.family,
            "version": self.version,
            "registration_digest": self.registration_digest,
            "schema_digest": self.schema_digest,
            "config_digest": self.config_digest,
            "source_digest": self.source_digest,
            "evidence_digest": self.evidence_digest,
            "blockers": list(self.blockers),
            "passed_gates": list(self.passed_gates),
        }


class NoPolicyTrainingValidator:
    """Rejects training lifecycle artifacts from registry and release inputs."""

    def validate_registration(
        self,
        registration: MechanismRegistration,
    ) -> None:
        self.validate_declaration(registration.no_policy_training)

    def validate_declaration(
        self,
        declaration: NoPolicyTrainingDeclaration,
    ) -> None:
        populated = {
            "runtime_entry_points": declaration.runtime_entry_points,
            "datasets": declaration.datasets,
            "checkpoints": declaration.checkpoints,
            "mutable_learned_parameters": (
                declaration.mutable_learned_parameters
            ),
        }
        violations = [name for name, values in populated.items() if values]
        if violations:
            raise PolicyRegistryError(
                "no-policy-training-artifact-present",
                "Training lifecycle artifacts are forbidden: "
                + ", ".join(violations),
            )
        self._validate_dependencies(declaration.dependencies)

    def validate_release_inputs(
        self,
        repository_root: Path,
        *,
        release_paths: Sequence[str],
        dependencies: Sequence[str],
    ) -> None:
        root = repository_root.resolve()
        violations: list[str] = []
        for raw_path in release_paths:
            path = Path(str(raw_path).replace("\\", "/"))
            candidate = (root / path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                violations.append(f"path_escape:{raw_path}")
                continue
            lowered_parts = {part.casefold() for part in path.parts}
            name = path.name.casefold()
            if (
                name in _FORBIDDEN_FILE_NAMES
                or name.startswith("train_")
                or name.startswith("training_")
                or name.startswith("trainer")
                or lowered_parts & _FORBIDDEN_PATH_PARTS
            ):
                violations.append(str(raw_path))
        if violations:
            raise PolicyRegistryError(
                "no-policy-training-release-path",
                "Training paths are forbidden in the policy release: "
                + ", ".join(sorted(violations)),
            )
        self._validate_dependencies(tuple(str(item) for item in dependencies))

    @staticmethod
    def _validate_dependencies(dependencies: Sequence[str]) -> None:
        normalized = {
            re.split(r"[\[<>=!~ ]", item.casefold(), maxsplit=1)[0]
            for item in dependencies
        }
        blocked = sorted(normalized & _FORBIDDEN_DEPENDENCIES)
        if blocked:
            raise PolicyRegistryError(
                "no-policy-training-dependency-present",
                "Training dependencies are forbidden: " + ", ".join(blocked),
            )


class StrongestProfileActivationGate:
    def __init__(
        self,
        *,
        no_training: NoPolicyTrainingValidator | None = None,
    ) -> None:
        self.no_training = no_training or NoPolicyTrainingValidator()

    def evaluate(
        self,
        registry: MechanismRegistry,
        *,
        family: str,
        version: str,
        evidence: ActivationEvidence,
    ) -> ActivationDecision:
        record = registry.get(family, version)
        blockers: list[str] = []
        passed: list[str] = []

        if record.profile_id != "phase2_strongest_v1":
            blockers.append("profile_id")
        else:
            passed.append("profile_id")
        if record.lifecycle is not MechanismLifecycle.VALIDATION:
            blockers.append("lifecycle_validation")
        else:
            passed.append("lifecycle_validation")
        if record.activation_state != "validation_ready":
            blockers.append("validation_state")
        else:
            passed.append("validation_state")

        expected_values = {
            "family": record.family,
            "version": record.version,
            "schema_digest": record.schema_digest,
            "config_digest": record.config_digest,
            "source_digest": record.source_digest,
            "implementation_commit": record.implementation_commit,
            "evidence_commit": record.evidence_commit,
            "readiness_digest": record.readiness_digest,
            "rollback_family": record.rollback_family,
            "rollback_version": record.rollback_version,
        }
        evidence_values = {
            "family": evidence.family,
            "version": evidence.version,
            "schema_digest": evidence.schema_digest,
            "config_digest": evidence.config_digest,
            "source_digest": evidence.source_digest,
            "implementation_commit": evidence.implementation_commit,
            "evidence_commit": evidence.evidence_commit,
            "readiness_digest": evidence.readiness_digest,
            "rollback_family": evidence.rollback_family,
            "rollback_version": evidence.rollback_version,
        }
        for name, expected in expected_values.items():
            if evidence_values[name] != expected:
                blockers.append(f"digest_or_identity:{name}")
            else:
                passed.append(f"digest_or_identity:{name}")

        readiness_blockers = [
            item.mechanism_id
            for item in record.readiness
            if item.stage is not ReadinessStage.ACTIVATION_READY
            or item.status is not ReadinessStatus.DETERMINISTIC_READY
        ]
        if readiness_blockers:
            blockers.extend(
                f"readiness:{item}" for item in readiness_blockers
            )
        else:
            passed.append("readiness")

        hard_gates = thaw_json(evidence.hard_gates)
        for gate_id in record.hard_gate_ids:
            if hard_gates.get(gate_id) is not True:
                blockers.append(f"hard_gate:{gate_id}")
            else:
                passed.append(f"hard_gate:{gate_id}")

        audits = thaw_json(evidence.audits)
        for gate_id in record.audit_gate_ids:
            if audits.get(gate_id) is not True:
                blockers.append(f"audit:{gate_id}")
            else:
                passed.append(f"audit:{gate_id}")

        try:
            rollback = registry.get(
                record.rollback_family,
                record.rollback_version,
            )
            if (
                record.rollback_family != family
                or rollback.lifecycle is MechanismLifecycle.RETIRED
                or (
                    rollback.lifecycle is not MechanismLifecycle.BASELINE
                    and rollback.activation_state != "rollback_ready"
                )
            ):
                blockers.append("rollback_target")
            else:
                passed.append("rollback_target")
        except PolicyRegistryError:
            blockers.append("rollback_target")

        if not evidence.no_policy_training_passed:
            blockers.append("no_policy_training_evidence")
        else:
            try:
                self.no_training.validate_registration(record)
            except PolicyRegistryError:
                blockers.append("no_policy_training_registry")
            else:
                passed.extend(
                    [
                        "no_policy_training_evidence",
                        "no_policy_training_registry",
                    ]
                )

        return ActivationDecision(
            eligible=not blockers,
            family=record.family,
            version=record.version,
            registration_digest=record.registration_digest,
            schema_digest=record.schema_digest,
            config_digest=record.config_digest,
            source_digest=record.source_digest,
            evidence_digest=canonical_digest(evidence.to_dict()),
            blockers=tuple(sorted(set(blockers))),
            passed_gates=tuple(sorted(set(passed))),
        )

    def activate(
        self,
        registry: MechanismRegistry,
        *,
        family: str,
        version: str,
        evidence: ActivationEvidence,
    ) -> tuple[ActivationDecision, RegistryTransition]:
        decision = self.evaluate(
            registry,
            family=family,
            version=version,
            evidence=evidence,
        )
        if not decision.eligible:
            raise PolicyRegistryError(
                "strongest-activation-gate-failed",
                "Strongest activation failed: " + ", ".join(decision.blockers),
            )
        transition = registry.activate_default(
            family,
            version,
            activation_decision=decision,
        )
        return decision, transition


class PolicyActivationRuntime:
    """Lifecycle mutation facade that writes through the existing event owner."""

    def __init__(
        self,
        registry: MechanismRegistry,
        *,
        admit_event: Callable[[Any], None],
        gate: StrongestProfileActivationGate | None = None,
    ) -> None:
        self.registry = registry
        self.admit_event = admit_event
        self.gate = gate or StrongestProfileActivationGate()

    def enter_validation(
        self,
        *,
        run_id: str,
        task_id: str,
        family: str,
        version: str,
        manifest: ValidationManifest,
    ) -> RegistryTransition:
        transition = self.registry.enter_validation(
            family,
            version,
            manifest=manifest,
        )
        self._publish(transition, run_id=run_id, task_id=task_id)
        return transition

    def activate_default(
        self,
        *,
        run_id: str,
        task_id: str,
        family: str,
        version: str,
        evidence: ActivationEvidence,
    ) -> tuple[ActivationDecision, RegistryTransition]:
        decision, transition = self.gate.activate(
            self.registry,
            family=family,
            version=version,
            evidence=evidence,
        )
        self._publish(transition, run_id=run_id, task_id=task_id)
        return decision, transition

    def rollback(
        self,
        *,
        run_id: str,
        task_id: str,
        family: str,
        target_version: str | None = None,
    ) -> RegistryTransition:
        transition = self.registry.rollback(
            family,
            target_version=target_version,
        )
        self._publish(transition, run_id=run_id, task_id=task_id)
        return transition

    def retire(
        self,
        *,
        run_id: str,
        task_id: str,
        family: str,
        version: str,
    ) -> RegistryTransition:
        transition = self.registry.retire(family, version)
        self._publish(transition, run_id=run_id, task_id=task_id)
        return transition

    def _publish(
        self,
        transition: RegistryTransition,
        *,
        run_id: str,
        task_id: str,
    ) -> None:
        self.admit_event(
            transition.to_event(run_id=run_id, task_id=task_id)
        )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyRegistryError(
            "activation-mapping-required",
            f"{label} must be an object.",
        )
    return value


def _text(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if not selected:
        raise PolicyRegistryError(
            "activation-text-required",
            f"{label} must be non-empty.",
        )
    return selected


__all__ = [
    "ActivationDecision",
    "ActivationEvidence",
    "NoPolicyTrainingValidator",
    "PolicyActivationRuntime",
    "StrongestProfileActivationGate",
]
