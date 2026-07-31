from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest


_API_MODULE = "apps.api.zyra_api.main"
_API_RESETTERS = (
    "reset_api_product_bootstrap",
    "reset_browser_runtime",
    "reset_worker_pool_api",
    "reset_runtime_event_spine_bridge",
    "reset_fault_runtime_api",
    "reset_recovery_runtime_api",
    "reset_workspace_manager",
    "reset_code_index_service",
    "reset_diff_review_api",
    "reset_terminal_api",
    "reset_mcp_runtime",
    "reset_control_runtime",
    "reset_subagent_runtime",
    "reset_memory_curator_runtime",
    "reset_runtime_owner_composition",
)


def _reset_loaded_api_runtime(module: ModuleType) -> None:
    failures: list[Exception] = []
    for name in _API_RESETTERS:
        reset = getattr(module, name, None)
        if not callable(reset):
            continue
        try:
            reset()
        except Exception as error:  # noqa: BLE001 - preserve every cleanup failure.
            failures.append(error)
    if failures:
        raise ExceptionGroup("API runtime cleanup failed", failures)


@pytest.fixture(autouse=True)
def isolate_process_environment_and_api_runtime() -> Iterator[None]:
    """Keep environment-backed API owners from leaking across test cases."""

    environment_before = dict(os.environ)
    try:
        yield
    finally:
        try:
            module = sys.modules.get(_API_MODULE)
            if module is not None:
                _reset_loaded_api_runtime(module)
        finally:
            os.environ.clear()
            os.environ.update(environment_before)
