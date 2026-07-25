from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_STATE_ROOT = (PROJECT_ROOT / ".tmp").resolve()


class EmbeddedScenarioError(RuntimeError):
    pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Run one canonical Zyra scenario in the foreground without opening "
            "a port or starting a background process. Runtime state is confined "
            "to a new directory beneath the project .tmp directory."
        )
    )
    result.add_argument(
        "--scenario",
        required=True,
        choices=("live.software-delivery", "live.cross-source-research"),
    )
    result.add_argument("--input", required=True)
    result.add_argument("--seed", type=int, required=True)
    result.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help="New directory beneath the project .tmp directory.",
    )
    result.add_argument(
        "--profile",
        default="live.heterogeneous-sealed",
    )
    result.add_argument(
        "--policy",
        default="sealed-autonomous-foundation",
    )
    result.add_argument("--timeout", type=float, default=900.0)
    return result


def _safe_new_state_root(raw: Path) -> Path:
    selected = raw if raw.is_absolute() else PROJECT_ROOT / raw
    selected = selected.resolve()
    if not selected.is_relative_to(SAFE_STATE_ROOT):
        raise EmbeddedScenarioError(
            f"state root must be beneath {SAFE_STATE_ROOT}"
        )
    if selected == SAFE_STATE_ROOT:
        raise EmbeddedScenarioError("state root must be a child of project .tmp")
    if selected.exists():
        raise EmbeddedScenarioError(
            f"state root already exists; refusing to overwrite it: {selected}"
        )
    selected.mkdir(parents=True)
    return selected


def _configure_isolated_runtime(state_root: Path) -> None:
    values = {
        "ZYRA_SQLITE_PATH": state_root / "api.sqlite3",
        "ZYRA_EVENT_LOG": state_root / "events.jsonl",
        "ZYRA_ARTIFACT_ROOT": state_root / "artifacts",
        "ZYRA_WORKER_POOL_STORE": state_root / "worker-pool.sqlite3",
        "ZYRA_GRAPH_STATE_STORE": state_root / "graph.sqlite3",
        "ZYRA_WORKSPACE_STATE_ROOT": state_root / "workspace-state",
        "ZYRA_WORKSPACE_DATA_ROOT": state_root / "workspace-data",
        "ZYRA_CONTROL_STATE": state_root / "control",
        "ZYRA_SUBAGENT_STATE": state_root / "subagents",
    }
    for key, value in values.items():
        os.environ[key] = str(value)
    os.environ["ZYRA_MODEL_PROVIDER"] = "excluded-by-user-boundary"
    os.environ["ZYRA_MODEL"] = "none"
    for key in (
        "ZYRA_LIVE_PROVIDER_EXECUTABLE",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        os.environ.pop(key, None)


def _require_response(
    response: Any,
    *,
    operation: str,
    expected_statuses: set[int],
) -> dict[str, Any]:
    if response is None:
        raise EmbeddedScenarioError(f"{operation} route was not handled")
    status = int(response.status)
    body = dict(response.body)
    if status not in expected_statuses:
        raise EmbeddedScenarioError(
            f"{operation} failed with HTTP {status}: "
            f"{json.dumps(body, ensure_ascii=False, sort_keys=True)}"
        )
    return body


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    state_root = _safe_new_state_root(arguments.state_root)
    _configure_isolated_runtime(state_root)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from apps.api.zyra_api import main as api_main
    from apps.api.zyra_api.scenario_api import (
        get_scenario_runner_api,
        reset_scenario_runner_api,
    )

    reset_scenario_runner_api(wait=True)
    api_main.reset_runtime_event_spine_bridge()
    api = get_scenario_runner_api()
    try:
        created = _require_response(
            api.route_post(
                ("scenarios", "runs"),
                {
                    "scenario_id": arguments.scenario,
                    "profile_id": arguments.profile,
                    "policy_id": arguments.policy,
                    "mode": "sealed",
                    "input": arguments.input,
                    "seed": arguments.seed,
                    "labels": {
                        "slice": "M2-S05-02",
                        "execution": "foreground-embedded",
                        "external_provider": "excluded",
                    },
                },
                actor_id="m2-s05-02-embedded-evidence",
            ),
            operation="create",
            expected_statuses={201},
        )
        created_run = _mapping(created.get("run"), "created run")
        scenario_run_id = str(created_run.get("scenario_run_id") or "")
        if not scenario_run_id:
            raise EmbeddedScenarioError("create response omitted scenario_run_id")

        started = _require_response(
            api.route_post(
                ("scenarios", "runs", scenario_run_id, "start"),
                {
                    "wait": True,
                    "timeout_seconds": max(1.0, float(arguments.timeout)),
                },
                actor_id="m2-s05-02-embedded-evidence",
            ),
            operation="start",
            expected_statuses={200},
        )
        run = _mapping(started.get("run"), "started run")
        if run.get("phase") != "succeeded" or run.get("terminal") is not True:
            raise EmbeddedScenarioError(
                f"scenario did not complete: phase={run.get('phase')}"
            )

        verified = _require_response(
            api.route_post(
                ("scenarios", "runs", scenario_run_id, "verify"),
                {},
                actor_id="m2-s05-02-embedded-evidence",
            ),
            operation="verify",
            expected_statuses={200},
        )
        receipt = _mapping(
            verified.get("verification_receipt"),
            "verification receipt",
        )
        if receipt.get("valid") is not True:
            raise EmbeddedScenarioError("scenario verification was not valid")

        claims = _mapping(
            _mapping(run.get("evidence_manifest"), "evidence manifest").get(
                "claims"
            ),
            "evidence claims",
        )
        if claims.get("external_provider_execution_excluded") is not True:
            raise EmbeddedScenarioError(
                "evidence did not exclude external provider execution"
            )
        if claims.get("authenticated_provider_cli_invoked") is not False:
            raise EmbeddedScenarioError(
                "evidence reported an authenticated provider CLI invocation"
            )

        result = {
            "schema": "zyra.embedded-scenario-evidence/v1",
            "execution": {
                "foreground": True,
                "background_process_started": False,
                "network_listener_opened": False,
                "authenticated_provider_cli_allowed": False,
            },
            "state_root": str(state_root),
            "run": run,
            "verification_receipt": receipt,
        }
        output = state_root / "scenario-result.json"
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "output": str(output),
            "scenario_run_id": scenario_run_id,
            "scenario_id": arguments.scenario,
            "phase": run.get("phase"),
            "effective_steps": len(
                _mapping(
                    _mapping(
                        run.get("evidence_manifest"),
                        "evidence manifest",
                    ).get("effective_steps"),
                    "effective steps",
                ).get("admitted_step_ids")
                or ()
            ),
            "human_intervention_count": run.get("human_intervention_count"),
            "verification_valid": receipt.get("valid"),
            "external_provider_execution_excluded": claims.get(
                "external_provider_execution_excluded"
            ),
            "authenticated_provider_cli_invoked": claims.get(
                "authenticated_provider_cli_invoked"
            ),
        }
    finally:
        reset_scenario_runner_api(wait=True)
        api_main.reset_runtime_event_spine_bridge()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EmbeddedScenarioError(f"{label} must be an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        result = execute(arguments)
    except EmbeddedScenarioError as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "embedded_scenario_failed",
                    "message": str(error),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
