from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType

from .contracts import FrozenDict, canonical_digest, freeze_json, thaw_json


POLICY_REGISTRY_SCHEMA = "zyra.phase2-policy-registry/v1"
POLICY_REGISTRY_CONFIG = Path("config/phase2/policies.yaml")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class PolicyRegistryError(ValueError):
    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class MechanismLifecycle(StrEnum):
    BASELINE = "baseline"
    VALIDATION = "validation"
    DEFAULT = "default"
    DIAGNOSTIC = "diagnostic"
    RETIRED = "retired"


class ResolutionPurpose(StrEnum):
    NORMAL = "normal"
    VALIDATION = "validation"
    DIAGNOSTIC = "diagnostic"


class ReadinessStage(StrEnum):
    INPUT_PRECHECK = "input_precheck"
    IMPLEMENTATION_VALIDATED = "implementation_validated"
    ACTIVATION_READY = "activation_ready"


class ReadinessStatus(StrEnum):
    DETERMINISTIC_READY = "deterministic_ready"
    EVIDENCE_ONLY = "evidence_only"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ValidationManifest:
    manifest_id: str
    scenario_id: str
    isolated: bool
    purpose: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ValidationManifest:
        return cls(
            manifest_id=_text(value.get("manifest_id"), "validation.manifest_id"),
            scenario_id=_text(value.get("scenario_id"), "validation.scenario_id"),
            isolated=value.get("isolated") is True,
            purpose=_text(value.get("purpose"), "validation.purpose"),
        )

    def validate(self) -> None:
        if not self.isolated:
            raise PolicyRegistryError(
                "validation-not-isolated",
                "Validation requires an explicitly isolated scenario or preflight manifest.",
            )
        if self.purpose not in {"scenario", "preflight"}:
            raise PolicyRegistryError(
                "validation-purpose-invalid",
                "Validation manifests are limited to scenario or preflight.",
            )


@dataclass(frozen=True, slots=True)
class ReadinessBinding:
    mechanism_id: str
    stage: ReadinessStage
    status: ReadinessStatus
    report_ref: str
    report_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ReadinessBinding:
        try:
            stage = ReadinessStage(
                _text(value.get("stage"), "readiness.stage")
            )
            status = ReadinessStatus(
                _text(value.get("status"), "readiness.status")
            )
        except ValueError as exc:
            raise PolicyRegistryError(
                "readiness-value-invalid",
                "A readiness binding has an unsupported stage or status.",
            ) from exc
        report_ref = _text(value.get("report_ref"), "readiness.report_ref")
        report_digest = str(value.get("report_digest") or "")
        if report_ref.startswith("pending:"):
            if status is not ReadinessStatus.UNAVAILABLE:
                raise PolicyRegistryError(
                    "pending-readiness-not-unavailable",
                    "A pending readiness report must remain unavailable.",
                )
        else:
            _digest(report_digest, "readiness.report_digest")
        return cls(
            mechanism_id=_text(
                value.get("mechanism_id"),
                "readiness.mechanism_id",
            ),
            stage=stage,
            status=status,
            report_ref=report_ref,
            report_digest=report_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": self.mechanism_id,
            "stage": self.stage.value,
            "status": self.status.value,
            "report_ref": self.report_ref,
            "report_digest": self.report_digest,
        }


@dataclass(frozen=True, slots=True)
class NoPolicyTrainingDeclaration:
    runtime_entry_points: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    checkpoints: tuple[str, ...] = ()
    mutable_learned_parameters: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> NoPolicyTrainingDeclaration:
        return cls(
            runtime_entry_points=_strings(
                value.get("runtime_entry_points"),
                "no_policy_training.runtime_entry_points",
            ),
            datasets=_strings(
                value.get("datasets"),
                "no_policy_training.datasets",
            ),
            checkpoints=_strings(
                value.get("checkpoints"),
                "no_policy_training.checkpoints",
            ),
            mutable_learned_parameters=_strings(
                value.get("mutable_learned_parameters"),
                "no_policy_training.mutable_learned_parameters",
            ),
            dependencies=_strings(
                value.get("dependencies"),
                "no_policy_training.dependencies",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_entry_points": list(self.runtime_entry_points),
            "datasets": list(self.datasets),
            "checkpoints": list(self.checkpoints),
            "mutable_learned_parameters": list(
                self.mutable_learned_parameters
            ),
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class MechanismRegistration:
    family: str
    version: str
    profile_id: str
    lifecycle: MechanismLifecycle
    activation_state: str
    schema_version: str
    schema_digest: str
    source_digest: str
    configuration: FrozenDict
    config_digest: str
    implementation_commit: str
    evidence_commit: str
    required_mechanisms: tuple[str, ...]
    readiness: tuple[ReadinessBinding, ...]
    rollback_family: str
    rollback_version: str
    timeout_seconds: float
    no_policy_training: NoPolicyTrainingDeclaration
    hard_gate_ids: tuple[str, ...]
    audit_gate_ids: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MechanismRegistration:
        try:
            lifecycle = MechanismLifecycle(
                _text(value.get("lifecycle"), "registration.lifecycle")
            )
        except ValueError as exc:
            raise PolicyRegistryError(
                "lifecycle-invalid",
                "A mechanism registration has an unsupported lifecycle.",
            ) from exc
        configuration = freeze_json(
            _mapping(value.get("configuration"), "registration.configuration")
        )
        if not isinstance(configuration, FrozenDict):
            raise PolicyRegistryError(
                "configuration-invalid",
                "Mechanism configuration must be an object.",
            )
        config_digest = _digest(
            value.get("config_digest"),
            "registration.config_digest",
        )
        if canonical_digest(thaw_json(configuration)) != config_digest:
            raise PolicyRegistryError(
                "config-digest-mismatch",
                "Mechanism configuration digest does not match its content.",
            )
        readiness = tuple(
            ReadinessBinding.from_mapping(
                _mapping(item, "registration.readiness[]")
            )
            for item in _sequence(
                value.get("readiness"),
                "registration.readiness",
            )
        )
        readiness_ids = [item.mechanism_id for item in readiness]
        if len(set(readiness_ids)) != len(readiness_ids):
            raise PolicyRegistryError(
                "readiness-duplicate",
                "A mechanism registration contains duplicate readiness entries.",
            )
        required_mechanisms = _strings(
            value.get("required_mechanisms"),
            "registration.required_mechanisms",
        )
        if set(required_mechanisms) != set(readiness_ids):
            if required_mechanisms or readiness_ids:
                raise PolicyRegistryError(
                    "readiness-required-set-mismatch",
                    "Required mechanisms and readiness bindings differ.",
                )
        timeout_seconds = float(value.get("timeout_seconds") or 0)
        if timeout_seconds <= 0:
            raise PolicyRegistryError(
                "timeout-invalid",
                "Mechanism timeout must be positive.",
            )
        training = NoPolicyTrainingDeclaration.from_mapping(
            _mapping(
                value.get("no_policy_training"),
                "registration.no_policy_training",
            )
        )
        registration = cls(
            family=_text(value.get("family"), "registration.family"),
            version=_text(value.get("version"), "registration.version"),
            profile_id=_text(
                value.get("profile_id"),
                "registration.profile_id",
            ),
            lifecycle=lifecycle,
            activation_state=_text(
                value.get("activation_state"),
                "registration.activation_state",
            ),
            schema_version=_text(
                value.get("schema_version"),
                "registration.schema_version",
            ),
            schema_digest=_digest(
                value.get("schema_digest"),
                "registration.schema_digest",
            ),
            source_digest=_digest(
                value.get("source_digest"),
                "registration.source_digest",
            ),
            configuration=configuration,
            config_digest=config_digest,
            implementation_commit=_commit(
                value.get("implementation_commit"),
                "registration.implementation_commit",
            ),
            evidence_commit=_commit(
                value.get("evidence_commit"),
                "registration.evidence_commit",
            ),
            required_mechanisms=required_mechanisms,
            readiness=readiness,
            rollback_family=_text(
                value.get("rollback_family"),
                "registration.rollback_family",
            ),
            rollback_version=_text(
                value.get("rollback_version"),
                "registration.rollback_version",
            ),
            timeout_seconds=timeout_seconds,
            no_policy_training=training,
            hard_gate_ids=_strings(
                value.get("hard_gate_ids"),
                "registration.hard_gate_ids",
            ),
            audit_gate_ids=_strings(
                value.get("audit_gate_ids"),
                "registration.audit_gate_ids",
            ),
        )
        _validate_lifecycle_state(registration)
        _validate_no_training_declaration(registration.no_policy_training)
        return registration

    @property
    def key(self) -> tuple[str, str]:
        return self.family, self.version

    @property
    def readiness_digest(self) -> str:
        return canonical_digest([item.to_dict() for item in self.readiness])

    @property
    def registration_digest(self) -> str:
        payload = self.to_dict()
        # Lifecycle and activation state are registry-owned mutable metadata.
        # A rollback/retirement must not invalidate a historical run pin.
        payload.pop("lifecycle", None)
        payload.pop("activation_state", None)
        return canonical_digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "version": self.version,
            "profile_id": self.profile_id,
            "lifecycle": self.lifecycle.value,
            "activation_state": self.activation_state,
            "schema_version": self.schema_version,
            "schema_digest": self.schema_digest,
            "source_digest": self.source_digest,
            "configuration": thaw_json(self.configuration),
            "config_digest": self.config_digest,
            "implementation_commit": self.implementation_commit,
            "evidence_commit": self.evidence_commit,
            "required_mechanisms": list(self.required_mechanisms),
            "readiness": [item.to_dict() for item in self.readiness],
            "rollback_family": self.rollback_family,
            "rollback_version": self.rollback_version,
            "timeout_seconds": self.timeout_seconds,
            "no_policy_training": self.no_policy_training.to_dict(),
            "hard_gate_ids": list(self.hard_gate_ids),
            "audit_gate_ids": list(self.audit_gate_ids),
        }


@dataclass(frozen=True, slots=True)
class MechanismRunPin:
    run_id: str
    family: str
    version: str
    profile_id: str
    lifecycle_at_pin: MechanismLifecycle
    schema_version: str
    schema_digest: str
    source_digest: str
    config_digest: str
    readiness_digest: str
    registration_digest: str
    registry_digest: str
    registry_revision: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MechanismRunPin:
        try:
            lifecycle = MechanismLifecycle(
                _text(value.get("lifecycle_at_pin"), "pin.lifecycle_at_pin")
            )
        except ValueError as exc:
            raise PolicyRegistryError(
                "pin-lifecycle-invalid",
                "A run pin has an unsupported lifecycle.",
            ) from exc
        return cls(
            run_id=_text(value.get("run_id"), "pin.run_id"),
            family=_text(value.get("family"), "pin.family"),
            version=_text(value.get("version"), "pin.version"),
            profile_id=_text(value.get("profile_id"), "pin.profile_id"),
            lifecycle_at_pin=lifecycle,
            schema_version=_text(
                value.get("schema_version"),
                "pin.schema_version",
            ),
            schema_digest=_digest(
                value.get("schema_digest"),
                "pin.schema_digest",
            ),
            source_digest=_digest(
                value.get("source_digest"),
                "pin.source_digest",
            ),
            config_digest=_digest(
                value.get("config_digest"),
                "pin.config_digest",
            ),
            readiness_digest=_digest(
                value.get("readiness_digest"),
                "pin.readiness_digest",
            ),
            registration_digest=_digest(
                value.get("registration_digest"),
                "pin.registration_digest",
            ),
            registry_digest=_digest(
                value.get("registry_digest"),
                "pin.registry_digest",
            ),
            registry_revision=int(value.get("registry_revision") or 0),
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "family": self.family,
            "version": self.version,
            "profile_id": self.profile_id,
            "lifecycle_at_pin": self.lifecycle_at_pin.value,
            "schema_version": self.schema_version,
            "schema_digest": self.schema_digest,
            "source_digest": self.source_digest,
            "config_digest": self.config_digest,
            "readiness_digest": self.readiness_digest,
            "registration_digest": self.registration_digest,
            "registry_digest": self.registry_digest,
            "registry_revision": self.registry_revision,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    compatible: bool
    reasons: tuple[str, ...]
    registration: MechanismRegistration | None


@dataclass(frozen=True, slots=True)
class RegistryTransition:
    action: str
    family: str
    version: str
    before_lifecycle: str
    after_lifecycle: str
    before_active_version: str
    after_active_version: str
    registry_revision: int
    registry_digest: str
    activation_decision_digest: str = ""
    manifest_id: str = ""

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-policy-registry-transition/v1",
            "action": self.action,
            "family": self.family,
            "version": self.version,
            "before_lifecycle": self.before_lifecycle,
            "after_lifecycle": self.after_lifecycle,
            "before_active_version": self.before_active_version,
            "after_active_version": self.after_active_version,
            "registry_revision": self.registry_revision,
            "registry_digest": self.registry_digest,
            "activation_decision_digest": self.activation_decision_digest,
            "manifest_id": self.manifest_id,
        }

    def to_event(self, *, run_id: str, task_id: str) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            event_id=f"event_policy_registry_{self.digest[:24]}",
            event_type=EventType.SYSTEM_NOTICE,
            payload={
                "event_name": f"phase2.policy_registry.{self.action}",
                "transition": self.to_dict(),
                "transition_digest": self.digest,
            },
        )


class MechanismRegistry:
    """Metadata/lifecycle owner; run pins remain with the existing run owner."""

    def __init__(
        self,
        *,
        records: Sequence[MechanismRegistration],
        active_versions: Mapping[str, str],
        fallback_family: str,
        fallback_version: str,
        registry_revision: int = 1,
        source_config_digest: str = "",
    ) -> None:
        self._records: dict[tuple[str, str], MechanismRegistration] = {}
        self._active_versions = {
            str(family): str(version)
            for family, version in active_versions.items()
        }
        self.fallback_family = fallback_family
        self.fallback_version = fallback_version
        self.registry_revision = int(registry_revision)
        self.source_config_digest = source_config_digest
        for record in records:
            self._register(record, increment=False)
        default_records = [
            record
            for record in self._records.values()
            if record.lifecycle is MechanismLifecycle.DEFAULT
        ]
        if len(default_records) > 1:
            raise PolicyRegistryError(
                "default-profile-not-unique",
                "The registry can contain only one default mechanism profile.",
            )
        if (
            default_records
            and default_records[0].profile_id != "phase2_strongest_v1"
        ):
            raise PolicyRegistryError(
                "default-profile-invalid",
                "The only permitted default profile is phase2_strongest_v1.",
            )
        fallback = self._records.get((fallback_family, fallback_version))
        if fallback is None or fallback.lifecycle is not MechanismLifecycle.BASELINE:
            raise PolicyRegistryError(
                "rollback-target-unavailable",
                "The registry fallback must be an available baseline record.",
            )
        for family, version in self._active_versions.items():
            record = self._records.get((family, version))
            if record is None:
                raise PolicyRegistryError(
                    "active-version-missing",
                    f"Active version {family}/{version} is not registered.",
                )
            if record.lifecycle not in {
                MechanismLifecycle.BASELINE,
                MechanismLifecycle.DEFAULT,
            }:
                raise PolicyRegistryError(
                    "active-lifecycle-invalid",
                    "Only baseline or default records can resolve normal runs.",
                )
            if record.activation_state != "active":
                raise PolicyRegistryError(
                    "active-state-invalid",
                    "The normal resolver target must have activation_state=active.",
                )
            if record.lifecycle is MechanismLifecycle.DEFAULT:
                _require_default_readiness(record)

    @classmethod
    def load(
        cls,
        repository_root: Path,
        *,
        config_path: Path | None = None,
    ) -> MechanismRegistry:
        root = repository_root.resolve()
        path = (
            config_path.resolve()
            if config_path is not None
            else root / POLICY_REGISTRY_CONFIG
        )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PolicyRegistryError(
                "registry-config-missing",
                "The mechanism registry configuration is missing.",
                path=str(path),
            ) from exc
        except json.JSONDecodeError as exc:
            raise PolicyRegistryError(
                "registry-config-corrupt",
                "The mechanism registry configuration is not valid JSON-compatible YAML.",
                path=str(path),
            ) from exc
        selected = _mapping(raw, "policy registry")
        if selected.get("schema") != POLICY_REGISTRY_SCHEMA:
            raise PolicyRegistryError(
                "registry-schema-incompatible",
                "The mechanism registry schema is incompatible.",
                path=str(path),
            )
        expected_digest = _digest(
            selected.get("registry_digest"),
            "policy registry.registry_digest",
        )
        digest_payload = dict(selected)
        digest_payload.pop("registry_digest", None)
        observed_digest = canonical_digest(digest_payload)
        if observed_digest != expected_digest:
            raise PolicyRegistryError(
                "registry-digest-mismatch",
                "The mechanism registry digest does not match its content.",
                path=str(path),
            )
        records = [
            MechanismRegistration.from_mapping(
                _mapping(item, "policy registry.records[]")
            )
            for item in _sequence(selected.get("records"), "policy registry.records")
        ]
        _validate_readiness_reports(root, records)
        fallback = _mapping(selected.get("fallback"), "policy registry.fallback")
        return cls(
            records=records,
            active_versions=_mapping(
                selected.get("active_versions"),
                "policy registry.active_versions",
            ),
            fallback_family=_text(
                fallback.get("family"),
                "policy registry.fallback.family",
            ),
            fallback_version=_text(
                fallback.get("version"),
                "policy registry.fallback.version",
            ),
            registry_revision=int(selected.get("registry_revision") or 0),
            source_config_digest=expected_digest,
        )

    @property
    def registry_digest(self) -> str:
        payload = {
            "records": [
                self._records[key].to_dict()
                for key in sorted(self._records)
            ],
            "active_versions": dict(sorted(self._active_versions.items())),
            "fallback": {
                "family": self.fallback_family,
                "version": self.fallback_version,
            },
            "registry_revision": self.registry_revision,
            "source_config_digest": self.source_config_digest,
        }
        return canonical_digest(payload)

    def records(self) -> tuple[MechanismRegistration, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def get(self, family: str, version: str) -> MechanismRegistration:
        record = self._records.get((family, version))
        if record is None:
            raise PolicyRegistryError(
                "version-not-registered",
                f"Mechanism version {family}/{version} is not registered.",
            )
        return record

    def register(self, record: MechanismRegistration) -> RegistryTransition:
        if record.lifecycle is MechanismLifecycle.DEFAULT:
            raise PolicyRegistryError(
                "default-registration-forbidden",
                "A default version must pass activate_default; it cannot be registered active.",
            )
        before = self._active_versions.get(record.family, "")
        self._register(record, increment=True)
        return self._transition(
            action="register",
            record=record,
            before_lifecycle="",
            before_active=before,
        )

    def _register(
        self,
        record: MechanismRegistration,
        *,
        increment: bool,
    ) -> None:
        if record.key in self._records:
            raise PolicyRegistryError(
                "duplicate-version",
                "A family/version pair can only be registered once, even with the same digest.",
            )
        _validate_lifecycle_state(record)
        _validate_no_training_declaration(record.no_policy_training)
        self._records[record.key] = record
        if increment:
            self.registry_revision += 1

    def resolve(
        self,
        family: str,
        *,
        purpose: ResolutionPurpose = ResolutionPurpose.NORMAL,
        version: str | None = None,
        validation_manifest: ValidationManifest | None = None,
    ) -> MechanismRegistration:
        if purpose is ResolutionPurpose.NORMAL:
            active_version = self._active_versions.get(family)
            if not active_version:
                if family == self.fallback_family:
                    active_version = self.fallback_version
                else:
                    raise PolicyRegistryError(
                        "normal-default-missing",
                        f"No normal resolver target exists for {family}.",
                    )
            if version is not None and version != active_version:
                candidate = self.get(family, version)
                if candidate.lifecycle is MechanismLifecycle.VALIDATION:
                    raise PolicyRegistryError(
                        "validation-normal-selection-forbidden",
                        "A normal run cannot select a validation mechanism.",
                    )
                raise PolicyRegistryError(
                    "normal-version-not-active",
                    "A normal run cannot bypass the active pinned version.",
                )
            record = self.get(family, active_version)
            if record.lifecycle not in {
                MechanismLifecycle.BASELINE,
                MechanismLifecycle.DEFAULT,
            }:
                raise PolicyRegistryError(
                    "normal-lifecycle-forbidden",
                    "Normal resolution is limited to the active baseline/default.",
                )
            return record

        if version is None:
            raise PolicyRegistryError(
                "explicit-version-required",
                "Validation and diagnostic resolution require an explicit version.",
            )
        record = self.get(family, version)
        if purpose is ResolutionPurpose.VALIDATION:
            if validation_manifest is None:
                raise PolicyRegistryError(
                    "validation-manifest-required",
                    "Validation cannot run without an explicit manifest.",
                )
            validation_manifest.validate()
            if (
                record.lifecycle is not MechanismLifecycle.VALIDATION
                or record.activation_state != "validation_ready"
            ):
                raise PolicyRegistryError(
                    "validation-version-unavailable",
                    "The requested mechanism has not entered validation.",
                )
            _require_validation_readiness(record)
            return record

        if record.lifecycle is not MechanismLifecycle.DIAGNOSTIC:
            raise PolicyRegistryError(
                "diagnostic-lifecycle-required",
                "Diagnostic resolution requires a diagnostic record.",
            )
        _require_diagnostic_readiness(record)
        return record

    def resolve_baseline(self) -> MechanismRegistration:
        return self.get(self.fallback_family, self.fallback_version)

    def pin(
        self,
        run_id: str,
        family: str,
        *,
        purpose: ResolutionPurpose = ResolutionPurpose.NORMAL,
        version: str | None = None,
        validation_manifest: ValidationManifest | None = None,
    ) -> MechanismRunPin:
        record = self.resolve(
            family,
            purpose=purpose,
            version=version,
            validation_manifest=validation_manifest,
        )
        return self._pin_record(run_id, record)

    def pin_baseline(self, run_id: str) -> MechanismRunPin:
        return self._pin_record(run_id, self.resolve_baseline())

    def _pin_record(
        self,
        run_id: str,
        record: MechanismRegistration,
    ) -> MechanismRunPin:
        return MechanismRunPin(
            run_id=_text(run_id, "run_id"),
            family=record.family,
            version=record.version,
            profile_id=record.profile_id,
            lifecycle_at_pin=record.lifecycle,
            schema_version=record.schema_version,
            schema_digest=record.schema_digest,
            source_digest=record.source_digest,
            config_digest=record.config_digest,
            readiness_digest=record.readiness_digest,
            registration_digest=record.registration_digest,
            registry_digest=self.registry_digest,
            registry_revision=self.registry_revision,
        )

    def check_compatibility(
        self,
        pin: MechanismRunPin,
        *,
        for_new_run: bool,
    ) -> CompatibilityReport:
        reasons: list[str] = []
        record = self._records.get((pin.family, pin.version))
        if record is None:
            return CompatibilityReport(
                compatible=False,
                reasons=("pinned family/version is not registered",),
                registration=None,
            )
        if record.profile_id != pin.profile_id:
            reasons.append("profile id differs")
        if record.schema_version != pin.schema_version:
            reasons.append("schema version differs")
        if record.schema_digest != pin.schema_digest:
            reasons.append("schema digest differs")
        if record.source_digest != pin.source_digest:
            reasons.append("source digest differs")
        if record.config_digest != pin.config_digest:
            reasons.append("config digest differs")
        if record.readiness_digest != pin.readiness_digest:
            reasons.append("readiness digest differs")
        if record.registration_digest != pin.registration_digest:
            reasons.append("registration digest differs")
        if for_new_run and record.lifecycle is MechanismLifecycle.RETIRED:
            reasons.append("retired versions cannot serve new runs")
        return CompatibilityReport(
            compatible=not reasons,
            reasons=tuple(reasons),
            registration=record,
        )

    def resolve_pinned(self, pin: MechanismRunPin) -> MechanismRegistration:
        compatibility = self.check_compatibility(pin, for_new_run=False)
        if not compatibility.compatible or compatibility.registration is None:
            raise PolicyRegistryError(
                "run-pin-incompatible",
                "Pinned mechanism compatibility failed: "
                + ", ".join(compatibility.reasons),
            )
        return compatibility.registration

    def enter_validation(
        self,
        family: str,
        version: str,
        *,
        manifest: ValidationManifest,
    ) -> RegistryTransition:
        manifest.validate()
        record = self.get(family, version)
        if record.lifecycle is MechanismLifecycle.RETIRED:
            raise PolicyRegistryError(
                "retired-validation-forbidden",
                "A retired version cannot re-enter validation.",
            )
        if record.lifecycle is MechanismLifecycle.BASELINE:
            raise PolicyRegistryError(
                "baseline-validation-forbidden",
                "A frozen baseline cannot enter validation.",
            )
        if self._active_versions.get(family) == version:
            raise PolicyRegistryError(
                "active-validation-forbidden",
                "The active version must be rolled back before entering validation.",
            )
        _require_validation_readiness(record)
        before = record.lifecycle.value
        updated = replace(
            record,
            lifecycle=MechanismLifecycle.VALIDATION,
            activation_state="validation_ready",
        )
        self._records[record.key] = updated
        self.registry_revision += 1
        return self._transition(
            action="enter_validation",
            record=updated,
            before_lifecycle=before,
            before_active=self._active_versions.get(family, ""),
            manifest_id=manifest.manifest_id,
        )

    def activate_default(
        self,
        family: str,
        version: str,
        *,
        activation_decision: Any,
    ) -> RegistryTransition:
        record = self.get(family, version)
        if record.profile_id != "phase2_strongest_v1":
            raise PolicyRegistryError(
                "default-profile-invalid",
                "The only permitted default profile is phase2_strongest_v1.",
            )
        if (
            record.lifecycle is not MechanismLifecycle.VALIDATION
            or record.activation_state != "validation_ready"
        ):
            raise PolicyRegistryError(
                "activation-source-invalid",
                "Default activation requires a validation_ready source version.",
            )
        if (
            getattr(activation_decision, "eligible", False) is not True
            or getattr(activation_decision, "family", "") != family
            or getattr(activation_decision, "version", "") != version
            or getattr(activation_decision, "registration_digest", "")
            != record.registration_digest
            or getattr(activation_decision, "config_digest", "")
            != record.config_digest
            or getattr(activation_decision, "source_digest", "")
            != record.source_digest
            or getattr(activation_decision, "schema_digest", "")
            != record.schema_digest
        ):
            raise PolicyRegistryError(
                "activation-decision-invalid",
                "Default activation requires a matching eligible activation decision.",
            )
        _require_default_readiness(record)
        rollback = self._records.get(
            (record.rollback_family, record.rollback_version)
        )
        if (
            rollback is None
            or record.rollback_family != family
            or rollback.lifecycle is MechanismLifecycle.RETIRED
            or (
                rollback.lifecycle is not MechanismLifecycle.BASELINE
                and rollback.activation_state != "rollback_ready"
            )
        ):
            raise PolicyRegistryError(
                "rollback-target-unavailable",
                "Default activation requires an available rollback target.",
            )
        before_active = self._active_versions.get(family, "")
        if before_active:
            previous = self.get(family, before_active)
            if previous.lifecycle is MechanismLifecycle.DEFAULT:
                self._records[previous.key] = replace(
                    previous,
                    lifecycle=MechanismLifecycle.VALIDATION,
                    activation_state="rollback_ready",
                )
            elif previous.lifecycle is MechanismLifecycle.BASELINE:
                self._records[previous.key] = replace(
                    previous,
                    activation_state="standby",
                )
        updated = replace(
            record,
            lifecycle=MechanismLifecycle.DEFAULT,
            activation_state="active",
        )
        self._records[record.key] = updated
        self._active_versions[family] = version
        self.registry_revision += 1
        return self._transition(
            action="activate_default",
            record=updated,
            before_lifecycle=record.lifecycle.value,
            before_active=before_active,
            activation_decision_digest=str(
                getattr(activation_decision, "decision_digest", "")
            ),
        )

    def rollback(
        self,
        family: str,
        *,
        target_version: str | None = None,
    ) -> RegistryTransition:
        before_active = self._active_versions.get(family, "")
        if not before_active:
            raise PolicyRegistryError(
                "rollback-active-version-missing",
                "There is no active version to roll back.",
            )
        current = self.get(family, before_active)
        target_family = (
            current.rollback_family
            if target_version is None
            else family
        )
        selected_version = (
            current.rollback_version
            if target_version is None
            else target_version
        )
        if target_family != family:
            raise PolicyRegistryError(
                "rollback-family-incompatible",
                "Rollback targets must stay within the pinned mechanism family.",
            )
        target = self.get(target_family, selected_version)
        if target.lifecycle is MechanismLifecycle.RETIRED:
            raise PolicyRegistryError(
                "rollback-target-retired",
                "A retired version cannot become a rollback target.",
            )
        if target.lifecycle is not MechanismLifecycle.BASELINE:
            if target.activation_state != "rollback_ready":
                raise PolicyRegistryError(
                    "rollback-target-never-activated",
                    "A non-baseline rollback target must be a previously activated version.",
                )
            _require_default_readiness(target)
        if current.lifecycle is MechanismLifecycle.DEFAULT:
            self._records[current.key] = replace(
                current,
                lifecycle=MechanismLifecycle.VALIDATION,
                activation_state="rollback_ready",
            )
        if target.lifecycle is not MechanismLifecycle.BASELINE:
            target = replace(
                target,
                lifecycle=MechanismLifecycle.DEFAULT,
                activation_state="active",
            )
            self._records[target.key] = target
        else:
            target = replace(target, activation_state="active")
            self._records[target.key] = target
        self._active_versions[family] = selected_version
        self.registry_revision += 1
        return self._transition(
            action="rollback",
            record=target,
            before_lifecycle=current.lifecycle.value,
            before_active=before_active,
        )

    def retire(self, family: str, version: str) -> RegistryTransition:
        record = self.get(family, version)
        if self._active_versions.get(family) == version:
            raise PolicyRegistryError(
                "active-retirement-forbidden",
                "The active version must be rolled back before retirement.",
            )
        if (family, version) == (
            self.fallback_family,
            self.fallback_version,
        ):
            raise PolicyRegistryError(
                "fallback-retirement-forbidden",
                "The frozen baseline fallback cannot be retired.",
            )
        before = record.lifecycle.value
        updated = replace(
            record,
            lifecycle=MechanismLifecycle.RETIRED,
            activation_state="disabled",
        )
        self._records[record.key] = updated
        self.registry_revision += 1
        return self._transition(
            action="retire",
            record=updated,
            before_lifecycle=before,
            before_active=self._active_versions.get(family, ""),
        )

    def _transition(
        self,
        *,
        action: str,
        record: MechanismRegistration,
        before_lifecycle: str,
        before_active: str,
        activation_decision_digest: str = "",
        manifest_id: str = "",
    ) -> RegistryTransition:
        return RegistryTransition(
            action=action,
            family=record.family,
            version=record.version,
            before_lifecycle=before_lifecycle,
            after_lifecycle=record.lifecycle.value,
            before_active_version=before_active,
            after_active_version=self._active_versions.get(record.family, ""),
            registry_revision=self.registry_revision,
            registry_digest=self.registry_digest,
            activation_decision_digest=activation_decision_digest,
            manifest_id=manifest_id,
        )


def _require_validation_readiness(record: MechanismRegistration) -> None:
    blockers = [
        item.mechanism_id
        for item in record.readiness
        if item.status is not ReadinessStatus.DETERMINISTIC_READY
        or item.stage
        not in {
            ReadinessStage.IMPLEMENTATION_VALIDATED,
            ReadinessStage.ACTIVATION_READY,
        }
    ]
    if blockers:
        raise PolicyRegistryError(
            "validation-readiness-failed",
            "Explicit validation requires implementation_validated deterministic "
            f"readiness: {', '.join(blockers)}.",
        )


def _require_default_readiness(record: MechanismRegistration) -> None:
    blockers = [
        item.mechanism_id
        for item in record.readiness
        if item.status is not ReadinessStatus.DETERMINISTIC_READY
        or item.stage is not ReadinessStage.ACTIVATION_READY
    ]
    if blockers:
        raise PolicyRegistryError(
            "default-readiness-failed",
            "Default activation requires activation_ready deterministic readiness: "
            + ", ".join(blockers),
        )


def _require_diagnostic_readiness(record: MechanismRegistration) -> None:
    unavailable = [
        item.mechanism_id
        for item in record.readiness
        if item.status is ReadinessStatus.UNAVAILABLE
    ]
    if unavailable:
        raise PolicyRegistryError(
            "diagnostic-readiness-unavailable",
            "Unavailable mechanisms must use baseline rather than diagnostic: "
            + ", ".join(unavailable),
        )
    development_only = [
        item.mechanism_id
        for item in record.readiness
        if item.status is ReadinessStatus.DETERMINISTIC_READY
        and item.stage is ReadinessStage.INPUT_PRECHECK
    ]
    if development_only:
        raise PolicyRegistryError(
            "diagnostic-readiness-development-only",
            "Deterministic input_precheck-only mechanisms must remain "
            "development-only rather than diagnostic: "
            + ", ".join(development_only),
        )


def _validate_lifecycle_state(record: MechanismRegistration) -> None:
    permitted = {
        MechanismLifecycle.BASELINE: {"active", "standby"},
        MechanismLifecycle.VALIDATION: {
            "blocked_pending_readiness",
            "validation_ready",
            "rollback_ready",
        },
        MechanismLifecycle.DEFAULT: {"active"},
        MechanismLifecycle.DIAGNOSTIC: {"read_only"},
        MechanismLifecycle.RETIRED: {"disabled"},
    }
    if record.activation_state not in permitted[record.lifecycle]:
        raise PolicyRegistryError(
            "lifecycle-state-invalid",
            "Mechanism lifecycle and activation state are incompatible: "
            f"{record.lifecycle.value}/{record.activation_state}.",
        )


def _validate_no_training_declaration(
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
            "Policy registry entries cannot declare training artifacts: "
            + ", ".join(violations),
        )
    forbidden_dependencies = {
        "torch",
        "tensorflow",
        "jax",
        "trl",
        "datasets",
        "stable-baselines3",
    }
    dependencies = {
        re.split(r"[\[<>=!~ ]", value.casefold(), maxsplit=1)[0]
        for value in declaration.dependencies
    }
    blocked = sorted(dependencies & forbidden_dependencies)
    if blocked:
        raise PolicyRegistryError(
            "no-policy-training-dependency-present",
            "Policy registry entries cannot add training dependencies: "
            + ", ".join(blocked),
        )


def _validate_readiness_reports(
    repository_root: Path,
    records: Sequence[MechanismRegistration],
) -> None:
    cache: dict[str, Mapping[str, Any]] = {}
    for record in records:
        for binding in record.readiness:
            if binding.report_ref.startswith("pending:"):
                continue
            report = cache.get(binding.report_ref)
            if report is None:
                report_path = _safe_path(repository_root, binding.report_ref)
                try:
                    value = json.loads(report_path.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise PolicyRegistryError(
                        "readiness-report-missing",
                        f"Readiness report is missing: {binding.report_ref}.",
                        path=str(report_path),
                    ) from exc
                except json.JSONDecodeError as exc:
                    raise PolicyRegistryError(
                        "readiness-report-corrupt",
                        f"Readiness report is corrupt: {binding.report_ref}.",
                        path=str(report_path),
                    ) from exc
                report = _mapping(value, "readiness report")
                cache[binding.report_ref] = report
            if report.get("report_digest") != binding.report_digest:
                raise PolicyRegistryError(
                    "readiness-report-digest-mismatch",
                    "Readiness report digest differs from the registry binding.",
                )
            mechanisms = _mapping(
                report.get("mechanisms"),
                "readiness report mechanisms",
            )
            mechanism = mechanisms.get(binding.mechanism_id)
            if not isinstance(mechanism, Mapping):
                raise PolicyRegistryError(
                    "readiness-report-mechanism-missing",
                    f"Readiness report omits {binding.mechanism_id}.",
                )
            if (
                mechanism.get("status") != binding.status.value
                or mechanism.get("readiness_stage") != binding.stage.value
            ):
                raise PolicyRegistryError(
                    "readiness-report-binding-mismatch",
                    f"Readiness report differs for {binding.mechanism_id}.",
                )


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PolicyRegistryError(
            "registry-path-escape",
            "Registry references must remain inside the repository.",
        ) from exc
    return candidate


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyRegistryError(
            "mapping-required",
            f"{label} must be an object.",
        )
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PolicyRegistryError(
            "sequence-required",
            f"{label} must be a list.",
        )
    return value


def _strings(value: Any, label: str) -> tuple[str, ...]:
    result = tuple(_text(item, f"{label}[]") for item in _sequence(value, label))
    if len(set(result)) != len(result):
        raise PolicyRegistryError(
            "duplicate-value",
            f"{label} contains duplicate values.",
        )
    return result


def _text(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if not selected:
        raise PolicyRegistryError(
            "text-required",
            f"{label} must be non-empty.",
        )
    return selected


def _digest(value: Any, label: str) -> str:
    selected = _text(value, label).casefold()
    if not _SHA256.fullmatch(selected):
        raise PolicyRegistryError(
            "digest-invalid",
            f"{label} must be a SHA-256 digest.",
        )
    return selected


def _commit(value: Any, label: str) -> str:
    selected = _text(value, label).casefold()
    if not _COMMIT.fullmatch(selected):
        raise PolicyRegistryError(
            "commit-invalid",
            f"{label} must be a full Git commit.",
        )
    return selected


__all__ = [
    "POLICY_REGISTRY_CONFIG",
    "POLICY_REGISTRY_SCHEMA",
    "CompatibilityReport",
    "MechanismLifecycle",
    "MechanismRegistration",
    "MechanismRegistry",
    "MechanismRunPin",
    "NoPolicyTrainingDeclaration",
    "PolicyRegistryError",
    "ReadinessBinding",
    "ReadinessStage",
    "ReadinessStatus",
    "RegistryTransition",
    "ResolutionPurpose",
    "ValidationManifest",
]
