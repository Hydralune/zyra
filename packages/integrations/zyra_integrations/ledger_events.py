from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ledger_audit import LedgerAuditReport
from .ledger_models import now_iso, to_jsonable
from .ledger_store import LedgerMutation


SYSTEM_RUN_ID = "system"
SYSTEM_TASK_ID = "internalization_ledger"
SYSTEM_NODE_ID = "ledger-audit"


def audit_event_payload(report: LedgerAuditReport, *, trigger: str = "manual") -> dict[str, Any]:
    return {
        "event_type": "system_notice",
        "run_id": SYSTEM_RUN_ID,
        "task_id": SYSTEM_TASK_ID,
        "node_id": SYSTEM_NODE_ID,
        "created_at": now_iso(),
        "payload": {
            "integration_ledger_audit": {
                "trigger": trigger,
                "ok": report.ok,
                "disposition": str(report.disposition),
                "total_entries": report.total_entries,
                "finding_count": report.finding_count,
                "error_count": report.error_count,
                "blocker_count": report.blocker_count,
                "warning_count": report.warning_count,
                "strict": report.strict,
                "summary": report.summary,
                "findings": [finding.to_dict() for finding in report.findings[:200]],
            }
        },
    }


def mutation_event_payload(mutation: LedgerMutation, *, trigger: str = "manual") -> dict[str, Any]:
    return {
        "event_type": "system_notice",
        "run_id": SYSTEM_RUN_ID,
        "task_id": SYSTEM_TASK_ID,
        "node_id": "ledger-update",
        "created_at": now_iso(),
        "payload": {
            "integration_ledger_update": {
                "trigger": trigger,
                "action": mutation.action,
                "ledger_id": mutation.ledger_id,
                "before": mutation.before,
                "after": mutation.after,
            }
        },
    }


def append_jsonl_event(payload: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return target


def event_record_from_audit(report: LedgerAuditReport, *, trigger: str = "manual") -> Any:
    from zyra_core import EventRecord, EventType

    return EventRecord(
        run_id=SYSTEM_RUN_ID,
        task_id=SYSTEM_TASK_ID,
        event_type=EventType.SYSTEM_NOTICE,
        node_id=SYSTEM_NODE_ID,
        payload=audit_event_payload(report, trigger=trigger)["payload"],
    )


def event_record_from_mutation(mutation: LedgerMutation, *, trigger: str = "manual") -> Any:
    from zyra_core import EventRecord, EventType

    return EventRecord(
        run_id=SYSTEM_RUN_ID,
        task_id=SYSTEM_TASK_ID,
        event_type=EventType.SYSTEM_NOTICE,
        node_id="ledger-update",
        payload=mutation_event_payload(mutation, trigger=trigger)["payload"],
    )
