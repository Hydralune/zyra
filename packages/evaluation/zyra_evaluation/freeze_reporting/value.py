from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from statistics import median
from typing import Any

from .canonical import digest, now, require_mapping, require_sequence
from .errors import blocker, require_no_blockers
from .inputs import FreezeInputSet


VALUE_METRICS = (
    "efficiency.cost_usd",
    "efficiency.wall_time_ms",
    "efficiency.throughput",
    "steps.effective",
    "task.success",
    "autonomy.human_interventions",
    "recovery.fault_success_rate",
)


class ApplicationValueBuilder:
    """Produces bounded application material from formal measurements.

    The builder deliberately separates observed benchmark values from adoption
    assumptions.  It never converts benchmark duration or token cost into a
    business-savings claim without an explicit workload and labor-rate input.
    """

    def __init__(self, inputs: FreezeInputSet) -> None:
        self.inputs = inputs

    def build(self) -> dict[str, Any]:
        samples = require_sequence(
            self.inputs.document("benchmark-raw-samples").get("samples"),
            "raw benchmark samples",
        )
        selected = [
            require_mapping(item, "raw benchmark sample")
            for item in samples
            if isinstance(item, Mapping)
            and item.get("metric_id") in VALUE_METRICS
            and item.get("status") == "observed"
        ]
        observations = self._aggregate(selected)
        applications = [
            self._application(
                application_id="cross-source-research",
                title="Auditable cross-source research",
                workflow_boundary=(
                    "source acquisition through citation-bound report delivery"
                ),
                observed=observations.get("cross-source-research", {}),
                beneficiaries=(
                    "research analysts, technical due-diligence teams, and "
                    "evidence-heavy public-sector review"
                ),
                human_baseline=(
                    "No human-time baseline was measured in M3; labor savings are "
                    "therefore an adoption input, not a benchmark result."
                ),
            ),
            self._application(
                application_id="software-delivery",
                title="Long-horizon software delivery",
                workflow_boundary=(
                    "requirement intake through tested artifact and recovery delivery"
                ),
                observed=observations.get("software-delivery", {}),
                beneficiaries=(
                    "engineering teams operating multi-module repositories with "
                    "fault, requirement-change, and approval constraints"
                ),
                human_baseline=(
                    "No engineer-hour control group was measured in M3; reported "
                    "runtime and provider cost cannot be called labor savings."
                ),
            ),
        ]
        material = {
            "schema": "zyra.first-stage-application-value/v1",
            "measurement_boundary": {
                "source": self.inputs.relative_path("benchmark-raw-samples"),
                "sha256": self.inputs.digests["benchmark-raw-samples"],
                "variant": "dynamic-heterogeneous-swarm",
                "statistics": "median over three formal repetitions per domain",
                "currency": "USD as recorded by the benchmark cost metric",
                "externalized_costs": [
                    "operator onboarding",
                    "organization-specific integration",
                    "long-term storage",
                    "security review",
                    "production support",
                ],
            },
            "applications": applications,
            "adoption_calculator_contract": self._calculator_contract(),
            "risk_register": self._risks(),
            "generated_at": now(),
        }
        material["verification"] = self.verify(material)
        material["material_digest"] = digest(material)
        return material

    def verify(self, material: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(material, "application value material")
        findings: list[dict[str, Any]] = []
        applications = [
            require_mapping(item, "application row")
            for item in require_sequence(
                selected.get("applications"),
                "application rows",
            )
        ]
        if len(applications) < 2:
            findings.append(
                blocker(
                    "application-domain-count-insufficient",
                    "Application material requires two distinct domains.",
                    observed=len(applications),
                )
            )
        domains = set()
        for application in applications:
            application_id = str(application.get("application_id") or "")
            if application_id in domains:
                findings.append(
                    blocker(
                        "application-domain-duplicate",
                        "Application domain is duplicated.",
                        application_id=application_id,
                    )
                )
            domains.add(application_id)
            observed = require_mapping(
                application.get("observed"),
                f"{application_id} observations",
            )
            missing = sorted(set(VALUE_METRICS) - set(observed))
            if missing:
                findings.append(
                    blocker(
                        "application-observation-missing",
                        "Application row omits a required measured metric.",
                        application_id=application_id,
                        missing=missing,
                    )
                )
            if application.get("labor_savings_claimed") is not False:
                findings.append(
                    blocker(
                        "application-unsupported-savings-claim",
                        "Labor savings cannot be claimed without a control group.",
                        application_id=application_id,
                    )
                )
            if not application.get("assumption_boundary"):
                findings.append(
                    blocker(
                        "application-assumption-boundary-missing",
                        "Application row lacks an explicit assumption boundary.",
                        application_id=application_id,
                    )
                )
        contract = require_mapping(
            selected.get("adoption_calculator_contract"),
            "adoption calculator contract",
        )
        if contract.get("result_is_projection") is not True:
            findings.append(
                blocker(
                    "application-projection-label-missing",
                    "Adoption estimates must be labelled as projections.",
                )
            )
        risks = require_sequence(selected.get("risk_register"), "risk register")
        if len(risks) < 4:
            findings.append(
                blocker(
                    "application-risk-register-incomplete",
                    "Application material requires operational and economic risks.",
                )
            )
        require_no_blockers(
            findings,
            code="application-value-invalid",
            message="Application value material contains unsupported claims.",
            phase="material",
        )
        receipt = {
            "schema": "zyra.first-stage-application-value-verification/v1",
            "valid": True,
            "application_count": len(applications),
            "domains": sorted(domains),
            "unsupported_savings_claims": 0,
            "risk_count": len(risks),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    @staticmethod
    def _aggregate(
        samples: list[Mapping[str, Any]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        values: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        units: dict[tuple[str, str], str] = {}
        for sample in samples:
            if sample.get("variant_id") != "dynamic-heterogeneous-swarm":
                continue
            domain = str(sample.get("domain") or "")
            metric = str(sample.get("metric_id") or "")
            raw_value = sample.get("value")
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                continue
            values[domain][metric].append(float(raw_value))
            units[(domain, metric)] = str(sample.get("unit") or "")
        return {
            domain: {
                metric: {
                    "median": median(observations),
                    "minimum": min(observations),
                    "maximum": max(observations),
                    "sample_count": len(observations),
                    "unit": units.get((domain, metric), ""),
                }
                for metric, observations in sorted(metrics.items())
                if observations
            }
            for domain, metrics in sorted(values.items())
        }

    @staticmethod
    def _application(
        *,
        application_id: str,
        title: str,
        workflow_boundary: str,
        observed: Mapping[str, Any],
        beneficiaries: str,
        human_baseline: str,
    ) -> dict[str, Any]:
        return {
            "application_id": application_id,
            "title": title,
            "workflow_boundary": workflow_boundary,
            "beneficiaries": beneficiaries,
            "observed": dict(observed),
            "labor_savings_claimed": False,
            "assumption_boundary": human_baseline,
            "measured_value": (
                "zero-human sealed delivery, deterministic verification, fault "
                "recovery, and checksum-linked artifact production"
            ),
            "unmeasured_value": [
                "organization-specific human time displaced",
                "downstream revenue impact",
                "quality cost avoided",
                "production-scale provider discounts",
            ],
        }

    @staticmethod
    def _calculator_contract() -> dict[str, Any]:
        return {
            "result_is_projection": True,
            "required_inputs": {
                "monthly_task_count": "integer >= 0",
                "human_hours_per_task": "number >= 0, supplied by adopter",
                "loaded_hourly_cost_usd": "number >= 0, supplied by adopter",
                "automation_acceptance_rate": "number in [0,1], measured in pilot",
                "production_runtime_cost_usd": "number >= 0, measured in pilot",
                "integration_amortization_usd": "number >= 0",
            },
            "formula": (
                "projected_net_value = tasks * human_hours * hourly_cost * "
                "acceptance_rate - tasks * runtime_cost - integration_amortization"
            ),
            "forbidden_inference": (
                "M3 benchmark wall time or cost may not substitute for adopter "
                "human baselines, acceptance rate, or production provider pricing."
            ),
        }

    @staticmethod
    def _risks() -> list[dict[str, str]]:
        return [
            {
                "risk_id": "domain-transfer",
                "category": "quality",
                "description": "Two formal domains do not prove every domain.",
                "mitigation": "Run sealed pilot cells with domain verifiers.",
            },
            {
                "risk_id": "provider-pricing",
                "category": "economic",
                "description": "Provider price and quota terms can change.",
                "mitigation": "Recalculate from current protected provider receipts.",
            },
            {
                "risk_id": "integration-cost",
                "category": "economic",
                "description": "Organization-specific integration was not measured.",
                "mitigation": "Track and amortize integration cost explicitly.",
            },
            {
                "risk_id": "credential-boundary",
                "category": "security",
                "description": "Missing or invalid credentials block cloud work.",
                "mitigation": "Fail closed and replan to an admitted placement.",
            },
            {
                "risk_id": "operator-trust",
                "category": "adoption",
                "description": "Auditable evidence does not guarantee user trust.",
                "mitigation": "Expose causal trace, artifacts, and approval history.",
            },
        ]
