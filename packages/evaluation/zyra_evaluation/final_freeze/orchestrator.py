"""Final-freeze composition, immutable verdict, and independent verifier."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from zyra_evaluation.freeze_reporting.canonical import digest, file_digest

from .common import (
    FindingLedger,
    FinalFreezeError,
    ensure_new_directory,
    fail,
    load_json,
    object_with_digest,
    require_boolean,
    require_commit,
    require_digest,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
    utc_now,
    write_json,
)


COMPONENT_SPECS: dict[str, dict[str, Any]] = {
    "critical_review": {
        "schema": "zyra.final-critical-review/v1",
        "digest_key": "review_digest",
        "valid_key": "verdict",
        "valid_value": "PASS",
        "filename": "critical-review.json",
    },
    "schedule": {
        "schema": "zyra.final-freeze.release-schedule.v1",
        "digest_key": "digest",
        "valid_key": "valid",
        "valid_value": True,
        "filename": "release-schedule.json",
    },
    "rehearsal": {
        "schema": "zyra.final-freeze.rehearsal-receipt.v1",
        "digest_key": "digest",
        "valid_key": "valid",
        "valid_value": True,
        "filename": "rehearsal-receipt.json",
    },
    "submission": {
        "schema": "zyra.final-freeze.submission-verification.v1",
        "digest_key": "digest",
        "valid_key": "valid",
        "valid_value": True,
        "filename": "submission-verification.json",
    },
    "dual_review": {
        "schema": "zyra.final-freeze.dual-review.v1",
        "digest_key": "digest",
        "valid_key": "valid",
        "valid_value": True,
        "filename": "dual-review-receipt.json",
    },
    "navigation": {
        "schema": "zyra.final-freeze.evidence-navigation.v1",
        "digest_key": "digest",
        "valid_key": "valid",
        "valid_value": True,
        "filename": "evidence-navigation.json",
    },
    "handoff": {
        "schema": "zyra.final-freeze.handoff-ledger.v1",
        "digest_key": "digest",
        "valid_key": "valid",
        "valid_value": True,
        "filename": "second-stage-handoff.json",
    },
}

REQUIRED_RELEASE_GATES = {
    "stage-freeze": "2026-07-28",
    "rc1": "2026-09-01",
    "clean-rehearsal": "2026-09-08",
    "dual-review": "2026-09-12",
    "submission-lock": "2026-09-12",
    "submit": "2026-09-15",
}
REQUIRED_REHEARSAL_KINDS = {
    "clean-install",
    "offline-startup",
    "process-restart",
    "provider-failure",
    "semantic-health",
    "package-build",
    "submission-verification",
}
REQUIRED_NAVIGATION_KINDS = {
    "default-entry",
    "semantic-health",
    "live-case",
    "ablation",
    "fault-recovery",
    "causal-trace",
    "final-artifact",
    "submission",
}
REQUIRED_REVIEW_CHECKS = {
    "manifest",
    "checksums",
    "clean-rehearsal",
    "email-checklist",
}


@dataclass(frozen=True, slots=True)
class FinalFreezeIdentity:
    """Immutable source, target, implementation, and evidence identities."""

    stage_baseline_commit: str
    slice_baseline_commit: str
    implementation_commit: str
    evidence_commit: str | None
    s03_evidence_commit: str
    freeze_output_path: str

    @classmethod
    def from_dict(cls, value: Any) -> "FinalFreezeIdentity":
        item = require_mapping(value, "freeze_identity")
        evidence = item.get("evidence_commit")
        return cls(
            stage_baseline_commit=require_commit(
                item.get("stage_baseline_commit"),
                "freeze_identity.stage_baseline_commit",
            ),
            slice_baseline_commit=require_commit(
                item.get("slice_baseline_commit"),
                "freeze_identity.slice_baseline_commit",
            ),
            implementation_commit=require_commit(
                item.get("implementation_commit"),
                "freeze_identity.implementation_commit",
            ),
            evidence_commit=(
                require_commit(
                    evidence,
                    "freeze_identity.evidence_commit",
                )
                if evidence is not None
                else None
            ),
            s03_evidence_commit=require_commit(
                item.get("s03_evidence_commit"),
                "freeze_identity.s03_evidence_commit",
            ),
            freeze_output_path=safe_relative_path(
                item.get("freeze_output_path"),
                "freeze_identity.freeze_output_path",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_baseline_commit": self.stage_baseline_commit,
            "slice_baseline_commit": self.slice_baseline_commit,
            "implementation_commit": self.implementation_commit,
            "evidence_commit": self.evidence_commit,
            "s03_evidence_commit": self.s03_evidence_commit,
            "freeze_output_path": self.freeze_output_path,
        }


class FinalFreezeOrchestrator:
    """Compose all blocking gates into one first-stage verdict."""

    def evaluate(
        self,
        identity: FinalFreezeIdentity | dict[str, Any],
        components: Mapping[str, Any],
        *,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        selected_identity = (
            identity
            if isinstance(identity, FinalFreezeIdentity)
            else FinalFreezeIdentity.from_dict(identity)
        )
        ledger = FindingLedger()
        normalized_components: dict[str, dict[str, Any]] = {}
        bindings: list[dict[str, Any]] = []
        for name, spec in COMPONENT_SPECS.items():
            raw = components.get(name)
            if raw is None:
                ledger.blocker(
                    "freeze-component-missing",
                    "final freeze is missing a required gate component",
                    category="freeze",
                    component=name,
                )
                continue
            item = require_mapping(raw, f"components.{name}")
            normalized_components[name] = item
            binding = self._verify_component(name, spec, item, ledger)
            bindings.append(binding)
        extra = sorted(set(components) - set(COMPONENT_SPECS))
        if extra:
            ledger.warning(
                "unrecognized-freeze-components",
                "final freeze input contains unrecognized components",
                category="freeze",
                components=extra,
            )
        self._verify_cross_bindings(
            selected_identity,
            normalized_components,
            ledger,
        )
        findings = ledger.to_dict()
        verdict = "PASS" if findings["valid"] else "BLOCKED"
        document = {
            "schema": "zyra.final-freeze.verdict.v1",
            "generated_at": generated_at or utc_now(),
            "verdict": verdict,
            "blocking": not findings["valid"],
            "final_freeze_claimed": findings["valid"],
            "scope": {
                "reviewed_increment": "M3-03",
                "stage_baseline_commit": (
                    selected_identity.stage_baseline_commit
                ),
                "protected_history_reopened": False,
                "inherited_receipts_reverified": True,
            },
            "identity": selected_identity.to_dict(),
            "component_bindings": sorted(
                bindings,
                key=lambda item: item["component"],
            ),
            "findings": findings,
            "handoff_policy": {
                "first_stage_blockers_deferred": False,
                "ci_hardening_may_be_handed_off": findings["valid"],
                "future_optimization_may_be_handed_off": findings["valid"],
            },
        }
        return object_with_digest(document, digest_key="freeze_digest")

    def require_pass(
        self,
        identity: FinalFreezeIdentity | dict[str, Any],
        components: Mapping[str, Any],
    ) -> dict[str, Any]:
        verdict = self.evaluate(identity, components)
        if verdict["blocking"]:
            raise FinalFreezeError(
                "final-freeze-blocked",
                "one or more final-freeze gates did not pass",
                phase="final-freeze",
                detail={
                    "freeze_digest": verdict["freeze_digest"],
                    "findings": verdict["findings"],
                },
            )
        return verdict

    def write(
        self,
        output_directory: str | Path,
        identity: FinalFreezeIdentity | dict[str, Any],
        components: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Write components and verdict without overwriting prior evidence."""

        verdict = self.require_pass(identity, components)
        output = ensure_new_directory(output_directory)
        try:
            component_files: list[dict[str, Any]] = []
            for name, spec in COMPONENT_SPECS.items():
                path = output / spec["filename"]
                write_json(path, components[name])
                component_files.append(
                    {
                        "component": name,
                        "path": path.name,
                        "sha256": file_digest(path),
                    }
                )
            verdict_path = output / "final-freeze-verdict.json"
            write_json(verdict_path, verdict)
            index = object_with_digest(
                {
                    "schema": "zyra.final-freeze.output-index.v1",
                    "valid": True,
                    "verdict": verdict["verdict"],
                    "freeze_digest": verdict["freeze_digest"],
                    "identity": verdict["identity"],
                    "components": sorted(
                        component_files,
                        key=lambda item: item["component"],
                    ),
                    "verdict_path": verdict_path.name,
                    "verdict_sha256": file_digest(verdict_path),
                }
            )
            write_json(output / "final-freeze-index.json", index)
            verification = FinalFreezeVerifier().verify(output)
            if not verification["valid"]:
                fail(
                    "written-freeze-verification-failed",
                    "independent verifier rejected freshly written freeze",
                    phase="final-freeze-write",
                    findings=verification["findings"],
                )
            write_json(output / "final-freeze-verification.json", verification)
            receipt = object_with_digest(
                {
                    "schema": "zyra.final-freeze.generation-receipt.v1",
                    "valid": True,
                    "output_directory": output.name,
                    "freeze_digest": verdict["freeze_digest"],
                    "index_digest": index["digest"],
                    "verification_digest": verification["digest"],
                    "component_count": len(component_files),
                    "generated_at": utc_now(),
                }
            )
            write_json(output / "generation-receipt.json", receipt)
            return receipt
        except Exception:
            if output.exists():
                shutil.rmtree(output)
            raise

    def _verify_component(
        self,
        name: str,
        spec: dict[str, Any],
        item: dict[str, Any],
        ledger: FindingLedger,
    ) -> dict[str, Any]:
        if item.get("schema") != spec["schema"]:
            ledger.blocker(
                "freeze-component-schema-mismatch",
                "final-freeze component has an unsupported schema",
                category="freeze",
                component=name,
                expected=spec["schema"],
                actual=item.get("schema"),
            )
        digest_key = spec["digest_key"]
        embedded = item.get(digest_key)
        expected = digest(
            {key: value for key, value in item.items() if key != digest_key}
        )
        if embedded != expected:
            ledger.blocker(
                "freeze-component-digest-mismatch",
                "final-freeze component does not match embedded digest",
                category="tamper",
                component=name,
                expected=expected,
                actual=embedded,
            )
        valid_key = spec["valid_key"]
        if item.get(valid_key) != spec["valid_value"]:
            ledger.blocker(
                "freeze-component-not-passing",
                "required final-freeze component did not pass",
                category="freeze",
                component=name,
                expected=spec["valid_value"],
                actual=item.get(valid_key),
            )
        self._verify_component_semantics(name, item, ledger)
        return {
            "component": name,
            "schema": item.get("schema"),
            "digest_key": digest_key,
            "digest": embedded,
            "valid_key": valid_key,
            "valid_value": item.get(valid_key),
        }

    def _verify_component_semantics(
        self,
        name: str,
        item: dict[str, Any],
        ledger: FindingLedger,
    ) -> None:
        if name == "critical_review":
            findings = item.get("findings")
            classification = item.get("classification")
            if (
                not isinstance(findings, dict)
                or findings.get("valid") is not True
                or findings.get("counts", {}).get("blocker") != 0
            ):
                ledger.blocker(
                    "critical-review-findings-invalid",
                    "critical review retains blocking findings",
                    category="freeze",
                )
            if (
                not isinstance(classification, dict)
                or classification.get("first_stage_deferred") is not False
                or classification.get("valid_for_freeze") is not True
            ):
                ledger.blocker(
                    "critical-review-classification-invalid",
                    "critical review classification permits invalid deferral",
                    category="freeze",
                )
        elif name == "schedule":
            self._verify_schedule_semantics(item, ledger)
        elif name == "rehearsal":
            self._verify_rehearsal_semantics(item, ledger)
        elif name == "submission":
            archive = item.get("archive")
            checksums = item.get("checksums")
            if (
                not isinstance(archive, dict)
                or archive.get("valid") is not True
                or not isinstance(checksums, dict)
                or checksums.get("valid") is not True
            ):
                ledger.blocker(
                    "submission-subchecks-invalid",
                    "submission archive or checksum verification failed",
                    category="submission",
                )
            if not isinstance(item.get("manifest_digest"), str):
                ledger.blocker(
                    "submission-manifest-digest-missing",
                    "submission verification has no manifest digest",
                    category="submission",
                )
        elif name == "dual_review":
            self._verify_dual_review_semantics(item, ledger)
        elif name == "navigation":
            covered = set(item.get("covered_kinds", []))
            missing = sorted(REQUIRED_NAVIGATION_KINDS - covered)
            if missing:
                ledger.blocker(
                    "navigation-coverage-incomplete",
                    "evidence navigation omits required categories",
                    category="freeze",
                    missing=missing,
                )
        elif name == "handoff":
            counts = item.get("classification_counts")
            if (
                not isinstance(counts, dict)
                or counts.get("first-stage-blocker") != 0
            ):
                ledger.blocker(
                    "handoff-first-stage-blocker",
                    "handoff contains a first-stage blocker",
                    category="handoff",
                    counts=counts,
                )

    def _verify_schedule_semantics(
        self,
        item: dict[str, Any],
        ledger: FindingLedger,
    ) -> None:
        rows = item.get("gates")
        if not isinstance(rows, list):
            ledger.blocker(
                "schedule-gates-invalid",
                "release schedule has no gate array",
                category="schedule",
            )
            return
        gates: dict[str, dict[str, Any]] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                ledger.blocker(
                    "schedule-gate-invalid",
                    "release schedule contains a non-object gate",
                    category="schedule",
                )
                continue
            gate_id = raw.get("gate_id")
            if not isinstance(gate_id, str):
                ledger.blocker(
                    "schedule-gate-id-missing",
                    "release schedule gate has no identity",
                    category="schedule",
                )
                continue
            if gate_id in gates:
                ledger.blocker(
                    "schedule-gate-duplicate",
                    "release schedule contains a duplicate gate",
                    category="schedule",
                    gate_id=gate_id,
                )
            gates[gate_id] = raw
        for gate_id, due_date in REQUIRED_RELEASE_GATES.items():
            gate = gates.get(gate_id)
            if gate is None:
                ledger.blocker(
                    "schedule-required-gate-missing",
                    "release schedule omits a frozen deadline gate",
                    category="schedule",
                    gate_id=gate_id,
                )
                continue
            if not str(gate.get("due_at", "")).startswith(due_date):
                ledger.blocker(
                    "schedule-gate-date-mismatch",
                    "release schedule gate has the wrong frozen date",
                    category="schedule",
                    gate_id=gate_id,
                    expected=due_date,
                    actual=gate.get("due_at"),
                )
            if gate.get("blocking") is not True:
                ledger.blocker(
                    "schedule-gate-not-blocking",
                    "required release schedule gate is not blocking",
                    category="schedule",
                    gate_id=gate_id,
                )

    def _verify_rehearsal_semantics(
        self,
        item: dict[str, Any],
        ledger: FindingLedger,
    ) -> None:
        drills = item.get("drills")
        if not isinstance(drills, list):
            ledger.blocker(
                "rehearsal-drills-invalid",
                "rehearsal receipt has no drill array",
                category="rehearsal",
            )
            return
        kinds: set[str] = set()
        passed = 0
        for raw in drills:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            if isinstance(kind, str):
                kinds.add(kind)
            if raw.get("required") is True and raw.get("valid") is True:
                passed += 1
            elif raw.get("required") is True:
                ledger.blocker(
                    "rehearsal-required-drill-failed",
                    "required rehearsal drill did not pass",
                    category="rehearsal",
                    drill_id=raw.get("drill_id"),
                    kind=kind,
                )
        missing = sorted(REQUIRED_REHEARSAL_KINDS - kinds)
        if missing:
            ledger.blocker(
                "rehearsal-kinds-incomplete",
                "rehearsal does not cover all required failure modes",
                category="rehearsal",
                missing=missing,
            )
        if item.get("required_passed") != passed:
            ledger.blocker(
                "rehearsal-pass-count-mismatch",
                "rehearsal required-pass count does not match drills",
                category="rehearsal",
                expected=passed,
                actual=item.get("required_passed"),
            )

    def _verify_dual_review_semantics(
        self,
        item: dict[str, Any],
        ledger: FindingLedger,
    ) -> None:
        rows = item.get("attestations")
        if not isinstance(rows, list):
            rows = []
        reviewers: set[str] = set()
        roles: set[str] = set()
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            reviewer = raw.get("reviewer_id")
            role = raw.get("role")
            if isinstance(reviewer, str):
                reviewers.add(reviewer)
            if isinstance(role, str):
                roles.add(role)
            if raw.get("decision") != "approve":
                ledger.blocker(
                    "dual-review-not-approved",
                    "review attestation is not an approval",
                    category="submission",
                    reviewer_id=reviewer,
                )
            checks = set(raw.get("checks", []))
            missing = sorted(REQUIRED_REVIEW_CHECKS - checks)
            if missing:
                ledger.blocker(
                    "dual-review-checks-incomplete",
                    "review attestation omits mandatory checks",
                    category="submission",
                    reviewer_id=reviewer,
                    missing=missing,
                )
            if raw.get("manifest_digest") != item.get("manifest_digest"):
                ledger.blocker(
                    "dual-review-manifest-mismatch",
                    "review attestation names a different manifest",
                    category="submission",
                    reviewer_id=reviewer,
                )
        if len(reviewers) < 2 or len(roles) < 2:
            ledger.blocker(
                "dual-review-independence-missing",
                "dual review requires distinct identities and roles",
                category="submission",
                reviewer_count=len(reviewers),
                role_count=len(roles),
            )
        if item.get("reviewer_count") != len(reviewers):
            ledger.blocker(
                "dual-review-count-mismatch",
                "dual review count does not match attestations",
                category="submission",
                expected=len(reviewers),
                actual=item.get("reviewer_count"),
            )

    def _verify_cross_bindings(
        self,
        identity: FinalFreezeIdentity,
        components: dict[str, dict[str, Any]],
        ledger: FindingLedger,
    ) -> None:
        critical = components.get("critical_review", {})
        review_scope = critical.get("review_scope")
        if isinstance(review_scope, dict):
            expected_scope = {
                "kind": "M3-03-increment-only",
                "stage_baseline_commit": identity.stage_baseline_commit,
                "review_target_commit": identity.implementation_commit,
                "protected_history_reopened": False,
                "inherited_evidence_reverified": True,
            }
            for key, expected in expected_scope.items():
                if review_scope.get(key) != expected:
                    ledger.blocker(
                        "critical-review-scope-mismatch",
                        "critical review scope contradicts freeze identity",
                        category="freeze",
                        field=key,
                        expected=expected,
                        actual=review_scope.get(key),
                    )
        else:
            ledger.blocker(
                "critical-review-scope-missing",
                "critical review does not declare its M3-03 scope",
                category="freeze",
            )
        rehearsal = components.get("rehearsal", {})
        for field in ("target_commit", "started_from_commit"):
            value = rehearsal.get(field)
            if not _commit_matches(value, identity.implementation_commit):
                ledger.blocker(
                    "rehearsal-commit-binding-mismatch",
                    "rehearsal is not bound to implementation commit",
                    category="freeze",
                    field=field,
                    expected=identity.implementation_commit,
                    actual=value,
                )
        submission = components.get("submission", {})
        dual_review = components.get("dual_review", {})
        manifest_digest = submission.get("manifest_digest")
        if dual_review.get("manifest_digest") != manifest_digest:
            ledger.blocker(
                "dual-review-submission-mismatch",
                "reviewers did not approve the verified manifest",
                category="freeze",
                submission_manifest=manifest_digest,
                reviewed_manifest=dual_review.get("manifest_digest"),
            )
        schedule = components.get("schedule", {})
        if schedule.get("submission_date") != "2026-09-15":
            ledger.blocker(
                "schedule-deadline-mismatch",
                "final schedule omits the official submission deadline",
                category="schedule",
                expected="2026-09-15",
                actual=schedule.get("submission_date"),
            )
        handoff = components.get("handoff", {})
        counts = handoff.get("classification_counts")
        if isinstance(counts, dict):
            if counts.get("first-stage-blocker", 0) != 0:
                ledger.blocker(
                    "first-stage-blocker-in-handoff",
                    "first-stage blocker was moved into handoff",
                    category="handoff",
                    count=counts.get("first-stage-blocker"),
                )
        else:
            ledger.blocker(
                "handoff-counts-missing",
                "handoff ledger has no classification counts",
                category="handoff",
            )
        navigation = components.get("navigation", {})
        covered = set(navigation.get("covered_kinds", []))
        for required in ("default-entry", "final-artifact", "submission"):
            if required not in covered:
                ledger.blocker(
                    "navigation-final-path-missing",
                    "evidence navigation misses a final acceptance path",
                    category="freeze",
                    required_kind=required,
                )


class FinalFreezeVerifier:
    """Verify a final-freeze output without using orchestrator memory."""

    def verify(self, output_directory: str | Path) -> dict[str, Any]:
        root = Path(output_directory).resolve()
        ledger = FindingLedger()
        index = self._load(
            root / "final-freeze-index.json",
            "final_freeze_index",
            ledger,
        )
        verdict = self._load(
            root / "final-freeze-verdict.json",
            "final_freeze_verdict",
            ledger,
        )
        components: dict[str, dict[str, Any]] = {}
        for name, spec in COMPONENT_SPECS.items():
            components[name] = self._load(
                root / spec["filename"],
                name,
                ledger,
            )
        self._verify_embedded_digest(index, "digest", "index", ledger)
        self._verify_embedded_digest(
            verdict,
            "freeze_digest",
            "verdict",
            ledger,
        )
        self._verify_index_files(root, index, verdict, components, ledger)
        identity_raw = verdict.get("identity")
        try:
            identity = FinalFreezeIdentity.from_dict(identity_raw)
        except FinalFreezeError as error:
            ledger.blocker(
                error.code,
                str(error),
                category="freeze-verification",
                **error.detail,
            )
            identity = None
        if identity is not None:
            independent = FinalFreezeOrchestrator().evaluate(
                identity,
                components,
                generated_at=verdict.get("generated_at"),
            )
            if independent["freeze_digest"] != verdict.get("freeze_digest"):
                ledger.blocker(
                    "freeze-verdict-recomputation-mismatch",
                    "recomputed final-freeze verdict differs from stored one",
                    category="tamper",
                    expected=independent["freeze_digest"],
                    actual=verdict.get("freeze_digest"),
                )
            if independent["verdict"] != "PASS":
                ledger.blocker(
                    "freeze-verdict-no-longer-passes",
                    "independent gate recomputation is blocking",
                    category="freeze-verification",
                    findings=independent["findings"],
                )
        if verdict.get("final_freeze_claimed") is not True:
            ledger.blocker(
                "final-freeze-not-claimed",
                "stored verdict does not claim final freeze",
                category="freeze-verification",
            )
        document = {
            "schema": "zyra.final-freeze.independent-verification.v1",
            "valid": ledger.valid,
            "output_directory": root.name,
            "freeze_digest": verdict.get("freeze_digest"),
            "index_digest": index.get("digest"),
            "component_count": len(components),
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)

    def require_valid(self, output_directory: str | Path) -> dict[str, Any]:
        receipt = self.verify(output_directory)
        if not receipt["valid"]:
            raise FinalFreezeError(
                "final-freeze-verification-failed",
                "independent final-freeze verification found blockers",
                phase="final-freeze-verification",
                detail={"findings": receipt["findings"]},
            )
        return receipt

    def _load(
        self,
        path: Path,
        label: str,
        ledger: FindingLedger,
    ) -> dict[str, Any]:
        if not path.is_file():
            ledger.blocker(
                "freeze-output-file-missing",
                "final-freeze output is missing a required file",
                category="freeze-verification",
                path=path.name,
            )
            return {}
        try:
            return load_json(path, label=label)
        except FinalFreezeError as error:
            ledger.blocker(
                error.code,
                str(error),
                category="freeze-verification",
                path=path.name,
                **error.detail,
            )
            return {}

    def _verify_embedded_digest(
        self,
        document: dict[str, Any],
        key: str,
        label: str,
        ledger: FindingLedger,
    ) -> None:
        expected = digest(
            {name: value for name, value in document.items() if name != key}
        )
        actual = document.get(key)
        if actual != expected:
            ledger.blocker(
                "freeze-output-digest-mismatch",
                "final-freeze output document failed digest verification",
                category="tamper",
                document=label,
                expected=expected,
                actual=actual,
            )

    def _verify_index_files(
        self,
        root: Path,
        index: dict[str, Any],
        verdict: dict[str, Any],
        components: dict[str, dict[str, Any]],
        ledger: FindingLedger,
    ) -> None:
        rows = require_sequence(
            index.get("components", []),
            "final_freeze_index.components",
            allow_empty=True,
        )
        indexed: dict[str, dict[str, Any]] = {}
        for position, raw in enumerate(rows):
            row = require_mapping(
                raw,
                f"final_freeze_index.components[{position}]",
            )
            name = require_text(
                row.get("component"),
                f"final_freeze_index.components[{position}].component",
            )
            if name in indexed:
                ledger.blocker(
                    "duplicate-freeze-index-component",
                    "final-freeze index names a component more than once",
                    category="freeze-verification",
                    component=name,
                )
            indexed[name] = row
            path_value = row.get("path")
            if not isinstance(path_value, str):
                ledger.blocker(
                    "freeze-index-path-invalid",
                    "final-freeze index component path is missing",
                    category="freeze-verification",
                    component=name,
                )
                continue
            try:
                relative = safe_relative_path(
                    path_value,
                    f"component.{name}.path",
                )
            except FinalFreezeError as error:
                ledger.blocker(
                    error.code,
                    str(error),
                    category="freeze-verification",
                    component=name,
                    **error.detail,
                )
                continue
            path = root / relative
            if not path.is_file():
                ledger.blocker(
                    "indexed-freeze-file-missing",
                    "indexed final-freeze component file is missing",
                    category="freeze-verification",
                    component=name,
                    path=relative,
                )
                continue
            actual = file_digest(path)
            if row.get("sha256") != actual:
                ledger.blocker(
                    "indexed-freeze-file-digest-mismatch",
                    "indexed final-freeze component file was modified",
                    category="tamper",
                    component=name,
                    expected=row.get("sha256"),
                    actual=actual,
                )
        missing = sorted(set(COMPONENT_SPECS) - set(indexed))
        if missing:
            ledger.blocker(
                "freeze-index-components-missing",
                "final-freeze index omits required components",
                category="freeze-verification",
                missing=missing,
            )
        if index.get("freeze_digest") != verdict.get("freeze_digest"):
            ledger.blocker(
                "freeze-index-verdict-mismatch",
                "final-freeze index names a different verdict digest",
                category="tamper",
                expected=verdict.get("freeze_digest"),
                actual=index.get("freeze_digest"),
            )
        verdict_path = root / "final-freeze-verdict.json"
        if (
            verdict_path.is_file()
            and index.get("verdict_sha256") != file_digest(verdict_path)
        ):
            ledger.blocker(
                "freeze-index-verdict-file-mismatch",
                "verdict file digest differs from final-freeze index",
                category="tamper",
                expected=index.get("verdict_sha256"),
                actual=file_digest(verdict_path),
            )


def _commit_matches(value: Any, expected: str) -> bool:
    return isinstance(value, str) and (
        value == expected
        or value.startswith(expected)
        or expected.startswith(value)
    )
