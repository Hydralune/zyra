from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import (
    bounded_integer,
    bounded_text,
    digest,
    identity,
    invalid,
    mapping,
    require_digest,
    require_keys,
    sequence,
    utc_now,
)
from .models import DomainKind


class DeterministicDomainVerifier:
    def verify(
        self,
        value: Mapping[str, Any],
        *,
        domain: DomainKind,
        run_id: str,
    ) -> dict[str, Any]:
        selected_run_id = identity(run_id, "run id")
        if value.get("run_id") not in {None, selected_run_id}:
            raise invalid(
                "benchmark_verifier_run_mismatch",
                "Deterministic verification belongs to a different run.",
                phase="verification",
            )
        if value.get("deterministic") is not True:
            raise invalid(
                "benchmark_verifier_not_deterministic",
                "Formal task verifier must be deterministic.",
                phase="verification",
            )
        verifier_id = identity(value.get("verifier_id"), "verifier id")
        implementation_digest = require_digest(
            value.get("implementation_digest"),
            "verifier implementation digest",
        )
        rules_digest = require_digest(
            value.get("rules_digest"),
            "verifier rules digest",
        )
        common = self._common(value)
        if domain is DomainKind.SOFTWARE_DELIVERY:
            domain_receipt = self._software(value)
        elif domain is DomainKind.CROSS_SOURCE_RESEARCH:
            domain_receipt = self._research(value)
        else:
            raise invalid(
                "benchmark_verifier_domain_unknown",
                "Deterministic verifier received an unsupported domain.",
                phase="verification",
            )
        model_judge = self._model_judge(value.get("model_judge"), common["valid"])
        output = {
            "schema": "zyra.live-benchmark-domain-verification/v1",
            "valid": True,
            "run_id": selected_run_id,
            "domain": domain.value,
            "verifier_id": verifier_id,
            "implementation_digest": implementation_digest,
            "rules_digest": rules_digest,
            "common_receipt": common,
            "domain_receipt": domain_receipt,
            "model_judge_receipt": model_judge,
            "verification_digest": digest(value),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _common(self, value: Mapping[str, Any]) -> dict[str, Any]:
        checks = tuple(
            mapping(item, "deterministic check")
            for item in sequence(value.get("checks"), "deterministic checks")
        )
        if not checks:
            raise invalid(
                "benchmark_deterministic_checks_empty",
                "Formal verifier contains no deterministic checks.",
                phase="verification",
            )
        identifiers: set[str] = set()
        kinds: Counter[str] = Counter()
        findings: list[dict[str, Any]] = []
        for index, check in enumerate(checks):
            check_id = identity(
                check.get("check_id"),
                f"deterministic check[{index}] id",
            )
            if check_id in identifiers:
                findings.append(
                    {"code": "check-id-duplicate", "check_id": check_id}
                )
            identifiers.add(check_id)
            kind = (
                bounded_text(
                    check.get("kind"),
                    f"deterministic check {check_id} kind",
                    maximum_bytes=128,
                )
                .lower()
                .replace("_", "-")
            )
            kinds[kind] += 1
            if check.get("passed") is not True:
                findings.append(
                    {"code": "deterministic-check-failed", "check_id": check_id}
                )
            if check.get("deterministic") is not True:
                findings.append(
                    {"code": "check-not-deterministic", "check_id": check_id}
                )
            require_digest(
                check.get("input_digest"),
                f"deterministic check {check_id} input digest",
            )
            require_digest(
                check.get("output_digest"),
                f"deterministic check {check_id} output digest",
            )
        required = {"artifact-invariant", "checksum", "completion"}
        missing = sorted(required - set(kinds))
        if missing:
            findings.append({"code": "common-check-kind-missing", "kinds": missing})
        if value.get("valid") is not True:
            findings.append({"code": "verifier-declared-invalid"})
        if findings:
            raise invalid(
                "benchmark_deterministic_verification_invalid",
                "Deterministic task verification failed.",
                phase="verification",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-common-verifier/v1",
            "valid": True,
            "check_count": len(checks),
            "kind_counts": dict(sorted(kinds.items())),
            "check_digest": digest(checks),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _software(self, value: Mapping[str, Any]) -> dict[str, Any]:
        software = mapping(value.get("software"), "software verification")
        require_keys(
            software,
            (
                "test_runs",
                "schema_checks",
                "patch_digest",
                "workspace_revision_before",
                "workspace_revision_after",
                "artifact_invariants",
            ),
            "software verification",
        )
        tests = tuple(
            mapping(item, "test run")
            for item in sequence(software.get("test_runs"), "test runs")
        )
        schemas = tuple(
            mapping(item, "schema check")
            for item in sequence(
                software.get("schema_checks"),
                "schema checks",
            )
        )
        invariants = tuple(
            mapping(item, "software artifact invariant")
            for item in sequence(
                software.get("artifact_invariants"),
                "software artifact invariants",
            )
        )
        findings: list[dict[str, Any]] = []
        if not tests:
            findings.append({"code": "software-tests-empty"})
        total_tests = 0
        failed_tests = 0
        commands: set[str] = set()
        for index, item in enumerate(tests):
            run_id = identity(item.get("test_run_id"), f"test run[{index}] id")
            command_digest = require_digest(
                item.get("command_digest"),
                f"test run {run_id} command digest",
            )
            commands.add(command_digest)
            collected = bounded_integer(
                item.get("collected"),
                f"test run {run_id} collected",
                minimum=1,
                maximum=10_000_000,
            )
            failed = bounded_integer(
                item.get("failed"),
                f"test run {run_id} failed",
                minimum=0,
                maximum=collected,
            )
            exit_code = bounded_integer(
                item.get("exit_code"),
                f"test run {run_id} exit code",
                minimum=-2**31,
                maximum=2**31 - 1,
            )
            total_tests += collected
            failed_tests += failed
            if failed or exit_code:
                findings.append(
                    {
                        "code": "software-test-run-failed",
                        "test_run_id": run_id,
                        "failed": failed,
                        "exit_code": exit_code,
                    }
                )
        if not schemas:
            findings.append({"code": "software-schema-checks-empty"})
        for index, item in enumerate(schemas):
            schema_id = identity(item.get("schema_id"), f"schema check[{index}] id")
            require_digest(
                item.get("schema_digest"),
                f"schema check {schema_id} schema digest",
            )
            require_digest(
                item.get("instance_digest"),
                f"schema check {schema_id} instance digest",
            )
            if item.get("valid") is not True:
                findings.append(
                    {"code": "software-schema-invalid", "schema_id": schema_id}
                )
        if not invariants:
            findings.append({"code": "software-artifact-invariants-empty"})
        for index, item in enumerate(invariants):
            invariant_id = identity(
                item.get("invariant_id"),
                f"software artifact invariant[{index}] id",
            )
            if item.get("passed") is not True:
                findings.append(
                    {
                        "code": "software-artifact-invariant-failed",
                        "invariant_id": invariant_id,
                    }
                )
        patch_digest = require_digest(
            software.get("patch_digest"),
            "software patch digest",
        )
        before = require_digest(
            software.get("workspace_revision_before"),
            "workspace revision before",
        )
        after = require_digest(
            software.get("workspace_revision_after"),
            "workspace revision after",
        )
        if before == after:
            findings.append({"code": "software-workspace-unchanged"})
        if software.get("dirty_state_preserved") is not True:
            findings.append({"code": "software-dirty-state-not-preserved"})
        if software.get("rollback_verified") is not True:
            findings.append({"code": "software-rollback-not-verified"})
        if findings:
            raise invalid(
                "benchmark_software_verification_invalid",
                "Software delivery failed deterministic verification.",
                phase="verification",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-software-verifier/v1",
            "valid": True,
            "test_run_count": len(tests),
            "test_count": total_tests,
            "failed_test_count": failed_tests,
            "command_count": len(commands),
            "schema_check_count": len(schemas),
            "artifact_invariant_count": len(invariants),
            "patch_digest": patch_digest,
            "workspace_revision_before": before,
            "workspace_revision_after": after,
            "software_verification_digest": digest(software),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _research(self, value: Mapping[str, Any]) -> dict[str, Any]:
        research = mapping(value.get("research"), "research verification")
        require_keys(
            research,
            (
                "sources",
                "claims",
                "citations",
                "artifact_invariants",
                "report_digest",
            ),
            "research verification",
        )
        sources = tuple(
            mapping(item, "research source")
            for item in sequence(research.get("sources"), "research sources")
        )
        claims = tuple(
            mapping(item, "research claim")
            for item in sequence(research.get("claims"), "research claims")
        )
        citations = tuple(
            mapping(item, "research citation")
            for item in sequence(
                research.get("citations"),
                "research citations",
            )
        )
        invariants = tuple(
            mapping(item, "research artifact invariant")
            for item in sequence(
                research.get("artifact_invariants"),
                "research artifact invariants",
            )
        )
        findings: list[dict[str, Any]] = []
        source_ids: set[str] = set()
        source_digests: set[str] = set()
        authorities: set[str] = set()
        for index, item in enumerate(sources):
            source_id = identity(item.get("source_id"), f"source[{index}] id")
            source_digest = require_digest(
                item.get("content_digest"),
                f"source {source_id} content digest",
            )
            authority = bounded_text(
                item.get("authority"),
                f"source {source_id} authority",
                maximum_bytes=512,
            ).lower()
            if source_id in source_ids:
                findings.append(
                    {"code": "research-source-id-duplicate", "source_id": source_id}
                )
            if source_digest in source_digests:
                findings.append(
                    {
                        "code": "research-source-content-duplicate",
                        "source_id": source_id,
                    }
                )
            source_ids.add(source_id)
            source_digests.add(source_digest)
            authorities.add(authority)
            if item.get("live") is not True or item.get("replay") is True:
                findings.append(
                    {"code": "research-source-not-live", "source_id": source_id}
                )
            status = bounded_integer(
                item.get("status"),
                f"source {source_id} status",
                minimum=100,
                maximum=599,
            )
            if status < 200 or status >= 300:
                findings.append(
                    {
                        "code": "research-source-fetch-failed",
                        "source_id": source_id,
                        "status": status,
                    }
                )
        if len(authorities) < 2:
            findings.append(
                {
                    "code": "research-authority-diversity-insufficient",
                    "authorities": sorted(authorities),
                }
            )
        citation_ids: set[str] = set()
        cited_sources: Counter[str] = Counter()
        for index, item in enumerate(citations):
            citation_id = identity(
                item.get("citation_id"),
                f"citation[{index}] id",
            )
            source_id = identity(
                item.get("source_id"),
                f"citation {citation_id} source id",
            )
            citation_ids.add(citation_id)
            cited_sources[source_id] += 1
            if source_id not in source_ids:
                findings.append(
                    {
                        "code": "research-citation-source-missing",
                        "citation_id": citation_id,
                        "source_id": source_id,
                    }
                )
            if item.get("span_verified") is not True:
                findings.append(
                    {
                        "code": "research-citation-span-invalid",
                        "citation_id": citation_id,
                    }
                )
        supported_claims = 0
        for index, item in enumerate(claims):
            claim_id = identity(item.get("claim_id"), f"claim[{index}] id")
            claim_citations = {
                identity(raw, f"claim {claim_id} citation id")
                for raw in sequence(
                    item.get("citation_ids"),
                    f"claim {claim_id} citation ids",
                )
            }
            missing_citations = sorted(claim_citations - citation_ids)
            if missing_citations:
                findings.append(
                    {
                        "code": "research-claim-citation-missing",
                        "claim_id": claim_id,
                        "citation_ids": missing_citations,
                    }
                )
            if item.get("supported") is True and claim_citations:
                supported_claims += 1
            else:
                findings.append(
                    {"code": "research-claim-unsupported", "claim_id": claim_id}
                )
        for index, item in enumerate(invariants):
            invariant_id = identity(
                item.get("invariant_id"),
                f"research artifact invariant[{index}] id",
            )
            if item.get("passed") is not True:
                findings.append(
                    {
                        "code": "research-artifact-invariant-failed",
                        "invariant_id": invariant_id,
                    }
                )
        report_digest = require_digest(
            research.get("report_digest"),
            "research report digest",
        )
        if findings:
            raise invalid(
                "benchmark_research_verification_invalid",
                "Research delivery failed deterministic verification.",
                phase="verification",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-research-verifier/v1",
            "valid": True,
            "source_count": len(sources),
            "authority_count": len(authorities),
            "claim_count": len(claims),
            "supported_claim_count": supported_claims,
            "citation_count": len(citations),
            "artifact_invariant_count": len(invariants),
            "report_digest": report_digest,
            "research_verification_digest": digest(research),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _model_judge(
        self,
        value: Any,
        deterministic_valid: bool,
    ) -> dict[str, Any]:
        if value is None:
            output = {
                "schema": "zyra.live-benchmark-model-judge/v1",
                "enabled": False,
                "isolated": True,
                "can_override_deterministic": False,
                "disagreement": False,
                "uncertainty": None,
                "verified_at": utc_now(),
            }
            output["receipt_digest"] = digest(output)
            return output
        judge = mapping(value, "model judge")
        findings: list[dict[str, Any]] = []
        if judge.get("isolated") is not True:
            findings.append({"code": "model-judge-not-isolated"})
        if judge.get("can_override_deterministic") is True:
            findings.append({"code": "model-judge-can-override"})
        uncertainty = float(judge.get("uncertainty", 1.0))
        if uncertainty < 0 or uncertainty > 1:
            findings.append({"code": "model-judge-uncertainty-invalid"})
        verdict = str(judge.get("verdict") or "").lower()
        if verdict not in {"pass", "fail", "inconclusive"}:
            findings.append({"code": "model-judge-verdict-invalid"})
        model_pass = verdict == "pass"
        disagreement = model_pass != deterministic_valid
        declared_disagreement = judge.get("disagreement") is True
        if disagreement != declared_disagreement:
            findings.append({"code": "model-judge-disagreement-mismatch"})
        if findings:
            raise invalid(
                "benchmark_model_judge_invalid",
                "Optional model judge violates isolation or uncertainty policy.",
                phase="verification",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-model-judge/v1",
            "enabled": True,
            "isolated": True,
            "can_override_deterministic": False,
            "verdict": verdict,
            "uncertainty": uncertainty,
            "disagreement": disagreement,
            "judge_digest": digest(judge),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output
