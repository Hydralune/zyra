"""Final critical review over verified first-stage evidence.

The review engine is intentionally evidence-oriented.  It does not rerun or
redefine M1/M2 runtime success, and it does not silently repair protected
records.  It verifies their immutable projections, checks the M3-03 product
boundary, and emits a blocking decision suitable for the final-freeze
orchestrator.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zyra_evaluation.freeze_reporting.canonical import digest, file_digest
from zyra_evaluation.freeze_reporting.pipeline import verify_freeze_output

from .common import (
    FindingLedger,
    FinalFreezeError,
    load_json,
    object_with_digest,
    require_boolean,
    require_commit,
    require_digest,
    require_identity,
    require_integer,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
    utc_now,
)


REQUIRED_REQUIREMENTS = {
    "REQ-APP-001",
    "REQ-APP-002",
    "REQ-APP-003",
    "REQ-APP-004",
    "REQ-APP-005",
    "REQ-COMP-001",
    "REQ-COMP-002",
    "REQ-COMP-003",
    "REQ-COMP-004",
    "REQ-COMP-005",
    "REQ-COMP-006",
    "REQ-COMP-007",
    "REQ-PERF-001",
    "REQ-PERF-002",
    "REQ-PERF-003",
    "REQ-TECH-001",
    "REQ-TECH-002",
    "REQ-TECH-003",
    "REQ-TECH-004",
}

REQUIRED_SOURCES = {
    "claude-code-best",
    "opencode",
    "browser-use",
    "OpenHands",
    "agentscope",
    "agent-framework",
    "hermes-agent",
    "langgraph",
    "oh-my-pi",
    "openclaw",
    "claudecode-related/claude-reviews-claude",
    "claudecode-related/Dive-into-Claude-Code",
}

M3_STAGE_ALLOWED_RUNTIME_PATHS = {
    "packages/integrations/zyra_integrations/ledger_audit.py",
    "packages/integrations/zyra_integrations/ledger_source_scan.py",
    "packages/orchestration/zyra_orchestration/deployment/doctor.py",
    "packages/productization/zyra_productization/release/ci.py",
    "packages/productization/zyra_productization/release/runtime.py",
    "packages/workers/zyra_workers/browser_worker.py",
}

REQUIRED_LANGGRAPH_ACTIVE = {
    "checkpoint-identity-lineage",
    "pending-committed-writes",
    "side-effect-fence",
    "exact-resume",
}

REQUIRED_LANGGRAPH_INACTIVE = {
    "stategraph-pregel",
    "generic-channel-reducer",
    "toolnode-stream-store",
    "sdk-server-deploy",
}

FIRST_STAGE_BLOCKER_CATEGORIES = {
    "bug-regression",
    "owner",
    "source",
    "runtime",
    "evidence",
    "release",
    "schedule",
    "submission",
    "rehearsal",
    "tamper",
}


class CriticalReviewEngine:
    """Review one verified M3-S03-01 output without trusting its builder."""

    def __init__(
        self,
        repository_root: str | Path,
        evidence_output: str | Path,
        *,
        expected_evidence_commit: str | None = None,
        stage_baseline_commit: str | None = None,
        review_target_commit: str | None = None,
    ) -> None:
        self.root = Path(repository_root).resolve(strict=True)
        candidate = Path(evidence_output)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        self.output = candidate.resolve(strict=True)
        self.expected_evidence_commit = (
            require_commit(
                expected_evidence_commit,
                "expected_evidence_commit",
            )
            if expected_evidence_commit
            else None
        )
        self.stage_baseline_commit = (
            require_commit(
                stage_baseline_commit,
                "stage_baseline_commit",
            )
            if stage_baseline_commit
            else None
        )
        self.review_target_commit = (
            require_commit(review_target_commit, "review_target_commit")
            if review_target_commit
            else self._head_commit()
        )
        self.ledger = FindingLedger()
        self.documents: dict[str, dict[str, Any]] = {}
        self.identities: dict[str, Any] = {}

    def review(self) -> dict[str, Any]:
        self._verify_repository_boundary()
        self._load_documents()
        self._verify_s03_output()
        self._review_score_and_requirements()
        self._review_live_cases()
        self._review_release_binding()
        self._review_sources_and_owner_custody()
        self._review_langgraph_boundary()
        self._review_algorithm_and_material_links()
        self._review_archive_and_replay()
        self._review_stage_increment()
        self._review_known_residuals()
        findings = self.ledger.to_dict()
        try:
            evidence_output = self.output.relative_to(self.root).as_posix()
        except ValueError:
            evidence_output = f"<external>/{self.output.name}"
        decision = {
            "schema": "zyra.final-critical-review/v1",
            "generated_at": utc_now(),
            "review_scope": {
                "kind": "M3-03-increment-only",
                "stage_baseline_commit": self.stage_baseline_commit,
                "review_target_commit": self.review_target_commit,
                "protected_history_reopened": False,
                "inherited_evidence_reverified": True,
            },
            "evidence_output": evidence_output,
            "identities": self.identities,
            "checks": self._check_summary(),
            "findings": findings,
            "classification": self._classification(findings),
            "blocking": not findings["valid"],
            "verdict": "BLOCKED" if not findings["valid"] else "PASS",
        }
        return object_with_digest(decision, digest_key="review_digest")

    def require_pass(self) -> dict[str, Any]:
        receipt = self.review()
        if receipt["blocking"]:
            raise FinalFreezeError(
                "critical-review-blocked",
                "final critical review found first-stage blockers",
                phase="critical-review",
                detail={
                    "review_digest": receipt["review_digest"],
                    "findings": receipt["findings"],
                },
            )
        return receipt

    def _load_documents(self) -> None:
        paths = {
            "generation": "generation-receipt.json",
            "archive_verification": "archive-verification.json",
            "replay_verification": "replay-verification.json",
            "evidence_index": "generated/100-point-evidence-index.json",
            "freeze_report": "generated/freeze-report.json",
            "ledger": "generated/internalization-ledger.json",
            "langgraph": "generated/langgraph-correction-matrix.json",
            "cases": "generated/case-studies.json",
            "algorithms": "generated/algorithm-material.json",
            "compatibility": "generated/compatibility-material.json",
            "ablation": "generated/ablation-material.json",
            "application_value": "generated/application-value.json",
            "input_set": "generated/input-set.json",
        }
        for key, relative in paths.items():
            path = self.output / relative
            self.documents[key] = load_json(path)
            self.identities[f"{key}_path"] = relative
            self.identities[f"{key}_sha256"] = file_digest(path)

        summary_path = (
            self.root
            / "docs/reviews/evidence/M3-S03-01/verification-summary.json"
        )
        self.documents["s03_summary"] = load_json(summary_path)
        self.identities["s03_summary_path"] = summary_path.relative_to(
            self.root
        ).as_posix()
        self.identities["s03_summary_sha256"] = file_digest(summary_path)

    def _verify_repository_boundary(self) -> None:
        git_root = self._git(
            "rev-parse",
            "--show-toplevel",
            label="repository-root",
        )
        if Path(git_root).resolve() != self.root:
            self.ledger.blocker(
                "repository-root-mismatch",
                "critical review is not running against the Zyra Git root",
                category="source",
                expected=str(self.root),
                actual=git_root,
            )
        if self.stage_baseline_commit:
            ancestor = self._git_returncode(
                "merge-base",
                "--is-ancestor",
                self.stage_baseline_commit,
                self.review_target_commit,
            )
            if ancestor != 0:
                self.ledger.blocker(
                    "stage-baseline-not-ancestor",
                    "M3-03 baseline is not an ancestor of the review target",
                    category="bug-regression",
                    baseline=self.stage_baseline_commit,
                    target=self.review_target_commit,
                )
        status = self._git("status", "--short", label="git-status")
        if status:
            self.ledger.warning(
                "review-worktree-dirty",
                "critical review was invoked with uncommitted files",
                category="bug-regression",
                lines=status.splitlines()[:100],
            )

    def _verify_s03_output(self) -> None:
        generation = self.documents["generation"]
        evidence_commit = require_commit(
            generation.get("target_commit"),
            "generation.target_commit",
        )
        if self.expected_evidence_commit:
            summary = self.documents["s03_summary"]
            recorded = require_commit(
                summary.get("target_commit"),
                "s03_summary.target_commit",
            )
            if recorded != evidence_commit:
                self.ledger.blocker(
                    "evidence-target-identity-mismatch",
                    "S03 summary and generated output target different commits",
                    category="evidence",
                    summary=recorded,
                    generation=evidence_commit,
                )
            if self.expected_evidence_commit != self._head_commit_of_record():
                self.ledger.blocker(
                    "unexpected-s03-evidence-commit",
                    "protected S03 evidence commit does not match the expected identity",
                    category="evidence",
                    expected=self.expected_evidence_commit,
                    actual=self._head_commit_of_record(),
                )
        try:
            verification = verify_freeze_output(
                self.output,
                expected_commit=evidence_commit,
            )
        except Exception as error:
            self.ledger.blocker(
                "s03-output-verification-failed",
                "independent S03 output verification failed",
                category="tamper",
                error=str(error),
            )
            return
        if verification.get("valid") is not True:
            self.ledger.blocker(
                "s03-output-not-valid",
                "S03 output verifier did not return valid=true",
                category="evidence",
                verification=verification,
            )
        self.identities["s03_target_commit"] = evidence_commit
        self.identities["s03_output_verification_digest"] = digest(verification)
        if generation.get("final_freeze_claimed") is not False:
            self.ledger.blocker(
                "premature-final-freeze-claim",
                "M3-S03-01 output must not claim final freeze",
                category="evidence",
            )
        if generation.get("next_freeze_owner") != "M3-S03-02":
            self.ledger.blocker(
                "wrong-final-freeze-owner",
                "M3-S03-01 did not hand final freeze to M3-S03-02",
                category="owner",
                actual=generation.get("next_freeze_owner"),
            )

    def _review_score_and_requirements(self) -> None:
        index = self.documents["evidence_index"]
        score = require_mapping(index.get("score"), "index.score")
        maximum = require_integer(
            score.get("maximum"),
            "index.score.maximum",
            minimum=100,
            maximum=100,
        )
        verified = require_integer(
            score.get("verified"),
            "index.score.verified",
            minimum=0,
            maximum=maximum,
        )
        complete = require_boolean(score.get("complete"), "index.score.complete")
        if verified != 100 or not complete:
            self.ledger.blocker(
                "score-not-complete",
                "100-point evidence index is incomplete",
                category="evidence",
                maximum=maximum,
                verified=verified,
                complete=complete,
            )
        requirements = require_mapping(
            index.get("requirements"),
            "index.requirements",
        )
        actual_ids = set(requirements)
        missing = sorted(REQUIRED_REQUIREMENTS - actual_ids)
        extra = sorted(actual_ids - REQUIRED_REQUIREMENTS)
        if missing:
            self.ledger.blocker(
                "required-evidence-rows-missing",
                "required competition evidence rows are missing",
                category="evidence",
                missing=missing,
            )
        if extra:
            self.ledger.warning(
                "unexpected-evidence-rows",
                "evidence index contains non-contract rows",
                category="evidence",
                extra=extra,
            )
        statuses: Counter[str] = Counter()
        for requirement_id, raw in requirements.items():
            row = require_mapping(raw, f"requirements.{requirement_id}")
            status = require_text(
                row.get("status"),
                f"requirements.{requirement_id}.status",
                maximum=64,
            )
            statuses[status] += 1
            blockers = require_sequence(
                row.get("blockers", []),
                f"requirements.{requirement_id}.blockers",
                allow_empty=True,
            )
            if blockers:
                self.ledger.blocker(
                    "requirement-has-blockers",
                    f"{requirement_id} retains evidence blockers",
                    category="evidence",
                    requirement_id=requirement_id,
                    blockers=blockers,
                )
            references = require_sequence(
                row.get("references"),
                f"requirements.{requirement_id}.references",
            )
            kinds = {
                require_text(
                    require_mapping(
                        reference,
                        f"{requirement_id}.references",
                    ).get("kind"),
                    f"{requirement_id}.references.kind",
                    maximum=64,
                )
                for reference in references
            }
            required_kinds = {
                "default-entry",
                "live-mutation",
                "artifact",
                "metric",
                "test",
                "commit",
                "config",
                "checksum",
            }
            absent_kinds = sorted(required_kinds - kinds)
            if absent_kinds:
                self.ledger.blocker(
                    "requirement-evidence-kind-missing",
                    f"{requirement_id} lacks mandatory evidence kinds",
                    category="evidence",
                    requirement_id=requirement_id,
                    missing=absent_kinds,
                )
        self.identities["requirement_statuses"] = dict(sorted(statuses.items()))

    def _review_live_cases(self) -> None:
        cases_document = self.documents["cases"]
        cases = require_sequence(cases_document.get("cases"), "cases.cases")
        domains: set[str] = set()
        live_count = 0
        transition_total = 0
        for raw in cases:
            case = require_mapping(raw, "case")
            case_id = require_identity(case.get("case_id"), "case.case_id")
            domain = require_identity(case.get("domain"), f"{case_id}.domain")
            domains.add(domain)
            live_source = require_boolean(
                case.get("live_source"),
                f"{case_id}.live_source",
            )
            replay_only = require_boolean(
                case.get("replay_only"),
                f"{case_id}.replay_only",
            )
            if live_source and not replay_only:
                live_count += 1
            minimum_transitions = require_integer(
                case.get("minimum_effective_transitions"),
                f"{case_id}.minimum_effective_transitions",
                minimum=0,
            )
            if minimum_transitions < 2000:
                self.ledger.blocker(
                    "case-effective-transition-floor",
                    f"{case_id} has fewer than 2,000 effective transitions",
                    category="evidence",
                    case_id=case_id,
                    actual=minimum_transitions,
                )
            transition_total += require_integer(
                case.get("total_effective_transitions"),
                f"{case_id}.total_effective_transitions",
                minimum=minimum_transitions,
            )
            human = require_integer(
                case.get("human_intervention_count"),
                f"{case_id}.human_intervention_count",
                minimum=0,
            )
            operator = require_integer(
                case.get("operator_intervention_count"),
                f"{case_id}.operator_intervention_count",
                minimum=0,
            )
            if human or operator:
                self.ledger.blocker(
                    "case-not-zero-human",
                    f"{case_id} contains human or operator intervention",
                    category="evidence",
                    case_id=case_id,
                    human=human,
                    operator=operator,
                )
            if not require_sequence(
                case.get("sealed_policy_hashes"),
                f"{case_id}.sealed_policy_hashes",
            ):
                self.ledger.blocker(
                    "case-sealed-policy-missing",
                    f"{case_id} lacks a sealed policy hash",
                    category="evidence",
                    case_id=case_id,
                )
            if not require_sequence(
                case.get("final_outcome_digests"),
                f"{case_id}.final_outcome_digests",
            ):
                self.ledger.blocker(
                    "case-final-artifact-missing",
                    f"{case_id} lacks final outcome digests",
                    category="evidence",
                    case_id=case_id,
                )
        if live_count < 2 or len(domains) < 2:
            self.ledger.blocker(
                "cross-domain-live-cases-missing",
                "at least two different live case domains are required",
                category="evidence",
                live_count=live_count,
                domains=sorted(domains),
            )
        self.identities["live_case_count"] = live_count
        self.identities["live_case_domains"] = sorted(domains)
        self.identities["live_effective_transition_total"] = transition_total

    def _review_release_binding(self) -> None:
        summary = self.documents["s03_summary"]
        binding = require_mapping(
            summary.get("release_binding"),
            "summary.release_binding",
        )
        if require_boolean(binding.get("required"), "release.required") is not True:
            self.ledger.blocker(
                "release-binding-not-required",
                "final evidence must require reviewed release input",
                category="release",
            )
        if binding.get("release_verdict") != "PASS":
            self.ledger.blocker(
                "release-verdict-not-pass",
                "reviewed release evidence is not passing",
                category="release",
                verdict=binding.get("release_verdict"),
            )
        if binding.get("release_ready") is not True:
            self.ledger.blocker(
                "release-not-ready",
                "reviewed release evidence is not ready",
                category="release",
            )
        passed = require_integer(
            binding.get("passed_gates"),
            "release.passed_gates",
            minimum=0,
        )
        mandatory = require_integer(
            binding.get("mandatory_gates"),
            "release.mandatory_gates",
            minimum=1,
        )
        failed = require_integer(
            binding.get("failed_gates"),
            "release.failed_gates",
            minimum=0,
        )
        blocked = require_integer(
            binding.get("blocked_gates"),
            "release.blocked_gates",
            minimum=0,
        )
        if passed != mandatory or failed or blocked:
            self.ledger.blocker(
                "release-gates-incomplete",
                "reviewed release gates are not fully passing",
                category="release",
                mandatory=mandatory,
                passed=passed,
                failed=failed,
                blocked=blocked,
            )
        if require_integer(
            binding.get("benchmark_score"),
            "release.benchmark_score",
            minimum=0,
            maximum=100,
        ) != 100:
            self.ledger.blocker(
                "release-benchmark-score-incomplete",
                "release binding does not carry a verified 100-point benchmark",
                category="release",
            )
        if require_integer(
            binding.get("benchmark_human_intervention_count"),
            "release.benchmark_human_intervention_count",
            minimum=0,
        ) != 0:
            self.ledger.blocker(
                "release-benchmark-human-intervention",
                "release binding contains human intervention",
                category="release",
            )
        self.identities["release_implementation_commit"] = require_commit(
            binding.get("release_implementation_commit"),
            "release.release_implementation_commit",
        )
        self.identities["release_evidence_commit"] = require_commit(
            binding.get("release_evidence_commit"),
            "release.release_evidence_commit",
        )

    def _review_sources_and_owner_custody(self) -> None:
        ledger = self.documents["ledger"]
        rows = require_sequence(ledger.get("rows"), "ledger.rows")
        source_rows: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        ownerless_active: list[str] = []
        for raw in rows:
            row = require_mapping(raw, "ledger.row")
            source_id = require_text(row.get("source_id"), "row.source_id")
            source_rows[source_id].append(row)
            role = require_text(row.get("role"), "row.role")
            status = require_text(row.get("status"), "row.status")
            owner = str(row.get("owner", "")).strip()
            if role in {
                "primary_implementation",
                "supplementary_implementation",
                "primary_semantic_source",
            }:
                if not owner:
                    ownerless_active.append(
                        f"{source_id}:{row.get('capability', '<unknown>')}"
                    )
                if status not in {"internalized", "productized", "active"}:
                    self.ledger.blocker(
                        "active-source-not-internalized",
                        "active source row is not internalized/productized",
                        category="source",
                        source_id=source_id,
                        capability=row.get("capability"),
                        role=role,
                        status=status,
                    )
                if not require_sequence(
                    row.get("target_paths"),
                    "row.target_paths",
                ):
                    self.ledger.blocker(
                        "active-source-target-missing",
                        "active source row has no target path",
                        category="source",
                        source_id=source_id,
                        capability=row.get("capability"),
                    )
                if not require_sequence(row.get("test_paths"), "row.test_paths"):
                    self.ledger.blocker(
                        "active-source-tests-missing",
                        "active source row has no behavior tests",
                        category="source",
                        source_id=source_id,
                        capability=row.get("capability"),
                    )
        missing_sources = sorted(REQUIRED_SOURCES - set(source_rows))
        if missing_sources:
            self.ledger.blocker(
                "required-source-rows-missing",
                "role-aware ledger omits required source records",
                category="source",
                missing=missing_sources,
            )
        if ownerless_active:
            self.ledger.blocker(
                "active-source-owner-missing",
                "active source rows lack canonical owners",
                category="owner",
                rows=sorted(ownerless_active),
            )
        for row in source_rows.get("openclaw", []):
            role = row.get("role")
            status = row.get("status")
            if role != "excluded_forward_only" or status not in {
                "historical-only",
                "historical_provenance_only",
                "excluded_forward_only",
            }:
                self.ledger.blocker(
                    "openclaw-forward-role",
                    "OpenClaw must remain excluded_forward_only",
                    category="source",
                    role=role,
                    status=status,
                    capability=row.get("capability"),
                )
        for source_id in (
            "claudecode-related/claude-reviews-claude",
            "claudecode-related/Dive-into-Claude-Code",
        ):
            for row in source_rows.get(source_id, []):
                if row.get("role") != "reference_only":
                    self.ledger.blocker(
                        "claude-reference-role-invalid",
                        "Claude auxiliary repository must remain reference-only",
                        category="source",
                        source_id=source_id,
                        role=row.get("role"),
                    )
        self.identities["ledger_row_count"] = len(rows)
        self.identities["ledger_source_count"] = len(source_rows)

    def _review_langgraph_boundary(self) -> None:
        document = self.documents["langgraph"]
        active_rows = require_sequence(
            document.get("active_langgraph_semantics"),
            "langgraph.active",
        )
        inactive_rows = require_sequence(
            document.get("inactive_langgraph_subsystems"),
            "langgraph.inactive",
        )
        active = {
            require_identity(
                require_mapping(row, "active_langgraph").get("mechanism"),
                "active_langgraph.mechanism",
            )
            for row in active_rows
        }
        inactive = {
            require_identity(
                require_mapping(row, "inactive_langgraph").get("mechanism"),
                "inactive_langgraph.mechanism",
            )
            for row in inactive_rows
        }
        if missing := sorted(REQUIRED_LANGGRAPH_ACTIVE - active):
            self.ledger.blocker(
                "langgraph-narrow-semantics-missing",
                "required narrow LangGraph recovery semantics are missing",
                category="source",
                missing=missing,
            )
        if missing := sorted(REQUIRED_LANGGRAPH_INACTIVE - inactive):
            self.ledger.blocker(
                "langgraph-broad-runtime-not-inactive",
                "broad LangGraph subsystems are not all frozen inactive",
                category="source",
                missing=missing,
            )
        for raw in inactive_rows:
            row = require_mapping(raw, "inactive_langgraph")
            if row.get("production_owner"):
                self.ledger.blocker(
                    "inactive-langgraph-has-owner",
                    "inactive LangGraph subsystem has a production owner",
                    category="owner",
                    mechanism=row.get("mechanism"),
                    owner=row.get("production_owner"),
                )
            if row.get("target_paths"):
                self.ledger.blocker(
                    "inactive-langgraph-has-target",
                    "inactive LangGraph subsystem has production target paths",
                    category="source",
                    mechanism=row.get("mechanism"),
                    paths=row.get("target_paths"),
                )
        for raw in active_rows:
            row = require_mapping(raw, "active_langgraph")
            if row.get("role") != "primary_semantic_source":
                self.ledger.blocker(
                    "langgraph-active-role-invalid",
                    "narrow LangGraph semantics must be a semantic source only",
                    category="source",
                    mechanism=row.get("mechanism"),
                    role=row.get("role"),
                )
            owner = require_text(
                row.get("production_owner"),
                "langgraph.production_owner",
            )
            if "GraphCommitRuntime" not in owner and "CheckpointRecoveryRuntime" not in owner:
                self.ledger.blocker(
                    "langgraph-owner-not-zyra-runtime",
                    "narrow LangGraph semantics are not owned by Zyra recovery",
                    category="owner",
                    mechanism=row.get("mechanism"),
                    owner=owner,
                )
        self.identities["langgraph_active"] = sorted(active)
        self.identities["langgraph_inactive"] = sorted(inactive)

    def _review_algorithm_and_material_links(self) -> None:
        algorithms = self.documents["algorithms"]
        rows = require_sequence(algorithms.get("algorithms"), "algorithms")
        required = {
            "dynamic-sparse-topology-routing",
            "low-entropy-structured-communication",
            "distributed-memory-compact-restore",
            "neuro-symbolic-action-admission",
            "device-edge-cloud-placement",
            "fault-classification-exact-recovery",
        }
        actual: set[str] = set()
        for raw in rows:
            row = require_mapping(raw, "algorithm")
            algorithm_id = require_identity(
                row.get("algorithm_id"),
                "algorithm.algorithm_id",
            )
            actual.add(algorithm_id)
            if not require_sequence(
                row.get("implementation_anchors"),
                f"{algorithm_id}.implementation_anchors",
            ):
                self.ledger.blocker(
                    "algorithm-implementation-link-missing",
                    "algorithm material lacks implementation links",
                    category="evidence",
                    algorithm_id=algorithm_id,
                )
            if not require_sequence(
                row.get("behavior_tests"),
                f"{algorithm_id}.behavior_tests",
            ):
                self.ledger.blocker(
                    "algorithm-test-link-missing",
                    "algorithm material lacks behavior test links",
                    category="evidence",
                    algorithm_id=algorithm_id,
                )
            complexity = require_mapping(
                row.get("complexity"),
                f"{algorithm_id}.complexity",
            )
            for kind in ("time", "space", "communication"):
                if not str(complexity.get(kind, "")).strip():
                    self.ledger.blocker(
                        "algorithm-complexity-missing",
                        f"algorithm material lacks {kind} complexity",
                        category="evidence",
                        algorithm_id=algorithm_id,
                        kind=kind,
                    )
        if missing := sorted(required - actual):
            self.ledger.blocker(
                "core-algorithm-material-missing",
                "core algorithm material is incomplete",
                category="evidence",
                missing=missing,
            )
        compatibility = self.documents["compatibility"]
        for key in ("deployment_profiles", "providers", "models"):
            if not require_sequence(
                compatibility.get(key),
                f"compatibility.{key}",
            ):
                self.ledger.blocker(
                    "compatibility-matrix-missing",
                    "compatibility material lacks a required matrix",
                    category="evidence",
                    matrix=key,
                )
        for key in ("failover_policy", "privacy_policy", "credential_policy"):
            if not require_mapping(
                compatibility.get(key),
                f"compatibility.{key}",
            ):
                self.ledger.blocker(
                    "compatibility-policy-missing",
                    "compatibility material lacks a required policy",
                    category="evidence",
                    policy=key,
                )

    def _review_archive_and_replay(self) -> None:
        generation = self.documents["generation"]
        archive = require_mapping(generation.get("archive"), "generation.archive")
        archive_path = self.output / safe_relative_path(
            archive.get("path"),
            "generation.archive.path",
        )
        expected = require_digest(
            archive.get("sha256"),
            "generation.archive.sha256",
        )
        actual = file_digest(archive_path)
        if actual != expected:
            self.ledger.blocker(
                "archive-byte-tamper",
                "first-stage archive bytes do not match the generation receipt",
                category="tamper",
                expected=expected,
                actual=actual,
            )
        archive_verification = self.documents["archive_verification"]
        if archive_verification.get("valid") is not True:
            self.ledger.blocker(
                "archive-verification-invalid",
                "archive verification is not valid",
                category="tamper",
            )
        replay = self.documents["replay_verification"]
        if replay.get("valid") is not True:
            self.ledger.blocker(
                "replay-verification-invalid",
                "projection replay verification is not valid",
                category="evidence",
            )
        if replay.get("task_success_recomputed") is not False:
            self.ledger.blocker(
                "replay-recomputed-task-success",
                "replay must not redefine task success",
                category="evidence",
            )
        projection_count = require_integer(
            replay.get("projection_count"),
            "replay.projection_count",
            minimum=1,
        )
        self.identities["replay_projection_count"] = projection_count
        self.identities["first_stage_archive_sha256"] = actual

    def _review_stage_increment(self) -> None:
        if not self.stage_baseline_commit:
            self.ledger.warning(
                "stage-increment-baseline-not-supplied",
                "M3-03 incremental diff was not available to the engine",
                category="bug-regression",
            )
            return
        name_status = self._git(
            "diff",
            "--name-status",
            self.stage_baseline_commit,
            self.review_target_commit,
            "--",
            "apps",
            "packages",
            "skills",
            "scripts",
            "tests",
            "docs",
            label="m3-03-name-status",
        )
        changed = []
        for line in name_status.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            path = parts[-1].replace("\\", "/")
            changed.append(path)
            if path.startswith(("vendor/", "vendor-runtimes/", "source-pool/")):
                self.ledger.blocker(
                    "m3-03-vendor-like-addition",
                    "M3-03 changed a vendor/source-pool boundary",
                    category="source",
                    path=path,
                )
        allowed_prefixes = (
            "packages/evaluation/",
            "scripts/",
            "tests/",
            "docs/",
        )
        unexpected = [
            path
            for path in changed
            if not path.startswith(allowed_prefixes)
            and path not in M3_STAGE_ALLOWED_RUNTIME_PATHS
        ]
        if unexpected:
            self.ledger.blocker(
                "m3-03-runtime-boundary-change",
                "M3-03 changed files outside report/freeze product boundaries",
                category="runtime",
                paths=unexpected,
            )
        forbidden = self._git(
            "diff",
            "--unified=0",
            self.stage_baseline_commit,
            self.review_target_commit,
            "--",
            "packages/evaluation",
            "scripts",
            label="m3-03-root-source-scan",
        )
        dependency_lines = [
            line[1:].strip()
            for line in forbidden.splitlines()
            if line.startswith("+")
            and not line.startswith("+++")
            and any(
                token in line.lower()
                for token in (
                    "file:" + "../",
                    "npm link " + "../",
                    "pip install -e " + "../",
                    "build-context: " + "../",
                    "workingdirectory: " + "../",
                    "cwd: " + "../",
                )
            )
        ]
        if dependency_lines:
            self.ledger.blocker(
                "m3-03-root-source-dependency",
                "M3-03 product code references root source repositories",
                category="source",
                hits=dependency_lines[:100],
            )
        self.identities["m3_03_changed_file_count"] = len(changed)
        self.identities["m3_03_changed_paths_digest"] = digest(sorted(changed))

    def _review_known_residuals(self) -> None:
        summary = self.documents["s03_summary"]
        residuals = require_sequence(
            summary.get("residual_non_blockers", []),
            "summary.residual_non_blockers",
            allow_empty=True,
        )
        for value in residuals:
            text = require_text(value, "residual", maximum=4096)
            lower = text.lower()
            if "does not claim the final first-stage freeze" in lower:
                continue
            if "offline wheelhouse" in lower:
                self.ledger.warning(
                    "offline-wheelhouse-not-proven",
                    text,
                    category="release",
                )
            elif "cloud provider credentials" in lower:
                self.ledger.observation(
                    "cloud-credentials-absent-fail-closed",
                    text,
                    category="runtime",
                )
            elif "separately" in lower and "same run" in lower:
                self.ledger.observation(
                    "case-provider-evidence-separated",
                    text,
                    category="evidence",
                )
            else:
                self.ledger.warning(
                    "inherited-residual",
                    text,
                    category="evidence",
                )

    def _check_summary(self) -> dict[str, Any]:
        return {
            "score_and_requirements": True,
            "live_cases": True,
            "release_binding": True,
            "source_and_owner_custody": True,
            "langgraph_boundary": True,
            "algorithm_and_material_links": True,
            "archive_and_replay": True,
            "m3_03_increment_boundary": bool(self.stage_baseline_commit),
            "protected_history_reopened": False,
        }

    def _classification(
        self,
        findings: Mapping[str, Any],
    ) -> dict[str, Any]:
        items = require_sequence(
            findings.get("findings", []),
            "findings.findings",
            allow_empty=True,
        )
        first_stage = []
        ci_hardening = []
        optimizations = []
        for raw in items:
            item = require_mapping(raw, "finding")
            severity = item.get("severity")
            category = item.get("category")
            if severity == "blocker":
                first_stage.append(item)
            elif severity == "warning":
                ci_hardening.append(item)
            else:
                optimizations.append(item)
        return {
            "first_stage_must_fix": first_stage,
            "second_stage_ci_hardening": ci_hardening,
            "pure_optimization": optimizations,
            "first_stage_deferred": False,
            "valid_for_freeze": not first_stage,
        }

    def _head_commit(self) -> str:
        return require_commit(
            self._git("rev-parse", "HEAD", label="head"),
            "head",
        )

    def _head_commit_of_record(self) -> str:
        return require_commit(
            self._git(
                "rev-parse",
                "98a001a44f2e506c0ef0144e912c3ba55699f11b",
                label="s03-evidence-commit",
            ),
            "s03_evidence_commit",
        )

    def _git(
        self,
        *arguments: str,
        label: str,
        allow_failure: bool = False,
    ) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0 and not allow_failure:
            self.ledger.blocker(
                "git-command-failed",
                f"Git command failed during {label}",
                category="bug-regression",
                command=["git", *arguments],
                returncode=completed.returncode,
                stderr=completed.stderr[-4000:],
            )
            return ""
        return completed.stdout.strip()

    def _git_returncode(self, *arguments: str) -> int:
        completed = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
        return completed.returncode
