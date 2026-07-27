from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .approval_security import (
    ApprovalChallenge,
    ApprovalObservation,
    ConcurrencyProbeReceipt,
    evaluate_approval_security,
)
from .artifacts import SecureArtifactStore
from .causality import CausalFact, evaluate_causality_campaign
from .clean_state import CleanStateGuard, StateRoot
from .code_index_security import (
    CodeIndexObservation,
    evaluate_code_index_security,
)
from .content_security import (
    ContentAdmission,
    ContentEnvelope,
    evaluate_content_security,
)
from .contracts import (
    ArtifactDeclaration,
    AssertionSeverity,
    CaseExecutionBuffer,
    CaseSpec,
    FailureKind,
    IsolationMode,
    ObservationKind,
    SuiteProfile,
    SuiteReceipt,
    stable_digest,
)
from .control_boundary import (
    ControlReceipt,
    LlmSuggestion,
    evaluate_control_boundary,
)
from .default_path import DefaultPathCampaign, EntrypointProbe
from .orchestrator import (
    CaseContext,
    OrchestratorConfig,
    RegressionOrchestrator,
)
from .registry import CaseRegistry
from .repository_security import (
    FrictionEffect,
    PatchReceiptObservation,
    evaluate_repository_security,
)
from .triage import FailureTriage, TriageReport


SUITE_ID = "m3-default-security-regression"
SUITE_VERSION = "1"


class CleanStateSubject(Protocol):
    def __call__(self, context: CaseContext) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class ControlCampaignInput:
    suggestions: tuple[LlmSuggestion, ...]
    receipts: tuple[ControlReceipt, ...]


@dataclass(frozen=True, slots=True)
class RepositoryCampaignInput:
    patches: tuple[PatchReceiptObservation, ...]
    git_commands: tuple[str | tuple[str, ...], ...]
    effects: tuple[FrictionEffect, ...]


@dataclass(frozen=True, slots=True)
class ContentCampaignInput:
    envelopes: tuple[ContentEnvelope, ...]
    admissions: tuple[ContentAdmission, ...]
    canaries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApprovalCampaignInput:
    challenges: tuple[ApprovalChallenge, ...]
    observations: tuple[ApprovalObservation, ...]
    concurrency_receipt: ConcurrencyProbeReceipt | None = None


@dataclass(frozen=True, slots=True)
class SubjectPorts:
    default_path_probes: tuple[EntrypointProbe, ...] = ()
    causality: Callable[[], Sequence[CausalFact | Mapping[str, Any]]] | None = None
    control_boundary: Callable[[], ControlCampaignInput] | None = None
    repository_security: Callable[[], RepositoryCampaignInput] | None = None
    content_security: Callable[[], ContentCampaignInput] | None = None
    code_index_security: Callable[[], Sequence[CodeIndexObservation]] | None = None
    approval_security: Callable[[], ApprovalCampaignInput] | None = None
    clean_state: CleanStateSubject | None = None
    clean_state_allowed_outputs: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    secret_canaries: tuple[str, ...] = ()

    def capability_map(self) -> dict[str, bool]:
        return {
            "default-path-probes": len(self.default_path_probes) == 4,
            "causality-subject": self.causality is not None,
            "control-boundary-subject": self.control_boundary is not None,
            "repository-security-subject": self.repository_security is not None,
            "content-security-subject": self.content_security is not None,
            "code-index-subject": self.code_index_security is not None,
            "approval-security-subject": self.approval_security is not None,
            "clean-state-subject": self.clean_state is not None,
        }


@dataclass(frozen=True, slots=True)
class RegressionRunResult:
    suite: SuiteReceipt
    triage: TriageReport
    suite_receipt_path: Path
    triage_path: Path

    @property
    def passed(self) -> bool:
        return self.suite.passed and self.triage.valid

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-regression-run-result/v1",
            "passed": self.passed,
            "suite": self.suite.to_dict(),
            "triage": self.triage.to_dict(),
            "suite_receipt_path": self.suite_receipt_path.as_posix(),
            "triage_path": self.triage_path.as_posix(),
        }


class RegressionHardeningService:
    def __init__(
        self,
        project_root: str | Path,
        artifact_root: str | Path,
        ports: SubjectPorts,
        *,
        temporary_parent: str | Path | None = None,
        retain_failed_isolation: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.ports = ports
        self.registry = self._registry()
        canaries = tuple(ports.secret_canaries)
        self.orchestrator = RegressionOrchestrator(
            self.registry,
            OrchestratorConfig.build(
                self.project_root,
                self.artifact_root,
                temporary_parent=temporary_parent,
                retain_failed_isolation=retain_failed_isolation,
                secret_canaries=canaries,
            ),
        )

    def run(
        self,
        *,
        profile: SuiteProfile | None = None,
        generated_inputs: Mapping[str, Mapping[str, Any]] | None = None,
        suite_metadata: Mapping[str, Any] | None = None,
    ) -> RegressionRunResult:
        selected_profile = profile or full_profile()
        selection = self.registry.select(selected_profile)
        inputs = self._generated_inputs(generated_inputs or {})
        suite = self.orchestrator.execute(
            selection,
            suite_id=SUITE_ID,
            suite_version=SUITE_VERSION,
            generated_inputs=inputs,
            suite_metadata=suite_metadata,
        )
        triage = FailureTriage().evaluate(suite)
        triage_store = SecureArtifactStore(
            self.artifact_root,
            "triage",
            (
                ArtifactDeclaration(
                    name="triage-report",
                    relative_path="triage-report.json",
                    maximum_bytes=16 * 1024 * 1024,
                ),
            ),
            secret_canaries=self.orchestrator.config.secret_canaries,
        )
        triage_store.write_json("triage-report", triage.to_dict())
        triage_store.finalize().require_valid()
        return RegressionRunResult(
            suite=suite,
            triage=triage,
            suite_receipt_path=self.artifact_root / "suite" / "suite-receipt.json",
            triage_path=self.artifact_root / "triage" / "triage-report.json",
        )

    def cancel(self) -> None:
        self.orchestrator.cancel()

    def describe(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-regression-hardening-service/v1",
            "project_root": self.project_root.as_posix(),
            "artifact_root": self.artifact_root.as_posix(),
            "registry": self.registry.describe(),
            "capabilities": self.ports.capability_map(),
            "profiles": [smoke_profile().to_dict(), full_profile().to_dict()],
        }

    def _registry(self) -> CaseRegistry:
        registry = CaseRegistry()
        for capability, available in self.ports.capability_map().items():
            registry.register_capability(
                capability,
                available=available,
                owner="M3RegressionSubjectPorts",
                detail=(
                    "real subject port bound"
                    if available
                    else "subject port is not configured"
                ),
            )
        registry.register(
            CaseSpec(
                case_id="default-path-reachability",
                version="1",
                title="Default CLI/API/Web/worker reachability, disable and mutation",
                tags=("default-path", "security", "smoke"),
                timeout_seconds=240,
                isolation=IsolationMode.TEMPORARY,
                required_capabilities=("default-path-probes",),
                artifacts=(
                    ArtifactDeclaration(
                        name="default-path-report",
                        relative_path="default-path-report.json",
                    ),
                ),
                mutation_required=True,
                disable_required=True,
                maximum_attempts=1,
            ),
            self._default_path_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="bidirectional-causality",
                version="1",
                title="Event/span/tool/artifact/route/mutation bidirectional causality",
                tags=("causality", "default-path", "smoke"),
                timeout_seconds=90,
                isolation=IsolationMode.TEMPORARY,
                dependencies=("default-path-reachability",),
                required_capabilities=("causality-subject",),
                artifacts=(
                    ArtifactDeclaration(
                        name="causality-report",
                        relative_path="causality-report.json",
                    ),
                ),
                mutation_required=True,
            ),
            self._causality_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="clean-state-isolation",
                version="1",
                title="Clean cache/SQLite/index/artifact/build generated-input isolation",
                tags=("clean-state", "default-path", "smoke"),
                timeout_seconds=180,
                isolation=IsolationMode.TEMPORARY,
                dependencies=("default-path-reachability",),
                required_capabilities=("clean-state-subject",),
                artifacts=(
                    ArtifactDeclaration(
                        name="clean-state-report",
                        relative_path="clean-state-report.json",
                    ),
                ),
                mutation_required=True,
                clean_state_required=True,
                exclusive_resources=("clean-state",),
            ),
            self._clean_state_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="llm-control-boundary",
                version="1",
                title="Deterministic permission/scheduler/recovery/compact custody",
                tags=("control", "security", "smoke"),
                timeout_seconds=90,
                isolation=IsolationMode.TEMPORARY,
                dependencies=("bidirectional-causality",),
                required_capabilities=("control-boundary-subject",),
                artifacts=(
                    ArtifactDeclaration(
                        name="control-boundary-report",
                        relative_path="control-boundary-report.json",
                    ),
                ),
                mutation_required=True,
            ),
            self._control_boundary_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="repository-security",
                version="1",
                title="Patch/Git stale, atomic, rollback, dirty and destructive guards",
                tags=("patch-git", "security"),
                timeout_seconds=120,
                isolation=IsolationMode.TEMPORARY,
                dependencies=("clean-state-isolation",),
                required_capabilities=("repository-security-subject",),
                artifacts=(
                    ArtifactDeclaration(
                        name="repository-security-report",
                        relative_path="repository-security-report.json",
                    ),
                ),
                mutation_required=True,
                security_required=True,
                exclusive_resources=("workspace",),
            ),
            self._repository_security_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="content-security",
                version="1",
                title="Secret redaction, memory blocking and untrusted injection",
                tags=("content", "security", "smoke"),
                timeout_seconds=90,
                isolation=IsolationMode.TEMPORARY,
                dependencies=("llm-control-boundary",),
                required_capabilities=("content-security-subject",),
                artifacts=(
                    ArtifactDeclaration(
                        name="content-security-report",
                        relative_path="content-security-report.json",
                    ),
                ),
                mutation_required=True,
                security_required=True,
            ),
            self._content_security_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="code-index-security",
                version="1",
                title="Code-index budget, permission, patch refresh and selection effects",
                tags=("code-index", "security"),
                timeout_seconds=180,
                isolation=IsolationMode.TEMPORARY,
                dependencies=("repository-security",),
                required_capabilities=("code-index-subject",),
                artifacts=(
                    ArtifactDeclaration(
                        name="code-index-security-report",
                        relative_path="code-index-security-report.json",
                    ),
                ),
                mutation_required=True,
                exclusive_resources=("workspace",),
            ),
            self._code_index_security_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="approval-security",
                version="1",
                title="Approval binding, nonce, stale, race, crash restore and sealed no-hang",
                tags=("approval", "security", "smoke"),
                timeout_seconds=180,
                isolation=IsolationMode.PROCESS,
                dependencies=("content-security",),
                required_capabilities=("approval-security-subject",),
                artifacts=(
                    ArtifactDeclaration(
                        name="approval-security-report",
                        relative_path="approval-security-report.json",
                    ),
                ),
                mutation_required=True,
                exclusive_resources=("approval-ledger",),
            ),
            self._approval_security_case,
            source="M3-S02A-01",
        )
        registry.register(
            CaseSpec(
                case_id="freeze-receipt-preflight",
                version="1",
                title="Preflight all regression case outputs before freeze admission",
                tags=("freeze", "smoke"),
                timeout_seconds=60,
                isolation=IsolationMode.TEMPORARY,
                dependencies=(
                    "approval-security",
                    "code-index-security",
                ),
                artifacts=(
                    ArtifactDeclaration(
                        name="freeze-preflight-report",
                        relative_path="freeze-preflight-report.json",
                    ),
                ),
                mutation_required=True,
            ),
            self._freeze_preflight_case,
            source="M3-S02A-01",
        )
        registry.seal()
        return registry

    def _default_path_case(self, context: CaseContext) -> CaseExecutionBuffer:
        campaign = DefaultPathCampaign(self.ports.default_path_probes)
        report, buffer = campaign.execute(
            {
                item.entry_kind: {
                    **context.generated_input,
                    "entry_kind": item.entry_kind.value,
                }
                for item in self.ports.default_path_probes
            }
        )
        context.artifact_store.write_json(
            "default-path-report",
            report.to_dict(),
        )
        return buffer

    def _causality_case(self, context: CaseContext) -> CaseExecutionBuffer:
        assert self.ports.causality is not None
        values = tuple(self.ports.causality())
        buffer = evaluate_causality_campaign(values)
        context.artifact_store.write_json(
            "causality-report",
            {
                "schema": "zyra.m3-causality-campaign-summary/v1",
                "input_count": len(values),
                "input_digest": stable_digest(
                    [
                        item.to_dict()
                        if isinstance(item, CausalFact)
                        else item
                        for item in values
                    ]
                ),
                "observations": [item.to_dict() for item in buffer.observations],
                "assertions": [item.to_dict() for item in buffer.assertions],
            },
        )
        return buffer

    def _clean_state_case(self, context: CaseContext) -> CaseExecutionBuffer:
        assert self.ports.clean_state is not None
        roots = tuple(
            StateRoot(
                root_id=kind,
                path=context.state_root / kind,
                kind=kind,
                allowed_outputs=tuple(
                    self.ports.clean_state_allowed_outputs.get(kind, ())
                ),
            )
            for kind in ("cache", "sqlite", "index", "artifacts", "build")
        )
        generated_input = {
            "case": context.case_id,
            "attempt": context.attempt,
            "payload": context.generated_input,
            "nonce": stable_digest(
                context.case_id,
                context.attempt,
                time.monotonic_ns(),
            ),
        }
        guard = CleanStateGuard(
            roots,
            generated_input=generated_input,
            prohibited_environment_paths=(self.project_root.parent,),
        )
        with guard:
            subject_result = dict(self.ports.clean_state(context))
        report = guard.require_report()
        buffer = CaseExecutionBuffer()
        clean_observation = buffer.observe(
            "clean-state.result",
            ObservationKind.CLEAN_STATE,
            "cache-sqlite-index-artifact-build",
            "verified" if report.valid else "pollution-detected",
            attributes={
                "report_digest": report.digest,
                "fresh_state": report.fresh_state,
                "deltas": [item.to_dict() for item in report.deltas],
                "subject_result_digest": stable_digest(subject_result),
            },
        )
        buffer.assert_that(
            "clean-state.baseline-valid",
            report.valid,
            "generated-input subject must use only declared clean-state outputs",
            evidence=(clean_observation.observation_id,),
            failure_kind=FailureKind.POLLUTION,
        )
        # The negative check is run against a separate declared root so the
        # accepting case does not leave pollution behind.
        negative_root = context.state_root / "pollution-probe"
        negative_root.mkdir(parents=True, exist_ok=True)
        negative = StateRoot(
            root_id="pollution-probe",
            path=negative_root,
            kind="pollution",
            allowed_outputs=("declared/**",),
        )
        negative_guard = CleanStateGuard(
            (negative,),
            generated_input={"negative": True, "nonce": generated_input["nonce"]},
        )
        with negative_guard:
            (negative_root / "hidden-pollution.bin").write_bytes(b"pollution")
        negative_report = negative_guard.require_report()
        mutation_observation = buffer.observe(
            "clean-state.pollution-negative",
            ObservationKind.MUTATION,
            "undeclared-output",
            "mutation-rejected" if not negative_report.valid else "mutation-accepted",
            attributes={"report_digest": negative_report.digest},
        )
        buffer.assert_that(
            "clean-state.pollution-rejected",
            not negative_report.valid,
            "undeclared state pollution must be detected",
            evidence=(mutation_observation.observation_id,),
            failure_kind=FailureKind.POLLUTION,
        )
        context.artifact_store.write_json(
            "clean-state-report",
            {
                "baseline": report.to_dict(),
                "pollution_negative": negative_report.to_dict(),
                "subject_result": subject_result,
            },
        )
        return buffer

    def _control_boundary_case(self, context: CaseContext) -> CaseExecutionBuffer:
        assert self.ports.control_boundary is not None
        value = self.ports.control_boundary()
        buffer = evaluate_control_boundary(value.suggestions, value.receipts)
        self._write_buffer_report(
            context,
            "control-boundary-report",
            buffer,
            input_digest=stable_digest(
                [item.to_dict() for item in value.suggestions],
                [item.to_dict() for item in value.receipts],
            ),
        )
        return buffer

    def _repository_security_case(self, context: CaseContext) -> CaseExecutionBuffer:
        assert self.ports.repository_security is not None
        value = self.ports.repository_security()
        buffer = evaluate_repository_security(
            value.patches,
            value.git_commands,
            value.effects,
        )
        self._write_buffer_report(
            context,
            "repository-security-report",
            buffer,
            input_digest=stable_digest(
                [item.to_dict() for item in value.patches],
                value.git_commands,
                [item.value for item in value.effects],
            ),
        )
        return buffer

    def _content_security_case(self, context: CaseContext) -> CaseExecutionBuffer:
        assert self.ports.content_security is not None
        value = self.ports.content_security()
        buffer = evaluate_content_security(
            value.envelopes,
            value.admissions,
            canaries=value.canaries,
        )
        self._write_buffer_report(
            context,
            "content-security-report",
            buffer,
            input_digest=stable_digest(
                [item.to_dict() for item in value.envelopes],
                [item.to_dict() for item in value.admissions],
            ),
        )
        return buffer

    def _code_index_security_case(self, context: CaseContext) -> CaseExecutionBuffer:
        assert self.ports.code_index_security is not None
        observations = tuple(self.ports.code_index_security())
        buffer = evaluate_code_index_security(observations)
        self._write_buffer_report(
            context,
            "code-index-security-report",
            buffer,
            input_digest=stable_digest(
                [item.to_dict() for item in observations]
            ),
        )
        return buffer

    def _approval_security_case(self, context: CaseContext) -> CaseExecutionBuffer:
        assert self.ports.approval_security is not None
        value = self.ports.approval_security()
        buffer = evaluate_approval_security(
            value.challenges,
            value.observations,
            concurrency_receipt=value.concurrency_receipt,
        )
        self._write_buffer_report(
            context,
            "approval-security-report",
            buffer,
            input_digest=stable_digest(
                [item.to_dict() for item in value.challenges],
                [item.to_dict() for item in value.observations],
                (
                    value.concurrency_receipt.to_dict()
                    if value.concurrency_receipt
                    else {}
                ),
            ),
        )
        return buffer

    def _freeze_preflight_case(self, context: CaseContext) -> CaseExecutionBuffer:
        buffer = CaseExecutionBuffer()
        dependency_digests = {
            case_id: receipt.digest
            for case_id, receipt in context.dependencies.items()
        }
        dependency_pass = bool(dependency_digests) and all(
            receipt.passed
            for receipt in context.dependencies.values()
        )
        dependency_observation = buffer.observe(
            "freeze-preflight.dependencies",
            ObservationKind.MUTATION,
            "regression-dependency-receipts",
            "mutation-verified" if dependency_pass else "mutation-invalid",
            attributes={"dependency_digests": dependency_digests},
        )
        buffer.assert_that(
            "freeze-preflight.dependencies-pass",
            dependency_pass,
            "freeze preflight dependencies must all have passing receipts",
            evidence=(dependency_observation.observation_id,),
        )
        artifact_roots = {
            case_id: tuple(
                item.relative_path
                for item in receipt.final_attempt.artifacts
            )
            for case_id, receipt in context.dependencies.items()
        }
        artifacts_present = all(artifact_roots.values())
        artifact_observation = buffer.observe(
            "freeze-preflight.artifacts",
            ObservationKind.ARTIFACT,
            "regression-case-artifacts",
            "verified" if artifacts_present else "missing",
            attributes={"artifacts": artifact_roots},
        )
        buffer.assert_that(
            "freeze-preflight.artifacts-present",
            artifacts_present,
            "freeze preflight dependencies must expose integrity-bound artifacts",
            evidence=(artifact_observation.observation_id,),
        )
        context.artifact_store.write_json(
            "freeze-preflight-report",
            {
                "schema": "zyra.m3-regression-freeze-preflight/v1",
                "dependency_digests": dependency_digests,
                "artifacts": artifact_roots,
                "valid": dependency_pass and artifacts_present,
            },
        )
        return buffer

    @staticmethod
    def _write_buffer_report(
        context: CaseContext,
        artifact_name: str,
        buffer: CaseExecutionBuffer,
        *,
        input_digest: str,
    ) -> None:
        context.artifact_store.write_json(
            artifact_name,
            {
                "schema": "zyra.m3-regression-campaign-summary/v1",
                "case_id": context.case_id,
                "input_digest": input_digest,
                "observations": [item.to_dict() for item in buffer.observations],
                "assertions": [item.to_dict() for item in buffer.assertions],
                "valid": bool(buffer.assertions)
                and all(item.passed for item in buffer.assertions),
            },
        )

    def _generated_inputs(
        self,
        overrides: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Mapping[str, Any]]:
        nonce = stable_digest(
            "m3-regression-generated-input",
            time.time_ns(),
            os.getpid(),
            self.registry.digest,
        )
        return {
            case.spec.case_id: {
                "schema": "zyra.m3-generated-regression-input/v1",
                "case_id": case.spec.case_id,
                "nonce": stable_digest(nonce, case.spec.case_id),
                **dict(overrides.get(case.spec.case_id, {})),
            }
            for case in self.registry.cases()
        }


def smoke_profile() -> SuiteProfile:
    return SuiteProfile(
        profile_id="m3-regression-smoke",
        include_tags=("smoke",),
        shard_count=2,
        maximum_workers=2,
        fail_fast=True,
        retry_transient=False,
    )


def full_profile() -> SuiteProfile:
    return SuiteProfile(
        profile_id="m3-regression-full",
        shard_count=4,
        maximum_workers=4,
        fail_fast=False,
        retry_transient=True,
    )


__all__ = [
    "ApprovalCampaignInput",
    "CleanStateSubject",
    "ContentCampaignInput",
    "ControlCampaignInput",
    "RegressionHardeningService",
    "RegressionRunResult",
    "RepositoryCampaignInput",
    "SUITE_ID",
    "SUITE_VERSION",
    "SubjectPorts",
    "full_profile",
    "smoke_profile",
]
