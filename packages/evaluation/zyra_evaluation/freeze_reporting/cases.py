from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import digest, now, require_digest, require_mapping, require_sequence
from .errors import blocker, fail, require_no_blockers
from .inputs import FreezeInputSet


class CaseStudyBuilder:
    """Builds dual-domain case studies only from admitted live source receipts."""

    def __init__(self, inputs: FreezeInputSet) -> None:
        self.inputs = inputs

    def build(self) -> dict[str, Any]:
        source_document = self.inputs.document("benchmark-source-runs")
        values = source_document.get("sources")
        if values is None:
            values = source_document.get("runs")
        sources = [
            require_mapping(item, "source run")
            for item in require_sequence(values, "source runs")
        ]
        grouped = defaultdict(list)
        for source in sources:
            grouped[str(source.get("domain") or "")].append(source)
        cases = [
            self._case(domain, runs)
            for domain, runs in sorted(grouped.items())
        ]
        material = {
            "schema": "zyra.first-stage-case-studies/v1",
            "cases": cases,
            "deployment_compatibility": self._deployment_compatibility(),
            "generated_at": now(),
        }
        material["verification"] = self.verify(material)
        material["material_digest"] = digest(material)
        return material

    def verify(self, material: Mapping[str, Any]) -> dict[str, Any]:
        cases = [
            require_mapping(item, "case study")
            for item in require_sequence(material.get("cases"), "case studies")
        ]
        findings: list[dict[str, Any]] = []
        domains = set()
        for case in cases:
            domain = str(case.get("domain") or "")
            if domain in domains:
                findings.append(
                    blocker(
                        "case-domain-duplicate",
                        "Case study domain is duplicated.",
                        domain=domain,
                    )
                )
            domains.add(domain)
            if int(case.get("minimum_effective_transitions") or 0) < 2_000:
                findings.append(
                    blocker(
                        "case-transition-threshold-missing",
                        "Case study does not contain a 2,000-transition live run.",
                        domain=domain,
                    )
                )
            if int(case.get("human_intervention_count", -1)) != 0:
                findings.append(
                    blocker(
                        "case-human-intervention-nonzero",
                        "Case study is not zero-human.",
                        domain=domain,
                    )
                )
            if int(case.get("repetition_count") or 0) < 3:
                findings.append(
                    blocker(
                        "case-repetition-count-insufficient",
                        "Case study has fewer than three live repetitions.",
                        domain=domain,
                    )
                )
            for required in (
                "new_input_digests",
                "sealed_policy_hashes",
                "archive_digests",
                "artifact_counts",
                "fault_counts",
                "configuration_digests",
                "verifier_receipts",
                "final_outcome_digests",
            ):
                if not case.get(required):
                    findings.append(
                        blocker(
                            "case-evidence-class-missing",
                            "Case study omits a required live evidence class.",
                            domain=domain,
                            evidence_class=required,
                        )
                    )
        if len(domains) < 2:
            findings.append(
                blocker(
                    "case-domain-count-insufficient",
                    "At least two cross-domain live case studies are required.",
                    domains=sorted(domains),
                )
            )
        deployment = require_mapping(
            material.get("deployment_compatibility"),
            "case deployment compatibility",
        )
        if deployment.get("same_run_as_case") is not False:
            findings.append(
                blocker(
                    "case-deployment-claim-conflated",
                    "Protected M1 deployment evidence cannot be presented as a "
                    "new provider call inside M3 case runs.",
                )
            )
        if deployment.get("case_runs_no_new_provider_call") is not True:
            findings.append(
                blocker(
                    "case-provider-call-boundary-invalid",
                    "Formal M3 case material must preserve its no-new-provider-call "
                    "boundary when using protected compatibility evidence.",
                )
            )
        if set(deployment.get("tiers") or []) != {"local", "edge", "cloud"}:
            findings.append(
                blocker(
                    "case-deployment-tier-evidence-incomplete",
                    "Case material lacks protected local-edge-cloud evidence.",
                )
            )
        if len(deployment.get("providers") or []) < 2:
            findings.append(
                blocker(
                    "case-deployment-provider-evidence-incomplete",
                    "Case material lacks two protected real provider/model receipts.",
                )
            )
        require_no_blockers(
            findings,
            code="case-studies-invalid",
            message="Dual-domain case-study material is incomplete.",
            phase="material",
        )
        receipt = {
            "schema": "zyra.first-stage-case-study-verification/v1",
            "valid": True,
            "domain_count": len(domains),
            "domains": sorted(domains),
            "case_count": len(cases),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _case(
        self,
        domain: str,
        runs: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        repetitions: set[int] = set()
        transitions = []
        event_counts = []
        human_interventions = 0
        operator_interventions = 0
        input_digests = []
        policy_hashes = []
        archive_digests = []
        artifact_counts = []
        fault_counts = []
        configuration_digests = []
        verifier_receipts = []
        final_outcomes = []
        run_rows = []
        workers: Counter[str] = Counter()
        for run in runs:
            repetition = int(run.get("repetition") or 0)
            payload = require_mapping(run.get("source"), "source run payload")
            repetitions.add(repetition)
            transition_count = int(payload.get("effective_transition_count") or 0)
            transitions.append(transition_count)
            event_counts.append(int(payload.get("event_count") or 0))
            human_interventions += int(payload.get("human_intervention_count") or 0)
            operator_interventions += int(
                payload.get("operator_intervention_count") or 0
            )
            input_digests.append(
                require_digest(payload.get("input_digest"), "case input digest")
            )
            manifest = require_mapping(
                payload.get("manifest"),
                "case causal archive manifest",
            )
            policy_hashes.append(
                require_digest(
                    payload.get("policy_digest") or manifest.get("policy_digest"),
                    "case policy digest",
                )
            )
            archive_digests.append(
                require_digest(payload.get("archive_digest"), "case archive digest")
            )
            artifact_counts.append(int(payload.get("artifact_count") or 0))
            fault_counts.append(int(payload.get("fault_count") or 0))
            configuration_digests.append(
                require_digest(
                    payload.get("configuration_digest"),
                    "case configuration digest",
                )
            )
            verifier = payload.get("domain_verification")
            verifier_receipts.append(
                require_digest(
                    verifier.get("receipt_digest"),
                    "case verifier receipt digest",
                )
                if isinstance(verifier, Mapping)
                else digest(str(verifier))
            )
            final_outcomes.append(
                require_digest(run.get("outcome_digest"), "case outcome digest")
            )
            worker_values = payload.get("workers") or []
            if isinstance(worker_values, str):
                worker_values = worker_values.split()
            for worker in worker_values:
                workers[worker] += 1
            run_rows.append(
                {
                    "repetition": repetition,
                    "scenario_run_id": payload.get("scenario_run_id"),
                    "task_id": payload.get("task_id"),
                    "effective_transitions": transition_count,
                    "event_count": int(payload.get("event_count") or 0),
                    "fault_count": int(payload.get("fault_count") or 0),
                    "artifact_count": int(payload.get("artifact_count") or 0),
                    "archive_digest": payload.get("archive_digest"),
                    "outcome_digest": run.get("outcome_digest"),
                }
            )
        return {
            "case_id": f"case-{domain}",
            "domain": domain,
            "live_source": True,
            "replay_only": False,
            "repetition_count": len(repetitions),
            "repetitions": sorted(repetitions),
            "minimum_effective_transitions": min(transitions, default=0),
            "maximum_effective_transitions": max(transitions, default=0),
            "total_effective_transitions": sum(transitions),
            "total_events": sum(event_counts),
            "human_intervention_count": human_interventions,
            "operator_intervention_count": operator_interventions,
            "new_input_digests": sorted(set(input_digests)),
            "sealed_policy_hashes": sorted(set(policy_hashes)),
            "archive_digests": sorted(set(archive_digests)),
            "artifact_counts": artifact_counts,
            "fault_counts": fault_counts,
            "configuration_digests": sorted(set(configuration_digests)),
            "verifier_receipts": sorted(set(verifier_receipts)),
            "final_outcome_digests": sorted(set(final_outcomes)),
            "worker_roles": dict(sorted(workers.items())),
            "runs": sorted(run_rows, key=lambda item: item["repetition"]),
        }

    def _deployment_compatibility(self) -> dict[str, Any]:
        protected = self.inputs.document("benchmark-protected-deployment")
        tiers = []
        for row in protected.get("tiers") or []:
            if not isinstance(row, Mapping):
                continue
            tier = str(row.get("tier") or "").lower()
            tiers.append("local" if tier == "device" else tier)
        providers = [
            {
                "provider_id": str(row.get("provider_id") or ""),
                "model_id": str(row.get("model_id") or ""),
                "wire_dialect": str(row.get("wire_dialect") or ""),
                "simulated": row.get("simulated"),
                "receipt_digest": digest(row),
            }
            for row in protected.get("providers") or []
            if isinstance(row, Mapping)
        ]
        return {
            "same_run_as_case": False,
            "case_runs_no_new_provider_call": self.inputs.document(
                "benchmark-source-runs"
            ).get("no_new_provider_call"),
            "evidence_role": (
                "protected live deployment compatibility; M3 case runs prove "
                "long-horizon sealed autonomy and do not claim new provider calls"
            ),
            "tiers": sorted(set(tiers)),
            "providers": providers,
            "source_path": self.inputs.relative_path(
                "benchmark-protected-deployment"
            ),
            "source_sha256": self.inputs.digests[
                "benchmark-protected-deployment"
            ],
        }
