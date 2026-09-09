from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ANALYSIS_CLASSES = {"calibration", "formal", "invalidated_development"}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _seconds_between(start: Any, end: Any) -> float | None:
    if not start or not end:
        return None
    try:
        left = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((right - left).total_seconds(), 3)


def _official_result(run_dir: Path) -> dict[str, Any] | None:
    reports = sorted((run_dir / "official-swebench-report").glob("*.json"))
    for report in reports:
        data = _read_json(report)
        if isinstance(data, Mapping):
            return {"path": str(report), "raw": data}
    return None


def summarize(
    run_dir: Path,
    *,
    analysis_class: str | None = None,
    invalidation_reason: str = "",
) -> dict[str, Any]:
    metadata = _read_json(run_dir / "run-metadata.json")
    task_run_path = run_dir / "task-run.json"
    task_document = _read_json(task_run_path) if task_run_path.exists() else {}
    task = task_document.get("task") if isinstance(task_document.get("task"), Mapping) else {}

    recorded_class = str(metadata.get("analysis_class") or metadata.get("run_class") or "")
    normalized = {
        "integration_calibration": "calibration",
        "calibration": "calibration",
        "formal": "formal",
        "invalidated-development": "invalidated_development",
        "invalidated_development": "invalidated_development",
    }.get(recorded_class, recorded_class)
    selected_class = analysis_class or normalized
    if selected_class not in ANALYSIS_CLASSES:
        raise ValueError(f"unsupported analysis class: {selected_class!r}")
    if selected_class == "invalidated_development" and not invalidation_reason:
        invalidation_reason = str(metadata.get("invalidation_reason") or "")
        if not invalidation_reason:
            raise ValueError("invalidated_development requires an invalidation reason")

    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
        "provider_calls": 0,
        "tool_calls": 0,
        "context_compactions": 0,
    }
    patches: list[dict[str, Any]] = []
    seen_receipts: set[str] = set()
    task_metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
    for receipt in task_metadata.get("physical_dispatch_receipts") or ():
        if not isinstance(receipt, Mapping):
            continue
        identity = str(receipt.get("digest") or receipt.get("contract_id") or "")
        if identity and identity in seen_receipts:
            continue
        if identity:
            seen_receipts.add(identity)
        payload = receipt.get("payload") if isinstance(receipt.get("payload"), Mapping) else {}
        provider = payload.get("provider_evidence") if isinstance(payload.get("provider_evidence"), Mapping) else {}
        usage = provider.get("usage") if isinstance(provider.get("usage"), Mapping) else {}
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            totals[key] += int(usage.get(key) or 0)
        totals["cost_usd"] += float(provider.get("cost_usd") or 0.0)
        totals["provider_calls"] += len(provider.get("calls") or ())
        signals = payload.get("input_signals") if isinstance(payload.get("input_signals"), Mapping) else {}
        domain = signals.get("domain_result") if isinstance(signals.get("domain_result"), Mapping) else {}
        totals["tool_calls"] += int(domain.get("tool_call_count") or 0)
        totals["context_compactions"] += int(domain.get("context_compaction_count") or 0)
        workspace_delta = domain.get("workspace_delta")
        if isinstance(workspace_delta, Mapping):
            patches.append(
                {
                    "schema": workspace_delta.get("schema"),
                    "before_manifest_digest": workspace_delta.get("before_manifest_digest"),
                    "after_manifest_digest": workspace_delta.get("after_manifest_digest"),
                    "created": list(workspace_delta.get("created") or ()),
                    "modified": list(workspace_delta.get("modified") or ()),
                    "deleted": list(workspace_delta.get("deleted") or ()),
                    "changed": list(workspace_delta.get("changed") or ()),
                }
            )

    official = _official_result(run_dir)
    task_status = str(task.get("status") or metadata.get("task_status") or "unknown")
    canonical_record_value = task_document.get(
        "canonical_task_record_observed",
        metadata.get("canonical_task_record_observed"),
    )
    canonical_task_record_observed = (
        bool(task.get("task_id"))
        if canonical_record_value is None
        else bool(canonical_record_value)
    )
    terminal_state_observed = task_status in {"completed", "failed", "cancelled"}
    run_transport = (
        task_document.get("run_transport")
        if isinstance(task_document.get("run_transport"), Mapping)
        else None
    )
    result = {
        "schema": "zyra.experiment-run-summary/v1",
        "instance_id": metadata.get("instance_id"),
        "system": metadata.get("system"),
        "analysis_class": selected_class,
        "efficacy_eligible": selected_class == "formal",
        "invalidation_reason": invalidation_reason if selected_class == "invalidated_development" else "",
        "task_id": task.get("task_id") or metadata.get("task_id"),
        "run_id": task.get("run_id"),
        "task_status": task_status,
        "started_at": metadata.get("started_at"),
        "ended_at": metadata.get("ended_at") or task.get("updated_at"),
        "elapsed_seconds": _seconds_between(
            metadata.get("started_at"), metadata.get("ended_at") or task.get("updated_at")
        ),
        "budget": {
            "max_total_tokens": metadata.get("max_total_tokens"),
            "max_turns": metadata.get("max_turns"),
            "deadline_minutes": metadata.get("deadline_minutes"),
        },
        "usage": (
            {**totals, "cost_usd": round(totals["cost_usd"], 8)}
            if seen_receipts
            else None
        ),
        "workspace_deltas": patches,
        "evidence_integrity": {
            "task_run_present": task_run_path.exists(),
            "canonical_task_record_observed": canonical_task_record_observed,
            "terminal_state_observed": terminal_state_observed,
            "run_transport_ok": (
                bool(run_transport.get("ok"))
                if run_transport is not None
                else task_run_path.exists()
            ),
            "run_transport_error": (
                str(run_transport.get("error") or "") if run_transport is not None else ""
            ),
        },
        "official_evaluator": official,
        "strict_success": bool(
            task_status == "completed"
            and canonical_task_record_observed
            and terminal_state_observed
            and official is not None
            and (
                official["raw"].get("resolved") is True
                or official["raw"].get("resolved_instances")
            )
        ),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--analysis-class", choices=sorted(ANALYSIS_CLASSES))
    parser.add_argument("--invalidation-reason", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = summarize(
        args.run_dir,
        analysis_class=args.analysis_class,
        invalidation_reason=args.invalidation_reason,
    )
    output = args.output or args.run_dir / "evidence-summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
