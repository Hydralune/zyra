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


OWNER_UNIT = "M1-S07B-01"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
DIRECT_TEST = (
    "python -m pytest -q tests/integration/"
    "test_watchdog_fault_injection_foundation.py"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "ledger_id": "ledger_07b01_browser_watchdog_fault_runtime",
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
            "BaseWatchdog.attach/start/stop",
            "BrowserSession.attach_all_watchdogs",
            "LocalBrowserWatchdog",
            "CrashWatchdog source-inactive contrast",
        ],
        "source_callsites": [
            "explicit observer registration, attachment, start, stop and disable",
            "typed browser process/CDP signal intake from the active 04D detector",
            "process-epoch callback reattachment after an ungraceful restart",
            "durable source-revision fence and deterministic fault classification",
            "same-run injection projection and durable 07C handoff outbox",
        ],
        "source_tests": [
            "browser_use browser watchdog lifecycle tests recorded in source graph",
            "upstream BrowserSession CrashWatchdog-disabled source fact",
        ],
        "capability_name": "watchdog_observer_lifecycle_fault_state_injection_and_recovery_handoff",
        "capability_summary": (
            "Browser Use's attached-watchdog lifecycle is cropped into a Zyra-owned observer "
            "registry and combined with the existing 04D BrowserCrashDetector, deterministic "
            "cross-runtime classification, durable fault/injection journals, epoch restoration, "
            "canonical event projection and a lease-fenced 07C handoff outbox."
        ),
        "target_paths": [
            "packages/scheduler/zyra_scheduler/fault_runtime/browser_source.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/observer_registry.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/observers.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/lifecycle_supervisor.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/classifier.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/state_store.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/injection.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/event_writer.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/recovery_bridge.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/runtime.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/api.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "cropped_same_language_watchdog_lifecycle_migration_with_zyra_fault_state_ownership",
        "runtime_module": "zyra_scheduler.fault_runtime",
        "runtime_function": "FaultRuntimeApplication",
        "event_types": ["worker_health", "failure_injected"],
        "api_routes": [
            "GET /tasks/{task_id}/faults",
            "POST /tasks/{task_id}/faults/inject",
            "POST /tasks/{task_id}/faults/observers",
            "/inject control command",
        ],
        "state_owner": (
            "FaultStateStore owns observer, observation, signal, injection, source cursor and "
            "handoff delivery journals; 04D BrowserCrashDetector keeps browser process/CDP "
            "evidence ownership and 07C keeps recovery-plan ownership"
        ),
        "rationale": (
            "The migration retains explicit Browser Use lifecycle semantics while replacing "
            "its event-bus/session custody with Zyra schemas, CAS journals, canonical events, "
            "MemoryFabric/backend-health projections and restart-safe callbacks. The upstream "
            "CrashWatchdog remains source_inactive and is never presented as an attached source."
        ),
    },
    {
        "ledger_id": "ledger_07b01_omp_runtime_observers",
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/advisor/emission-guard.ts;"
            "packages/coding-agent/src/mcp/transports/stdio.ts;"
            "packages/coding-agent/src/mcp/manager.ts;"
            "packages/coding-agent/src/mcp/timeout.ts;"
            "packages/ai/src/error/retryable.ts;"
            "packages/ai/src/error/rate-limit.ts;"
            "packages/ai/src/utils/provider-response.ts"
        ),
        "source_language": "typescript",
        "target_language": "typescript",
        "source_symbols": [
            "EmissionGuard",
            "stdio process lifecycle",
            "MCP close/reconnect epoch fence",
            "structured retryable provider errors",
            "provider rate-limit classification",
        ],
        "source_callsites": [
            "real tool deadline and terminal-result observation",
            "permission receipt observation",
            "provider terminal response observation",
            "process and MCP transport close observation",
            "structured tool_failure_signal ingress to the Python canonical store",
        ],
        "source_tests": [
            "OMP timeout, MCP stdio and provider error tests recorded in source graph",
        ],
        "capability_name": "omp_typescript_tool_permission_provider_process_and_mcp_fault_observers",
        "capability_summary": (
            "OMP emission, timeout, process/MCP and provider-error mechanisms remain in "
            "TypeScript and are attached to the real Claude runtime stdio/query path. They emit "
            "structured refs into the Python canonical fault owner without owning recovery."
        ),
        "target_paths": [
            "packages/runtime/claude-runtime/src/watchdog/runtime.ts",
            "packages/runtime/claude-runtime/src/watchdog/index.ts",
            "packages/runtime/claude-runtime/src/stdio.ts",
            "packages/runtime/claude-runtime/src/index.ts",
            "packages/scheduler/zyra_scheduler/fault_runtime/runtime_event_adapter.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/provider_supervision.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/supervision.py",
            "packages/scheduler/zyra_scheduler/fault_runtime/deadline_runtime.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_same_language_runtime_observer_migration_with_python_canonical_fault_custody",
        "runtime_module": "@zyra/claude-runtime/watchdog",
        "runtime_function": "RuntimeWatchdog",
        "event_types": ["tool_failure_signal", "worker_health"],
        "api_routes": ["Claude runtime stdio event stream", "GET /tasks/{task_id}/faults"],
        "state_owner": (
            "TypeScript RuntimeWatchdog owns process-local observation and emission-guard state; "
            "Python FaultStateStore owns durable signals/injections and 07C owns recovery plans"
        ),
        "rationale": (
            "The original-language supplement preserves OMP timeout cleanup, structured provider "
            "errors, process/MCP lifecycle and bounded emission behavior. Explicit refs and the "
            "existing runtime event frame prevent a second durable fault owner."
        ),
    },
)


def _entry(decision: Mapping[str, Any]) -> dict[str, Any]:
    entry = foundation._entry(decision)
    entry["owner_unit"] = OWNER_UNIT
    entry["downstream_units"] = ["M1-S07B-02", "M1-S07C", "M2-fault-console"]
    entry["dependencies"] = [
        "M1-S04D-02",
        "M1-S05A-02",
        "M1-S05C-02",
        "M1-S05D-02",
        "M1-S07A-02",
    ]
    entry["runtime_entry"]["health_check"] = DIRECT_TEST
    entry["test_entries"] = [
        {
            "path": "tests/integration/test_watchdog_fault_injection_foundation.py",
            "command": DIRECT_TEST,
            "kind": "integration_main_path",
            "expected_signal": (
                "real observers and five injection kinds mutate canonical events, memory/health "
                "projections, durable cursors, handoff leases and same-run state"
            ),
            "required": True,
        },
        {
            "path": "packages/runtime/claude-runtime/test/watchdog.test.ts",
            "command": "bun test packages/runtime/claude-runtime/test/watchdog.test.ts",
            "kind": "original_language_behavior",
            "expected_signal": (
                "TypeScript tool/provider/process/MCP observers emit structured fault frames and "
                "disable blocks real emission"
            ),
            "required": True,
        },
    ]
    entry["main_path"]["surfaces"] = [
        "runtime_observer_registry",
        "browser_04d_crash_source",
        "typescript_query_runtime_observers",
        "canonical_fault_event_projection",
        "same_run_fault_injection",
        "fault_http_and_control_command",
        "m1_07c_handoff_outbox",
    ]
    entry["main_path"]["control_commands"] = ["inject", "enable_observer", "disable_observer"]
    entry["source_evidence"][0]["reason"] = (
        "Pinned source graph and concrete source files verified for M1-S07B-01."
    )
    entry["source_evidence"][0]["tags"] = [decision["source_role"], OWNER_UNIT]
    entry["notes"] = entry["notes"].replace("M1-S07A-01", OWNER_UNIT)
    entry["metadata"]["owner_unit"] = OWNER_UNIT
    entry["metadata"]["openclaw_forward_excluded"] = True
    entry["metadata"]["requirement_changed_is_fault"] = False
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
    print(f"watchdog_fault_source_ledger_aligned={str(aligned).lower()}")
    print(f"watchdog_fault_source_decision_count={count}")
    print(f"watchdog_fault_source_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
