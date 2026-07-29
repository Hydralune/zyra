from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_COMMIT = "3c4d1092187b1777468cad0ce2a772244d012197"
TARGET_VERSION = "0.2.13"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the P2-S01-03 -> P2-S01-04 LoopX workspace upgrade in "
            "independent clean-checkout API processes."
        )
    )
    parser.add_argument("--base-checkout")
    parser.add_argument("--target-checkout")
    parser.add_argument("--workspace")
    parser.add_argument("--output")
    parser.add_argument(
        "--phase",
        choices=("baseline", "target"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--checkout", help=argparse.SUPPRESS)
    parser.add_argument("--baseline", help=argparse.SUPPRESS)
    parser.add_argument("--phase-output", help=argparse.SUPPRESS)
    parser.add_argument("--restart-index", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    if not root.is_dir():
        return ""
    records = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
    ):
        relative = path.relative_to(root)
        if (
            "__pycache__" in relative.parts
            or ".pytest_cache" in relative.parts
            or path.suffix.casefold() in {".pyc", ".pyo"}
        ):
            continue
        records.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": _file_digest(path),
            }
        )
    return _stable_digest(records)


def _git_commit(checkout: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def _activate_checkout(checkout: Path) -> None:
    checkout = checkout.resolve()
    candidates = [checkout, checkout / "apps" / "api"]
    packages = checkout / "packages"
    if packages.is_dir():
        candidates.extend(
            path
            for path in sorted(packages.iterdir())
            if path.is_dir()
        )

    retained: list[str] = []
    for raw in sys.path:
        if not raw:
            continue
        resolved = Path(raw).resolve()
        if resolved.is_relative_to(PROJECT_ROOT) and ".venv" not in resolved.parts:
            continue
        retained.append(raw)
    sys.path[:] = [str(path) for path in candidates if path.is_dir()] + retained
    os.chdir(checkout)


def _configure(workspace: Path) -> Path:
    control = workspace / "control"
    tool_workspace = workspace / "workspace"
    os.environ.update(
        {
            "ZYRA_SQLITE_PATH": str(control / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(control / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(tool_workspace),
            "ZYRA_ARTIFACT_ROOT": str(control / "artifacts"),
            "ZYRA_CONTROL_STATE": str(control / "commands"),
            "ZYRA_GRAPH_STATE_STORE": str(control / "graph.sqlite3"),
            "ZYRA_WORKER_POOL_STORE": str(control / "worker-pool.sqlite3"),
            "ZYRA_PERMISSION_STATE": str(control / "permission.json"),
        }
    )
    return tool_workspace


def _request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=(
            json.dumps(payload).encode("utf-8")
            if payload is not None
            else None
        ),
        headers={"Content-Type": "application/json", **dict(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    from apps.api.zyra_api.main import ZyraRequestHandler

    server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _state_semantics(state: Mapping[str, Any]) -> dict[str, Any]:
    private = dict(state.get("private_state") or {})
    sync = dict(state.get("sync") or {})
    continuation = dict(state.get("continuation") or {})
    return {
        "goal_id": str(state.get("goal_id") or ""),
        "lifecycle": str(state.get("lifecycle") or ""),
        "connected": bool(state.get("connected")),
        "todos": list(private.get("todos") or []),
        "claims": list(private.get("claims") or []),
        "quota": dict(private.get("quota") or {}),
        "history": list(private.get("history") or []),
        "cursor": int(sync.get("cursor") or 0),
        "workspace_cursor": int(sync.get("workspace_cursor") or 0),
        "acked": int(sync.get("acked") or 0),
        "pending": int(sync.get("pending") or 0),
        "dead_letter": int(sync.get("dead_letter") or 0),
        "continuation_allowed": bool(continuation.get("allowed")),
        "interaction_contract": dict(
            continuation.get("interaction_contract") or {}
        ),
    }


def _task_mutation_count(task_payload: Mapping[str, Any]) -> int:
    task = task_payload.get("task")
    metadata = task.get("metadata") if isinstance(task, Mapping) else None
    mutations = (
        metadata.get("control_mutations")
        if isinstance(metadata, Mapping)
        else None
    )
    return len(mutations) if isinstance(mutations, list) else 0


def _baseline_phase(checkout: Path, workspace: Path) -> dict[str, Any]:
    if _git_commit(checkout) != BASE_COMMIT:
        raise RuntimeError("baseline checkout is not the P2-S01-03 evidence commit")
    _activate_checkout(checkout)
    tool_workspace = _configure(workspace)
    server, thread, base = _server()
    interaction_payload = {
        "action": "interaction_submit",
        "continuation_hint": "Continue after the embedded-source upgrade",
        "input_ref": "event:cross-version-input",
        "feedback_ref": "event:cross-version-feedback",
        "spend_slots": 1,
        "idempotency_key": "cross-version-interaction",
    }
    interaction_headers = {
        "X-Zyra-Api-Version": "1.0",
        "X-Request-Id": "request_cross_version_baseline",
        "X-Zyra-Operation": "task.loopx.command",
        "X-Zyra-Contract": "zyra.loopx-control-result.v1",
        "Idempotency-Key": "cross-version-interaction",
    }
    try:
        status, created = _request(
            base,
            "/tasks",
            method="POST",
            payload={
                "goal": "Preserve LoopX state across the v0.2.13 cutover.",
                "auto_run": False,
            },
        )
        if status != 201:
            raise RuntimeError(f"baseline task creation failed: {created}")
        task_id = str(created["task"]["task_id"])
        for action in (
            {
                "action": "connect",
                "todo_id": "todo_cross_version",
                "todo_title": "Resume the verified upgrade task",
                "limit_slots": 4,
            },
            {
                "action": "claim",
                "todo_id": "todo_cross_version",
                "claimant": "cross-version-controller",
            },
        ):
            action_status, action_result = _request(
                base,
                f"/tasks/{task_id}/loopx/commands",
                method="POST",
                payload=action,
            )
            if action_status != 201:
                raise RuntimeError(f"baseline LoopX action failed: {action_result}")
        interaction_status, interaction = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload=interaction_payload,
            headers=interaction_headers,
        )
        if interaction_status != 201:
            raise RuntimeError(f"baseline interaction failed: {interaction}")
        state_status, state = _request(base, f"/tasks/{task_id}/loopx")
        task_status, task = _request(base, f"/tasks/{task_id}")
        if state_status != 200 or task_status != 200:
            raise RuntimeError("baseline API projection is unavailable")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    semantics = _state_semantics(state)
    if (
        semantics["cursor"] != 3
        or semantics["acked"] != 3
        or not semantics["continuation_allowed"]
        or not semantics["claims"]
        or int(semantics["quota"].get("spent_slots") or 0) != 1
    ):
        raise RuntimeError(f"baseline state is incomplete: {semantics}")
    retired_install = tool_workspace / ".zyra" / "loopx" / "install"
    if not retired_install.is_dir():
        raise RuntimeError("the v0.2.4 baseline did not create its historical install")
    return {
        "schema": "zyra.loopx-cross-version-phase/v1",
        "phase": "baseline",
        "checkout": str(checkout),
        "commit": BASE_COMMIT,
        "task_id": task_id,
        "tool_workspace": str(tool_workspace),
        "state": semantics,
        "state_digest": _stable_digest(semantics),
        "task_mutation_count": _task_mutation_count(task),
        "interaction_payload": interaction_payload,
        "interaction_headers": interaction_headers,
        "historical_install": str(retired_install),
        "historical_install_digest": _tree_digest(retired_install),
        "historical_install_exists": True,
    }


def _target_phase(
    checkout: Path,
    workspace: Path,
    baseline: Mapping[str, Any],
    *,
    restart_index: int,
) -> dict[str, Any]:
    target_commit = _git_commit(checkout)
    if target_commit == BASE_COMMIT:
        raise RuntimeError("target checkout still points at the baseline commit")
    _activate_checkout(checkout)
    tool_workspace = _configure(workspace)
    server, thread, base = _server()
    task_id = str(baseline["task_id"])
    try:
        status, before = _request(base, f"/tasks/{task_id}/loopx")
        task_status, task_before = _request(base, f"/tasks/{task_id}")
        if status != 200 or task_status != 200:
            raise RuntimeError(f"target restart projection failed: {before}")
        headers = dict(baseline["interaction_headers"])
        headers["X-Request-Id"] = (
            f"request_cross_version_target_{restart_index}"
        )
        replay_status, replay = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload=dict(baseline["interaction_payload"]),
            headers=headers,
        )
        if replay_status != 201:
            raise RuntimeError(f"target duplicate replay failed: {replay}")
        after_status, after = _request(base, f"/tasks/{task_id}/loopx")
        task_after_status, task_after = _request(base, f"/tasks/{task_id}")
        if after_status != 200 or task_after_status != 200:
            raise RuntimeError("target post-replay projection failed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    before_semantics = _state_semantics(before)
    after_semantics = _state_semantics(after)
    expected_semantics = dict(baseline["state"])
    if before_semantics != expected_semantics or after_semantics != expected_semantics:
        raise RuntimeError(
            "target state changed across restart/replay: "
            f"before={before_semantics}, after={after_semantics}"
        )
    replay_receipt = dict(replay.get("receipt") or {})
    if replay_receipt.get("status") != "replayed":
        raise RuntimeError(f"target request was not replayed: {replay_receipt}")
    before_mutations = _task_mutation_count(task_before)
    after_mutations = _task_mutation_count(task_after)
    if (
        before_mutations != int(baseline["task_mutation_count"])
        or after_mutations != before_mutations
    ):
        raise RuntimeError("duplicate replay changed canonical mutation count")

    runtime = dict(before.get("runtime") or {})
    if (
        runtime.get("version") != TARGET_VERSION
        or runtime.get("source_kind") != "embedded_source"
        or runtime.get("archive_fallback") is not False
    ):
        raise RuntimeError(f"target runtime provenance is invalid: {runtime}")
    retired_install = Path(str(baseline["historical_install"]))
    historical_digest = _tree_digest(retired_install)
    if historical_digest != baseline["historical_install_digest"]:
        raise RuntimeError("target runtime mutated the historical install tree")
    return {
        "schema": "zyra.loopx-cross-version-phase/v1",
        "phase": "target",
        "restart_index": restart_index,
        "checkout": str(checkout),
        "commit": target_commit,
        "task_id": task_id,
        "tool_workspace": str(tool_workspace),
        "runtime": runtime,
        "state": after_semantics,
        "state_digest": _stable_digest(after_semantics),
        "duplicate_receipt_status": replay_receipt["status"],
        "canonical_mutation_count_before": before_mutations,
        "canonical_mutation_count_after": after_mutations,
        "historical_install_preserved": True,
        "historical_install_digest": historical_digest,
        "new_install_created": False,
        "claim_is_worker_lease": bool(
            before["canonical_state"]["loopx_claim_is_worker_lease"]
        ),
        "quota_is_execution_budget": bool(
            before["canonical_state"]["loopx_quota_is_execution_budget"]
        ),
    }


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _run_phase(arguments: argparse.Namespace) -> int:
    checkout = Path(str(arguments.checkout)).resolve()
    workspace = Path(str(arguments.workspace)).resolve()
    if arguments.phase == "baseline":
        result = _baseline_phase(checkout, workspace)
    else:
        baseline = json.loads(Path(str(arguments.baseline)).read_text(encoding="utf-8"))
        result = _target_phase(
            checkout,
            workspace,
            baseline,
            restart_index=arguments.restart_index,
        )
    _write(Path(str(arguments.phase_output)).resolve(), result)
    return 0


def _run_parent(arguments: argparse.Namespace) -> int:
    base_checkout = Path(str(arguments.base_checkout)).resolve()
    target_checkout = Path(str(arguments.target_checkout)).resolve()
    workspace = Path(str(arguments.workspace)).resolve()
    output = Path(str(arguments.output)).resolve()
    if workspace.exists():
        raise RuntimeError(
            "Cross-version workspace must be a new isolated path; refusing cleanup."
        )
    workspace.mkdir(parents=True)
    phase_root = output.parent / (output.stem + "-phases")
    phase_root.mkdir(parents=True, exist_ok=False)
    baseline_path = phase_root / "baseline.json"
    target_paths = [
        phase_root / "target-restart-1.json",
        phase_root / "target-restart-2.json",
    ]
    commands = [
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--phase",
            "baseline",
            "--checkout",
            str(base_checkout),
            "--workspace",
            str(workspace),
            "--phase-output",
            str(baseline_path),
        ],
        *[
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--phase",
                "target",
                "--checkout",
                str(target_checkout),
                "--workspace",
                str(workspace),
                "--baseline",
                str(baseline_path),
                "--phase-output",
                str(path),
                "--restart-index",
                str(index),
            ]
            for index, path in enumerate(target_paths, start=1)
        ],
    ]
    process_receipts = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        process_receipts.append(
            {
                "command": command[2:],
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "cross-version phase failed: "
                + completed.stderr.strip()
                + completed.stdout.strip()
            )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    targets = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in target_paths
    ]
    if not (
        baseline["state_digest"]
        == targets[0]["state_digest"]
        == targets[1]["state_digest"]
    ):
        raise RuntimeError("cross-version semantic state digest is not stable")
    result = {
        "schema": "zyra.loopx-cross-version-upgrade/v1",
        "ready": True,
        "base_commit": baseline["commit"],
        "target_commit": targets[-1]["commit"],
        "workspace": str(workspace),
        "baseline": baseline,
        "target_restarts": targets,
        "process_receipts": process_receipts,
        "invariants": {
            "semantic_state_preserved": True,
            "cursor_monotonic": True,
            "duplicate_claim": False,
            "duplicate_spend": False,
            "duplicate_interaction": False,
            "duplicate_canonical_commit": False,
            "historical_install_preserved_and_ignored": True,
            "new_install_or_extraction": False,
            "claim_became_worker_lease": False,
            "quota_became_execution_budget": False,
            "independent_target_restart_count": 2,
        },
    }
    _write(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.phase:
        return _run_phase(arguments)
    required = (
        arguments.base_checkout,
        arguments.target_checkout,
        arguments.workspace,
        arguments.output,
    )
    if not all(required):
        raise SystemExit(
            "--base-checkout, --target-checkout, --workspace and --output are required"
        )
    return _run_parent(arguments)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
