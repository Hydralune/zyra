from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime import (
    ClaudeProductizationFoundation,
    FoundationExecutionRequest,
    FoundationProbeResult,
    WorkerRequest,
    WorkerResult,
    assert_foundation_ready,
    foundation_event_payload,
)


@dataclass(frozen=True, slots=True)
class ClaudeFoundationWorkerRun:
    worker_result: WorkerResult
    event_records: list[EventRecord]
    probe_result: FoundationProbeResult


class ClaudeProductizationFoundationWorker:
    def __init__(
        self,
        *,
        project_root: str | Path,
        artifact_root: str | Path | None = None,
        source_workspace_root: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else self.project_root / "tmp" / "artifacts"
        self.runtime = ClaudeProductizationFoundation(
            self.project_root,
            source_workspace_root=source_workspace_root,
        )

    def run(self, request: WorkerRequest | None = None) -> ClaudeFoundationWorkerRun:
        worker_request = request or WorkerRequest(
            run_id="m1-02a-foundation",
            task_id="claude-source-productization-foundation",
            worker_name="CodeWorkerRuntime",
            request_id="m1-02a-foundation-worker",
            node_id="code-worker",
            constraints={},
        )
        foundation_request = _foundation_request_from_worker(worker_request)
        probe = self.runtime.inspect(foundation_request)
        artifacts = [_write_probe_artifact(self.artifact_root, worker_request, probe)]
        metadata = _worker_metadata(probe)
        worker_result = WorkerResult(
            request_id=worker_request.request_id,
            ok=probe.ok,
            summary=(
                "Claude productization foundation is connected to Zyra-owned runtime surfaces."
                if probe.ok
                else "Claude productization foundation has blocking source-to-target findings."
            ),
            artifacts=artifacts,
            events=[to_jsonable(event) for event in probe.event_records],
            error=None if probe.ok else "claude_productization_foundation_failed",
            metadata=metadata,
        )
        event_records = [
            *probe.event_records,
            EventRecord(
                run_id=worker_request.run_id,
                task_id=worker_request.task_id,
                node_id=worker_request.node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "worker_request": to_jsonable(worker_request),
                    "worker_result": to_jsonable(worker_result),
                    "foundation": foundation_event_payload(probe),
                },
            ),
        ]
        return ClaudeFoundationWorkerRun(
            worker_result=worker_result,
            event_records=event_records,
            probe_result=probe,
        )

    def assert_ready(self) -> FoundationProbeResult:
        result = self.runtime.inspect()
        assert_foundation_ready(result)
        return result


def _foundation_request_from_worker(request: WorkerRequest) -> FoundationExecutionRequest:
    return FoundationExecutionRequest(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id or "code-worker",
        include_reference_sources=request.constraints.get("include_reference_sources") is not False,
        require_clean_source_pool=request.constraints.get("require_clean_source_pool") is not False,
        require_primary_source_files=request.constraints.get("require_primary_source_files") is not False,
        allow_source_pool_fallback=request.constraints.get("allow_source_pool_fallback") is True,
        require_worker_runtime=request.constraints.get("require_worker_runtime") is not False,
        extra_constraints=dict(request.constraints),
    )


def _write_probe_artifact(artifact_root: Path, request: WorkerRequest, probe: FoundationProbeResult) -> Any:
    from zyra_runtime import LocalArtifactStore

    store = LocalArtifactStore(artifact_root)
    return store.write_text(
        run_id=request.run_id,
        task_id=request.task_id,
        title="Claude productization foundation probe",
        content=_probe_markdown(probe),
        kind=ArtifactKind.STRUCTURED_DATA,
        extension=".md",
        producer_node_id=request.node_id,
    )


def _probe_markdown(probe: FoundationProbeResult) -> str:
    lines = [
        "# Claude Productization Foundation Probe",
        "",
        f"- owner_unit: `{probe.owner_unit}`",
        f"- ok: `{str(probe.ok).lower()}`",
        f"- source_root: `{probe.source_root}`",
        f"- source_pool_root: `{probe.source_pool_root}`",
        f"- blocking_count: `{probe.blocking_count}`",
        f"- warning_count: `{probe.warning_count}`",
        "",
        "## Coverage",
        "",
    ]
    for key, value in probe.coverage.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Boundaries", ""])
    for boundary in probe.boundaries:
        lines.append(f"### {boundary.spec.boundary_id}")
        lines.append("")
        lines.append(f"- decision: `{boundary.decision}`")
        lines.append(f"- ok: `{str(boundary.ok).lower()}`")
        lines.append(f"- primary_source_count: `{boundary.primary_source_count}`")
        lines.append(f"- zyra_target_count: `{boundary.zyra_target_count}`")
        if boundary.findings:
            lines.append("- findings:")
            for finding in boundary.findings:
                lines.append(f"  - `{finding.severity}` `{finding.code}` {finding.message}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _worker_metadata(probe: FoundationProbeResult) -> dict[str, str]:
    coverage = probe.coverage
    return {
        "claude_foundation_ok": str(probe.ok).lower(),
        "claude_foundation_owner_unit": probe.owner_unit,
        "claude_foundation_boundary_count": str(coverage.get("boundary_count", 0)),
        "claude_foundation_primary_source_count": str(coverage.get("primary_source_count", 0)),
        "claude_foundation_zyra_target_count": str(coverage.get("zyra_target_count", 0)),
        "claude_foundation_source_pool_target_count": str(coverage.get("source_pool_target_count", 0)),
        "claude_foundation_source_pool_only_primary_sources": str(coverage.get("source_pool_only_primary_sources", 0)),
        "claude_foundation_blocking_count": str(probe.blocking_count),
        "claude_foundation_decisions": ",".join(f"{key}={value}" for key, value in sorted(probe.decision_counts.items())),
    }
