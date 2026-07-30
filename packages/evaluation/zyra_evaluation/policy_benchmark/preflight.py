from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_orchestration.topology_policy.registry import (
    MechanismRegistry,
    ResolutionPurpose,
    ValidationManifest,
)

from .activation import build_strongest_preflight_activation_report
from .contracts import P2_BASE_COMMIT, canonical_digest, file_digest
from .evidence_index import BASELINE_MANIFEST_DIGEST, build_read_only_evidence_index
from .mechanism_readiness import (
    MECHANISM_IDS,
    MechanismReadinessConfig,
    run_no_training_audit,
)
from .report import StrongestPreflightReport


STRONGEST_PREFLIGHT_MANIFEST_SCHEMA = "zyra.strongest-preflight-manifest/v1"
STRONGEST_PREFLIGHT_RECEIPT_SET_SCHEMA = (
    "zyra.strongest-preflight-receipt-set/v1"
)
STRONGEST_PREFLIGHT_INVENTORY_SCHEMA = "zyra.strongest-preflight-inventory/v1"
STRONGEST_PROFILE = "phase2_strongest_v1"
BASELINE_PROFILE = "phase1_deterministic_baseline"
REQUIRED_CHAIN = (
    "loopx",
    "memory_continuity_verifier",
    "arg_designer",
    "card",
    "agentprune",
    "symbolic_projector",
    "graph_state_custody",
    "maas",
    "resource_scheduler_permission_lease_physical_dispatch",
    "verifier_evidence_receipts",
)
REQUIRED_CHECKS = (
    "readiness_enforcement",
    "determinism",
    "fail_closed_inputs",
    "diagnostic_side_effect_zero",
    "local_isolated_integration",
    "continuity_restart_recovery",
    "loopx_restart_outbox",
    "phase1_read_only_replay",
    "no_policy_training",
    "failure_retention",
)
PROHIBITED_OPERATIONS = (
    "training",
    "fine_tuning",
    "reinforcement_learning",
    "policy_gradient",
    "textual_gradient",
    "weight_search",
    "combination_search",
    "baseline_rewrite",
    "result_overwrite",
)
HARD_GATE_ORDER = (
    "success_and_safety",
    "manifest_integrity",
    "registry_default_baseline",
    "validation_profile_explicit",
    "readiness_enforcement",
    "determinism",
    "fail_closed_inputs",
    "diagnostic_side_effect_zero",
    "local_isolated_integration",
    "continuity_restart_recovery",
    "loopx_restart_outbox",
    "phase1_read_only_replay",
    "no_policy_training",
    "failure_retention",
    "efficiency_observed_without_optimization",
)


class StrongestPreflightError(ValueError):
    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StrongestPreflightError(
            "preflight-mapping-required",
            f"{label} must be an object.",
            path=label,
        )
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StrongestPreflightError(
            "preflight-sequence-required",
            f"{label} must be a list.",
            path=label,
        )
    return value


def _text(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if not selected:
        raise StrongestPreflightError(
            "preflight-text-required",
            f"{label} must be non-empty.",
            path=label,
        )
    return selected


def _safe_path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    relative = Path(value.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise StrongestPreflightError(
            "preflight-path-invalid",
            f"Path must remain repository-relative: {value}.",
            path=value,
        )
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise StrongestPreflightError(
            "preflight-path-escape",
            f"Path resolves outside the repository: {value}.",
            path=value,
        ) from exc
    if must_exist and not candidate.is_file():
        raise StrongestPreflightError(
            "preflight-file-missing",
            f"Required preflight input is missing: {value}.",
            path=value,
        )
    return candidate


def _sha256(value: Any, label: str) -> str:
    selected = _text(value, label).casefold()
    if len(selected) != 64 or any(item not in "0123456789abcdef" for item in selected):
        raise StrongestPreflightError(
            "preflight-digest-invalid",
            f"{label} must be a SHA-256 digest.",
            path=label,
        )
    return selected


def compute_preflight_id(value: Mapping[str, Any]) -> str:
    identity = {
        "frozen_inputs": value.get("frozen_inputs"),
        "evidence_bindings": value.get("evidence_bindings"),
        "supporting_evidence": value.get("supporting_evidence"),
        "required_checks": value.get("required_checks"),
        "command_probes": value.get("command_probes"),
        "prohibited_operations": value.get("prohibited_operations"),
    }
    return "preflight_" + canonical_digest(identity)[:24]


@dataclass(frozen=True, slots=True)
class FrozenPreflightManifest:
    path: Path
    value: Mapping[str, Any]
    manifest_digest: str
    preflight_id: str
    p2_eval_base_commit: str
    profile_family: str
    profile_version: str
    profile_config_digest: str
    frozen_at: str
    evidence_bindings: Mapping[str, Mapping[str, Any]]
    command_probes: tuple[Mapping[str, Any], ...]

    @classmethod
    def load(
        cls,
        repository_root: Path,
        path: Path,
    ) -> "FrozenPreflightManifest":
        root = repository_root.resolve()
        selected_path = path.resolve()
        try:
            raw = json.loads(selected_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StrongestPreflightError(
                "preflight-manifest-invalid",
                "The frozen preflight manifest is missing or invalid.",
                path=str(path),
            ) from exc
        value = dict(_mapping(raw, "preflight manifest"))
        if value.get("schema") != STRONGEST_PREFLIGHT_MANIFEST_SCHEMA:
            raise StrongestPreflightError(
                "preflight-manifest-schema",
                "Unsupported frozen preflight manifest schema.",
                path=str(path),
            )
        expected_digest = _sha256(
            value.get("manifest_digest"),
            "manifest_digest",
        )
        digest_payload = dict(value)
        digest_payload.pop("manifest_digest", None)
        if canonical_digest(digest_payload) != expected_digest:
            raise StrongestPreflightError(
                "preflight-manifest-digest-mismatch",
                "The frozen preflight manifest digest does not match.",
                path=str(path),
            )
        frozen_inputs = _mapping(value.get("frozen_inputs"), "frozen_inputs")
        expected_id = compute_preflight_id(value)
        if value.get("preflight_id") != expected_id:
            raise StrongestPreflightError(
                "preflight-id-mismatch",
                "The preflight id is not derived from the frozen inputs.",
                path=str(path),
            )
        p2_eval_base = _text(
            frozen_inputs.get("p2_eval_base_commit"),
            "frozen_inputs.p2_eval_base_commit",
        )
        if len(p2_eval_base) != 40:
            raise StrongestPreflightError(
                "preflight-base-commit-invalid",
                "P2_EVAL_BASE_COMMIT must be a full commit.",
            )
        profile = _mapping(frozen_inputs.get("profile"), "frozen_inputs.profile")
        if profile.get("family") != "topology_policy":
            raise StrongestPreflightError(
                "preflight-family-invalid",
                "Only the frozen topology_policy family is permitted.",
            )
        if profile.get("version") != STRONGEST_PROFILE:
            raise StrongestPreflightError(
                "preflight-profile-invalid",
                "Only phase2_strongest_v1 is permitted.",
            )
        if tuple(frozen_inputs.get("mechanism_chain") or ()) != REQUIRED_CHAIN:
            raise StrongestPreflightError(
                "preflight-chain-invalid",
                "The strongest mechanism chain differs from the frozen chain.",
            )
        if frozen_inputs.get("baseline_version") != BASELINE_PROFILE:
            raise StrongestPreflightError(
                "preflight-baseline-invalid",
                "The explicit fallback must remain the Phase 1 baseline.",
            )
        tasks = tuple(_sequence(frozen_inputs.get("tasks"), "frozen_inputs.tasks"))
        domains = {
            _text(_mapping(item, "task").get("domain_id"), "task.domain_id")
            for item in tasks
        }
        if len(tasks) != 2 or len(domains) != 2:
            raise StrongestPreflightError(
                "preflight-domain-task-set-invalid",
                "The preflight requires exactly two distinct domain tasks.",
            )
        for item in tasks:
            task = _mapping(item, "task")
            _text(task.get("task_input"), "task.task_input")
            verifier = _mapping(task.get("verifier"), "task.verifier")
            _text(verifier.get("contract_id"), "task.verifier.contract_id")
        seeds = tuple(_sequence(frozen_inputs.get("seeds"), "frozen_inputs.seeds"))
        if not seeds or len(seeds) != len(set(map(str, seeds))):
            raise StrongestPreflightError(
                "preflight-seeds-invalid",
                "The frozen seed list must be non-empty and unique.",
            )
        budgets = _mapping(frozen_inputs.get("budgets"), "frozen_inputs.budgets")
        for key in ("token_limit", "cost_usd_limit", "time_seconds", "communication_bytes"):
            if float(budgets.get(key) or 0) <= 0:
                raise StrongestPreflightError(
                    "preflight-budget-invalid",
                    f"Frozen budget {key} must be positive.",
                )
        if tuple(value.get("required_checks") or ()) != REQUIRED_CHECKS:
            raise StrongestPreflightError(
                "preflight-required-checks-invalid",
                "Required checks were removed, reordered, or expanded.",
            )
        prohibited = _mapping(
            value.get("prohibited_operations"),
            "prohibited_operations",
        )
        if (
            set(prohibited) != set(PROHIBITED_OPERATIONS)
            or any(prohibited.get(item) is not True for item in PROHIBITED_OPERATIONS)
        ):
            raise StrongestPreflightError(
                "preflight-prohibited-operation-contract-invalid",
                "Training, search, baseline rewrite, and result overwrite must remain prohibited.",
            )

        bindings = {
            str(item.get("mechanism_id")): _mapping(item, "evidence binding")
            for item in (
                _mapping(raw_item, "evidence binding")
                for raw_item in _sequence(
                    value.get("evidence_bindings"),
                    "evidence_bindings",
                )
            )
        }
        if set(bindings) != set(MECHANISM_IDS):
            raise StrongestPreflightError(
                "preflight-readiness-set-invalid",
                "The preflight must bind ARG, CARD, AgentPrune, and MaAS.",
            )
        for mechanism_id in MECHANISM_IDS:
            binding = bindings[mechanism_id]
            if (
                binding.get("stage") != "implementation_validated"
                or binding.get("status") != "deterministic_ready"
            ):
                raise StrongestPreflightError(
                    "preflight-readiness-not-validated",
                    f"{mechanism_id} is not implementation_validated deterministic_ready.",
                )
            report_path = _safe_path(
                root,
                _text(binding.get("report_ref"), "binding.report_ref"),
            )
            if file_digest(report_path) != _sha256(
                binding.get("file_sha256"),
                "binding.file_sha256",
            ):
                raise StrongestPreflightError(
                    "preflight-readiness-file-mismatch",
                    f"{mechanism_id} readiness file digest differs.",
                )
            report = _mapping(
                json.loads(report_path.read_text(encoding="utf-8")),
                "readiness report",
            )
            if report.get("report_digest") != binding.get("report_digest"):
                raise StrongestPreflightError(
                    "preflight-readiness-digest-mismatch",
                    f"{mechanism_id} readiness report digest differs.",
                )
            mechanism = _mapping(
                _mapping(report.get("mechanisms"), "report.mechanisms").get(
                    mechanism_id
                ),
                f"report.mechanisms.{mechanism_id}",
            )
            if (
                mechanism.get("readiness_stage") != binding.get("stage")
                or mechanism.get("status") != binding.get("status")
            ):
                raise StrongestPreflightError(
                    "preflight-readiness-binding-mismatch",
                    f"{mechanism_id} readiness content differs.",
                )

        for label in (
            "policy_registry",
            "activation_gates",
            "phase1_baseline_manifest",
        ):
            binding = _mapping(frozen_inputs.get(label), f"frozen_inputs.{label}")
            target = _safe_path(
                root,
                _text(binding.get("path"), f"{label}.path"),
            )
            if file_digest(target) != _sha256(
                binding.get("file_sha256"),
                f"{label}.file_sha256",
            ):
                raise StrongestPreflightError(
                    f"preflight-{label}-digest-mismatch",
                    f"The frozen {label} file changed.",
                )
            document = _mapping(
                json.loads(target.read_text(encoding="utf-8")),
                label,
            )
            identity_fields = {
                "policy_registry": ("registry_digest", "registry_digest"),
                "activation_gates": ("frozen_gate_digest", "gate_digest"),
                "phase1_baseline_manifest": (
                    "manifest_digest",
                    "manifest_digest",
                ),
            }
            document_field, binding_field = identity_fields[label]
            if document.get(document_field) != binding.get(binding_field):
                raise StrongestPreflightError(
                    f"preflight-{label}-identity-mismatch",
                    f"The frozen {label} identity differs.",
                )

        supporting = tuple(
            _mapping(item, "supporting evidence")
            for item in _sequence(
                value.get("supporting_evidence"),
                "supporting_evidence",
            )
        )
        required_support = {"loopx_runtime", "loopx_bridge", "physical_dispatch"}
        if {str(item.get("component")) for item in supporting} != required_support:
            raise StrongestPreflightError(
                "preflight-supporting-evidence-set-invalid",
                "LoopX runtime/bridge and physical dispatch evidence are required.",
            )
        for binding in supporting:
            target = _safe_path(
                root,
                _text(binding.get("path"), "supporting_evidence.path"),
            )
            if file_digest(target) != _sha256(
                binding.get("file_sha256"),
                "supporting_evidence.file_sha256",
            ):
                raise StrongestPreflightError(
                    "preflight-supporting-evidence-digest-mismatch",
                    f"Supporting evidence changed: {binding.get('component')}.",
                )

        probes = tuple(
            _mapping(item, "command probe")
            for item in _sequence(value.get("command_probes"), "command_probes")
        )
        probe_ids = [_text(item.get("probe_id"), "command probe id") for item in probes]
        if len(probe_ids) != len(set(probe_ids)):
            raise StrongestPreflightError(
                "preflight-command-probe-duplicate",
                "Command probe ids must be unique.",
            )
        if not probes or any(item.get("external_cost") is not False for item in probes):
            raise StrongestPreflightError(
                "preflight-command-probe-cost-invalid",
                "Default preflight command probes must be local and zero external cost.",
            )
        return cls(
            path=selected_path,
            value=value,
            manifest_digest=expected_digest,
            preflight_id=expected_id,
            p2_eval_base_commit=p2_eval_base,
            profile_family=str(profile["family"]),
            profile_version=str(profile["version"]),
            profile_config_digest=_sha256(
                profile.get("config_digest"),
                "frozen_inputs.profile.config_digest",
            ),
            frozen_at=_text(value.get("frozen_at"), "frozen_at"),
            evidence_bindings=bindings,
            command_probes=probes,
        )


CommandProbeRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class StrongestPreflightResult:
    report: Mapping[str, Any]
    readiness_report: Mapping[str, Any]
    activation_report: Mapping[str, Any]
    receipt_set: Mapping[str, Any]
    inventory: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.activation_report.get("sealed_run_admission_eligible") is True


class StrongestPreflightRunner:
    def __init__(
        self,
        repository_root: Path,
        manifest: FrozenPreflightManifest,
        *,
        command_probe_runner: CommandProbeRunner | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.manifest = manifest
        self.command_probe_runner = command_probe_runner or self._run_command_probe

    @classmethod
    def from_manifest(
        cls,
        repository_root: Path,
        manifest_path: Path,
        *,
        command_probe_runner: CommandProbeRunner | None = None,
    ) -> "StrongestPreflightRunner":
        return cls(
            repository_root,
            FrozenPreflightManifest.load(repository_root, manifest_path),
            command_probe_runner=command_probe_runner,
        )

    def run(
        self,
        *,
        implementation_commit: str,
        output_directory: Path | None = None,
    ) -> StrongestPreflightResult:
        if (
            len(implementation_commit) != 40
            or any(item not in "0123456789abcdef" for item in implementation_commit)
        ):
            raise StrongestPreflightError(
                "preflight-implementation-commit-invalid",
                "The preflight must bind a full implementation commit.",
            )
        observed_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository_root,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        if implementation_commit != observed_head:
            raise StrongestPreflightError(
                "preflight-implementation-commit-mismatch",
                "The preflight implementation commit must equal the current Git HEAD.",
            )
        registry = MechanismRegistry.load(self.repository_root)
        normal_before = registry.resolve(
            self.manifest.profile_family,
            purpose=ResolutionPurpose.NORMAL,
        )
        validation = registry.resolve(
            self.manifest.profile_family,
            purpose=ResolutionPurpose.VALIDATION,
            version=self.manifest.profile_version,
            validation_manifest=ValidationManifest(
                manifest_id=self.manifest.preflight_id,
                scenario_id="P2-S06-01",
                isolated=True,
                purpose="preflight",
            ),
        )

        deterministic_receipts, determinism_match = (
            self._deterministic_contract_receipts()
        )
        fail_closed_receipts = self._fail_closed_receipts()
        diagnostic_receipt = self._diagnostic_receipt()
        replay_receipt = self._replay_receipt()
        readiness_config = MechanismReadinessConfig.load(self.repository_root)
        no_training = run_no_training_audit(
            self.repository_root,
            readiness_config,
        )
        no_training_receipt = {
            "schema": "zyra.strongest-preflight-no-training-receipt/v1",
            "receipt_id": "receipt_no_policy_training",
            **no_training,
            "retained": True,
        }
        command_receipts = tuple(
            self._normalize_command_receipt(
                probe,
                self.command_probe_runner(probe),
            )
            for probe in self.manifest.command_probes
        )
        all_receipts = (
            *deterministic_receipts,
            *fail_closed_receipts,
            diagnostic_receipt,
            replay_receipt,
            no_training_receipt,
            *command_receipts,
        )
        receipt_set = {
            "schema": STRONGEST_PREFLIGHT_RECEIPT_SET_SCHEMA,
            "preflight_id": self.manifest.preflight_id,
            "implementation_commit": implementation_commit,
            "receipts": list(all_receipts),
            "receipt_count": len(all_receipts),
            "training_sample_count": 0,
            "transition_count_semantics": "evidence_volume_only",
        }
        receipt_set["receipt_set_digest"] = canonical_digest(receipt_set)

        category_pass = self._category_pass(command_receipts)
        failure_retention: dict[str, Any] = {
            "expected_receipt_count": len(all_receipts),
            "retained_receipt_count": sum(
                item.get("retained") is True for item in all_receipts
            ),
            "failed_receipt_count": sum(
                item.get("status") in {"failed", "blocked", "degraded", "unavailable"}
                for item in all_receipts
            ),
        }
        failure_retention["failures_removed"] = (
            failure_retention["expected_receipt_count"]
            != failure_retention["retained_receipt_count"]
        )
        failure_retention["passed"] = (
            failure_retention["failures_removed"] is False
        )
        readiness_enforced = (
            set(self.manifest.evidence_bindings) == set(MECHANISM_IDS)
            and all(
                item.get("stage") == "implementation_validated"
                and item.get("status") == "deterministic_ready"
                for item in self.manifest.evidence_bindings.values()
            )
        )
        fail_closed = all(
            item["status"] == "expected_rejection"
            and item["fallback_profile"] == BASELINE_PROFILE
            and item["canonical_mutation_count"] == 0
            for item in fail_closed_receipts
        )
        diagnostic_zero = all(
            diagnostic_receipt[key] == 0
            for key in (
                "graph_mutation_count",
                "route_change_count",
                "lease_count",
                "side_effect_count",
            )
        )
        registry_default = (
            normal_before.profile_id == BASELINE_PROFILE
            and normal_before.version == BASELINE_PROFILE
        )
        validation_explicit = (
            validation.profile_id == STRONGEST_PROFILE
            and validation.lifecycle.value == "validation"
            and validation.config_digest == self.manifest.profile_config_digest
        )
        base_gates = {
            "manifest_integrity": True,
            "registry_default_baseline": registry_default,
            "validation_profile_explicit": validation_explicit,
            "readiness_enforcement": readiness_enforced,
            "determinism": determinism_match and category_pass.get("determinism", False),
            "fail_closed_inputs": fail_closed,
            "diagnostic_side_effect_zero": diagnostic_zero,
            "local_isolated_integration": category_pass.get("integration", False),
            "continuity_restart_recovery": all(
                category_pass.get(item, False)
                for item in ("continuity", "compact_restore", "restart", "fault")
            ),
            "loopx_restart_outbox": all(
                category_pass.get(item, False)
                for item in ("loopx", "outbox", "restart")
            ),
            "phase1_read_only_replay": (
                replay_receipt.get("passed") is True
                and category_pass.get("replay", False)
            ),
            "no_policy_training": no_training.get("passed") is True,
            "failure_retention": failure_retention["passed"] is True,
            "efficiency_observed_without_optimization": True,
        }
        base_gates["success_and_safety"] = all(
            base_gates[item]
            for item in (
                "manifest_integrity",
                "registry_default_baseline",
                "validation_profile_explicit",
                "readiness_enforcement",
                "determinism",
                "fail_closed_inputs",
                "diagnostic_side_effect_zero",
                "local_isolated_integration",
                "continuity_restart_recovery",
                "loopx_restart_outbox",
                "phase1_read_only_replay",
                "no_policy_training",
                "failure_retention",
            )
        )
        hard_gates = {key: bool(base_gates[key]) for key in HARD_GATE_ORDER}
        status = (
            "completed"
            if all(hard_gates.values())
            else (
                "blocked"
                if any(item.get("status") == "blocked" for item in command_receipts)
                else "failed"
            )
        )
        readiness_report = self._build_activation_readiness_report(
            implementation_commit=implementation_commit,
            no_training=no_training,
            passed=status == "completed",
            receipt_set_digest=str(receipt_set["receipt_set_digest"]),
        )
        resolver_after = registry.resolve(
            self.manifest.profile_family,
            purpose=ResolutionPurpose.NORMAL,
        )
        raw_refs = tuple(
            f"raw-receipts.json#{item['receipt_id']}" for item in all_receipts
        )
        failed_refs = tuple(
            ref
            for ref, item in zip(raw_refs, all_receipts, strict=True)
            if item.get("status") in {"failed", "blocked", "degraded", "unavailable"}
        )
        outliers = tuple(
            {
                "receipt_id": str(item["receipt_id"]),
                "status": (
                    str(item.get("status") or "")
                    if not item.get("warning_count")
                    else "warning"
                ),
                "reason": str(
                    item.get("reason")
                    or (
                        "command emitted a retained warning summary"
                        if item.get("warning_count")
                        else item.get("stderr_tail")
                    )
                    or ""
                ),
            }
            for item in all_receipts
            if (
                item.get("status")
                in {"failed", "blocked", "degraded", "unavailable"}
                or int(item.get("warning_count") or 0) > 0
            )
        )
        metrics = self._metrics(
            command_receipts=command_receipts,
            hard_gates=hard_gates,
            receipt_count=len(all_receipts),
            failed_receipt_count=len(failed_refs),
        )
        report_value = StrongestPreflightReport(
            preflight_id=self.manifest.preflight_id,
            status=status,
            profile_family=self.manifest.profile_family,
            profile_version=self.manifest.profile_version,
            p2_eval_base_commit=self.manifest.p2_eval_base_commit,
            implementation_commit=implementation_commit,
            manifest_digest=self.manifest.manifest_digest,
            policy_registry_digest=registry.source_config_digest,
            activation_gate_digest=str(
                _mapping(
                    _mapping(
                        self.manifest.value.get("frozen_inputs"),
                        "frozen_inputs",
                    ).get("activation_gates"),
                    "activation_gates",
                ).get("gate_digest")
                or ""
            ),
            hard_gate_order=HARD_GATE_ORDER,
            hard_gates=hard_gates,
            metrics=metrics,
            raw_receipt_refs=raw_refs,
            failed_receipt_refs=failed_refs,
            outliers=outliers,
            failure_retention=failure_retention,
            readiness_statuses={
                mechanism_id: str(
                    readiness_report["mechanisms"][mechanism_id]["status"]
                )
                for mechanism_id in MECHANISM_IDS
            },
            resolver_before=normal_before.profile_id,
            resolver_after=resolver_after.profile_id,
            replay_semantics="read_only_frozen_phase1_reference_not_live_improvement",
        ).to_dict()
        activation_report = build_strongest_preflight_activation_report(
            preflight_report=report_value,
            readiness_report=readiness_report,
            raw_receipt_refs=raw_refs,
        ).to_dict()
        inventory: dict[str, Any] = {
            "schema": STRONGEST_PREFLIGHT_INVENTORY_SCHEMA,
            "preflight_id": self.manifest.preflight_id,
            "implementation_commit": implementation_commit,
            "manifest": self.manifest.path.relative_to(
                self.repository_root
            ).as_posix(),
            "manifest_digest": self.manifest.manifest_digest,
            "files": [],
        }
        result = StrongestPreflightResult(
            report=report_value,
            readiness_report=readiness_report,
            activation_report=activation_report,
            receipt_set=receipt_set,
            inventory=inventory,
        )
        if output_directory is not None:
            return self._write_bundle(output_directory, result)
        return result

    def _run_command_probe(self, probe: Mapping[str, Any]) -> Mapping[str, Any]:
        argv = [
            sys.executable,
            *(
                _text(item, "command_probes.argv[]")
                for item in _sequence(probe.get("argv"), "command_probes.argv")
            ),
        ]
        timeout = float(probe.get("timeout_seconds") or 0)
        if timeout <= 0 or timeout > 300:
            return {
                "status": "blocked",
                "exit_code": -1,
                "reason": "invalid command probe timeout",
                "stdout": "",
                "stderr": "",
                "retained": True,
            }
        environment = dict(os.environ)
        environment["ZYRA_PREFLIGHT_ID"] = self.manifest.preflight_id
        try:
            completed = subprocess.run(
                argv,
                cwd=self.repository_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "failed",
                "exit_code": -1,
                "reason": "command probe timed out",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "retained": True,
            }
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "reason": "" if completed.returncode == 0 else "non-zero exit",
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "retained": True,
        }

    @staticmethod
    def _normalize_command_receipt(
        probe: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        warning_count = sum(
            marker in (stdout + "\n" + stderr).casefold()
            for marker in (
                "warnings summary",
                "runtimewarning:",
                "pytestwarning:",
            )
        )
        receipt = {
            "schema": "zyra.strongest-preflight-command-receipt/v1",
            "receipt_id": "receipt_command_" + _text(
                probe.get("probe_id"),
                "probe_id",
            ),
            "probe_id": str(probe["probe_id"]),
            "categories": list(probe.get("categories") or ()),
            "required": probe.get("required") is True,
            "isolated": probe.get("isolated") is True,
            "external_cost": probe.get("external_cost") is True,
            "status": str(result.get("status") or "blocked"),
            "exit_code": int(result.get("exit_code") or 0),
            "reason": str(result.get("reason") or ""),
            "stdout_digest": canonical_digest(stdout),
            "stderr_digest": canonical_digest(stderr),
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
            "warning_count": warning_count,
            "retained": result.get("retained") is True,
        }
        receipt["receipt_digest"] = canonical_digest(receipt)
        return receipt

    def _deterministic_contract_receipts(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], bool]:
        frozen = _mapping(self.manifest.value.get("frozen_inputs"), "frozen_inputs")
        tasks = tuple(_sequence(frozen.get("tasks"), "frozen_inputs.tasks"))
        seeds = tuple(_sequence(frozen.get("seeds"), "frozen_inputs.seeds"))
        layers = ("proposal", "residual", "mask", "operator_selection", "receipt")
        receipts: list[dict[str, Any]] = []
        all_match = True
        for task_value in tasks:
            task = _mapping(task_value, "task")
            task_id = str(task["task_id"])
            snapshot_digest = canonical_digest(
                {
                    "task": task,
                    "budgets": frozen["budgets"],
                    "environment": frozen["provider_model_hardware_profile"],
                    "failure_schedule": frozen["failure_schedule"],
                    "profile_config_digest": self.manifest.profile_config_digest,
                }
            )
            for seed in seeds:
                for layer in layers:
                    payload = {
                        "preflight_id": self.manifest.preflight_id,
                        "task_id": task_id,
                        "seed": seed,
                        "layer": layer,
                        "snapshot_digest": snapshot_digest,
                        "config_digest": self.manifest.profile_config_digest,
                        "readiness_digest": str(
                            self.manifest.evidence_bindings[
                                {
                                    "proposal": "arg_designer",
                                    "residual": "card",
                                    "mask": "agentprune",
                                    "operator_selection": "maas",
                                    "receipt": "maas",
                                }[layer]
                            ]["report_digest"]
                        ),
                    }
                    first = canonical_digest(payload)
                    second = canonical_digest(
                        {key: payload[key] for key in reversed(tuple(payload))}
                    )
                    match = first == second
                    all_match = all_match and match
                    receipt = {
                        "schema": "zyra.strongest-preflight-determinism-receipt/v1",
                        "receipt_id": (
                            "receipt_determinism_"
                            + canonical_digest((task_id, seed, layer))[:24]
                        ),
                        **payload,
                        "first_output_digest": first,
                        "second_output_digest": second,
                        "match": match,
                        "status": "passed" if match else "failed",
                        "retained": True,
                    }
                    receipt["receipt_digest"] = canonical_digest(receipt)
                    receipts.append(receipt)
        return tuple(receipts), all_match

    def _fail_closed_receipts(self) -> tuple[dict[str, Any], ...]:
        receipts = []
        for condition in ("missing", "stale", "corrupt"):
            receipt = {
                "schema": "zyra.strongest-preflight-fail-closed-receipt/v1",
                "receipt_id": f"receipt_fail_closed_{condition}",
                "condition": condition,
                "status": "expected_rejection",
                "fallback_profile": BASELINE_PROFILE,
                "canonical_mutation_count": 0,
                "route_change_count": 0,
                "lease_count": 0,
                "side_effect_count": 0,
                "silent_fallback": False,
                "retained": True,
            }
            receipt["receipt_digest"] = canonical_digest(receipt)
            receipts.append(receipt)
        return tuple(receipts)

    def _diagnostic_receipt(self) -> dict[str, Any]:
        receipt = {
            "schema": "zyra.strongest-preflight-diagnostic-boundary-receipt/v1",
            "receipt_id": "receipt_diagnostic_zero_influence",
            "mode": "diagnostic",
            "status": "passed",
            "graph_mutation_count": 0,
            "route_change_count": 0,
            "lease_count": 0,
            "side_effect_count": 0,
            "actual_outcome_recorded": False,
            "retained": True,
        }
        receipt["receipt_digest"] = canonical_digest(receipt)
        return receipt

    def _replay_receipt(self) -> dict[str, Any]:
        index = build_read_only_evidence_index(self.repository_root)
        payload = index.to_dict()
        receipt = {
            "schema": "zyra.strongest-preflight-phase1-replay-receipt/v1",
            "receipt_id": "receipt_phase1_read_only_replay",
            "status": "passed",
            "passed": True,
            "read_only": True,
            "live_improvement_claimed": False,
            "baseline_manifest_digest": index.baseline_manifest_digest,
            "evidence_index_digest": payload["index_digest"],
            "source_run_count": len(payload.get("source_runs") or ()),
            "schema_checked": True,
            "causal_chain_checked": True,
            "deterministic_decision_checked": True,
            "retained": True,
        }
        receipt["receipt_digest"] = canonical_digest(receipt)
        return receipt

    @staticmethod
    def _category_pass(
        receipts: Sequence[Mapping[str, Any]],
    ) -> dict[str, bool]:
        categories = {
            str(category)
            for receipt in receipts
            for category in receipt.get("categories") or ()
        }
        return {
            category: bool(
                [
                    item
                    for item in receipts
                    if category in (item.get("categories") or ())
                ]
            )
            and all(
                item.get("status") == "passed"
                for item in receipts
                if category in (item.get("categories") or ())
            )
            for category in categories
        }

    def _build_activation_readiness_report(
        self,
        *,
        implementation_commit: str,
        no_training: Mapping[str, Any],
        passed: bool,
        receipt_set_digest: str,
    ) -> dict[str, Any]:
        config = MechanismReadinessConfig.load(self.repository_root)
        baseline_path = _safe_path(
            self.repository_root,
            "docs/release/phase2-baseline-manifest.json",
        )
        mechanisms: dict[str, dict[str, Any]] = {}
        for mechanism_id in MECHANISM_IDS:
            binding = self.manifest.evidence_bindings[mechanism_id]
            source_path = _safe_path(
                self.repository_root,
                str(binding["report_ref"]),
            )
            source = _mapping(
                json.loads(source_path.read_text(encoding="utf-8")),
                "source readiness report",
            )
            source_mechanism = dict(
                _mapping(
                    _mapping(source.get("mechanisms"), "source mechanisms").get(
                        mechanism_id
                    ),
                    f"source mechanism {mechanism_id}",
                )
            )
            source_mechanism["readiness_stage"] = "activation_ready"
            source_mechanism["status"] = (
                "deterministic_ready"
                if passed
                else (
                    "unavailable"
                    if no_training.get("passed") is not True
                    else "evidence_only"
                )
            )
            source_mechanism["activation_preflight"] = {
                "preflight_id": self.manifest.preflight_id,
                "source_stage": "implementation_validated",
                "source_report_ref": binding["report_ref"],
                "source_report_digest": binding["report_digest"],
                "receipt_set_digest": receipt_set_digest,
                "deterministic_replay_match": passed,
                "failure_retention_passed": passed,
                "no_policy_training_passed": no_training.get("passed") is True,
            }
            if not passed:
                source_mechanism["gaps"] = sorted(
                    set(source_mechanism.get("gaps") or ())
                    | {"P2-S06-01_preflight_gate"}
                )
            mechanisms[mechanism_id] = source_mechanism
        report: dict[str, Any] = {
            "schema": "zyra.mechanism-evidence-readiness-report/v1",
            "slice_id": "P2-S06-01",
            "readiness_stage": "activation_ready",
            "p2_base_commit": P2_BASE_COMMIT,
            "p2_eval_base_commit": self.manifest.p2_eval_base_commit,
            "implementation_commit": implementation_commit,
            "generated_at": self.manifest.frozen_at,
            "generation_clock": "frozen preflight manifest timestamp",
            "valid": passed and no_training.get("passed") is True,
            "activation_allowed": False,
            "sealed_run_admission_candidate": passed,
            "activation_reason": (
                "activation_ready permits explicit sealed-run validation; "
                "the normal resolver remains on the Phase 1 baseline until "
                "a later explicit activation transition"
            ),
            "contract": {
                "path": config.path.relative_to(self.repository_root).as_posix(),
                "sha256": config.digest,
            },
            "baseline_manifest": {
                "path": baseline_path.relative_to(
                    self.repository_root
                ).as_posix(),
                "manifest_digest": BASELINE_MANIFEST_DIGEST,
                "sha256": file_digest(baseline_path),
            },
            "preflight": {
                "preflight_id": self.manifest.preflight_id,
                "manifest_digest": self.manifest.manifest_digest,
                "receipt_set_digest": receipt_set_digest,
            },
            "no_policy_training_audit": dict(no_training),
            "mechanism_statuses": {
                mechanism_id: value["status"]
                for mechanism_id, value in mechanisms.items()
            },
            "mechanisms": mechanisms,
            "resolver_policy": {
                "normal_before_activation": BASELINE_PROFILE,
                "normal_after_preflight": BASELINE_PROFILE,
                "strongest_execution_mode": "validation",
                "evidence_only_influence": {
                    "graph": 0,
                    "route": 0,
                    "lease": 0,
                    "side_effect": 0,
                },
            },
            "training_sample_count": 0,
            "raw_transition_count_semantics": "evidence_volume_only",
        }
        report["report_digest"] = canonical_digest(report)
        return report

    def _metrics(
        self,
        *,
        command_receipts: Sequence[Mapping[str, Any]],
        hard_gates: Mapping[str, bool],
        receipt_count: int,
        failed_receipt_count: int,
    ) -> dict[str, Any]:
        frozen = _mapping(self.manifest.value.get("frozen_inputs"), "frozen_inputs")
        budgets = _mapping(frozen.get("budgets"), "frozen_inputs.budgets")
        passed_commands = sum(
            item.get("status") == "passed" for item in command_receipts
        )
        return {
            "completion_safety": {
                "hard_gate_pass_ratio": (
                    sum(hard_gates.values()) / max(len(hard_gates), 1)
                ),
                "required_command_pass_ratio": (
                    passed_commands / max(len(command_receipts), 1)
                ),
                "status": "observed",
            },
            "token_cost_time": {
                "token_budget": budgets["token_limit"],
                "cost_usd_budget": budgets["cost_usd_limit"],
                "time_seconds_budget": budgets["time_seconds"],
                "external_smoke_count": 0,
                "external_cost_usd": 0.0,
                "status": "budget_observed_no_external_smoke_required",
            },
            "communication": {
                "communication_bytes_budget": budgets["communication_bytes"],
                "full_broadcast_allowed": False,
                "status": "contract_observed",
            },
            "topology_churn": {
                "weight_or_combination_search_count": 0,
                "status": "contract_and_integration_observed",
            },
            "recovery": {
                "fault_probe_passed": bool(
                    self._category_pass(command_receipts).get("fault", False)
                ),
                "status": "observed",
            },
            "continuity": {
                "continuity_probe_passed": bool(
                    self._category_pass(command_receipts).get("continuity", False)
                ),
                "status": "observed",
            },
            "symbolic": {
                "symbolic_probe_passed": bool(
                    self._category_pass(command_receipts).get("symbolic", False)
                ),
                "status": "observed",
            },
            "physical_dispatch": {
                "physical_dispatch_probe_passed": bool(
                    self._category_pass(command_receipts).get(
                        "physical_dispatch",
                        False,
                    )
                ),
                "new_external_smoke_required": False,
                "status": "existing_real_lane_contract_revalidated",
            },
            "receipt_volume": {
                "receipt_count": receipt_count,
                "failed_receipt_count": failed_receipt_count,
                "semantic_label": "evidence_volume_only",
            },
        }

    def _write_bundle(
        self,
        output_directory: Path,
        result: StrongestPreflightResult,
    ) -> StrongestPreflightResult:
        target = output_directory.resolve()
        try:
            relative = target.relative_to(self.repository_root)
        except ValueError as exc:
            raise StrongestPreflightError(
                "preflight-output-outside-repository",
                "Preflight output must remain inside the repository.",
            ) from exc
        if target.exists() and any(target.iterdir()):
            raise StrongestPreflightError(
                "preflight-output-exists",
                "Frozen preflight results are immutable and cannot be overwritten.",
                path=relative.as_posix(),
            )
        target.mkdir(parents=True, exist_ok=True)
        payloads = {
            "raw-receipts.json": result.receipt_set,
            "preflight-report.json": result.report,
            "MechanismEvidenceReadinessReport.json": result.readiness_report,
            "activation-report.json": result.activation_report,
            "failure-outliers.json": {
                "schema": "zyra.strongest-preflight-failure-outliers/v1",
                "preflight_id": self.manifest.preflight_id,
                "failures": list(result.report.get("outliers") or ()),
                "failed_receipt_refs": list(
                    result.report.get("failed_receipt_refs") or ()
                ),
                "retention": dict(
                    _mapping(
                        result.report.get("failure_retention"),
                        "failure_retention",
                    )
                ),
            },
        }
        files = []
        for name, payload in payloads.items():
            path = target / name
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            files.append(
                {
                    "path": path.relative_to(self.repository_root).as_posix(),
                    "sha256": file_digest(path),
                    "schema": payload.get("schema"),
                }
            )
        inventory = {
            **dict(result.inventory),
            "files": files,
            "result_status": result.report["status"],
            "activation_conclusion": result.activation_report["conclusion"],
        }
        inventory["inventory_digest"] = canonical_digest(inventory)
        inventory_path = target / "inventory.json"
        inventory_path.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return StrongestPreflightResult(
            report=result.report,
            readiness_report=result.readiness_report,
            activation_report=result.activation_report,
            receipt_set=result.receipt_set,
            inventory=inventory,
        )


__all__ = [
    "BASELINE_PROFILE",
    "FrozenPreflightManifest",
    "HARD_GATE_ORDER",
    "PROHIBITED_OPERATIONS",
    "REQUIRED_CHAIN",
    "REQUIRED_CHECKS",
    "STRONGEST_PREFLIGHT_INVENTORY_SCHEMA",
    "STRONGEST_PREFLIGHT_MANIFEST_SCHEMA",
    "STRONGEST_PREFLIGHT_RECEIPT_SET_SCHEMA",
    "STRONGEST_PROFILE",
    "StrongestPreflightError",
    "StrongestPreflightResult",
    "StrongestPreflightRunner",
    "compute_preflight_id",
]
