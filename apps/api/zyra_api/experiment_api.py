from __future__ import annotations

import threading

from zyra_evaluation.experiment_runtime import (
    ExperimentApi,
    ExperimentMatrixRuntime,
    ExperimentStore,
)


_LOCK = threading.RLock()
_API: ExperimentApi | None = None
_KEY: tuple[str, str, str] | None = None


def get_experiment_api() -> ExperimentApi:
    from . import main as api_main

    global _API, _KEY
    state_path = api_main.sqlite_path().with_name(
        f"{api_main.sqlite_path().stem}.experiments.sqlite3"
    ).resolve()
    artifact_root = api_main.artifact_root_path().resolve()
    project_root = api_main.PROJECT_ROOT.resolve()
    key = (str(state_path), str(artifact_root), str(project_root))
    with _LOCK:
        if _API is not None and _KEY == key:
            return _API
        if _API is not None:
            _API.runtime.close(wait=False)
        runtime = ExperimentMatrixRuntime(
            project_root=project_root,
            store=ExperimentStore(state_path),
            artifact_root=artifact_root,
            allowed_source_roots=(
                project_root,
                artifact_root,
            ),
            maximum_workers=2,
            enable_ablation_verifier=True,
            enable_metric_aggregator=True,
            enable_evidence_verifier=True,
            auto_reconcile=True,
        )
        _API = ExperimentApi(runtime)
        _KEY = key
        return _API


def reset_experiment_api(*, wait: bool = False) -> None:
    global _API, _KEY
    with _LOCK:
        selected = _API
        _API = None
        _KEY = None
    if selected is not None:
        selected.runtime.close(wait=wait)
