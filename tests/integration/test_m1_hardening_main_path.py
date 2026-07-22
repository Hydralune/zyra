from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from zyra_evaluation.m1_hardening.scenario import HttpScenarioTransport, M1MainPathScenarioSuite
from zyra_evaluation.m1_hardening.contracts import HardeningContext
from zyra_evaluation.m1_hardening.service import AuditOptions, M1HardeningService


ROOT = Path(__file__).resolve().parents[2]


def _fresh_api_handler() -> type[BaseHTTPRequestHandler]:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)
    return module.ZyraRequestHandler


def test_public_api_foundation_scenario_mutates_state_and_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
        }
        with patch.dict(os.environ, environment, clear=False):
            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                run, gate = M1MainPathScenarioSuite().run_http_foundation(base_url, timeout_seconds=120)
                status, hardening_status = HttpScenarioTransport(base_url).get("/hardening/m1/status")
                assert status == 200
                assert hardening_status["schema"] == "zyra.m1-hardening-service-status/v1"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)

        assert gate.ok, gate.to_dict()
        assert run.task_id
        assert run.run_id
        assert len(run.events_after) > len(run.events_before)
        assert run.disable_evidence["baseline_ok"] is True
        assert run.disable_evidence["error_code"]
        assert run.disable_evidence["fallback_masked"] is False
        assert any(step.step_id == "trace-artifact" and step.ok for step in run.steps)
        assert any(step.step_id == "disable-codeworker" and step.status >= 400 for step in run.steps)
        assert run.task_after.get("artifacts")
        assert (root / "workspace" / "m1-hardening" / "output.txt").exists() is False

        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as audit_directory:
            service = M1HardeningService(
                ROOT,
                source_workspace=ROOT.parent,
                artifact_root=Path(audit_directory),
            )
            outcome = service.audit(
                HardeningContext(
                    project_root=ROOT,
                    workspace_root=ROOT.parent,
                    artifact_root=Path(audit_directory),
                    task=run.task_after,
                    events=run.events_after,
                    options={"scenario_id": run.scenario_id},
                ),
                AuditOptions(
                    baseline_commit="44da53ad8ea909147709857e358b7d16e39f6313",
                    minimum_effective_lines=0,
                    line_audit_head="HEAD",
                    persist=True,
                    run_dynamic_graph_probes=True,
                    run_disable_probes=True,
                ),
                scenario_gate=gate,
                scenario_run=run,
            )
            if not outcome.accepted:
                print(json.dumps(outcome.disposition.to_dict(), indent=2))
                print(
                    json.dumps(
                        {
                            gate.gate_id: {
                                "status": gate.status.value,
                                "findings": [finding.to_dict() for finding in gate.findings],
                            }
                            for gate in outcome.report.gates
                            if not gate.ok
                        },
                        indent=2,
                    )
                )
            assert outcome.accepted, outcome.disposition.to_dict()
            assert outcome.record is not None
            assert service.store.load(outcome.report.report_id, verify=True)["report_id"] == outcome.report.report_id
            assert outcome.report.gate("source-to-target-coverage").ok
            assert outcome.report.gate("m1-state-custody").ok
            assert outcome.report.gate("langgraph-boundary").ok
            disable_gate = outcome.report.gate("disable-module-probe")
            assert disable_gate.ok
            assert disable_gate.metrics["executed_count"] == 1
            assert disable_gate.metrics["live_scenario_disable"]["fallback_masked"] is False
