from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
for package_path in (ROOT / "packages" / "core", ROOT / "packages" / "integrations"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

import sync_worker_pool_foundation_source_ledger as foundation  # noqa: E402
from zyra_integrations import InternalizationLedger, InternalizationLedgerEntry  # noqa: E402
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


OWNER_UNIT = "M1-S07B-02"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
PYTHON_TEST = (
    "python -m pytest -q tests/integration/"
    "test_watchdog_fault_injection_integration.py"
)
TYPESCRIPT_TEST = (
    "node --experimental-strip-types --test packages/runtime/claude-runtime/"
    "test/watchdog-integration.test.ts"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "ledger_id": "ledger_07b02_browser_source_session_fault_integration",
        "source_repo": "browser-use",
        "source_commit": "18484f23ac96bb955259a1c54530a7d265dfffdb",
        "source_path": (
            "browser_use/browser/watchdog_base.py;"
            "browser_use/browser/watchdogs/local_browser_watchdog.py;"
            "browser_use/browser/session.py"
        ),
        "source_language": "python",
        "target_language": "python",
        "source_symbols": [
            "BaseWatchdog.attach_to_session/start/stop",
            "BrowserSession lifecycle event dispatch",
            "target crash listener",
            "process-death and CDP responsiveness observation",
        ],
        "source_callsites": [
            "task-scoped source-session bind and generation fence",
            "explicit browser callback subscribe and unsubscribe",
            "typed browser process, target and CDP observation admission",
            "canonical signal projection and same-run containment",
            "lease-fenced recovery handoff dispatch and acknowledgement",
        ],
        "source_tests": [
            "browser-use watchdog lifecycle and BrowserSession tests recorded in the source graph",
        ],
        "capability_name": "browser_source_session_watchdog_fault_integration",
        "capability_summary": (
            "Browser Use's explicit watchdog attachment and cleanup semantics are cropped into "
            "Zyra source sessions and the active 04D browser evidence path. Typed observations "
            "flow through the existing classifier/store/event/memory/health owners, then through "
            "same-run containment and an acknowledged 07C-compatible handoff."
        ),
        "target_paths": [
            "packages/scheduler/zyra_scheduler/fault_runtime/source_session.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/browser_integration.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/observation_port.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/effect_runtime.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/containment_runtime.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/handoff_runtime.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/integration.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/runtime.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/api.py",
            "packages/commands/zyra_commands/runtime/watchdog_control.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "cropped_same_language_browser_observer_integration_with_zyra_state_custody",
        "runtime_module": "zyra_scheduler.fault_runtime.integration",
        "runtime_function": "WatchdogFaultIntegrationRuntime",
        "event_types": ["failure_injected"],
        "api_routes": [
            "POST /tasks/{task_id}/faults/sources/bind",
            "POST /tasks/{task_id}/faults/sources/observe",
            "POST /tasks/{task_id}/faults/handoffs/dispatch",
            "GET /tasks/{task_id}/faults",
        ],
        "state_owner": (
            "FaultStateStore owns durable source, observation, signal, projection and handoff "
            "facts; 04D owns browser process/CDP evidence; MemoryFabric and BackendRegistry "
            "retain memory and scheduler-health custody; 07C owns recovery-plan choice"
        ),
        "rationale": (
            "The same-language migration retains explicit attach/detach and lifecycle-loss "
            "behavior while replacing Browser Use event/session custody with Zyra identities, "
            "generation fences, canonical events, projections and durable delivery. Disabling "
            "the browser observer removes every capture callback and has no injection fallback."
        ),
    },
    {
        "ledger_id": "ledger_07b02_omp_execution_transport_supervision",
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/advisor/emission-guard.ts;"
            "packages/coding-agent/src/mcp/transports/stdio.ts;"
            "packages/coding-agent/src/mcp/manager.ts;"
            "packages/coding-agent/src/mcp/timeout.ts;"
            "packages/ai/src/error/retryable.ts;"
            "packages/ai/src/utils/provider-response.ts"
        ),
        "source_language": "typescript",
        "target_language": "typescript",
        "source_symbols": [
            "turn/tool abort and terminal settlement fences",
            "partial provider stream retry cursor",
            "MCP timeout, transport close and reconnect single-flight",
            "worker process generation and durable job handback",
        ],
        "source_callsites": [
            "Claude runtime stdio batch execution",
            "tool deadline abort with committed-effect preservation",
            "provider partial-stream resume without duplicate side effects",
            "MCP crash-window breaker and request cleanup",
            "worker restart budget and checkpointed job requeue",
        ],
        "source_tests": [
            "OMP tool execution, MCP transport/timeout and provider error tests recorded in the source graph",
        ],
        "capability_name": "omp_execution_provider_mcp_and_worker_restart_supervision",
        "capability_summary": (
            "OMP-derived execution and transport supervision remains TypeScript-native in the "
            "Claude runtime path. It aborts overdue work, fences committed effects, resumes "
            "partial streams, bounds MCP reconnect storms and hands durable jobs back across "
            "worker generations while emitting typed observations to Python custody."
        ),
        "target_paths": [
            "packages/runtime/claude-runtime/src/watchdog/execution-supervisor.ts",
            "packages/runtime/claude-runtime/src/watchdog/provider-stream-supervisor.ts",
            "packages/runtime/claude-runtime/src/watchdog/mcp-supervisor.ts",
            "packages/runtime/claude-runtime/src/watchdog/worker-restart-supervisor.ts",
            "packages/runtime/claude-runtime/src/watchdog/integration-supervisor.ts",
            "packages/runtime/claude-runtime/src/watchdog/runtime.ts",
            "packages/runtime/claude-runtime/src/watchdog/index.ts",
            "packages/runtime/claude-runtime/src/stdio.ts",
            "packages/scheduler/zyra_scheduler/fault_runtime/observation_port.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/integration.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_same_language_execution_transport_supervision_with_python_canonical_custody",
        "runtime_module": "zyra_scheduler.fault_runtime.integration",
        "runtime_function": "WatchdogFaultIntegrationRuntime",
        "event_types": ["tool_failure_signal"],
        "api_routes": [
            "POST /tasks/{task_id}/faults/runtime-events",
        ],
        "state_owner": (
            "TypeScript owns process-local deadlines, abort handles, partial-stream cursors, "
            "MCP reconnect state and worker generations; Python FaultStateStore owns durable "
            "fault facts; 07C owns recovery planning"
        ),
        "rationale": (
            "The cropped original-language mechanisms preserve abort/settlement and transport "
            "semantics at their real execution boundary. Explicit signal IDs and Python admission "
            "fences prevent a second classifier, durable store or recovery owner."
        ),
    },
)


def _entry(decision: Mapping[str, Any]) -> dict[str, Any]:
    entry = foundation._entry(decision)
    entry["owner_unit"] = OWNER_UNIT
    entry["downstream_units"] = ["M1-S07C-01", "M2-fault-console"]
    entry["dependencies"] = [
        "M1-S04D-02",
        "M1-S05A-02",
        "M1-S05C-02",
        "M1-S05D-02",
        "M1-S07A-02",
        "M1-S07B-01",
    ]
    entry["runtime_entry"]["health_check"] = PYTHON_TEST
    entry["test_entries"] = [
        {
            "path": "tests/integration/test_watchdog_fault_injection_integration.py",
            "command": PYTHON_TEST,
            "kind": "integration_main_path",
            "expected_signal": (
                "typed real observations mutate canonical events, memory, scheduler health and "
                "same-run containment before an acknowledged recovery handoff"
            ),
            "required": True,
        },
        {
            "path": "tests/integration/test_watchdog_fault_injection_foundation.py",
            "command": (
                "python -m pytest -q tests/integration/"
                "test_watchdog_fault_injection_foundation.py"
            ),
            "kind": "adjacent_negative_path_regression",
            "expected_signal": (
                "disabled observers, stale generations, invalid identities and failed handoff "
                "leases remain fail-closed"
            ),
            "required": True,
        },
        {
            "path": "tests/integration/test_api_control_commands.py",
            "command": (
                "python -m pytest -q tests/integration/test_api_control_commands.py::"
                "ApiControlCommandTests::test_change_command_creates_requirement_change_event"
            ),
            "kind": "adjacent_control_boundary_regression",
            "expected_signal": (
                "the /change path replans while remaining outside fault counts, pressure, "
                "scheduler health and failure memory"
            ),
            "required": True,
        },
    ]
    entry["main_path"]["surfaces"] = [
        "task_scoped_source_session",
        "browser_04d_observer_bridge",
        "typescript_execution_transport_supervision",
        "canonical_fault_projection_and_containment",
        "fault_http_and_control_commands",
        "m1_07c_consumer_dispatch",
    ]
    entry["main_path"]["control_commands"] = ["/inject", "/change"]
    entry["source_evidence"][0]["reason"] = (
        "Pinned source graph and concrete source files verified for M1-S07B-02."
    )
    entry["source_evidence"][0]["tags"] = [decision["source_role"], OWNER_UNIT]
    entry["notes"] = entry["notes"].replace("M1-S07A-01", OWNER_UNIT)
    entry["metadata"]["owner_unit"] = OWNER_UNIT
    entry["metadata"]["openclaw_forward_excluded"] = True
    entry["metadata"]["requirement_changed_is_fault"] = False
    entry["metadata"]["recovery_plan_owner"] = "M1-S07C"
    entry["metadata"]["original_language_runtime"] = (
        "@zyra/claude-runtime/watchdog IntegratedRuntimeSupervisor"
    )
    entry["metadata"]["original_language_behavior_test"] = TYPESCRIPT_TEST
    entry["metadata"]["shared_watchdog_control_command"] = "/watchdog"
    return entry


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    foundation._validate_decisions(DECISIONS)
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document if isinstance(document, list) else document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {item["ledger_id"]: item for item in (_entry(item) for item in DECISIONS)}
    output: list[dict[str, Any]] = []
    for item in entries:
        ledger_id = str(item.get("ledger_id") or "")
        if str(item.get("owner_unit") or "") == OWNER_UNIT and ledger_id not in replacements:
            continue
        output.append(replacements.pop(ledger_id, item))
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    expected = (
        output
        if isinstance(document, list)
        else {
            **document,
            "entries": output,
            "summary": to_jsonable(InternalizationLedger(typed).summary()),
        }
    )
    aligned = _canonical(document) == _canonical(expected)
    if write and not aligned:
        path.write_text(_canonical(expected), encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(DECISIONS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    aligned, count = synchronize(
        arguments.ledger.resolve(),
        write=arguments.write or not arguments.check,
    )
    print(f"watchdog_fault_integration_ledger_aligned={str(aligned).lower()}")
    print(f"watchdog_fault_integration_decision_count={count}")
    print(f"watchdog_fault_integration_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
