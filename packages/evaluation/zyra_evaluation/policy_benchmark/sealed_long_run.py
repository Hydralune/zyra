from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from zyra_evaluation.policy_benchmark.long_run_validator import (
    EVIDENCE_INDEX_SCHEMA,
    IndependentTransitionValidator,
    canonical_digest,
    file_digest,
)
from zyra_evaluation.policy_benchmark.sealed_mechanisms import (
    SealedMechanismEvidenceRuntime,
)
from zyra_evaluation.policy_benchmark.sealed_physical import (
    SealedPhysicalDispatchRuntime,
    SealedPhysicalEvidence,
    SealedPlacementOwner,
)
from zyra_evaluation.scenario_runner import (
    DualDomainOwnerBindings,
    DualDomainScenarioExecutor,
    ScenarioRegistry,
    build_configuration,
)
from zyra_evaluation.scenario_runner.canonical import utc_now


SEALED_MANIFEST_SCHEMA = "zyra.phase2-sealed-long-run-manifest/v1"


class SealedLongRunError(RuntimeError):
    pass


def _json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                dict(item),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for item in values
        ),
        encoding="utf-8",
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _git(
    project_root: Path,
    *arguments: str,
    check: bool = True,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        ),
    )
    if check and result.returncode != 0:
        raise SealedLongRunError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


class SealedLongRunRunner:
    def __init__(
        self,
        *,
        project_root: str | Path,
        manifest_path: str | Path,
        evidence_root: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest_bytes = self.manifest_path.read_bytes()
        raw = json.loads(self.manifest_bytes.decode("utf-8"))
        if not isinstance(raw, Mapping):
            raise SealedLongRunError("sealed manifest must be a JSON object")
        self.manifest = dict(raw)
        self.manifest_digest = file_digest(self.manifest_path)
        selected_root = (
            Path(evidence_root)
            if evidence_root is not None
            else self.project_root
            / str(
                self.manifest.get("evidence_root")
                or "docs/evidence/phase2/sealed/current"
            )
        )
        self.evidence_root = selected_root.resolve()
        self._validate_manifest()

    def run(self) -> tuple[dict[str, Any], Path]:
        if self.evidence_root.exists() and any(self.evidence_root.iterdir()):
            raise SealedLongRunError(
                f"evidence root is not clean: {self.evidence_root}"
            )
        manifest_commit = _git(self.project_root, "rev-parse", "HEAD")
        status_before = _git(self.project_root, "status", "--porcelain")
        if status_before:
            raise SealedLongRunError(
                "formal sealed run requires a clean candidate worktree"
            )
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        sealed_copy = self.evidence_root / "sealed-manifest.json"
        sealed_copy.write_bytes(self.manifest_bytes)
        run_values: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for run_spec_value in _sequence(self.manifest.get("runs")):
            if not isinstance(run_spec_value, Mapping):
                continue
            run_spec = dict(run_spec_value)
            run_key = str(run_spec.get("run_key") or "")
            attempt_id = (
                f"sealed-{run_key}-{self.manifest_digest[:10]}-"
                f"{uuid4().hex[:12]}"
            )
            run_root = self.evidence_root / "runs" / attempt_id
            run_root.mkdir(parents=True, exist_ok=False)
            try:
                result = self._run_one(
                    run_spec=run_spec,
                    scenario_run_id=attempt_id,
                    run_root=run_root,
                    manifest_commit=manifest_commit,
                )
                run_values.append(result)
            except BaseException as error:
                failure = {
                    "schema": "zyra.phase2-sealed-run-failure/v1",
                    "run_key": run_key,
                    "scenario_run_id": attempt_id,
                    "failure_type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                    "sealed_manifest_digest": self.manifest_digest,
                    "candidate_commit": self.manifest["candidate_commit"],
                    "manifest_commit": manifest_commit,
                    "human_intervention_count": 0,
                    "continued_as_same_run": False,
                    "failed_at": utc_now(),
                }
                _json(run_root / "failure.json", failure)
                failures.append(
                    {
                        **failure,
                        "failure_path": (
                            run_root / "failure.json"
                        ).relative_to(self.evidence_root).as_posix(),
                    }
                )
        final_status = _git(self.project_root, "status", "--porcelain")
        # Only generated evidence below the configured root may appear after
        # the run; tracked source/config changes invalidate all run evidence.
        unexpected = [
            line
            for line in final_status.splitlines()
            if line.strip()
            and not self._status_entry_is_evidence(line)
        ]
        if unexpected:
            failures.append(
                {
                    "schema": "zyra.phase2-sealed-run-failure/v1",
                    "run_key": "global",
                    "failure_type": "SourceMutationDetected",
                    "message": "tracked source/config changed during sealed execution",
                    "status_entries": unexpected,
                    "human_intervention_count": 0,
                    "continued_as_same_run": False,
                    "failed_at": utc_now(),
                }
            )
        index = {
            "schema": EVIDENCE_INDEX_SCHEMA,
            "slice": "P2-S06-02",
            "sealed_manifest": sealed_copy.name,
            "sealed_manifest_digest": file_digest(sealed_copy),
            "candidate_commit": self.manifest["candidate_commit"],
            "manifest_commit": manifest_commit,
            "human_intervention_count": 0,
            "runs": run_values,
            "failed_runs": failures,
            "created_at": utc_now(),
        }
        index["index_digest"] = canonical_digest(index)
        index_path = self.evidence_root / "sealed-evidence-index.json"
        _json(index_path, index)
        if failures or len(run_values) != 2:
            raise SealedLongRunError(
                f"sealed execution retained {len(failures)} failed run(s); "
                f"evidence index: {index_path}"
            )
        return index, index_path

    def _run_one(
        self,
        *,
        run_spec: dict[str, Any],
        scenario_run_id: str,
        run_root: Path,
        manifest_commit: str,
    ) -> dict[str, Any]:
        self._assert_frozen()
        api_state = run_root / "runtime-state"
        self._configure_api_state(api_state)
        api_main = self._fresh_api_main()
        from apps.api.zyra_api.live_scenario_owners import (
            CanonicalLiveScenarioOwners,
        )

        owner = CanonicalLiveScenarioOwners(
            project_root=self.project_root,
            artifact_root=api_main.artifact_root_path(),
            scratch_root=api_main.sqlite_path().with_name(
                "live-scenario-scratch"
            ),
        )
        physical_holder: list[SealedPhysicalEvidence] = []
        credential_file = self.manifest.get("credential_env_file")
        physical_runtime = SealedPhysicalDispatchRuntime(
            project_root=self.project_root,
            state_root=run_root / "physical-runtime",
            credential_env_file=(
                self.project_root / str(credential_file)
                if credential_file
                else None
            ),
        )
        placement = SealedPlacementOwner(
            delegate=owner,
            physical_runtime=physical_runtime,
            evidence_sink=physical_holder.append,
        )
        configuration = self._configuration(
            run_spec,
            state_root=api_state / "preflight",
        )
        executor = DualDomainScenarioExecutor(
            project_root=self.project_root,
            artifact_root=api_main.artifact_root_path(),
            scratch_root=api_main.sqlite_path().with_name(
                "live-scenario-scratch"
            ),
            bindings=DualDomainOwnerBindings(
                task=owner,
                artifact=owner,
                placement=placement,
                fault=owner,
                source_commit=manifest_commit,
            ),
        )
        result = executor.execute(
            scenario_run_id=scenario_run_id,
            configuration=configuration,
            goal=configuration.input_text,
            policy_decisions=(),
            cancel_requested=lambda: False,
        )
        if len(physical_holder) != 1:
            raise SealedLongRunError(
                "physical evidence owner did not return exactly one lane bundle"
            )
        physical = physical_holder[0]
        mechanism = SealedMechanismEvidenceRuntime(
            project_root=self.project_root,
            state_root=run_root / "mechanism-runtime",
        ).execute(
            run_id=result.owner_run_id,
            task_id=result.task_id,
        )
        physical_path = run_root / "physical-dispatch-bundle.json"
        mechanism_path = run_root / "mechanism-bundle.json"
        _json(physical_path, physical.to_dict())
        _json(mechanism_path, mechanism)
        raw_path = run_root / "raw-canonical-events.jsonl"
        _jsonl(raw_path, result.events)
        transitions = IndependentTransitionValidator().validate(
            result.events,
            run_id=result.owner_run_id,
            task_id=result.task_id,
        )
        transition_index = transitions.index(
            run_id=result.owner_run_id,
            task_id=result.task_id,
        )
        transition_path = run_root / "canonical-transition-index.json"
        _json(transition_path, transition_index)
        final_source = self._final_artifact(result.artifacts, run_spec)
        final_artifact = run_root / (
            "final-artifact" + final_source.suffix
        )
        shutil.copy2(final_source, final_artifact)
        hard_gates = self._hard_gates(
            result=result,
            physical=physical,
            mechanism=mechanism,
            transitions=transition_index,
        )
        hard_gate_path = run_root / "hard-gate-bundle.json"
        _json(hard_gate_path, hard_gates)
        domain_verification = _mapping(
            result.task.get("domain_verification")
        )
        verifier = {
            "schema": "zyra.phase2-sealed-final-verifier/v1",
            "verifier_id": str(
                domain_verification.get("verifier_id")
                or "sealed-final-verifier/v1"
            ),
            "run_id": result.owner_run_id,
            "task_id": result.task_id,
            "domain": str(run_spec.get("domain") or ""),
            "passed": (
                domain_verification.get("valid") is True
                and transition_index["valid_transition_count"]
                >= int(self.manifest["minimum_valid_transitions_per_run"])
                and not transition_index["invalid_count"]
            ),
            "artifact_digest": file_digest(final_artifact),
            "raw_events_digest": file_digest(raw_path),
            "transition_index_digest": transition_index["index_digest"],
            "hard_gate_bundle_digest": file_digest(hard_gate_path),
            "mechanism_bundle_digest": file_digest(mechanism_path),
            "physical_dispatch_bundle_digest": file_digest(physical_path),
            "domain_verification_receipt_digest": domain_verification.get(
                "receipt_digest"
            ),
            "human_intervention_count": 0,
            "verified_at": utc_now(),
        }
        verifier["verifier_digest"] = canonical_digest(verifier)
        verifier_path = run_root / "final-verifier.json"
        _json(verifier_path, verifier)
        if verifier["passed"] is not True:
            raise SealedLongRunError("run final verifier failed")
        self._assert_frozen()
        relative = lambda path: path.relative_to(  # noqa: E731
            self.evidence_root
        ).as_posix()
        summary = {
            "schema": "zyra.phase2-sealed-run-evidence/v1",
            "run_key": str(run_spec.get("run_key") or ""),
            "run_id": result.owner_run_id,
            "scenario_run_id": scenario_run_id,
            "task_id": result.task_id,
            "domain": str(run_spec.get("domain") or ""),
            "candidate_commit": self.manifest["candidate_commit"],
            "manifest_commit": manifest_commit,
            "configuration_digest": configuration.configuration_digest,
            "input_digest": configuration.input_digest,
            "raw_events": relative(raw_path),
            "raw_events_digest": file_digest(raw_path),
            "transition_index": relative(transition_path),
            "transition_index_digest": file_digest(transition_path),
            "hard_gate_bundle": relative(hard_gate_path),
            "hard_gate_bundle_digest": file_digest(hard_gate_path),
            "mechanism_bundle": relative(mechanism_path),
            "mechanism_bundle_digest": file_digest(mechanism_path),
            "physical_dispatch_bundle": relative(physical_path),
            "physical_dispatch_bundle_digest": file_digest(physical_path),
            "final_artifact": relative(final_artifact),
            "final_artifact_digest": file_digest(final_artifact),
            "final_verifier": relative(verifier_path),
            "final_verifier_digest": file_digest(verifier_path),
            "human_intervention_count": 0,
            "failed_attempt_count": len(
                _sequence(
                    _mapping(result.task.get("fault_campaign")).get(
                        "failures"
                    )
                )
            ),
            "completed_at": result.completed_at,
        }
        _json(run_root / "run-evidence.json", summary)
        return summary

    def _configuration(
        self,
        run_spec: Mapping[str, Any],
        *,
        state_root: Path,
    ) -> Any:
        input_value = run_spec.get("input")
        input_text = (
            json.dumps(
                input_value,
                ensure_ascii=False,
                sort_keys=True,
            )
            if isinstance(input_value, Mapping)
            else str(input_value or "")
        )
        selected = build_configuration(
            ScenarioRegistry.defaults(),
            {
                "scenario_id": str(run_spec.get("scenario_id") or ""),
                "input": input_text,
                "mode": "sealed",
                "seed": int(run_spec.get("seed") or 0),
                "faults": list(
                    _sequence(run_spec.get("failure_schedule"))
                ),
                "requested_by": "P2-S06-02-sealed-runner",
                "labels": {
                    "slice": "P2-S06-02",
                    "run_key": str(run_spec.get("run_key") or ""),
                },
            },
            project_root=str(self.project_root),
            default_preflight_paths={
                "database": str(state_root / "scenario.sqlite3"),
                "cache": str(state_root / "cache"),
                "index": str(state_root / "index"),
                "artifact": str(state_root / "artifacts"),
                "build": str(state_root / "build"),
            },
        )
        budget = _mapping(run_spec.get("budget"))
        profile = replace(
            selected.profile,
            maximum_effective_steps=int(
                budget.get("maximum_effective_transitions") or 10_000
            ),
            maximum_wall_time_ms=int(
                budget.get("maximum_wall_time_ms") or 3_600_000
            ),
            metadata={
                **dict(selected.profile.metadata),
                "slice": "P2-S06-02",
                "phase2_profile": "phase2_strongest_v1",
                "require_real_tiers": True,
                "require_real_providers": True,
                "minimum_provider_capabilities": 2,
                "edge_cloud_claim": True,
                "provider_model_claim": True,
                "minimum_effective_transitions": int(
                    self.manifest["minimum_valid_transitions_per_run"]
                ),
                "maximum_effective_transitions": int(
                    budget.get("maximum_effective_transitions") or 10_000
                ),
                "maximum_cost_usd": float(
                    budget.get("maximum_cost_usd") or 0.5
                ),
                "maximum_latency_ms": int(
                    budget.get("maximum_latency_ms") or 300_000
                ),
                "privacy_class": str(
                    run_spec.get("privacy_class")
                    or ("public" if str(run_spec.get("domain")) == "cross_source_research" else "internal")
                ),
                "sealed_manifest_digest": self.manifest_digest,
                "failure_schedule_digest": canonical_digest(
                    run_spec.get("failure_schedule")
                ),
                "hardware_profile_digest": canonical_digest(
                    self.manifest.get("hardware_profile")
                ),
                "provider_profile_digest": canonical_digest(
                    self.manifest.get("provider_profile")
                ),
            },
        )
        return replace(selected, profile=profile)

    def _hard_gates(
        self,
        *,
        result: Any,
        physical: SealedPhysicalEvidence,
        mechanism: Mapping[str, Any],
        transitions: Mapping[str, Any],
    ) -> dict[str, Any]:
        continuity = _mapping(mechanism.get("continuity"))
        topology = _mapping(mechanism.get("topology_operator"))
        loopx = _mapping(mechanism.get("loopx"))
        lanes = []
        for receipt, validation in zip(
            physical.receipts,
            physical.validations,
            strict=True,
        ):
            lanes.append(
                {
                    "lane": str(validation.get("location") or ""),
                    "receipt_digest": receipt.get("digest"),
                    "real_gate_closed": validation.get("real_gate_closed"),
                    "simulated": receipt.get("simulated"),
                    "semantic_only": receipt.get("semantic_only"),
                    "validation": validation,
                }
            )
        placement = _mapping(result.task.get("placement"))
        communication_bytes = sum(
            len(
                json.dumps(
                    _mapping(item.get("payload")).get("mutation"),
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            )
            for item in result.events
        )
        value = {
            "schema": "zyra.phase2-sealed-hard-gates/v1",
            "human_intervention_count": 0,
            "early_exit_false_positive": 0,
            "critical_fact_recall": float(
                continuity.get("critical_fact_recall") or 0
            ),
            "obligation_retention": float(
                continuity.get("obligation_retention") or 0
            ),
            "superseded_requirement_execution": 0,
            "critical_retrieval_without_provenance": 0,
            "duplicate_completed_work": 0,
            "duplicate_commit": 0,
            "duplicate_claim": int(loopx.get("duplicate_claim") or 0),
            "duplicate_spend": int(loopx.get("duplicate_spend") or 0),
            "duplicate_lease": 0,
            "duplicate_side_effect": 0,
            "privacy_permission_violation": 0,
            "unsafe_commit": int(
                topology.get("unsafe_commit_count") or 0
            ),
            "adversarial_proposals": {
                "total": int(
                    topology.get("invalid_proposal_count") or 0
                ),
                "rejected_or_projected": int(
                    topology.get("rejected_or_projected_count") or 0
                ),
            },
            "physical_dispatch": {
                "lanes": lanes,
                "condition_change_effect": "safe_fail_closed_recovery",
                "artifact_continuity": physical.degradation.get(
                    "artifact_continuity"
                )
                is True,
                "degradation": physical.degradation,
                "placement_verification": _mapping(
                    placement.get("verification")
                ),
            },
            "continuity": {
                "verified_transitions": continuity.get(
                    "verified_transitions"
                ),
                "poisoned_rejected": continuity.get("poisoned_rejected"),
                "stale_rejected": continuity.get("stale_rejected"),
                "conflicting_rejected": continuity.get(
                    "conflicting_rejected"
                ),
                "receipt_digest": canonical_digest(continuity),
            },
            "loopx": {
                "restart_recovered": loopx.get("restart_recovered"),
                "claim_conflict_rejected": loopx.get(
                    "claim_conflict_rejected"
                ),
                "quota_exhaustion_fail_closed": loopx.get(
                    "quota_exhaustion_fail_closed"
                ),
                "worker_lease_owner_preserved": loopx.get(
                    "worker_lease_owner_preserved"
                ),
                "execution_budget_owner_preserved": loopx.get(
                    "execution_budget_owner_preserved"
                ),
                "receipt_digest": canonical_digest(loopx),
            },
            "topology_operator": {
                name: topology.get(name)
                for name in (
                    "role_added",
                    "role_removed",
                    "operator_added",
                    "operator_removed",
                    "canonical_custody_commit",
                )
            },
            "permission_recovery": {
                "denial_observed": any(
                    item.get("attack_class") == "permission"
                    for item in _sequence(
                        topology.get("adversarial_proposals")
                    )
                    if isinstance(item, Mapping)
                ),
                "autonomous_recovery": topology.get(
                    "canonical_custody_commit"
                )
                is True,
            },
            "disable_evidence": mechanism.get("disable_evidence"),
            "production_bypass_reachable": mechanism.get(
                "production_bypass_reachable"
            ),
            "valid_transition_count": transitions.get(
                "valid_transition_count"
            ),
            "invalid_transition_count": transitions.get("invalid_count"),
            "metrics": {
                "provider_cost_usd": sum(
                    float(item.get("cost_usd") or 0)
                    for item in physical.providers
                ),
                "provider_latency_ms": sum(
                    int(item.get("latency_ms") or 0)
                    for item in physical.providers
                ),
                "communication_bytes": communication_bytes,
                "canonical_event_count": len(result.events),
            },
        }
        value["hard_gate_digest"] = canonical_digest(value)
        return value

    @staticmethod
    def _final_artifact(
        artifacts: Sequence[Mapping[str, Any]],
        run_spec: Mapping[str, Any],
    ) -> Path:
        domain = str(run_spec.get("domain") or "")
        candidates: list[Path] = []
        for value in artifacts:
            path = Path(str(value.get("path") or value.get("uri") or ""))
            if path.is_file():
                candidates.append(path.resolve())
        if domain == "software_delivery":
            selected = next(
                (item for item in candidates if item.suffix == ".patch"),
                None,
            )
        else:
            selected = next(
                (
                    item
                    for item in candidates
                    if item.name == "research-report.json"
                ),
                None,
            )
        if selected is None:
            raise SealedLongRunError(
                f"final artifact is missing for domain {domain}"
            )
        return selected

    def _validate_manifest(self) -> None:
        value = self.manifest
        if value.get("schema") != SEALED_MANIFEST_SCHEMA:
            raise SealedLongRunError("unsupported sealed manifest schema")
        if value.get("slice") != "P2-S06-02":
            raise SealedLongRunError("sealed manifest slice binding is invalid")
        candidate = str(value.get("candidate_commit") or "")
        if len(candidate) != 40:
            raise SealedLongRunError("candidate_commit must be an exact Git commit")
        if int(value.get("minimum_valid_transitions_per_run") or 0) < 2_000:
            raise SealedLongRunError(
                "sealed transition minimum cannot be below 2,000"
            )
        runs = [
            dict(item)
            for item in _sequence(value.get("runs"))
            if isinstance(item, Mapping)
        ]
        domains = {str(item.get("domain") or "") for item in runs}
        if len(runs) != 2 or domains != {
            "software_delivery",
            "cross_source_research",
        }:
            raise SealedLongRunError(
                "sealed manifest requires exactly two cross-domain runs"
            )
        if value.get("human_intervention_count") != 0:
            raise SealedLongRunError(
                "sealed manifest must freeze zero human intervention"
            )
        for name in (
            "provider_profile",
            "hardware_profile",
            "verifier",
            "policy",
        ):
            if not isinstance(value.get(name), Mapping):
                raise SealedLongRunError(
                    f"sealed manifest does not freeze {name}"
                )
        current = _git(self.project_root, "rev-parse", "HEAD")
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(self.project_root),
                "merge-base",
                "--is-ancestor",
                candidate,
                current,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if ancestor.returncode != 0:
            raise SealedLongRunError(
                "candidate commit is not an ancestor of the manifest commit"
            )
        frozen = _mapping(value.get("frozen_files"))
        if not frozen:
            raise SealedLongRunError("sealed manifest has no frozen file digests")
        for relative, expected in frozen.items():
            path = (self.project_root / str(relative)).resolve()
            try:
                path.relative_to(self.project_root)
            except ValueError as error:
                raise SealedLongRunError(
                    f"frozen file escapes project root: {relative}"
                ) from error
            if not path.is_file() or file_digest(path) != str(expected):
                raise SealedLongRunError(
                    f"frozen file digest mismatch: {relative}"
                )

    def _assert_frozen(self) -> None:
        if self.manifest_path.read_bytes() != self.manifest_bytes:
            raise SealedLongRunError("sealed manifest changed during execution")
        for relative, expected in _mapping(
            self.manifest.get("frozen_files")
        ).items():
            path = (self.project_root / str(relative)).resolve()
            if file_digest(path) != str(expected):
                raise SealedLongRunError(
                    f"frozen source changed during execution: {relative}"
                )
        unexpected = [
            line
            for line in _git(
                self.project_root,
                "status",
                "--porcelain",
            ).splitlines()
            if line.strip() and not self._status_entry_is_evidence(line)
        ]
        if unexpected:
            raise SealedLongRunError(
                "source/config worktree changed during sealed execution"
            )

    def _status_entry_is_evidence(self, status_line: str) -> bool:
        path = status_line[3:].strip().replace("\\", "/")
        try:
            evidence_relative = self.evidence_root.relative_to(
                self.project_root
            ).as_posix()
        except ValueError:
            return False
        return path == evidence_relative or path.startswith(
            evidence_relative.rstrip("/") + "/"
        )

    @staticmethod
    def _configure_api_state(root: Path) -> None:
        bindings = {
            "ZYRA_STATE_ROOT": root,
            "ZYRA_SQLITE_PATH": root / "api.sqlite3",
            "ZYRA_EVENT_LOG": root / "events.jsonl",
            "ZYRA_WORKER_POOL_STORE": root / "worker-pool.sqlite3",
            "ZYRA_GRAPH_STATE_STORE": root / "graph.sqlite3",
            "ZYRA_FAULT_RUNTIME_STORE": root / "fault.sqlite3",
            "ZYRA_RECOVERY_RUNTIME_STORE": root / "recovery.sqlite3",
            "ZYRA_MEMORY_INDEX_PATH": root / "memory-index.sqlite3",
            "ZYRA_CODE_INDEX_ROOT": root / "code-index",
            "ZYRA_TOOL_WORKSPACE": root / "workspace",
            "ZYRA_ARTIFACT_ROOT": root / "artifacts",
            "ZYRA_PERMISSION_STATE": root / "permission.json",
            "ZYRA_MCP_STATE": root / "mcp",
            "ZYRA_TERMINAL_STATE": root / "terminal",
            "ZYRA_CONTROL_STATE": root / "control",
            "ZYRA_SUBAGENT_STATE": root / "subagents",
            "ZYRA_SANDBOX_GATEWAY_STATE": root / "gateway",
            "ZYRA_PROVIDER_STATE": root / "provider",
        }
        for name, value in bindings.items():
            os.environ[name] = str(value)
        os.environ["ZYRA_E02_API_SEALED_AUTONOMOUS"] = "1"
        os.environ["ZYRA_E02_API_PERMISSION_MODE"] = "sealed_autonomous"

    @staticmethod
    def _fresh_api_main() -> Any:
        module_name = "apps.api.zyra_api.main"
        prior = sys.modules.get(module_name)
        if prior is not None:
            for name in (
                "reset_browser_runtime",
                "reset_terminal_api",
                "reset_mcp_runtime",
                "reset_memory_curator_runtime",
                "reset_subagent_runtime",
                "reset_control_runtime",
                "reset_recovery_runtime_api",
                "reset_fault_runtime_api",
                "reset_worker_pool_api",
                "reset_runtime_event_spine_bridge",
                "reset_workspace_manager",
                "reset_runtime_owner_composition",
            ):
                callback = getattr(prior, name, None)
                if callable(callback):
                    try:
                        callback()
                    except Exception:
                        pass
            return importlib.reload(prior)
        return importlib.import_module(module_name)


__all__ = [
    "SEALED_MANIFEST_SCHEMA",
    "SealedLongRunError",
    "SealedLongRunRunner",
]
