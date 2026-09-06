from copy import deepcopy

import pytest

from zyra_core import PlanNodeStatus, create_task_state, to_jsonable
from zyra_memory import SQLiteStore
from zyra_runtime import LocalArtifactStore
from zyra_runtime.artifacts import ArtifactIntegrityError


def test_worker_artifacts_require_exact_task_custody_and_verified_bytes(tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    reference = artifacts.write_text(run_id="run-a", task_id="task-a", content="verified", title="Deliverable")
    raw = to_jsonable(reference)
    assert artifacts.admit_refs([raw, raw], run_id="run-a", task_id="task-a") == [reference]
    with pytest.raises(ValueError, match="outside the active task"):
        artifacts.admit_refs([raw], run_id="run-a", task_id="task-b")
    missing_digest = deepcopy(raw)
    missing_digest["metadata"].pop("sha256")
    with pytest.raises(ValueError, match="integrity"):
        artifacts.admit_refs([missing_digest], run_id="run-a", task_id="task-a")
    artifacts.resolve_path(reference).write_text("tampered")
    with pytest.raises(ArtifactIntegrityError):
        artifacts.admit_refs([raw], run_id="run-a", task_id="task-a")


def test_graph_progress_keeps_live_artifacts_rename_and_cancellation(tmp_path, monkeypatch):
    from apps.api.zyra_api import main as api
    store = SQLiteStore(tmp_path / "state.sqlite3")
    monkeypatch.setattr(api, "get_store", lambda: store)
    executing = create_task_state("Observe a running CLI task")
    executing.status = PlanNodeStatus.RUNNING
    store.save_checkpoint(executing)
    canonical = store.load_task(executing.task_id)
    canonical.metadata.update(session_title="New title", session_title_revision=1)
    reference = LocalArtifactStore(tmp_path / "artifacts").write_text(
        run_id=executing.run_id, task_id=executing.task_id, content="live", title="Live delivery",
    )
    canonical.artifacts.append(reference)
    store.save_checkpoint(canonical)
    executing.status = PlanNodeStatus.PENDING
    api._project_task_graph_execution_state(executing, [], "stage_progress")
    observed = store.load_task(executing.task_id)
    assert observed.status == PlanNodeStatus.RUNNING
    assert observed.metadata["session_title"] == "New title"
    assert observed.artifacts == [reference]
    observed.status = PlanNodeStatus.CANCELLED
    store.save_checkpoint(observed)
    api._project_task_graph_execution_state(executing, [], "stage_progress")
    assert store.load_task(executing.task_id).status == PlanNodeStatus.CANCELLED
