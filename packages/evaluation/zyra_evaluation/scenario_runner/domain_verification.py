from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .canonical import canonical_json, canonicalize, digest, file_digest, path_within
from .errors import conflict, invalid, unavailable
from .live_models import (
    ActionKind,
    ActionResult,
    DomainInput,
    DomainVerification,
    FaultKind,
    FaultObservation,
    LiveDomain,
    LivePlan,
    PrivacyClass,
    ProviderObservation,
    TierKind,
    TierObservation,
    VerificationFinding,
    VerificationSeverity,
)


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    command_id: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout_path: str
    stderr_path: str
    stdout_digest: str
    stderr_digest: str
    started_at: str
    completed_at: str
    timed_out: bool
    metadata: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CommandEvidence":
        return cls(
            command_id=str(value.get("command_id") or ""),
            argv=tuple(str(item) for item in value.get("argv") or ()),
            cwd=str(value.get("cwd") or ""),
            exit_code=int(value.get("exit_code") or 0),
            stdout_path=str(value.get("stdout_path") or ""),
            stderr_path=str(value.get("stderr_path") or ""),
            stdout_digest=str(value.get("stdout_digest") or ""),
            stderr_digest=str(value.get("stderr_digest") or ""),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            timed_out=value.get("timed_out") is True,
            metadata=dict(value.get("metadata") or {}),
        )

    def validate(self, *, workspace_root: str | Path) -> list[VerificationFinding]:
        findings: list[VerificationFinding] = []
        if not self.command_id:
            findings.append(
                _finding(
                    "command.identity_missing",
                    VerificationSeverity.BLOCKER,
                    "Command receipt lacks identity.",
                    "command",
                )
            )
        if not self.argv:
            findings.append(
                _finding(
                    "command.argv_missing",
                    VerificationSeverity.BLOCKER,
                    "Command receipt lacks an executable argument vector.",
                    self.command_id,
                )
            )
        root = Path(workspace_root).resolve(strict=False)
        cwd = Path(self.cwd).resolve(strict=False)
        if not path_within(cwd, root):
            findings.append(
                _finding(
                    "command.cwd_escape",
                    VerificationSeverity.BLOCKER,
                    "Verification command ran outside the admitted workspace.",
                    self.command_id,
                    expected=str(root),
                    observed=str(cwd),
                )
            )
        for label, path_value, declared in (
            ("stdout", self.stdout_path, self.stdout_digest),
            ("stderr", self.stderr_path, self.stderr_digest),
        ):
            path = Path(path_value).resolve(strict=False)
            if not path_within(path, root):
                findings.append(
                    _finding(
                        f"command.{label}_escape",
                        VerificationSeverity.BLOCKER,
                        f"{label} evidence is outside the workspace.",
                        self.command_id,
                        observed=str(path),
                    )
                )
                continue
            if not path.is_file():
                findings.append(
                    _finding(
                        f"command.{label}_missing",
                        VerificationSeverity.ERROR,
                        f"{label} evidence is missing.",
                        self.command_id,
                        observed=str(path),
                    )
                )
                continue
            observed, _ = file_digest(path)
            if observed != declared:
                findings.append(
                    _finding(
                        f"command.{label}_digest_mismatch",
                        VerificationSeverity.BLOCKER,
                        f"{label} bytes do not match the command receipt.",
                        self.command_id,
                        expected=declared,
                        observed=observed,
                        refs=(str(path),),
                    )
                )
        if self.timed_out:
            findings.append(
                _finding(
                    "command.timed_out",
                    VerificationSeverity.ERROR,
                    "Verification command exceeded its bounded timeout.",
                    self.command_id,
                )
            )
        elif self.exit_code != 0:
            findings.append(
                _finding(
                    "command.nonzero_exit",
                    VerificationSeverity.ERROR,
                    "Verification command returned a non-zero exit code.",
                    self.command_id,
                    expected=0,
                    observed=self.exit_code,
                )
            )
        return findings


@dataclass(frozen=True, slots=True)
class CitationEvidence:
    citation_id: str
    claim_id: str
    source_id: str
    url: str
    source_digest: str
    quote_digest: str
    quote_text: str
    byte_start: int
    byte_end: int
    acquired_path: str
    acquired_at: str
    status: int
    media_type: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CitationEvidence":
        return cls(
            citation_id=str(value.get("citation_id") or ""),
            claim_id=str(value.get("claim_id") or ""),
            source_id=str(value.get("source_id") or ""),
            url=str(value.get("url") or ""),
            source_digest=str(value.get("source_digest") or ""),
            quote_digest=str(value.get("quote_digest") or ""),
            quote_text=str(value.get("quote_text") or ""),
            byte_start=int(value.get("byte_start") or 0),
            byte_end=int(value.get("byte_end") or 0),
            acquired_path=str(value.get("acquired_path") or ""),
            acquired_at=str(value.get("acquired_at") or ""),
            status=int(value.get("status") or 0),
            media_type=str(value.get("media_type") or ""),
        )


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    claim_id: str
    statement: str
    normalized_statement: str
    citation_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    confidence: float
    agreement: str
    deterministic: bool
    uncertainty: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClaimEvidence":
        return cls(
            claim_id=str(value.get("claim_id") or ""),
            statement=str(value.get("statement") or ""),
            normalized_statement=str(value.get("normalized_statement") or ""),
            citation_ids=tuple(str(item) for item in value.get("citation_ids") or ()),
            source_ids=tuple(str(item) for item in value.get("source_ids") or ()),
            confidence=float(value.get("confidence") or 0),
            agreement=str(value.get("agreement") or ""),
            deterministic=value.get("deterministic") is True,
            uncertainty=str(value.get("uncertainty") or ""),
        )


class ArtifactIntegrityVerifier:
    def __init__(self, *, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root).resolve(strict=False)

    def verify_many(
        self,
        artifacts: Iterable[Mapping[str, Any]],
        *,
        minimum_count: int = 1,
    ) -> tuple[list[VerificationFinding], dict[str, str]]:
        findings: list[VerificationFinding] = []
        observed: dict[str, str] = {}
        identities: set[str] = set()
        values = tuple(artifacts)
        if len(values) < minimum_count:
            findings.append(
                _finding(
                    "artifact.minimum_unmet",
                    VerificationSeverity.BLOCKER,
                    "Delivery did not produce the required artifacts.",
                    "artifact-set",
                    expected=minimum_count,
                    observed=len(values),
                )
            )
        for item in values:
            artifact_id = str(item.get("artifact_id") or "")
            path_value = str(
                item.get("uri")
                or item.get("path")
                or item.get("absolute_path")
                or ""
            )
            if not artifact_id:
                findings.append(
                    _finding(
                        "artifact.identity_missing",
                        VerificationSeverity.BLOCKER,
                        "Artifact receipt lacks identity.",
                        path_value or "unknown-artifact",
                    )
                )
                continue
            if artifact_id in identities:
                findings.append(
                    _finding(
                        "artifact.identity_duplicate",
                        VerificationSeverity.BLOCKER,
                        "Artifact identity is duplicated.",
                        artifact_id,
                    )
                )
                continue
            identities.add(artifact_id)
            path = Path(path_value).resolve(strict=False)
            if not path_within(path, self.artifact_root):
                findings.append(
                    _finding(
                        "artifact.path_escape",
                        VerificationSeverity.BLOCKER,
                        "Artifact path is outside the canonical artifact owner.",
                        artifact_id,
                        expected=str(self.artifact_root),
                        observed=str(path),
                    )
                )
                continue
            if path.is_symlink():
                findings.append(
                    _finding(
                        "artifact.symlink_forbidden",
                        VerificationSeverity.BLOCKER,
                        "Formal artifact cannot be a symlink.",
                        artifact_id,
                        observed=str(path),
                    )
                )
                continue
            if not path.is_file():
                findings.append(
                    _finding(
                        "artifact.bytes_missing",
                        VerificationSeverity.BLOCKER,
                        "Artifact bytes are unavailable.",
                        artifact_id,
                        observed=str(path),
                    )
                )
                continue
            checksum, size = file_digest(path)
            declared = str(
                item.get("sha256")
                or (item.get("metadata") or {}).get("sha256")
                or ""
            )
            if declared and declared != checksum:
                findings.append(
                    _finding(
                        "artifact.digest_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Artifact bytes do not match their owner checksum.",
                        artifact_id,
                        expected=declared,
                        observed=checksum,
                        refs=(str(path),),
                    )
                )
            if size <= 0:
                findings.append(
                    _finding(
                        "artifact.empty",
                        VerificationSeverity.ERROR,
                        "Artifact is empty.",
                        artifact_id,
                        observed=size,
                    )
                )
            observed[artifact_id] = checksum
        return findings, observed


class EventCausalityVerifier:
    def verify(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        expected_run_id: str,
        expected_task_id: str,
        minimum_transitions: int,
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        identities: set[str] = set()
        known: set[str] = set()
        semantic: set[str] = set()
        last_sequence = 0
        roots = 0
        leaves: set[str] = set()
        referenced: set[str] = set()
        effect_counts: Counter[str] = Counter()
        for position, event in enumerate(events, start=1):
            event_id = str(event.get("event_id") or "")
            event_type = str(event.get("event_type") or "")
            run_id = str(event.get("run_id") or "")
            task_id = str(event.get("task_id") or "")
            payload = event.get("payload")
            payload = dict(payload) if isinstance(payload, Mapping) else {}
            metadata = event.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
            if not event_id or not event_type:
                findings.append(
                    _finding(
                        "event.identity_missing",
                        VerificationSeverity.BLOCKER,
                        "Canonical event lacks identity or type.",
                        f"position:{position}",
                    )
                )
                continue
            if event_id in identities:
                findings.append(
                    _finding(
                        "event.identity_duplicate",
                        VerificationSeverity.BLOCKER,
                        "Canonical event identity is duplicated.",
                        event_id,
                    )
                )
                continue
            identities.add(event_id)
            if run_id != expected_run_id or task_id != expected_task_id:
                findings.append(
                    _finding(
                        "event.scope_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Canonical event is outside the owner run/task partition.",
                        event_id,
                        expected={"run_id": expected_run_id, "task_id": expected_task_id},
                        observed={"run_id": run_id, "task_id": task_id},
                    )
                )
            sequence = int(event.get("sequence") or position)
            if sequence <= last_sequence:
                findings.append(
                    _finding(
                        "event.sequence_non_monotonic",
                        VerificationSeverity.BLOCKER,
                        "Canonical event sequence is not strictly increasing.",
                        event_id,
                        expected=f">{last_sequence}",
                        observed=sequence,
                    )
                )
            last_sequence = sequence
            parent = str(
                event.get("causation_id")
                or event.get("parent_event_id")
                or payload.get("causation_id")
                or ""
            )
            if parent:
                referenced.add(parent)
                if parent not in known:
                    findings.append(
                        _finding(
                            "event.causation_forward_or_missing",
                            VerificationSeverity.BLOCKER,
                            "Canonical event references an unavailable prior cause.",
                            event_id,
                            observed=parent,
                        )
                    )
            else:
                roots += 1
                if position != 1:
                    findings.append(
                        _finding(
                            "event.unexpected_root",
                            VerificationSeverity.ERROR,
                            "Only the first event may be a causal root.",
                            event_id,
                        )
                    )
            known.add(event_id)
            leaves.add(event_id)
            leaves.discard(parent)
            effect = str(
                metadata.get("semantic_effect")
                or payload.get("semantic_effect")
                or ""
            )
            if effect:
                effect_counts[effect] += 1
            semantic_payload = {
                "event_type": event_type,
                "effect": effect,
                "stage": str(metadata.get("stage") or payload.get("stage") or ""),
                "worker_id": str(metadata.get("worker_id") or payload.get("worker_id") or ""),
                "mutation": canonicalize(
                    payload.get("mutation")
                    or payload.get("result")
                    or payload.get("artifact")
                    or payload.get("state")
                    or payload
                ),
            }
            semantic_digest = digest(semantic_payload)
            if semantic_digest in semantic:
                findings.append(
                    _finding(
                        "event.semantic_duplicate",
                        VerificationSeverity.BLOCKER,
                        "Repeated semantic mutations cannot inflate effective transitions.",
                        event_id,
                    )
                )
            semantic.add(semantic_digest)
        checks["transition_minimum"] = len(events) >= minimum_transitions
        checks["identity_unique"] = len(identities) == len(events)
        checks["single_causal_root"] = roots == 1
        checks["single_delivery_leaf"] = len(leaves) == 1
        checks["semantic_unique"] = len(semantic) == len(events)
        required_effects = {
            "state_mutation",
            "route",
            "placement",
            "tool",
            "verification",
            "permission",
            "compact_restore",
            "fault",
            "recovery",
            "artifact",
            "delivery",
            "topology",
            "memory",
        }
        checks["effect_coverage"] = required_effects.issubset(effect_counts)
        if not checks["transition_minimum"]:
            findings.append(
                _finding(
                    "event.transition_minimum_unmet",
                    VerificationSeverity.BLOCKER,
                    "Live scenario did not reach its effective transition floor.",
                    "event-stream",
                    expected=minimum_transitions,
                    observed=len(events),
                )
            )
        missing_effects = sorted(required_effects - set(effect_counts))
        if missing_effects:
            findings.append(
                _finding(
                    "event.effect_coverage_missing",
                    VerificationSeverity.BLOCKER,
                    "Live scenario lacks required semantic effect families.",
                    "event-stream",
                    expected=sorted(required_effects),
                    observed=sorted(effect_counts),
                )
            )
        return findings, checks


class PlacementVerifier:
    def verify(
        self,
        *,
        domain_input: DomainInput,
        actions: Sequence[ActionResult],
        tiers: Sequence[TierObservation],
        providers: Sequence[ProviderObservation],
        faults: Sequence[FaultObservation],
        require_real_tiers: bool,
        require_real_providers: bool,
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        routes = {item.route_id for item in actions if item.route_id}
        workers = {item.worker_id for item in actions if item.worker_id}
        checks["route_identity_present"] = bool(routes)
        checks["worker_identity_present"] = bool(workers)
        if not routes:
            findings.append(
                _finding(
                    "placement.route_missing",
                    VerificationSeverity.BLOCKER,
                    "No canonical scheduler route was observed.",
                    "placement",
                )
            )
        if not workers:
            findings.append(
                _finding(
                    "placement.worker_missing",
                    VerificationSeverity.BLOCKER,
                    "No canonical worker lease was observed.",
                    "placement",
                )
            )
        tier_by_kind: dict[TierKind, list[TierObservation]] = defaultdict(list)
        for item in tiers:
            tier_by_kind[item.tier].append(item)
            try:
                item.validate()
            except Exception as error:
                findings.append(
                    _finding(
                        "placement.tier_invalid",
                        VerificationSeverity.BLOCKER,
                        str(error),
                        item.observation_id,
                    )
                )
        checks["tier_coverage"] = not require_real_tiers or set(TierKind).issubset(tier_by_kind)
        if require_real_tiers and not checks["tier_coverage"]:
            findings.append(
                _finding(
                    "placement.tier_coverage_missing",
                    VerificationSeverity.BLOCKER,
                    "Device, isolated edge and cloud execution were not all observed.",
                    "placement",
                    expected=[item.value for item in TierKind],
                    observed=[item.value for item in tier_by_kind],
                )
            )
        if tier_by_kind[TierKind.DEVICE] and tier_by_kind[TierKind.EDGE]:
            device = tier_by_kind[TierKind.DEVICE][0]
            edge = tier_by_kind[TierKind.EDGE][0]
            shared = {
                device.endpoint_id,
                device.runtime_id,
                device.process_id,
                device.isolation_id,
            }.intersection(
                {
                    edge.endpoint_id,
                    edge.runtime_id,
                    edge.process_id,
                    edge.isolation_id,
                }
            )
            checks["edge_independent"] = not shared
            if shared:
                findings.append(
                    _finding(
                        "placement.edge_not_independent",
                        VerificationSeverity.BLOCKER,
                        "Edge evidence shares runtime identity with device execution.",
                        edge.observation_id,
                        observed=sorted(shared),
                    )
                )
        elif require_real_tiers:
            checks["edge_independent"] = False
        capabilities: set[tuple[str, str]] = set()
        for item in providers:
            capabilities.add((item.provider_id, item.model_id))
            try:
                item.validate()
            except Exception as error:
                findings.append(
                    _finding(
                        "placement.provider_invalid",
                        VerificationSeverity.BLOCKER,
                        str(error),
                        item.observation_id,
                    )
                )
            if item.cost_usd > domain_input.maximum_cost_usd:
                findings.append(
                    _finding(
                        "placement.provider_cost_exceeded",
                        VerificationSeverity.BLOCKER,
                        "Provider turn exceeded the admitted cost budget.",
                        item.observation_id,
                        expected=domain_input.maximum_cost_usd,
                        observed=item.cost_usd,
                    )
                )
            if item.latency_ms > domain_input.maximum_latency_ms:
                findings.append(
                    _finding(
                        "placement.provider_latency_exceeded",
                        VerificationSeverity.BLOCKER,
                        "Provider turn exceeded the admitted latency SLA.",
                        item.observation_id,
                        expected=domain_input.maximum_latency_ms,
                        observed=item.latency_ms,
                    )
                )
        checks["provider_capability_minimum"] = (
            not require_real_providers or len(capabilities) >= 2
        )
        if require_real_providers and len(capabilities) < 2:
            findings.append(
                _finding(
                    "placement.provider_capability_minimum",
                    VerificationSeverity.BLOCKER,
                    "At least two real provider/model capabilities are required.",
                    "providers",
                    expected=2,
                    observed=len(capabilities),
                )
            )
        cloud_actions = [item for item in actions if item.tier is TierKind.CLOUD]
        if domain_input.privacy_class in {
            PrivacyClass.CONFIDENTIAL,
            PrivacyClass.RESTRICTED,
        } and cloud_actions:
            findings.append(
                _finding(
                    "placement.privacy_cloud_violation",
                    VerificationSeverity.BLOCKER,
                    "Sensitive work was placed on cloud despite privacy policy.",
                    "placement",
                    observed=[item.action_id for item in cloud_actions],
                )
            )
            checks["privacy_policy"] = False
        else:
            checks["privacy_policy"] = True
        migration_faults = [
            item
            for item in faults
            if item.kind
            in {
                FaultKind.WORKER_LOSS,
                FaultKind.NODE_LOSS,
                FaultKind.PROVIDER_RATE_LIMIT,
                FaultKind.PROVIDER_FAILURE,
                FaultKind.EDGE_NETWORK_LOSS,
            }
        ]
        checks["fault_route_migration"] = bool(migration_faults) and all(
            item.route_before
            and item.route_after
            and item.route_before != item.route_after
            for item in migration_faults
        )
        if not checks["fault_route_migration"]:
            findings.append(
                _finding(
                    "placement.fault_migration_missing",
                    VerificationSeverity.BLOCKER,
                    "Placement faults did not produce a verifiable route migration.",
                    "fault-matrix",
                )
            )
        return findings, checks


class SoftwareDeliveryVerifier:
    def __init__(
        self,
        *,
        artifact_root: str | Path,
        workspace_root: str | Path,
        project_root: str | Path,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.project_root = Path(project_root).resolve(strict=False)

    def verify(
        self,
        *,
        domain_input: DomainInput,
        plan: LivePlan,
        action_results: Sequence[ActionResult],
        artifacts: Sequence[Mapping[str, Any]],
        command_receipts: Sequence[Mapping[str, Any]],
        patch_receipt: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        faults: Sequence[FaultObservation],
        tiers: Sequence[TierObservation] = (),
        providers: Sequence[ProviderObservation] = (),
        require_real_tiers: bool = False,
        require_real_providers: bool = False,
    ) -> DomainVerification:
        if os.environ.get("ZYRA_SCENARIO_DOMAIN_VERIFIER_DISABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise unavailable(
                "live_domain_verifier_disabled",
                "Formal software delivery requires the deterministic domain verifier.",
                phase="domain-verification",
            )
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        artifact_findings, artifact_digests = ArtifactIntegrityVerifier(
            artifact_root=self.artifact_root
        ).verify_many(artifacts, minimum_count=4)
        findings.extend(artifact_findings)
        checks["artifact_integrity"] = not any(item.blocking for item in artifact_findings)
        plan_findings, plan_checks = self._verify_plan(plan, action_results)
        findings.extend(plan_findings)
        checks.update(plan_checks)
        patch_findings, patch_checks = self._verify_patch(
            domain_input=domain_input,
            patch_receipt=patch_receipt,
        )
        findings.extend(patch_findings)
        checks.update(patch_checks)
        commands = tuple(CommandEvidence.from_mapping(item) for item in command_receipts)
        command_findings: list[VerificationFinding] = []
        for item in commands:
            command_findings.extend(item.validate(workspace_root=self.workspace_root))
        findings.extend(command_findings)
        checks["commands_present"] = len(commands) >= 3
        checks["commands_passed"] = bool(commands) and not any(
            item.blocking for item in command_findings
        )
        command_names = {Path(item.argv[0]).name.casefold() for item in commands if item.argv}
        checks["git_executed"] = any(name.startswith("git") for name in command_names)
        checks["tests_executed"] = any(
            item.metadata.get("purpose") in {"test", "verification", "compile"}
            for item in commands
        )
        if not checks["git_executed"]:
            findings.append(
                _finding(
                    "software.git_missing",
                    VerificationSeverity.BLOCKER,
                    "Software scenario did not execute Git diff/status verification.",
                    "commands",
                )
            )
        if not checks["tests_executed"]:
            findings.append(
                _finding(
                    "software.tests_missing",
                    VerificationSeverity.BLOCKER,
                    "Software scenario did not execute a real test or compile command.",
                    "commands",
                )
            )
        requirement_findings, requirement_checks = self._verify_requirements(
            domain_input=domain_input,
            patch_receipt=patch_receipt,
            artifacts=artifacts,
        )
        findings.extend(requirement_findings)
        checks.update(requirement_checks)
        event_findings, event_checks = EventCausalityVerifier().verify(
            events,
            expected_run_id=str(events[0].get("run_id") or "") if events else "",
            expected_task_id=str(events[0].get("task_id") or "") if events else "",
            minimum_transitions=2_000,
        )
        findings.extend(event_findings)
        checks.update({f"events.{key}": value for key, value in event_checks.items()})
        placement_findings, placement_checks = PlacementVerifier().verify(
            domain_input=domain_input,
            actions=action_results,
            tiers=tiers,
            providers=providers,
            faults=faults,
            require_real_tiers=require_real_tiers,
            require_real_providers=require_real_providers,
        )
        findings.extend(placement_findings)
        checks.update(
            {f"placement.{key}": value for key, value in placement_checks.items()}
        )
        fault_findings, fault_checks = verify_fault_matrix(faults)
        findings.extend(fault_findings)
        checks.update({f"faults.{key}": value for key, value in fault_checks.items()})
        return DomainVerification.create(
            verifier_id=f"software-verifier:{plan.plan_id}",
            domain=LiveDomain.SOFTWARE_DELIVERY,
            findings=findings,
            checks=checks,
            artifact_digests=artifact_digests,
            uncertainty=(),
        )

    def _verify_plan(
        self,
        plan: LivePlan,
        results: Sequence[ActionResult],
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        indexed = {item.action_id: item for item in results}
        checks["all_actions_settled"] = len(indexed) == len(plan.actions)
        if len(indexed) != len(plan.actions):
            findings.append(
                _finding(
                    "software.plan_unsettled",
                    VerificationSeverity.BLOCKER,
                    "Not every software plan action has a unique result.",
                    plan.plan_id,
                    expected=len(plan.actions),
                    observed=len(indexed),
                )
            )
        verified_dependencies = True
        for action in plan.actions:
            result = indexed.get(action.action_id)
            if result is None:
                continue
            try:
                result.validate(action)
            except Exception as error:
                findings.append(
                    _finding(
                        "software.action_invalid",
                        VerificationSeverity.BLOCKER,
                        str(error),
                        action.action_id,
                    )
                )
            for dependency_id in action.dependency_ids:
                dependency = indexed.get(dependency_id)
                if dependency is None or not dependency.succeeded:
                    verified_dependencies = False
                    findings.append(
                        _finding(
                            "software.dependency_unsettled",
                            VerificationSeverity.BLOCKER,
                            "Action committed before a dependency settled.",
                            action.action_id,
                            observed=dependency_id,
                        )
                    )
                elif dependency.completed_at > result.started_at:
                    verified_dependencies = False
                    findings.append(
                        _finding(
                            "software.dependency_time_order",
                            VerificationSeverity.BLOCKER,
                            "Action started before its dependency completed.",
                            action.action_id,
                            expected=dependency.completed_at,
                            observed=result.started_at,
                        )
                    )
        checks["dependency_order"] = verified_dependencies
        kinds = {item.kind for item in plan.actions}
        required = {
            ActionKind.DISCOVER,
            ActionKind.INDEX,
            ActionKind.PLAN,
            ActionKind.PATCH,
            ActionKind.TEST,
            ActionKind.VERIFY,
            ActionKind.DELIVER,
        }
        checks["software_stage_coverage"] = required.issubset(kinds)
        if not checks["software_stage_coverage"]:
            findings.append(
                _finding(
                    "software.stage_coverage_missing",
                    VerificationSeverity.BLOCKER,
                    "Software plan omits a required delivery stage.",
                    plan.plan_id,
                    expected=sorted(item.value for item in required),
                    observed=sorted(item.value for item in kinds),
                )
            )
        return findings, checks

    def _verify_patch(
        self,
        *,
        domain_input: DomainInput,
        patch_receipt: Mapping[str, Any],
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        patch_path = Path(str(patch_receipt.get("patch_path") or "")).resolve(
            strict=False
        )
        target_path = Path(str(patch_receipt.get("target_path") or "")).resolve(
            strict=False
        )
        before_path = Path(str(patch_receipt.get("before_path") or "")).resolve(
            strict=False
        )
        workspace_target_path = Path(
            str(patch_receipt.get("workspace_target_path") or "")
        ).resolve(strict=False)
        checks["patch_artifacts_confined"] = all(
            path_within(path, self.artifact_root)
            for path in (patch_path, target_path, before_path)
        )
        checks["workspace_target_confined"] = path_within(
            workspace_target_path,
            self.workspace_root,
        )
        if not (
            checks["patch_artifacts_confined"]
            and checks["workspace_target_confined"]
        ):
            findings.append(
                _finding(
                    "software.patch_path_escape",
                    VerificationSeverity.BLOCKER,
                    "Patch evidence escapes its artifact or scratch-workspace boundary.",
                    "patch",
                    expected={
                        "artifacts": str(self.artifact_root),
                        "workspace": str(self.workspace_root),
                    },
                    observed={
                        "artifacts": [
                            str(patch_path),
                            str(target_path),
                            str(before_path),
                        ],
                        "workspace_target": str(workspace_target_path),
                    },
                )
            )
            return findings, checks
        missing = [
            str(path)
            for path in (
                patch_path,
                target_path,
                before_path,
                workspace_target_path,
            )
            if not path.is_file()
        ]
        checks["patch_files_exist"] = not missing
        if missing:
            findings.append(
                _finding(
                    "software.patch_files_missing",
                    VerificationSeverity.BLOCKER,
                    "Patch, target or before-image bytes are missing.",
                    "patch",
                    observed=missing,
                )
            )
            return findings, checks
        before_digest, _ = file_digest(before_path)
        after_digest, _ = file_digest(target_path)
        patch_digest, patch_size = file_digest(patch_path)
        checks["before_digest_matches"] = before_digest == str(
            patch_receipt.get("before_digest") or ""
        )
        checks["after_digest_matches"] = after_digest == str(
            patch_receipt.get("after_digest") or ""
        )
        workspace_after_digest, _ = file_digest(workspace_target_path)
        checks["workspace_target_matches"] = workspace_after_digest == after_digest
        checks["patch_digest_matches"] = patch_digest == str(
            patch_receipt.get("patch_digest") or ""
        )
        checks["patch_changes_bytes"] = before_digest != after_digest
        checks["patch_nonempty"] = patch_size > 0
        for key, valid in tuple(checks.items()):
            if not valid:
                findings.append(
                    _finding(
                        f"software.{key}",
                        VerificationSeverity.BLOCKER,
                        "Patch integrity check failed.",
                        "patch",
                    )
                )
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        checks["unified_diff"] = (
            "--- " in patch_text
            and "+++ " in patch_text
            and "@@" in patch_text
            and any(line.startswith(("+", "-")) for line in patch_text.splitlines()[2:])
        )
        if not checks["unified_diff"]:
            findings.append(
                _finding(
                    "software.unified_diff_invalid",
                    VerificationSeverity.BLOCKER,
                    "Patch artifact is not a non-empty unified diff.",
                    str(patch_path),
                )
            )
        suffix = target_path.suffix.casefold()
        syntax_ok = self._syntax_check(target_path, suffix)
        checks["target_syntax_valid"] = syntax_ok
        if not syntax_ok:
            findings.append(
                _finding(
                    "software.target_syntax_invalid",
                    VerificationSeverity.BLOCKER,
                    "Patched target fails deterministic syntax validation.",
                    str(target_path),
                )
            )
        checks["input_bound_patch"] = str(
            patch_receipt.get("input_digest") or ""
        ) == domain_input.input_digest
        if not checks["input_bound_patch"]:
            findings.append(
                _finding(
                    "software.patch_input_unbound",
                    VerificationSeverity.BLOCKER,
                    "Patch is not bound to the new scenario input.",
                    "patch",
                    expected=domain_input.input_digest,
                    observed=patch_receipt.get("input_digest"),
                )
            )
        return findings, checks

    @staticmethod
    def _syntax_check(path: Path, suffix: str) -> bool:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        if suffix in {".py", ".pyi"}:
            try:
                ast.parse(content, filename=str(path))
            except SyntaxError:
                return False
            return True
        if suffix in {".json"}:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                return False
            return True
        if suffix in {".ts", ".tsx", ".js", ".jsx", ".rs", ".toml", ".md"}:
            return bool(content.strip()) and "\x00" not in content
        return bool(content.strip())

    def _verify_requirements(
        self,
        *,
        domain_input: DomainInput,
        patch_receipt: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        requirement_receipts = patch_receipt.get("requirement_receipts")
        requirement_receipts = (
            tuple(requirement_receipts)
            if isinstance(requirement_receipts, Sequence)
            and not isinstance(requirement_receipts, (str, bytes, bytearray))
            else ()
        )
        indexed = {
            str(item.get("requirement") or ""): item
            for item in requirement_receipts
            if isinstance(item, Mapping)
        }
        for index, requirement in enumerate(domain_input.requirements, start=1):
            selected = indexed.get(requirement)
            key = f"requirement_{index}"
            valid = bool(
                selected
                and selected.get("satisfied") is True
                and selected.get("evidence_refs")
            )
            checks[key] = valid
            if not valid:
                findings.append(
                    _finding(
                        "software.requirement_unverified",
                        VerificationSeverity.BLOCKER,
                        "A software requirement lacks deterministic evidence.",
                        requirement,
                        observed=canonicalize(selected),
                    )
                )
        changed_requirement = patch_receipt.get("requirement_change")
        checks["requirement_change_applied"] = bool(
            isinstance(changed_requirement, Mapping)
            and changed_requirement.get("applied") is True
            and changed_requirement.get("event_id")
            and changed_requirement.get("reverification_id")
        )
        if not checks["requirement_change_applied"]:
            findings.append(
                _finding(
                    "software.requirement_change_missing",
                    VerificationSeverity.BLOCKER,
                    "Mid-run requirement change was not applied and reverified.",
                    "requirement-change",
                )
            )
        artifact_ids = {str(item.get("artifact_id") or "") for item in artifacts}
        referenced = {
            str(ref)
            for item in indexed.values()
            for ref in item.get("evidence_refs") or ()
        }
        checks["requirement_artifacts_resolve"] = referenced.issubset(artifact_ids)
        if not checks["requirement_artifacts_resolve"]:
            findings.append(
                _finding(
                    "software.requirement_artifact_missing",
                    VerificationSeverity.BLOCKER,
                    "Requirement evidence references an unavailable artifact.",
                    "requirements",
                    observed=sorted(referenced - artifact_ids),
                )
            )
        return findings, checks


class ResearchDeliveryVerifier:
    def __init__(self, *, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root).resolve(strict=False)

    def verify(
        self,
        *,
        domain_input: DomainInput,
        plan: LivePlan,
        action_results: Sequence[ActionResult],
        artifacts: Sequence[Mapping[str, Any]],
        sources: Sequence[Mapping[str, Any]],
        claims: Sequence[Mapping[str, Any]],
        citations: Sequence[Mapping[str, Any]],
        report: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        faults: Sequence[FaultObservation],
        tiers: Sequence[TierObservation] = (),
        providers: Sequence[ProviderObservation] = (),
        require_real_tiers: bool = False,
        require_real_providers: bool = False,
    ) -> DomainVerification:
        if os.environ.get("ZYRA_SCENARIO_DOMAIN_VERIFIER_DISABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise unavailable(
                "live_domain_verifier_disabled",
                "Formal research delivery requires the deterministic domain verifier.",
                phase="domain-verification",
            )
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        artifact_findings, artifact_digests = ArtifactIntegrityVerifier(
            artifact_root=self.artifact_root
        ).verify_many(artifacts, minimum_count=4)
        findings.extend(artifact_findings)
        checks["artifact_integrity"] = not any(item.blocking for item in artifact_findings)
        source_findings, source_checks, source_index = self._verify_sources(
            domain_input=domain_input,
            sources=sources,
        )
        findings.extend(source_findings)
        checks.update(source_checks)
        citation_values = tuple(CitationEvidence.from_mapping(item) for item in citations)
        citation_findings, citation_checks = self._verify_citations(
            citations=citation_values,
            source_index=source_index,
        )
        findings.extend(citation_findings)
        checks.update(citation_checks)
        claim_values = tuple(ClaimEvidence.from_mapping(item) for item in claims)
        claim_findings, claim_checks, uncertainty = self._verify_claims(
            claims=claim_values,
            citations=citation_values,
            source_index=source_index,
        )
        findings.extend(claim_findings)
        checks.update(claim_checks)
        report_findings, report_checks = self._verify_report(
            domain_input=domain_input,
            report=report,
            claims=claim_values,
            citations=citation_values,
            artifacts=artifacts,
        )
        findings.extend(report_findings)
        checks.update(report_checks)
        action_findings, action_checks = self._verify_actions(plan, action_results)
        findings.extend(action_findings)
        checks.update(action_checks)
        event_findings, event_checks = EventCausalityVerifier().verify(
            events,
            expected_run_id=str(events[0].get("run_id") or "") if events else "",
            expected_task_id=str(events[0].get("task_id") or "") if events else "",
            minimum_transitions=2_000,
        )
        findings.extend(event_findings)
        checks.update({f"events.{key}": value for key, value in event_checks.items()})
        placement_findings, placement_checks = PlacementVerifier().verify(
            domain_input=domain_input,
            actions=action_results,
            tiers=tiers,
            providers=providers,
            faults=faults,
            require_real_tiers=require_real_tiers,
            require_real_providers=require_real_providers,
        )
        findings.extend(placement_findings)
        checks.update(
            {f"placement.{key}": value for key, value in placement_checks.items()}
        )
        fault_findings, fault_checks = verify_fault_matrix(faults)
        findings.extend(fault_findings)
        checks.update({f"faults.{key}": value for key, value in fault_checks.items()})
        return DomainVerification.create(
            verifier_id=f"research-verifier:{plan.plan_id}",
            domain=LiveDomain.CROSS_SOURCE_RESEARCH,
            findings=findings,
            checks=checks,
            artifact_digests=artifact_digests,
            uncertainty=uncertainty,
        )

    def _verify_sources(
        self,
        *,
        domain_input: DomainInput,
        sources: Sequence[Mapping[str, Any]],
    ) -> tuple[
        list[VerificationFinding],
        dict[str, bool],
        dict[str, Mapping[str, Any]],
    ]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        indexed: dict[str, Mapping[str, Any]] = {}
        authorities: set[str] = set()
        urls: set[str] = set()
        requested_urls = set(domain_input.source_urls)
        for item in sources:
            source_id = str(item.get("source_id") or "")
            url = str(item.get("url") or "")
            path = Path(str(item.get("acquired_path") or "")).resolve(strict=False)
            declared_digest = str(item.get("source_digest") or "")
            status = int(item.get("status") or 0)
            if not source_id or source_id in indexed:
                findings.append(
                    _finding(
                        "research.source_identity_invalid",
                        VerificationSeverity.BLOCKER,
                        "Research source identity is absent or duplicated.",
                        source_id or url or "unknown-source",
                    )
                )
                continue
            indexed[source_id] = item
            if url not in requested_urls:
                findings.append(
                    _finding(
                        "research.source_not_requested",
                        VerificationSeverity.BLOCKER,
                        "Acquired source is not bound to the new input contract.",
                        source_id,
                        observed=url,
                    )
                )
            if url in urls:
                findings.append(
                    _finding(
                        "research.source_url_duplicate",
                        VerificationSeverity.ERROR,
                        "Research source URL is duplicated.",
                        source_id,
                        observed=url,
                    )
                )
            urls.add(url)
            parsed = urlparse(url)
            if parsed.hostname:
                authorities.add(parsed.hostname.casefold())
            if not 200 <= status < 300:
                findings.append(
                    _finding(
                        "research.source_http_failed",
                        VerificationSeverity.BLOCKER,
                        "Research source acquisition did not succeed.",
                        source_id,
                        observed=status,
                    )
                )
            if not path_within(path, self.artifact_root):
                findings.append(
                    _finding(
                        "research.source_path_escape",
                        VerificationSeverity.BLOCKER,
                        "Acquired source bytes are outside the artifact owner.",
                        source_id,
                        observed=str(path),
                    )
                )
                continue
            if not path.is_file():
                findings.append(
                    _finding(
                        "research.source_bytes_missing",
                        VerificationSeverity.BLOCKER,
                        "Acquired source bytes are unavailable.",
                        source_id,
                        observed=str(path),
                    )
                )
                continue
            observed_digest, size = file_digest(path)
            if observed_digest != declared_digest:
                findings.append(
                    _finding(
                        "research.source_digest_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Acquired source checksum does not match the receipt.",
                        source_id,
                        expected=declared_digest,
                        observed=observed_digest,
                    )
                )
            if size < 32:
                findings.append(
                    _finding(
                        "research.source_too_small",
                        VerificationSeverity.ERROR,
                        "Acquired source is too small to support a formal claim.",
                        source_id,
                        observed=size,
                    )
                )
            if item.get("live") is not True or item.get("replay") is True:
                findings.append(
                    _finding(
                        "research.source_not_live",
                        VerificationSeverity.BLOCKER,
                        "Research source is replayed or lacks live acquisition evidence.",
                        source_id,
                    )
                )
            if not item.get("request_id") or not item.get("acquired_at"):
                findings.append(
                    _finding(
                        "research.source_wire_identity_missing",
                        VerificationSeverity.BLOCKER,
                        "Research source lacks request and acquisition identity.",
                        source_id,
                    )
                )
        checks["source_count"] = len(indexed) >= 2
        checks["authority_diversity"] = len(authorities) >= 2
        checks["requested_sources_covered"] = requested_urls.issubset(urls)
        checks["source_integrity"] = not any(item.blocking for item in findings)
        if not checks["authority_diversity"]:
            findings.append(
                _finding(
                    "research.authority_diversity_missing",
                    VerificationSeverity.BLOCKER,
                    "Research evidence does not span at least two authorities.",
                    "sources",
                    expected=2,
                    observed=len(authorities),
                )
            )
        return findings, checks, indexed

    def _verify_citations(
        self,
        *,
        citations: Sequence[CitationEvidence],
        source_index: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        identities: set[str] = set()
        for citation in citations:
            if not citation.citation_id or citation.citation_id in identities:
                findings.append(
                    _finding(
                        "research.citation_identity_invalid",
                        VerificationSeverity.BLOCKER,
                        "Citation identity is absent or duplicated.",
                        citation.citation_id or citation.claim_id,
                    )
                )
                continue
            identities.add(citation.citation_id)
            source = source_index.get(citation.source_id)
            if source is None:
                findings.append(
                    _finding(
                        "research.citation_source_missing",
                        VerificationSeverity.BLOCKER,
                        "Citation references an unavailable source.",
                        citation.citation_id,
                        observed=citation.source_id,
                    )
                )
                continue
            if citation.url != str(source.get("url") or ""):
                findings.append(
                    _finding(
                        "research.citation_url_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Citation URL does not match its source receipt.",
                        citation.citation_id,
                    )
                )
            if citation.source_digest != str(source.get("source_digest") or ""):
                findings.append(
                    _finding(
                        "research.citation_source_digest_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Citation is bound to a different source checksum.",
                        citation.citation_id,
                    )
                )
            source_path = Path(citation.acquired_path).resolve(strict=False)
            source_bytes = source_path.read_bytes() if source_path.is_file() else b""
            if citation.byte_start < 0 or citation.byte_end <= citation.byte_start:
                findings.append(
                    _finding(
                        "research.citation_offsets_invalid",
                        VerificationSeverity.BLOCKER,
                        "Citation byte offsets are invalid.",
                        citation.citation_id,
                        observed=[citation.byte_start, citation.byte_end],
                    )
                )
                continue
            if citation.byte_end > len(source_bytes):
                findings.append(
                    _finding(
                        "research.citation_offsets_oob",
                        VerificationSeverity.BLOCKER,
                        "Citation byte offsets exceed source bytes.",
                        citation.citation_id,
                        expected=len(source_bytes),
                        observed=citation.byte_end,
                    )
                )
                continue
            selected = source_bytes[citation.byte_start : citation.byte_end]
            selected_text = selected.decode("utf-8", errors="replace")
            selected_digest = hashlib.sha256(selected).hexdigest()
            if selected_digest != citation.quote_digest:
                findings.append(
                    _finding(
                        "research.citation_quote_digest_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Citation quote checksum does not match source bytes.",
                        citation.citation_id,
                        expected=citation.quote_digest,
                        observed=selected_digest,
                    )
                )
            if _normalize_space(selected_text) != _normalize_space(citation.quote_text):
                findings.append(
                    _finding(
                        "research.citation_quote_text_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Citation quote text does not match acquired bytes.",
                        citation.citation_id,
                    )
                )
            if len(_normalize_space(citation.quote_text)) < 12:
                findings.append(
                    _finding(
                        "research.citation_quote_too_short",
                        VerificationSeverity.ERROR,
                        "Citation quote is too short to support a claim.",
                        citation.citation_id,
                    )
                )
        checks["citations_present"] = bool(citations)
        checks["citation_identity_unique"] = len(identities) == len(citations)
        checks["citation_integrity"] = not any(item.blocking for item in findings)
        return findings, checks

    def _verify_claims(
        self,
        *,
        claims: Sequence[ClaimEvidence],
        citations: Sequence[CitationEvidence],
        source_index: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[VerificationFinding], dict[str, bool], list[dict[str, Any]]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        uncertainty: list[dict[str, Any]] = []
        citation_index = {item.citation_id: item for item in citations}
        identities: set[str] = set()
        multi_source = 0
        for claim in claims:
            if not claim.claim_id or claim.claim_id in identities:
                findings.append(
                    _finding(
                        "research.claim_identity_invalid",
                        VerificationSeverity.BLOCKER,
                        "Claim identity is absent or duplicated.",
                        claim.claim_id or "unknown-claim",
                    )
                )
                continue
            identities.add(claim.claim_id)
            if len(claim.statement.strip()) < 16:
                findings.append(
                    _finding(
                        "research.claim_too_short",
                        VerificationSeverity.ERROR,
                        "Claim is too short to be independently checked.",
                        claim.claim_id,
                    )
                )
            observed_normalized = _normalize_claim(claim.statement)
            if observed_normalized != claim.normalized_statement:
                findings.append(
                    _finding(
                        "research.claim_normalization_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Claim normalization is not deterministic.",
                        claim.claim_id,
                        expected=observed_normalized,
                        observed=claim.normalized_statement,
                    )
                )
            claim_citations = [
                citation_index[item]
                for item in claim.citation_ids
                if item in citation_index
            ]
            if len(claim_citations) != len(claim.citation_ids):
                findings.append(
                    _finding(
                        "research.claim_citation_missing",
                        VerificationSeverity.BLOCKER,
                        "Claim references an unavailable citation.",
                        claim.claim_id,
                    )
                )
            citation_sources = {item.source_id for item in claim_citations}
            if set(claim.source_ids) != citation_sources:
                findings.append(
                    _finding(
                        "research.claim_source_binding_mismatch",
                        VerificationSeverity.BLOCKER,
                        "Claim source list does not match citation sources.",
                        claim.claim_id,
                        expected=sorted(citation_sources),
                        observed=sorted(claim.source_ids),
                    )
                )
            if len(citation_sources) >= 2:
                multi_source += 1
            if not citation_sources.issubset(source_index):
                findings.append(
                    _finding(
                        "research.claim_source_missing",
                        VerificationSeverity.BLOCKER,
                        "Claim references an unavailable acquired source.",
                        claim.claim_id,
                    )
                )
            if not 0 <= claim.confidence <= 1:
                findings.append(
                    _finding(
                        "research.claim_confidence_invalid",
                        VerificationSeverity.ERROR,
                        "Claim confidence is outside [0, 1].",
                        claim.claim_id,
                        observed=claim.confidence,
                    )
                )
            if claim.agreement not in {
                "single_source",
                "corroborated",
                "partially_corroborated",
                "contradicted",
                "uncertain",
            }:
                findings.append(
                    _finding(
                        "research.claim_agreement_invalid",
                        VerificationSeverity.ERROR,
                        "Claim agreement classification is unsupported.",
                        claim.claim_id,
                        observed=claim.agreement,
                    )
                )
            if claim.agreement in {"contradicted", "uncertain"}:
                if not claim.uncertainty:
                    findings.append(
                        _finding(
                            "research.claim_uncertainty_missing",
                            VerificationSeverity.ERROR,
                            "Contradicted or uncertain claim lacks an uncertainty note.",
                            claim.claim_id,
                        )
                    )
                uncertainty.append(
                    {
                        "claim_id": claim.claim_id,
                        "agreement": claim.agreement,
                        "confidence": claim.confidence,
                        "note": claim.uncertainty,
                        "model_review_isolated": not claim.deterministic,
                    }
                )
        checks["claims_present"] = bool(claims)
        checks["claim_identity_unique"] = len(identities) == len(claims)
        checks["cross_source_claim_present"] = multi_source >= 1
        checks["claim_integrity"] = not any(item.blocking for item in findings)
        if not checks["cross_source_claim_present"]:
            findings.append(
                _finding(
                    "research.cross_source_claim_missing",
                    VerificationSeverity.BLOCKER,
                    "Research output lacks a claim checked across multiple sources.",
                    "claims",
                )
            )
        return findings, checks, uncertainty

    def _verify_report(
        self,
        *,
        domain_input: DomainInput,
        report: Mapping[str, Any],
        claims: Sequence[ClaimEvidence],
        citations: Sequence[CitationEvidence],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        schema = str(report.get("schema") or "")
        checks["report_schema"] = schema == "zyra.cross-source-research-report/v1"
        if not checks["report_schema"]:
            findings.append(
                _finding(
                    "research.report_schema_invalid",
                    VerificationSeverity.BLOCKER,
                    "Research report schema is missing or unsupported.",
                    "report",
                    expected="zyra.cross-source-research-report/v1",
                    observed=schema,
                )
            )
        report_claims = {
            str(item.get("claim_id") or "")
            for item in report.get("claims") or ()
            if isinstance(item, Mapping)
        }
        report_citations = {
            str(item.get("citation_id") or "")
            for item in report.get("citations") or ()
            if isinstance(item, Mapping)
        }
        checks["report_claim_coverage"] = report_claims == {
            item.claim_id for item in claims
        }
        checks["report_citation_coverage"] = report_citations == {
            item.citation_id for item in citations
        }
        checks["report_input_bound"] = (
            str(report.get("input_digest") or "") == domain_input.input_digest
        )
        checks["report_human_count_zero"] = (
            int(report.get("human_intervention_count") or 0) == 0
        )
        checks["report_uncertainty_explicit"] = isinstance(
            report.get("uncertainty"), list
        )
        checks["report_sections"] = all(
            report.get(key)
            for key in (
                "question",
                "method",
                "findings",
                "limitations",
                "source_manifest",
            )
        )
        artifact_ids = {str(item.get("artifact_id") or "") for item in artifacts}
        report_artifacts = {
            str(item) for item in report.get("artifact_ids") or () if str(item)
        }
        checks["report_artifacts_resolve"] = bool(report_artifacts) and report_artifacts.issubset(
            artifact_ids
        )
        for key, valid in checks.items():
            if not valid:
                findings.append(
                    _finding(
                        f"research.{key}",
                        VerificationSeverity.BLOCKER,
                        "Structured research report verification failed.",
                        "report",
                    )
                )
        return findings, checks

    def _verify_actions(
        self,
        plan: LivePlan,
        results: Sequence[ActionResult],
    ) -> tuple[list[VerificationFinding], dict[str, bool]]:
        findings: list[VerificationFinding] = []
        checks: dict[str, bool] = {}
        indexed = {item.action_id: item for item in results}
        checks["all_actions_settled"] = len(indexed) == len(plan.actions)
        kinds = {item.kind for item in plan.actions}
        required = {
            ActionKind.ACQUIRE,
            ActionKind.TRANSFORM,
            ActionKind.VERIFY,
            ActionKind.DELIVER,
        }
        checks["research_stage_coverage"] = required.issubset(kinds)
        dependency_ok = True
        for action in plan.actions:
            result = indexed.get(action.action_id)
            if result is None:
                dependency_ok = False
                findings.append(
                    _finding(
                        "research.action_result_missing",
                        VerificationSeverity.BLOCKER,
                        "Research plan action lacks a result.",
                        action.action_id,
                    )
                )
                continue
            try:
                result.validate(action)
            except Exception as error:
                findings.append(
                    _finding(
                        "research.action_result_invalid",
                        VerificationSeverity.BLOCKER,
                        str(error),
                        action.action_id,
                    )
                )
            for dependency_id in action.dependency_ids:
                dependency = indexed.get(dependency_id)
                if dependency is None or not dependency.succeeded:
                    dependency_ok = False
                elif dependency.completed_at > result.started_at:
                    dependency_ok = False
        checks["dependency_order"] = dependency_ok
        for key, valid in checks.items():
            if not valid:
                findings.append(
                    _finding(
                        f"research.{key}",
                        VerificationSeverity.BLOCKER,
                        "Research plan verification failed.",
                        plan.plan_id,
                    )
                )
        return findings, checks


def verify_fault_matrix(
    faults: Sequence[FaultObservation],
) -> tuple[list[VerificationFinding], dict[str, bool]]:
    findings: list[VerificationFinding] = []
    checks: dict[str, bool] = {}
    by_kind = {item.kind: item for item in faults}
    required_groups = {
        "requirement_change": {FaultKind.REQUIREMENT_CHANGE},
        "exception_or_timeout": {FaultKind.TOOL_EXCEPTION, FaultKind.TOOL_TIMEOUT},
        "worker_or_node_loss": {FaultKind.WORKER_LOSS, FaultKind.NODE_LOSS},
        "provider_failure": {
            FaultKind.PROVIDER_FAILURE,
            FaultKind.PROVIDER_RATE_LIMIT,
        },
        "edge_or_network_loss": {
            FaultKind.EDGE_NETWORK_LOSS,
            FaultKind.NETWORK_LOSS,
        },
    }
    for group, kinds in required_groups.items():
        selected = [by_kind[item] for item in kinds if item in by_kind]
        valid = bool(selected) and any(item.resolved for item in selected)
        checks[group] = valid
        if not valid:
            findings.append(
                _finding(
                    f"fault.{group}_missing",
                    VerificationSeverity.BLOCKER,
                    "Representative live fault group is missing or unresolved.",
                    group,
                    expected=sorted(item.value for item in kinds),
                )
            )
    for item in faults:
        try:
            item.validate()
        except Exception as error:
            findings.append(
                _finding(
                    "fault.observation_invalid",
                    VerificationSeverity.BLOCKER,
                    str(error),
                    item.injection_id,
                )
            )
    checks["all_faults_resolved"] = bool(faults) and all(item.resolved for item in faults)
    checks["checkpoint_restore_present"] = any(item.checkpoint_id for item in faults)
    checks["reverification_present"] = all(
        item.verifier_event_id for item in faults if item.state.value == "recovered"
    )
    for key in ("all_faults_resolved", "checkpoint_restore_present", "reverification_present"):
        if not checks[key]:
            findings.append(
                _finding(
                    f"fault.{key}",
                    VerificationSeverity.BLOCKER,
                    "Fault campaign invariant failed.",
                    "fault-matrix",
                )
            )
    return findings, checks


def verify_json_schema_shape(
    value: Any,
    schema: Mapping[str, Any],
    *,
    subject: str = "$",
) -> tuple[VerificationFinding, ...]:
    findings: list[VerificationFinding] = []
    expected_type = schema.get("type")
    if expected_type:
        type_ok = _json_type_ok(value, str(expected_type))
        if not type_ok:
            findings.append(
                _finding(
                    "schema.type_mismatch",
                    VerificationSeverity.BLOCKER,
                    "JSON value does not match the expected type.",
                    subject,
                    expected=expected_type,
                    observed=type(value).__name__,
                )
            )
            return tuple(findings)
    if isinstance(value, Mapping):
        required = tuple(str(item) for item in schema.get("required") or ())
        for key in required:
            if key not in value:
                findings.append(
                    _finding(
                        "schema.required_missing",
                        VerificationSeverity.BLOCKER,
                        "JSON object lacks a required property.",
                        f"{subject}.{key}",
                    )
                )
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, Mapping):
                    findings.extend(
                        verify_json_schema_shape(
                            value[key],
                            child_schema,
                            subject=f"{subject}.{key}",
                        )
                    )
        if schema.get("additionalProperties") is False and isinstance(properties, Mapping):
            extras = sorted(set(value) - set(properties))
            for key in extras:
                findings.append(
                    _finding(
                        "schema.additional_property",
                        VerificationSeverity.ERROR,
                        "JSON object contains an undeclared property.",
                        f"{subject}.{key}",
                    )
                )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        minimum = int(schema.get("minItems") or 0)
        if len(value) < minimum:
            findings.append(
                _finding(
                    "schema.minimum_items",
                    VerificationSeverity.BLOCKER,
                    "JSON array has fewer items than required.",
                    subject,
                    expected=minimum,
                    observed=len(value),
                )
            )
        child_schema = schema.get("items")
        if isinstance(child_schema, Mapping):
            for index, item in enumerate(value):
                findings.extend(
                    verify_json_schema_shape(
                        item,
                        child_schema,
                        subject=f"{subject}[{index}]",
                    )
                )
    if isinstance(value, str):
        minimum = int(schema.get("minLength") or 0)
        if len(value) < minimum:
            findings.append(
                _finding(
                    "schema.minimum_length",
                    VerificationSeverity.ERROR,
                    "JSON string is shorter than required.",
                    subject,
                    expected=minimum,
                    observed=len(value),
                )
            )
        pattern = str(schema.get("pattern") or "")
        if pattern:
            try:
                matches = re.search(pattern, value) is not None
            except re.error:
                matches = False
            if not matches:
                findings.append(
                    _finding(
                        "schema.pattern_mismatch",
                        VerificationSeverity.ERROR,
                        "JSON string does not match its declared pattern.",
                        subject,
                        expected=pattern,
                    )
                )
    return tuple(findings)


def verify_stable_artifact(
    path: str | Path,
    *,
    expected_json_digest: str = "",
) -> tuple[str, int]:
    selected = Path(path).resolve(strict=False)
    checksum, size = file_digest(selected)
    if expected_json_digest:
        try:
            parsed = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise conflict(
                "stable_artifact_json_invalid",
                "Stable artifact is not valid JSON.",
                phase="domain-verification",
                detail={"path": str(selected)},
            ) from error
        observed = digest(parsed)
        if observed != expected_json_digest:
            raise conflict(
                "stable_artifact_semantic_digest_mismatch",
                "Stable artifact semantic digest does not match.",
                phase="domain-verification",
                detail={"expected": expected_json_digest, "observed": observed},
            )
    return checksum, size


def compare_verifications(
    values: Sequence[DomainVerification],
) -> dict[str, Any]:
    if not values:
        raise invalid(
            "verification_comparison_empty",
            "Verification comparison needs at least one run.",
            phase="domain-verification",
        )
    domains = {item.domain for item in values}
    finding_sets = [
        {
            (finding.code, finding.subject, finding.severity.value)
            for finding in item.findings
        }
        for item in values
    ]
    stable_findings = set.intersection(*finding_sets) if finding_sets else set()
    changing_findings = set.union(*finding_sets) - stable_findings if finding_sets else set()
    check_keys = set.intersection(*(set(item.checks) for item in values))
    unstable_checks = {
        key: [item.checks[key] for item in values]
        for key in check_keys
        if len({item.checks[key] for item in values}) > 1
    }
    artifact_keys = set.intersection(*(set(item.artifact_digests) for item in values))
    stable_artifacts = {
        key: values[0].artifact_digests[key]
        for key in artifact_keys
        if len({item.artifact_digests[key] for item in values}) == 1
    }
    result = {
        "schema": "zyra.live-verification-comparison/v1",
        "run_count": len(values),
        "domains": sorted(item.value for item in domains),
        "all_valid": all(item.valid for item in values),
        "stable_findings": sorted(stable_findings),
        "changing_findings": sorted(changing_findings),
        "unstable_checks": unstable_checks,
        "stable_artifacts": stable_artifacts,
        "receipt_digests": [item.receipt_digest for item in values],
    }
    result["comparison_digest"] = digest(result)
    return result


def _json_type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _finding(
    code: str,
    severity: VerificationSeverity,
    summary: str,
    subject: str,
    *,
    expected: Any = None,
    observed: Any = None,
    refs: Iterable[str] = (),
) -> VerificationFinding:
    return VerificationFinding(
        code=code,
        severity=severity,
        summary=summary,
        subject=subject,
        expected=expected,
        observed=observed,
        evidence_refs=tuple(refs),
    )


def _normalize_space(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split())


def _normalize_claim(value: str) -> str:
    normalized = _normalize_space(value).casefold()
    normalized = re.sub(r"[^\w\s%./:+-]", "", normalized)
    return " ".join(normalized.split())


__all__ = [
    "ArtifactIntegrityVerifier",
    "CitationEvidence",
    "ClaimEvidence",
    "CommandEvidence",
    "EventCausalityVerifier",
    "PlacementVerifier",
    "ResearchDeliveryVerifier",
    "SoftwareDeliveryVerifier",
    "compare_verifications",
    "verify_fault_matrix",
    "verify_json_schema_shape",
    "verify_stable_artifact",
]
