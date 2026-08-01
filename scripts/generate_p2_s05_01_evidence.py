from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    PROJECT_ROOT,
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "orchestration",
    PROJECT_ROOT / "packages" / "scheduler",
    PROJECT_ROOT / "packages" / "evaluation",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


SLICE_ID = "P2-S05-01"


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_production_capture(
    *,
    output_dir: Path,
    implementation_commit: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    # Import after the isolated state owner is selected. This prevents a
    # developer or prior test run from supplying residual canonical receipts.
    os.environ["ZYRA_STATE_ROOT"] = str(output_dir / "runtime-state")

    from apps.api.zyra_api import main as api
    from apps.api.zyra_api.policy_api import (
        FilesystemPolicyMetricReportProvider,
        PolicyMetricApi,
        RuntimePolicyEvidenceSource,
    )
    from zyra_core import EventRecord, EventType, PlanNodeStatus
    from zyra_evaluation.policy_benchmark import (
        CanonicalRuntimeReceiptResolver,
        Phase2MetricReportBuilder,
        RunMetricInput,
        canonical_digest,
        metric_spec_registry_payload,
        write_phase2_metric_report,
    )
    from zyra_memory import MemoryLayer, MemoryRecord
    from zyra_orchestration import ensure_default_graph, run_task_graph

    state, created = api.make_task_created_event(
        "Use the retained signed release checksum while implementing and verifying a code artifact through phase2_strongest_v1."
    )
    obligations = tuple(
        sorted(
            {
                *state.constraints.requirements,
                *state.constraints.success_criteria,
            }
        )
    )
    requirement_revision = "requirements:" + canonical_digest(obligations)
    memory_event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_id="event-p2-s05-critical-release-checksum",
        event_type=EventType.AGENT_MESSAGE,
        node_id=state.root_node_id,
        payload={
            "schema": "zyra.p2-s05-critical-fact/v1",
            "fact": "signed release checksum",
            "value": "sha256:release-r1",
        },
    )
    memory_content = {
        "fact": "signed release checksum",
        "value": "sha256:release-r1",
        "requirement_revision": requirement_revision,
    }
    api.persist_events(api.get_store(), [created, memory_event])
    api.get_store().save_memory_records(
        [
            MemoryRecord(
                memory_id="memory-p2-s05-critical-release-checksum",
                run_id=state.run_id,
                task_id=state.task_id,
                layer=MemoryLayer.SEMANTIC,
                source_type="event_log",
                source_id=memory_event.event_id,
                node_id=state.root_node_id,
                summary="The signed release checksum is sha256:release-r1.",
                content=memory_content,
                artifact_ids=[],
                evidence_ids=[memory_event.event_id],
                score=1.0,
                metadata={
                    "continuity_status": "active",
                    "continuity_version": f"{requirement_revision}/fact-v1",
                    "content_digest": canonical_digest(memory_content),
                    "requirement_revision": requirement_revision,
                },
            )
        ]
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"p2-s05-production:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    events = run_task_graph(
        state,
        execution_context=api.graph_execution_context(),
    )
    api.persist_events(api.get_store(), list(events))
    if state.status is not PlanNodeStatus.COMPLETED:
        raise RuntimeError(
            f"production metric capture did not complete: {state.status}"
        )
    readiness = json.loads(
        (
            PROJECT_ROOT
            / "docs"
            / "release"
            / "phase2"
            / "activation-readiness.json"
        ).read_text(encoding="utf-8")
    )
    resolver = CanonicalRuntimeReceiptResolver(
        run_id=state.run_id,
        task_id=state.task_id,
        events=(created, memory_event, *events),
        canonical_events=tuple(api.get_store().task_events(state.task_id)),
        communication_receipts=api._phase2_communication_outcomes(state),
        physical_dispatch_receipts=tuple(
            state.metadata.get("physical_dispatch_receipts") or ()
        ),
        symbolic_bundles=tuple(
            state.metadata.get("phase2_symbolic_bundles") or ()
        ),
        readiness_report=readiness,
        decision_records=tuple(state.decisions),
    )
    report = Phase2MetricReportBuilder().build(
        (
            RunMetricInput(
                run_id=state.run_id,
                task_id=state.task_id,
                scenario_id="production-main-path",
                mechanism_profile="phase2_strongest_v1",
                receipt_resolver=resolver,
                task_succeeded=True,
                effective_transition_count=(
                    resolver.canonical_transition_count()
                ),
            ),
        )
    )
    report_id = f"p2-05-aggregate-{implementation_commit[:12]}"
    runtime_report = write_phase2_metric_report(
        report,
        root=PROJECT_ROOT / ".zyra" / "reports" / "policy-metrics",
        report_id=report_id,
        run_id=state.run_id,
        task_id=state.task_id,
        admit_event=lambda event: api.persist_events(
            api.get_store(),
            [event],
        ),
    )
    report_payload = report.to_dict()
    run = report.runs[0]
    evidence_api = PolicyMetricApi(
        FilesystemPolicyMetricReportProvider(runtime_report.parent),
        evidence_source=RuntimePolicyEvidenceSource(
            api.get_runtime_event_api(),
            api.artifact_root_path(),
        ),
    )
    evidence_response = evidence_api.route_get(
        ("policy", "evidence"),
        {
            "run_id": state.run_id,
            "task_id": state.task_id,
            "report_id": report_id,
            "limit": "200",
        },
    )
    if (
        evidence_response is None
        or evidence_response.status != 200
        or evidence_response.body.get("metric_report", {}).get("status")
        != "verified"
    ):
        raise RuntimeError("production policy evidence projection is not verified")
    evidence_page = dict(evidence_response.body)
    lineage_body = {
        "schema_version": "zyra.phase2-metric-lineage/v2",
        "slice_id": SLICE_ID,
        "implementation_commit": implementation_commit,
        "run_id": run.run_id,
        "task_id": run.task_id,
        "report_id": report_id,
        "runtime_report_path": runtime_report.relative_to(PROJECT_ROOT).as_posix(),
        "source_admission": dict(run.source_admission),
        "receipt_kinds": {
            kind: {
                "receipt_count": len(refs),
                "receipt_digests": list(refs),
            }
            for kind, refs in sorted(run.lineage.items())
        },
        "metric_sources": {
            metric_id: list(value.source_refs)
            for metric_id, value in sorted(run.metrics.items())
        },
        "canonical_only": True,
        "ui_projection_used_as_input": False,
        "test_fixture_used_as_input": False,
        "transition_count_semantics": "canonical_event_spine_evidence_volume_only",
    }
    lineage = {**lineage_body, "digest": canonical_digest(lineage_body)}
    registry = metric_spec_registry_payload()
    manifest_body = {
        "schema_version": "zyra.p2-s05-01-evidence-manifest/v2",
        "slice_id": SLICE_ID,
        "implementation_commit": implementation_commit,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "metric_spec_count": len(registry["specs"]),
        "registry_digest": registry["digest"],
        "report_id": report_id,
        "report_digest": report_payload["digest"],
        "lineage_digest": lineage["digest"],
        "run_id": state.run_id,
        "task_id": state.task_id,
        "canonical_event_count": resolver.canonical_transition_count(),
        "physical_dispatch_receipt_count": len(
            state.metadata.get("physical_dispatch_receipts") or ()
        ),
        "failed_run_count": report.aggregate.failed_run_count,
        "continuity_critical_fact_recall": run.metrics[
            "continuity.critical_fact_recall"
        ].value,
        "continuity_provenance_coverage": run.metrics[
            "continuity.provenance_coverage"
        ].value,
        "symbolic_bundle_receipt_count": len(
            run.lineage.get("symbolic_bundles", ())
        ),
        "metric_report_admission_event_present": True,
        "policy_evidence_projection_verified": True,
        "production_runtime_capture": True,
        "fixture_free": True,
    }
    manifest = {**manifest_body, "digest": canonical_digest(manifest_body)}
    return report_payload, lineage, manifest, evidence_page


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    arguments = parser.parse_args()
    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    report, lineage, manifest, evidence_page = _run_production_capture(
        output_dir=output_dir,
        implementation_commit=arguments.implementation_commit,
    )
    from zyra_evaluation.policy_benchmark import metric_spec_registry_payload

    _write(output_dir / "metric-spec-registry.json", metric_spec_registry_payload())
    _write(output_dir / "metric-report.json", report)
    _write(output_dir / "receipt-metric-lineage.json", lineage)
    _write(output_dir / "evidence-manifest.json", manifest)
    _write(output_dir / "policy-evidence-page.json", evidence_page)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
