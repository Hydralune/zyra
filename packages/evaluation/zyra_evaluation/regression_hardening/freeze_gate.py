from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    SCHEMA_VERSION,
    canonical_json,
    stable_digest,
)
from .live_matrix import verify_live_matrix_payload
from .service import SUITE_ID, SUITE_VERSION


REQUIRED_CASES = (
    "default-path-reachability",
    "bidirectional-causality",
    "clean-state-isolation",
    "llm-control-boundary",
    "repository-security",
    "content-security",
    "code-index-security",
    "approval-security",
    "freeze-receipt-preflight",
)


@dataclass(frozen=True, slots=True)
class AdmissionFinding:
    code: str
    severity: str
    path: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.severity in {"blocker", "error"}

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "code": self.code,
            "severity": self.severity,
            "blocking": self.blocking,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class FreezeAdmissionReport:
    receipt_path: str
    expected_revision: str
    observed_revision: str
    suite_digest: str
    prerequisite_digest: str
    live_matrix_digest: str
    findings: tuple[AdmissionFinding, ...]
    case_status_counts: Mapping[str, int]
    case_ids: tuple[str, ...]
    mutation_assertions: int
    security_observations: int
    digest: str

    @property
    def valid(self) -> bool:
        return not any(item.blocking for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-regression-freeze-admission/v1",
            "valid": self.valid,
            "receipt_path": self.receipt_path,
            "expected_revision": self.expected_revision,
            "observed_revision": self.observed_revision,
            "suite_digest": self.suite_digest,
            "prerequisite_digest": self.prerequisite_digest,
            "live_matrix_digest": self.live_matrix_digest,
            "findings": [item.to_dict() for item in self.findings],
            "case_status_counts": dict(sorted(self.case_status_counts.items())),
            "case_ids": list(self.case_ids),
            "mutation_assertions": self.mutation_assertions,
            "security_observations": self.security_observations,
            "digest": self.digest,
        }


class RegressionFreezeGate:
    def __init__(
        self,
        project_root: str | Path,
        *,
        required_cases: Sequence[str] = REQUIRED_CASES,
        prerequisite_path: str | Path | None = None,
        live_matrix_path: str | Path | None = None,
        secret_canaries: Sequence[str] = (),
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.required_cases = tuple(required_cases)
        self.prerequisite_path = (
            Path(prerequisite_path).resolve(strict=False)
            if prerequisite_path is not None
            else self.project_root
            / "docs"
            / "reviews"
            / "evidence"
            / "M3-01-independent-review"
            / "aggregate-review-evidence.json"
        )
        self.live_matrix_path = (
            Path(live_matrix_path).resolve(strict=False)
            if live_matrix_path is not None
            else self.project_root
            / "docs"
            / "reviews"
            / "evidence"
            / "M3-S02A-01"
            / "live-matrix-receipt.json"
        )
        self.secret_canaries = tuple(
            str(item)
            for item in secret_canaries
            if len(str(item)) >= 4
        )

    def verify_file(
        self,
        receipt_path: str | Path,
        *,
        expected_revision: str = "HEAD",
    ) -> FreezeAdmissionReport:
        path = Path(receipt_path).resolve(strict=False)
        if not path.is_file():
            raise ContractError(f"regression receipt does not exist: {path}")
        if path.stat().st_size > 128 * 1024 * 1024:
            raise ContractError("regression receipt exceeds 128 MiB admission limit")
        raw = path.read_bytes()
        for canary in self.secret_canaries:
            if canary.encode("utf-8") in raw:
                raise ContractError(
                    f"regression receipt contains secret canary "
                    f"{stable_digest('canary', canary)}"
                )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError(f"invalid regression receipt JSON: {error}") from error
        if not isinstance(payload, Mapping):
            raise ContractError("regression receipt must be a JSON object")
        revision = self._revision(expected_revision)
        return self.verify_payload(
            payload,
            receipt_path=path,
            expected_revision=revision,
        )

    def verify_payload(
        self,
        payload: Mapping[str, Any],
        *,
        receipt_path: str | Path = "",
        expected_revision: str,
    ) -> FreezeAdmissionReport:
        findings: list[AdmissionFinding] = []

        def add(
            code: str,
            message: str,
            *,
            path: str = "",
            severity: str = "blocker",
        ) -> None:
            findings.append(
                AdmissionFinding(
                    code=code,
                    severity=severity,
                    path=path,
                    message=message,
                )
            )

        if payload.get("schema") != f"{SCHEMA_VERSION}/suite-receipt":
            add(
                "schema_invalid",
                "suite receipt schema is absent or unsupported",
                path="schema",
            )
        if payload.get("suite_id") != SUITE_ID:
            add(
                "suite_id_invalid",
                f"expected {SUITE_ID}",
                path="suite_id",
            )
        if str(payload.get("suite_version") or "") != SUITE_VERSION:
            add(
                "suite_version_invalid",
                f"expected {SUITE_VERSION}",
                path="suite_version",
            )
        observed_revision = str(payload.get("revision") or "")
        if observed_revision != expected_revision:
            add(
                "revision_mismatch",
                f"receipt targets {observed_revision}, expected {expected_revision}",
                path="revision",
            )
        if payload.get("passed") is not True:
            add(
                "suite_not_passed",
                "regression suite did not report a passing terminal result",
                path="passed",
            )
        if payload.get("cancelled") is True:
            add(
                "suite_cancelled",
                "cancelled suite cannot enter the freeze gate",
                path="cancelled",
            )
        suite_digest = str(payload.get("receipt_digest") or "")
        recomputed_suite = self._suite_digest(payload)
        if not hmac.compare_digest(suite_digest, recomputed_suite):
            add(
                "suite_digest_mismatch",
                f"expected {recomputed_suite}, observed {suite_digest}",
                path="receipt_digest",
            )
        registry_digest = str(payload.get("registry_digest") or "")
        if not registry_digest.startswith("sha256:"):
            add(
                "registry_digest_missing",
                "suite is not bound to a sealed registry digest",
                path="registry_digest",
            )
        cases = payload.get("cases")
        if not isinstance(cases, list):
            add("cases_invalid", "suite cases must be an array", path="cases")
            cases = []
        case_ids: list[str] = []
        status_counts: Counter[str] = Counter()
        mutation_assertions = 0
        security_observations = 0
        positive_fallbacks: list[str] = []
        for index, raw_case in enumerate(cases):
            path = f"cases[{index}]"
            if not isinstance(raw_case, Mapping):
                add("case_invalid", "case receipt is not an object", path=path)
                continue
            case = raw_case.get("case")
            if not isinstance(case, Mapping):
                add("case_spec_missing", "case spec is missing", path=f"{path}.case")
                continue
            case_id = str(case.get("case_id") or "")
            case_ids.append(case_id)
            status = str(raw_case.get("status") or "")
            status_counts[status] += 1
            if raw_case.get("passed") is not True or status != "passed":
                add(
                    "case_not_passed",
                    f"{case_id} did not pass",
                    path=path,
                )
            expected_case_digest = self._case_digest(raw_case)
            observed_case_digest = str(raw_case.get("receipt_digest") or "")
            if not hmac.compare_digest(expected_case_digest, observed_case_digest):
                add(
                    "case_digest_mismatch",
                    f"{case_id} receipt digest mismatch",
                    path=f"{path}.receipt_digest",
                )
            if not str(raw_case.get("input_digest") or "").startswith("sha256:"):
                add(
                    "generated_input_missing",
                    f"{case_id} lacks a generated-input digest",
                    path=f"{path}.input_digest",
                )
            attempts = raw_case.get("attempts")
            if not isinstance(attempts, list) or not attempts:
                add(
                    "attempts_missing",
                    f"{case_id} has no attempts",
                    path=f"{path}.attempts",
                )
                continue
            final_attempt = attempts[-1]
            if not isinstance(final_attempt, Mapping):
                add(
                    "attempt_invalid",
                    f"{case_id} final attempt is invalid",
                    path=f"{path}.attempts[-1]",
                )
                continue
            assertions = final_attempt.get("assertions")
            observations = final_attempt.get("observations")
            artifacts = final_attempt.get("artifacts")
            if not isinstance(assertions, list) or not assertions:
                add(
                    "assertions_missing",
                    f"{case_id} passed without assertions",
                    path=f"{path}.attempts[-1].assertions",
                )
                assertions = []
            if not isinstance(observations, list) or not observations:
                add(
                    "observations_missing",
                    f"{case_id} passed without runtime observations",
                    path=f"{path}.attempts[-1].observations",
                )
                observations = []
            if not isinstance(artifacts, list) or not artifacts:
                add(
                    "artifacts_missing",
                    f"{case_id} passed without an integrity-bound artifact",
                    path=f"{path}.attempts[-1].artifacts",
                )
                artifacts = []
            for assertion in assertions:
                if not isinstance(assertion, Mapping):
                    continue
                assertion_id = str(assertion.get("assertion_id") or "")
                if assertion.get("passed") is not True:
                    add(
                        "failed_assertion_in_passed_case",
                        f"{case_id}/{assertion_id} is failed",
                        path=path,
                    )
                if "reject-" in assertion_id or "mutation" in assertion_id:
                    mutation_assertions += 1
            for observation in observations:
                if not isinstance(observation, Mapping):
                    continue
                if str(observation.get("kind") or "") == "security":
                    security_observations += 1
                fallback = self._positive_fallback(observation)
                if fallback:
                    positive_fallbacks.append(f"{case_id}:{fallback}")
            for artifact_index, artifact in enumerate(artifacts):
                if not isinstance(artifact, Mapping):
                    add(
                        "artifact_invalid",
                        f"{case_id} artifact receipt is invalid",
                        path=f"{path}.attempts[-1].artifacts[{artifact_index}]",
                    )
                    continue
                digest = str(artifact.get("digest") or "")
                relative = str(artifact.get("relative_path") or "")
                if not digest.startswith("sha256:") or not relative:
                    add(
                        "artifact_integrity_missing",
                        f"{case_id} artifact lacks path/digest",
                        path=f"{path}.attempts[-1].artifacts[{artifact_index}]",
                    )
        duplicates = [
            case_id
            for case_id, count in Counter(case_ids).items()
            if count > 1
        ]
        if duplicates:
            add(
                "duplicate_cases",
                f"duplicate case receipts: {sorted(duplicates)}",
                path="cases",
            )
        missing = sorted(set(self.required_cases) - set(case_ids))
        if missing:
            add(
                "required_cases_missing",
                f"required cases are missing: {missing}",
                path="cases",
            )
        extras = sorted(set(case_ids) - set(self.required_cases))
        if extras:
            add(
                "unexpected_cases",
                f"unexpected cases are present: {extras}",
                path="cases",
                severity="warning",
            )
        if mutation_assertions < 20:
            add(
                "mutation_coverage_low",
                f"only {mutation_assertions} rejection/mutation assertions were observed",
                path="cases",
            )
        if security_observations < 2:
            add(
                "security_observation_low",
                f"only {security_observations} security observations were observed",
                path="cases",
            )
        if positive_fallbacks:
            add(
                "fallback_success",
                f"positive fallback behavior observed: {positive_fallbacks}",
                path="cases",
            )
        metadata = payload.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
            add(
                "suite_metadata_invalid",
                "suite metadata must bind the real-owner live matrix",
                path="metadata",
            )
        live_matrix_digest, live_findings = self._live_matrix(
            metadata,
            expected_revision,
        )
        findings.extend(live_findings)
        prerequisite_digest, prerequisite_findings = self._prerequisite(
            expected_revision
        )
        findings.extend(prerequisite_findings)
        material = {
            "receipt_path": str(receipt_path),
            "expected_revision": expected_revision,
            "observed_revision": observed_revision,
            "suite_digest": suite_digest,
            "prerequisite_digest": prerequisite_digest,
            "live_matrix_digest": live_matrix_digest,
            "findings": [item.to_dict() for item in findings],
            "case_status_counts": status_counts,
            "case_ids": case_ids,
            "mutation_assertions": mutation_assertions,
            "security_observations": security_observations,
        }
        return FreezeAdmissionReport(
            receipt_path=str(receipt_path),
            expected_revision=expected_revision,
            observed_revision=observed_revision,
            suite_digest=suite_digest,
            prerequisite_digest=prerequisite_digest,
            live_matrix_digest=live_matrix_digest,
            findings=tuple(findings),
            case_status_counts=dict(status_counts),
            case_ids=tuple(case_ids),
            mutation_assertions=mutation_assertions,
            security_observations=security_observations,
            digest=stable_digest(material),
        )

    def _live_matrix(
        self,
        suite_metadata: Mapping[str, Any],
        expected_revision: str,
    ) -> tuple[str, list[AdmissionFinding]]:
        findings: list[AdmissionFinding] = []
        claimed = str(
            suite_metadata.get("live_matrix_receipt_digest")
            or ""
        )
        if not claimed.startswith("sha256:"):
            findings.append(
                AdmissionFinding(
                    code="live_matrix_binding_missing",
                    severity="blocker",
                    path="metadata.live_matrix_receipt_digest",
                    message="suite is not bound to a real-owner live matrix receipt",
                )
            )
        if suite_metadata.get("live_matrix_passed") is not True:
            findings.append(
                AdmissionFinding(
                    code="live_matrix_binding_not_passed",
                    severity="blocker",
                    path="metadata.live_matrix_passed",
                    message="suite does not assert a passing live matrix",
                )
            )
        if not self.live_matrix_path.is_file():
            findings.append(
                AdmissionFinding(
                    code="live_matrix_receipt_missing",
                    severity="blocker",
                    path=self.live_matrix_path.as_posix(),
                    message="real-owner live matrix receipt is missing",
                )
            )
            return claimed, findings
        try:
            payload = json.loads(
                self.live_matrix_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            findings.append(
                AdmissionFinding(
                    code="live_matrix_receipt_invalid",
                    severity="blocker",
                    path=self.live_matrix_path.as_posix(),
                    message=f"live matrix JSON is invalid: {error}",
                )
            )
            return claimed, findings
        if not isinstance(payload, Mapping):
            findings.append(
                AdmissionFinding(
                    code="live_matrix_receipt_invalid",
                    severity="blocker",
                    path=self.live_matrix_path.as_posix(),
                    message="live matrix receipt is not an object",
                )
            )
            return claimed, findings
        valid, failures = verify_live_matrix_payload(
            payload,
            expected_revision=expected_revision,
        )
        observed = str(payload.get("receipt_digest") or "")
        if claimed and not hmac.compare_digest(claimed, observed):
            findings.append(
                AdmissionFinding(
                    code="live_matrix_binding_mismatch",
                    severity="blocker",
                    path="metadata.live_matrix_receipt_digest",
                    message=(
                        f"suite binds {claimed}, live receipt reports {observed}"
                    ),
                )
            )
        if not valid:
            findings.extend(
                AdmissionFinding(
                    code=code.split(":", 1)[0],
                    severity="blocker",
                    path=self.live_matrix_path.as_posix(),
                    message=code,
                )
                for code in failures
            )
        return observed or claimed, findings

    @staticmethod
    def _suite_digest(payload: Mapping[str, Any]) -> str:
        fields = (
            "suite_id",
            "suite_version",
            "profile",
            "revision",
            "started_at",
            "completed_at",
            "duration_seconds",
            "cases",
            "registry_digest",
            "artifact_root",
            "cancelled",
            "metadata",
        )
        return stable_digest({key: payload.get(key) for key in fields})

    @staticmethod
    def _case_digest(payload: Mapping[str, Any]) -> str:
        fields = (
            "spec_digest",
            "shard_id",
            "input_digest",
            "isolation_id",
            "attempts",
            "dependency_receipts",
        )
        return stable_digest({key: payload.get(key) for key in fields})

    @staticmethod
    def _positive_fallback(observation: Mapping[str, Any]) -> str:
        status = str(observation.get("status") or "").casefold()
        if status not in {
            "completed",
            "succeeded",
            "accepted",
            "verified",
            "mutation-accepted",
        }:
            return ""
        attributes = observation.get("attributes")
        if not isinstance(attributes, Mapping):
            return ""
        fallback = str(
            attributes.get("fallback_kind")
            or attributes.get("fallback_owner_id")
            or ""
        ).casefold()
        if fallback in {
            "mock_fallback",
            "fixture_fallback",
            "legacy_fallback",
            "source_repository_fallback",
        }:
            return fallback
        return ""

    def _prerequisite(
        self,
        expected_revision: str,
    ) -> tuple[str, list[AdmissionFinding]]:
        findings: list[AdmissionFinding] = []
        if not self.prerequisite_path.is_file():
            findings.append(
                AdmissionFinding(
                    code="m3_01_prerequisite_missing",
                    severity="blocker",
                    path=self.prerequisite_path.as_posix(),
                    message="M3-01 aggregate prerequisite receipt is missing",
                )
            )
            return "", findings
        raw = self.prerequisite_path.read_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            findings.append(
                AdmissionFinding(
                    code="m3_01_prerequisite_invalid",
                    severity="blocker",
                    path=self.prerequisite_path.as_posix(),
                    message=f"M3-01 prerequisite JSON is invalid: {error}",
                )
            )
            return digest, findings
        if not isinstance(payload, Mapping):
            findings.append(
                AdmissionFinding(
                    code="m3_01_prerequisite_invalid",
                    severity="blocker",
                    path=self.prerequisite_path.as_posix(),
                    message="M3-01 prerequisite is not an object",
                )
            )
            return digest, findings
        verdict = str(
            payload.get("verdict")
            or payload.get("aggregate_verdict")
            or payload.get("status")
            or ""
        )
        if verdict not in {"PASS_AFTER_FIXES", "pass", "passed", "PASS"}:
            # The aggregate evidence nests the verdict in metadata in some
            # revisions; accept only an explicit passing token anywhere in the
            # bounded top-level summary, never a missing verdict.
            summary = canonical_json(
                {
                    key: payload.get(key)
                    for key in (
                        "verdict",
                        "aggregate_verdict",
                        "status",
                        "valid",
                        "accepted",
                        "review",
                    )
                }
            )
            if (
                '"valid":true' not in summary
                and '"accepted":true' not in summary
                and "PASS_AFTER_FIXES" not in summary
            ):
                findings.append(
                    AdmissionFinding(
                        code="m3_01_prerequisite_not_passed",
                        severity="blocker",
                        path=self.prerequisite_path.as_posix(),
                        message="M3-01 prerequisite does not carry an explicit passing verdict",
                    )
                )
        return digest, findings

    def _revision(self, value: str) -> str:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.project_root.as_posix()}",
                "rev-parse",
                value,
            ],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        revision = completed.stdout.strip().casefold()
        if completed.returncode or len(revision) != 40:
            raise ContractError(
                f"cannot resolve Git revision {value}: {completed.stderr.strip()}"
            )
        return revision


__all__ = [
    "AdmissionFinding",
    "FreezeAdmissionReport",
    "REQUIRED_CASES",
    "RegressionFreezeGate",
]
