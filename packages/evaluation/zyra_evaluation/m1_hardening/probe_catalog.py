from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from zyra_orchestration.graph_custody.store import GraphStateStore


class GraphCustodyDisabled(RuntimeError):
    """Stable failure raised while the canonical graph owner is disconnected."""


class GraphCustodyDisconnectProbe:
    """Exercise, disconnect, and restore the real GraphStateStore entrypoint.

    The probe replaces the class-level initializer rather than a test double.  A
    fresh store therefore cannot silently use a second owner or an existing
    database after the canonical owner is disconnected.
    """

    probe_id = "disable-graph-state-store"
    capability = "graph-custody"
    dependencies: tuple[str, ...] = ()
    timeout_seconds = 20.0
    expected_error_codes = ("graph_custody_disabled",)
    allow_success_with_difference = False

    def __init__(self, artifact_root: str | Path) -> None:
        self.path = Path(artifact_root).resolve() / "disable-probes" / "graph-custody.sqlite3"

    def capture(self) -> Mapping[str, Any]:
        return {
            "initializer": GraphStateStore.initialize,
            "path": str(self.path),
            "owner": "GraphStateStore",
        }

    def exercise(self) -> Mapping[str, Any]:
        store = GraphStateStore(self.path)
        try:
            store.initialize()
        except GraphCustodyDisabled:
            return {
                "ok": False,
                "error": "graph_custody_disabled",
                "canonical_owner": "GraphStateStore",
                "fallback": False,
            }
        except Exception as error:
            return {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error),
                "canonical_owner": "GraphStateStore",
                "fallback": False,
            }
        tables = self._tables()
        return {
            "ok": "graph_heads" in tables and "graph_snapshots" in tables,
            "canonical_owner": "GraphStateStore",
            "fallback": False,
            "schema_tables": sorted(tables),
        }

    def disable(self) -> Mapping[str, Any]:
        def disconnected(_store: GraphStateStore) -> None:
            raise GraphCustodyDisabled("canonical graph-state owner was disabled by the M1 hardening probe")

        GraphStateStore.initialize = disconnected  # type: ignore[method-assign]
        return {
            "ok": True,
            "disabled_owner": "GraphStateStore.initialize",
            "fallback_enabled": False,
        }

    def restore(self, captured: Mapping[str, Any]) -> Mapping[str, Any]:
        initializer = captured.get("initializer")
        if not callable(initializer):
            raise RuntimeError("captured GraphStateStore initializer is not callable")
        GraphStateStore.initialize = initializer  # type: ignore[method-assign,assignment]
        return {
            "ok": True,
            "restored_owner": "GraphStateStore.initialize",
        }

    def _tables(self) -> set[str]:
        if not self.path.is_file():
            return set()
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            connection.close()
        return {str(row[0]) for row in rows}


def default_disable_probes(artifact_root: str | Path) -> tuple[GraphCustodyDisconnectProbe, ...]:
    """Build the foundation batch of real, reversible disconnect probes."""

    return (GraphCustodyDisconnectProbe(artifact_root),)
