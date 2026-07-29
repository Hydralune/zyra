from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import digest, invalid, mapping, require_commit, require_digest, utc_now
from .integrity import EvidenceIntegrityVerifier


class LiveBenchmarkFreezeGate:
    def verify(
        self,
        evidence_root: str | Path,
        *,
        expected_commit: str,
    ) -> dict[str, Any]:
        root = Path(evidence_root).resolve(strict=True)
        commit = require_commit(expected_commit)
        summary = load_json(root / "verification-summary.json")
        report = load_json(root / "benchmark-report.json")
        index = load_json(root / "100-point-evidence-index.json")
        manifest = load_json(root / "evidence-manifest.json")
        metadata = load_json(root / "implementation-metadata.json")
        findings: list[dict[str, Any]] = []
        if summary.get("verdict") != "PASS":
            findings.append({"code": "summary-verdict-not-pass"})
        if summary.get("target_commit") != commit:
            findings.append({"code": "summary-commit-mismatch"})
        if metadata.get("implementation_commit") != commit:
            findings.append({"code": "metadata-commit-mismatch"})
        if report.get("commit_sha") != commit:
            findings.append({"code": "report-commit-mismatch"})
        if index.get("commit_sha") != commit:
            findings.append({"code": "index-commit-mismatch"})
        if summary.get("human_intervention_count") != 0:
            findings.append({"code": "human-intervention-nonzero"})
        if summary.get("operator_intervention_count") != 0:
            findings.append({"code": "operator-intervention-nonzero"})
        if int(summary.get("long_run_max_effective_steps") or 0) < 2_000:
            findings.append({"code": "long-run-threshold-missing"})
        if int(summary.get("domain_count") or 0) < 2:
            findings.append({"code": "domain-count-insufficient"})
        if int(summary.get("variant_count") or 0) != 7:
            findings.append({"code": "variant-count-invalid"})
        if int(summary.get("repetition_count") or 0) < 3:
            findings.append({"code": "repetition-count-insufficient"})
        if int(summary.get("provider_count") or 0) < 2:
            findings.append({"code": "provider-count-insufficient"})
        if int(summary.get("model_count") or 0) < 2:
            findings.append({"code": "model-count-insufficient"})
        if sorted(summary.get("tier_ids") or []) != ["cloud", "device", "edge"]:
            findings.append({"code": "tier-evidence-incomplete"})
        provider_boundary = summary.get("provider_boundary")
        if not isinstance(provider_boundary, Mapping):
            findings.append({"code": "provider-boundary-missing"})
        else:
            if provider_boundary.get("protected_prior_receipts_only") is not False:
                findings.append({"code": "current-provider-evidence-missing"})
            if provider_boundary.get("external_model_request_made") is not True:
                findings.append({"code": "current-model-request-missing"})
            if int(provider_boundary.get("current_provider_count") or 0) < 2:
                findings.append({"code": "current-provider-count-insufficient"})
            if int(provider_boundary.get("current_model_count") or 0) < 2:
                findings.append({"code": "current-model-count-insufficient"})
            if (
                sorted(provider_boundary.get("current_tier_ids") or [])
                != ["cloud", "device", "edge"]
            ):
                findings.append({"code": "current-tier-evidence-incomplete"})
            if provider_boundary.get("same_run_as_formal_cases") is not True:
                findings.append({"code": "case-deployment-evidence-separated"})
        score = mapping(report.get("score"), "benchmark score")
        if score.get("verified") != 100 or score.get("complete") is not True:
            findings.append({"code": "report-score-incomplete"})
        report_digest = verify_embedded_digest(report, "report_digest")
        index_digest = verify_embedded_digest(index, "index_digest")
        summary_report = str(summary.get("report_digest") or "")
        summary_index = str(summary.get("evidence_index_digest") or "")
        if summary_report != report_digest:
            findings.append({"code": "summary-report-digest-mismatch"})
        if summary_index != index_digest:
            findings.append({"code": "summary-index-digest-mismatch"})
        if index.get("report_digest") != report_digest:
            findings.append({"code": "index-report-digest-mismatch"})
        campaign_id = str(report.get("campaign_id") or "")
        integrity = EvidenceIntegrityVerifier().verify(
            manifest,
            evidence_root=root,
            expected_campaign_id=campaign_id,
            expected_commit=commit,
        )
        if summary.get("manifest_digest") != integrity["manifest_digest"]:
            findings.append({"code": "summary-manifest-digest-mismatch"})
        required_blocks = (
            "line_gate",
            "focused_validation",
            "adjacent_regression",
            "source_boundary",
            "parent_closeout",
        )
        for key in required_blocks:
            block = summary.get(key)
            if not isinstance(block, Mapping) or block.get("status") != "passed":
                findings.append({"code": f"{key.replace('_', '-')}-not-passed"})
        if findings:
            raise invalid(
                "benchmark_freeze_admission_failed",
                "M3 live benchmark evidence failed freeze admission.",
                phase="freeze",
                detail={"findings": findings},
            )
        receipt = {
            "schema": "zyra.live-benchmark-freeze-admission/v1",
            "valid": True,
            "target_commit": commit,
            "campaign_id": campaign_id,
            "report_digest": report_digest,
            "evidence_index_digest": index_digest,
            "manifest_digest": integrity["manifest_digest"],
            "score": 100,
            "human_intervention_count": 0,
            "operator_intervention_count": 0,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise invalid(
            "benchmark_freeze_member_missing",
            "Required benchmark freeze member is missing.",
            phase="freeze",
            detail={"path": str(path)},
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise invalid(
            "benchmark_freeze_member_invalid",
            "Benchmark freeze member cannot be decoded.",
            phase="freeze",
            detail={"path": str(path)},
        ) from error
    return dict(mapping(value, "benchmark freeze member"))


def verify_embedded_digest(value: Mapping[str, Any], field: str) -> str:
    projection = dict(value)
    declared = require_digest(projection.pop(field, ""), field)
    observed = digest(projection)
    if declared != observed:
        raise invalid(
            "benchmark_freeze_embedded_digest_mismatch",
            "Benchmark evidence embedded digest is invalid.",
            phase="freeze",
            detail={"field": field},
        )
    return observed
