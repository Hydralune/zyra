from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from zyra_integrations.loopx.bridge import (
    LoopXDispatcher,
    LoopXOutbox,
    LoopXRuntimeStateAdapter,
    LoopXSingleWriter,
)
from zyra_integrations.loopx.install import InstallProfile, LoopXInstaller
from zyra_orchestration.graph_custody import GraphStateCustody, GraphStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def install_loopx(workspace: Path) -> dict[str, Any]:
    return LoopXInstaller(PROJECT_ROOT).install(
        workspace,
        profile=InstallProfile.WINDOWS_RELEASE,
        python_executable=Path(sys.executable),
    )


def committed_graph_mutation(
    root: Path,
    *,
    graph_id: str = "graph-loopx-bridge",
    run_id: str = "run-loopx-bridge",
    metadata_key: str = "requirement_revision",
    metadata_value: str = "r1",
):
    store = GraphStateStore(root / "graph.sqlite3")
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(graph_id_value=graph_id, run_id=run_id)
    builder = custody.branch(
        graph_id,
        branch_id=f"branch-{metadata_value}",
        actor_id="loopx-bridge-test",
        causation_id=f"cause-{metadata_value}",
        correlation_id=run_id,
        idempotency_key=f"graph-{metadata_value}",
    )
    builder.set_metadata(metadata_key, metadata_value)
    return custody, custody.commit(builder.build())


def valid_update(
    *,
    goal_id: str = "goal-bridge",
    todo_id: str = "todo_bridge_primary",
    claimant: str = "loopx-controller",
    limit_slots: int = 3,
    spend_slots: int = 1,
    gates_passed: bool = True,
) -> dict[str, Any]:
    return {
        "goal_id": goal_id,
        "objective_ref": "zyra://run/run-loopx-bridge/objective/r1",
        "requirement_revision": "r1",
        "todos": [
            {
                "todo_id": todo_id,
                "title": "Continue the verified bridge work item",
                "role": "agent",
                "priority": "P1",
                "action_kind": "advance",
            }
        ],
        "claims": [{"todo_id": todo_id, "claimant": claimant}],
        "quota": {
            "limit_slots": limit_slots,
            "requested_spend_slots": spend_slots,
            "window_hours": 24,
        },
        "validation": {
            "validation_passed": gates_passed,
            "permission_allowed": gates_passed,
            "lease_valid": gates_passed,
            "budget_allowed": gates_passed,
            "validation_receipt_id": "validation-1",
            "permission_receipt_id": "permission-1",
            "lease_receipt_id": "lease-1",
            "budget_receipt_id": "budget-1",
        },
        "history": [
            {
                "source_event_id": "zyra-event-verified-1",
                "summary": "Canonical graph mutation verified",
                "verified": True,
                "evidence_refs": ["artifact:graph-receipt-1"],
            }
        ],
        "interaction": {
            "input_ref": "event:continuation-input-1",
            "feedback_ref": "event:verification-feedback-1",
            "continuation_hint": "Continue the next bounded verified todo",
        },
    }


def bridge_runtime(
    workspace: Path,
    install_receipt: dict[str, Any],
    *,
    observability=None,
    max_attempts: int = 3,
) -> tuple[LoopXOutbox, LoopXSingleWriter, LoopXRuntimeStateAdapter, LoopXDispatcher]:
    outbox = LoopXOutbox(workspace_root=workspace)
    writer = LoopXSingleWriter(workspace_root=workspace)
    runtime = LoopXRuntimeStateAdapter(
        workspace_root=workspace,
        install_receipt=install_receipt,
    )
    dispatcher = LoopXDispatcher(
        outbox=outbox,
        single_writer=writer,
        runtime=runtime,
        observability=observability,
        max_attempts=max_attempts,
    )
    return outbox, writer, runtime, dispatcher
