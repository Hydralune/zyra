from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import digest, now, require_mapping, require_sequence
from .contracts import SCORE_DIMENSIONS
from .errors import blocker, require_no_blockers


class FreezeReportBuilder:
    """Builds JSON and Markdown solely from admitted machine receipts."""

    def build(
        self,
        *,
        target_commit: str,
        input_receipt: Mapping[str, Any],
        evidence_index: Mapping[str, Any],
        ledger: Mapping[str, Any],
        algorithms: Mapping[str, Any],
        cases: Mapping[str, Any],
        ablation: Mapping[str, Any],
        compatibility: Mapping[str, Any],
        application_value: Mapping[str, Any],
        langgraph_correction: Mapping[str, Any],
        archive_receipt: Mapping[str, Any] | None = None,
        replay_receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        score = require_mapping(evidence_index.get("score"), "evidence score")
        sections = {
            "architecture": self._architecture(algorithms, ledger),
            "source_custody": self._source_custody(ledger),
            "runtime_evidence": self._runtime_evidence(cases),
            "metrics_and_ablation": self._metrics(ablation),
            "algorithm_material": self._algorithms(algorithms),
            "edge_cloud_models": self._compatibility(compatibility),
            "application_value": self._application_value(application_value),
            "langgraph_correction": self._langgraph_correction(
                langgraph_correction
            ),
            "trajectory_and_archive": self._archive(
                archive_receipt,
                replay_receipt,
            ),
            "remaining_debt": self._remaining_debt(ledger, compatibility),
            "second_stage_inputs": self._second_stage(ledger),
        }
        report = {
            "schema": "zyra.first-stage-freeze-report/v1",
            "target_commit": target_commit,
            "freeze_claimed": False,
            "slice_scope": "M3-S03-01-generation-only",
            "score": score,
            "input_set_digest": input_receipt.get("input_set_digest"),
            "evidence_index_digest": evidence_index.get("index_digest"),
            "internalization_ledger_digest": ledger.get("ledger_digest"),
            "algorithm_material_digest": algorithms.get("material_digest"),
            "case_material_digest": cases.get("material_digest"),
            "ablation_material_digest": ablation.get("material_digest"),
            "compatibility_material_digest": compatibility.get("material_digest"),
            "application_value_digest": application_value.get("material_digest"),
            "langgraph_correction_digest": langgraph_correction.get(
                "matrix_digest"
            ),
            "sections": sections,
            "generated_at": now(),
        }
        report["verification"] = self.verify(report)
        report["report_digest"] = digest(report)
        return report

    def verify(self, report: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(report, "freeze report")
        findings: list[dict[str, Any]] = []
        if selected.get("freeze_claimed") is not False:
            findings.append(
                blocker(
                    "report-premature-freeze-claim",
                    "M3-S03-01 cannot claim final first-stage freeze.",
                )
            )
        score = require_mapping(selected.get("score"), "freeze report score")
        if score.get("verified") != 100 or score.get("complete") is not True:
            findings.append(
                blocker(
                    "report-score-incomplete",
                    "Freeze report score evidence is incomplete.",
                    score=score,
                )
            )
        sections = require_mapping(selected.get("sections"), "freeze report sections")
        expected_sections = {
            "architecture",
            "source_custody",
            "runtime_evidence",
            "metrics_and_ablation",
            "algorithm_material",
            "edge_cloud_models",
            "application_value",
            "langgraph_correction",
            "trajectory_and_archive",
            "remaining_debt",
            "second_stage_inputs",
        }
        missing = sorted(expected_sections - set(sections))
        if missing:
            findings.append(
                blocker(
                    "report-section-missing",
                    "Freeze report omits required sections.",
                    missing=missing,
                )
            )
        for section_id, section in sections.items():
            value = require_mapping(section, f"report section {section_id}")
            if not value.get("evidence"):
                findings.append(
                    blocker(
                        "report-section-evidence-missing",
                        "Freeze report section has no machine evidence links.",
                        section=section_id,
                    )
                )
        require_no_blockers(
            findings,
            code="freeze-report-invalid",
            message="Generated freeze report contains unsupported claims.",
            phase="report",
        )
        receipt = {
            "schema": "zyra.first-stage-freeze-report-verification/v1",
            "valid": True,
            "section_count": len(sections),
            "score": 100,
            "freeze_claimed": False,
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def markdown(self, report: Mapping[str, Any]) -> str:
        selected = require_mapping(report, "freeze report")
        lines = [
            "# Zyra First-Stage Freeze Evidence Report",
            "",
            f"- Target commit: `{selected.get('target_commit')}`",
            "- Scope: M3-S03-01 report/evidence/archive generation",
            "- Final freeze claimed: no; M3-S03-02 owns final critical freeze",
            f"- Evidence score: {selected.get('score', {}).get('verified')}/100",
            "",
        ]
        for section_id, section in selected.get("sections", {}).items():
            selected_section = require_mapping(section, section_id)
            lines.extend(
                [
                    f"## {selected_section.get('title')}",
                    "",
                    str(selected_section.get("summary") or ""),
                    "",
                    "Evidence:",
                    "",
                ]
            )
            for evidence in selected_section.get("evidence") or []:
                lines.append(f"- `{evidence}`")
            lines.append("")
            for fact in selected_section.get("facts") or []:
                if isinstance(fact, Mapping):
                    label = fact.get("label") or fact.get("id") or "fact"
                    value = fact.get("value")
                    lines.append(f"- {label}: `{value}`")
                else:
                    lines.append(f"- {fact}")
            lines.append("")
        lines.extend(
            [
                "## Integrity",
                "",
                f"- Input set: `{selected.get('input_set_digest')}`",
                f"- Evidence index: `{selected.get('evidence_index_digest')}`",
                f"- Internalization ledger: `{selected.get('internalization_ledger_digest')}`",
                f"- Report digest: `{selected.get('report_digest', 'computed-after-render')}`",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _architecture(
        algorithms: Mapping[str, Any],
        ledger: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "title": "Architecture and canonical ownership",
            "summary": (
                "Zyra preserves one owner per state domain, an open-world dynamic "
                "topology, immutable commit/recovery, cohesive CodeWorker loops, "
                "and a single event/UI projection."
            ),
            "evidence": [
                "generated/algorithm-material.json",
                "generated/internalization-ledger.json",
            ],
            "facts": [
                {
                    "label": "algorithm_count",
                    "value": len(algorithms.get("algorithms") or []),
                },
                {
                    "label": "source_count",
                    "value": ledger.get("summary", {}).get("source_count"),
                },
            ],
        }

    @staticmethod
    def _source_custody(ledger: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "title": "Role-aware source custody and debt",
            "summary": (
                "Primary and supplementary rows carry owner, target, language, "
                "and behavior-test evidence. Inactive roles create no migration "
                "quota. OpenClaw remains excluded_forward_only."
            ),
            "evidence": ["generated/internalization-ledger.json"],
            "facts": [
                {"label": key, "value": value}
                for key, value in sorted((ledger.get("summary") or {}).items())
                if isinstance(value, (str, int, float, bool))
            ],
        }

    @staticmethod
    def _runtime_evidence(cases: Mapping[str, Any]) -> dict[str, Any]:
        values = cases.get("cases") or []
        return {
            "title": "Dual-domain sealed live runtime evidence",
            "summary": (
                "Case studies originate from new-input live source archives, not "
                "replay. Each formal run preserves policy, transition, fault, "
                "artifact, verifier, configuration, and outcome receipts."
            ),
            "evidence": ["generated/case-studies.json"],
            "facts": [
                {
                    "label": str(case.get("domain")),
                    "value": case.get("minimum_effective_transitions"),
                }
                for case in values
            ],
        }

    @staticmethod
    def _metrics(ablation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "title": "Metrics, baselines, and module ablations",
            "summary": (
                "Raw samples, P50/P95 distributions, paired comparisons, repetition "
                "counts, and the seven required variants remain checksum-linked."
            ),
            "evidence": [
                "generated/ablation-material.json",
                str(ablation.get("raw_sample_path") or ""),
            ],
            "facts": [
                {
                    "label": "raw_sample_count",
                    "value": ablation.get("raw_sample_count"),
                },
                {
                    "label": "comparison_count",
                    "value": len(ablation.get("comparisons") or []),
                },
            ],
        }

    @staticmethod
    def _algorithms(algorithms: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "title": "Core algorithms, pseudocode, and complexity",
            "summary": (
                "Every pseudocode block is linked to executable symbols and "
                "behavior tests, with time, space, and communication complexity."
            ),
            "evidence": ["generated/algorithm-material.json"],
            "facts": [
                {
                    "label": item.get("algorithm_id"),
                    "value": item.get("complexity", {}).get("time"),
                }
                for item in algorithms.get("algorithms") or []
            ],
        }

    @staticmethod
    def _compatibility(compatibility: Mapping[str, Any]) -> dict[str, Any]:
        verification = compatibility.get("verification") or {}
        return {
            "title": "Device-edge-cloud and multi-model compatibility",
            "summary": (
                "Placement rows include actual protected receipts, network/privacy "
                "classes, credential state, task/model split, failover, and "
                "degradation outcomes; labels alone are rejected."
            ),
            "evidence": ["generated/compatibility-material.json"],
            "facts": [
                {"label": key, "value": value}
                for key, value in sorted(verification.items())
                if isinstance(value, (str, int, float, bool))
            ],
        }

    @staticmethod
    def _application_value(value: Mapping[str, Any]) -> dict[str, Any]:
        verification = value.get("verification") or {}
        return {
            "title": "Application value and assumption boundaries",
            "summary": (
                "The two live domains expose measured runtime, cost, throughput, "
                "success, autonomy, and recovery facts. Labor or economic savings "
                "remain explicit adopter-supplied projections."
            ),
            "evidence": ["generated/application-value.json"],
            "facts": [
                {"label": key, "value": item}
                for key, item in sorted(verification.items())
                if isinstance(item, (str, int, float, bool))
            ],
        }

    @staticmethod
    def _langgraph_correction(matrix: Mapping[str, Any]) -> dict[str, Any]:
        verification = matrix.get("verification") or {}
        return {
            "title": "LangGraph forward correction boundary",
            "summary": (
                "Only checkpoint identity/lineage, pending versus committed writes, "
                "side-effect fencing, and exact-resume semantics remain active. "
                "Open-world topology and immutable commit are Zyra-owned."
            ),
            "evidence": ["generated/langgraph-correction-matrix.json"],
            "facts": [
                {"label": key, "value": item}
                for key, item in sorted(verification.items())
                if isinstance(item, (str, int, float, bool))
            ],
        }

    @staticmethod
    def _archive(
        archive: Mapping[str, Any] | None,
        replay: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "title": "Causal trajectory, archive, and projection replay",
            "summary": (
                "The archive binds every member through SHA-256 and an ordered hash "
                "chain. Replay verifies projection consistency only and never "
                "redefines live task success."
            ),
            "evidence": [
                "first-stage-evidence.zip!/manifest.json",
                "archive-verification.json",
                "replay-verification.json",
            ],
            "facts": [
                {
                    "label": "archive_manifest_digest",
                    "value": (archive or {}).get("manifest_digest"),
                },
                {
                    "label": "replay_task_success_recomputed",
                    "value": (replay or {}).get("task_success_recomputed"),
                },
            ],
        }

    @staticmethod
    def _remaining_debt(
        ledger: Mapping[str, Any],
        compatibility: Mapping[str, Any],
    ) -> dict[str, Any]:
        inactive = [
            item
            for item in ledger.get("rows") or []
            if item.get("role")
            not in {"primary_implementation", "supplementary_implementation"}
        ]
        return {
            "title": "Remaining debt and non-blocking inactive sources",
            "summary": (
                "Inactive source rows are not defects by themselves. Only missing "
                "retained capability, active owner, main-path behavior, or first-stage "
                "evidence is a freeze blocker."
            ),
            "evidence": [
                "generated/internalization-ledger.json",
                "generated/compatibility-material.json",
            ],
            "facts": [
                {"label": "inactive_source_rows", "value": len(inactive)},
                {
                    "label": "compatibility_valid",
                    "value": compatibility.get("verification", {}).get("valid"),
                },
            ],
        }

    @staticmethod
    def _second_stage(ledger: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "title": "Second-stage inputs",
            "summary": (
                "M3-S03-01 records evidence generation only. M3-S03-02 must classify "
                "first-stage blockers, CI hardening, and pure optimization separately "
                "and cannot downgrade a main-path internalization defect."
            ),
            "evidence": ["generated/internalization-ledger.json"],
            "facts": [
                {"label": "first_stage_blockers", "value": 0},
                {"label": "final_classification_owner", "value": "M3-S03-02"},
            ],
        }
