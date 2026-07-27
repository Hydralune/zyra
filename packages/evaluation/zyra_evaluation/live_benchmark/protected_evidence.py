from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonicalize, digest, invalid, require_digest, utc_now


M1_EXIT_EVIDENCE_SHA256 = (
    "ce9b659581625d0f7362a0c25221f622e6a237d7bc8703b48d5e952185b7a2da"
)


@dataclass(frozen=True, slots=True)
class ProtectedDeploymentEvidence:
    source_path: str
    file_sha256: str
    content_digest: str
    target_commit: str
    tiers: tuple[dict[str, Any], ...]
    providers: tuple[dict[str, Any], ...]
    verified_at: str

    @property
    def bundle_digest(self) -> str:
        return digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.protected-deployment-evidence/v1",
            "source_path": self.source_path,
            "file_sha256": self.file_sha256,
            "content_digest": self.content_digest,
            "target_commit": self.target_commit,
            "tiers": canonicalize(self.tiers),
            "providers": canonicalize(self.providers),
            "no_new_provider_call": True,
            "verified_at": self.verified_at,
        }


class ProtectedDeploymentEvidenceLoader:
    """Load the exact protected M1 deployment/provider evidence.

    The loader deliberately exposes only facts present in the protected review.
    It does not manufacture current request IDs, endpoints, latency, cost or
    response payloads for M3.
    """

    def load(
        self,
        path: str | Path,
        *,
        expected_sha256: str = M1_EXIT_EVIDENCE_SHA256,
    ) -> ProtectedDeploymentEvidence:
        selected = Path(path).resolve(strict=True)
        payload = selected.read_bytes()
        observed_sha256 = hashlib.sha256(payload).hexdigest()
        expected = require_digest(expected_sha256, "protected evidence sha256")
        if observed_sha256 != expected:
            raise invalid(
                "benchmark_protected_evidence_changed",
                "Protected M1 deployment evidence no longer matches its frozen checksum.",
                phase="integrity",
                detail={
                    "path": str(selected),
                    "expected": expected,
                    "observed": observed_sha256,
                },
            )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise invalid(
                "benchmark_protected_evidence_invalid",
                "Protected M1 deployment evidence is not valid UTF-8 JSON.",
                phase="integrity",
                detail={"path": str(selected), "reason": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise invalid(
                "benchmark_protected_evidence_shape_invalid",
                "Protected M1 deployment evidence must be an object.",
                phase="integrity",
            )
        verdict = value.get("verdict")
        exit_bundle = value.get("exit_bundle")
        competition = value.get("competition_evidence")
        if not isinstance(verdict, Mapping) or not isinstance(
            exit_bundle, Mapping
        ) or not isinstance(competition, Mapping):
            raise invalid(
                "benchmark_protected_evidence_sections_missing",
                "Protected M1 evidence is missing exit or competition sections.",
                phase="integrity",
            )
        findings: list[dict[str, Any]] = []
        if verdict.get("m1_milestone_exit") != "passed":
            findings.append({"code": "m1-exit-not-passed"})
        if verdict.get("execution_state_update_authorized") is not True:
            findings.append({"code": "m1-exit-not-authorized"})
        if exit_bundle.get("handoff_ready") is not True:
            findings.append({"code": "m1-handoff-not-ready"})
        if exit_bundle.get("limitations") not in ([], ()):
            findings.append({"code": "m1-limitations-present"})
        content_digest = str(exit_bundle.get("content_digest") or "")
        try:
            require_digest(content_digest, "protected exit content digest")
        except ValueError:
            findings.append({"code": "m1-content-digest-invalid"})
        tiers = tuple(
            dict(item)
            for item in competition.get("execution_tiers") or ()
            if isinstance(item, Mapping)
        )
        providers = tuple(
            dict(item)
            for item in competition.get("providers") or ()
            if isinstance(item, Mapping)
        )
        if {str(item.get("tier") or "") for item in tiers} != {
            "local",
            "edge",
            "cloud",
        }:
            findings.append({"code": "m1-tier-set-incomplete"})
        if len({str(item.get("provider_id") or "") for item in providers}) < 2:
            findings.append({"code": "m1-provider-set-incomplete"})
        if any(item.get("simulated") is not False for item in (*tiers, *providers)):
            findings.append({"code": "m1-evidence-simulated"})
        commits = value.get("commits")
        commits = commits if isinstance(commits, Mapping) else {}
        target_commit = str(commits.get("final_implementation_target") or "")
        if len(target_commit) != 40:
            findings.append({"code": "m1-target-commit-invalid"})
        if findings:
            raise invalid(
                "benchmark_protected_evidence_not_admissible",
                "Protected M1 deployment evidence failed admission.",
                phase="integrity",
                detail={"findings": findings},
            )
        return ProtectedDeploymentEvidence(
            source_path=str(selected),
            file_sha256=observed_sha256,
            content_digest=content_digest,
            target_commit=target_commit,
            tiers=tiers,
            providers=providers,
            verified_at=utc_now(),
        )


def protected_fact_receipt(
    *,
    bundle: ProtectedDeploymentEvidence,
    kind: str,
    fact: Mapping[str, Any],
) -> dict[str, Any]:
    selected = dict(fact)
    identifier = (
        selected.get("tier")
        or selected.get("provider_id")
        or digest(selected)[:16]
    )
    projection = {
        "kind": kind,
        "identifier": str(identifier),
        "fact": canonicalize(selected),
        "source_file_sha256": bundle.file_sha256,
        "source_content_digest": bundle.content_digest,
        "source_target_commit": bundle.target_commit,
    }
    return {
        "prior_receipt_id": f"M1-08-exit:{kind}:{identifier}",
        "prior_receipt_digest": digest(projection),
        "prior_receipt_still_valid": True,
        "protected_projection": projection,
    }


__all__ = [
    "M1_EXIT_EVIDENCE_SHA256",
    "ProtectedDeploymentEvidence",
    "ProtectedDeploymentEvidenceLoader",
    "protected_fact_receipt",
]
