from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .canonical import (
    atomic_json,
    bounded_integer,
    canonicalize,
    digest,
    identity,
    invalid,
    mapping,
    new_identity,
    parse_utc,
    resolve_within,
    utc_now,
)
from .models import CampaignPhase, CellPhase


_TERMINAL_CELL_PHASES = {
    CellPhase.ADMITTED.value,
    CellPhase.REJECTED.value,
    CellPhase.FAILED.value,
    CellPhase.CANCELLED.value,
}
_TERMINAL_CAMPAIGN_PHASES = {
    CampaignPhase.SUCCEEDED.value,
    CampaignPhase.FAILED.value,
    CampaignPhase.CANCELLED.value,
    CampaignPhase.FROZEN.value,
}
_CAMPAIGN_TRANSITIONS = {
    CampaignPhase.PLANNED.value: {
        CampaignPhase.RUNNING.value,
        CampaignPhase.CANCELLED.value,
        CampaignPhase.FAILED.value,
    },
    CampaignPhase.RUNNING.value: {
        CampaignPhase.VERIFYING.value,
        CampaignPhase.CANCELLED.value,
        CampaignPhase.FAILED.value,
    },
    CampaignPhase.VERIFYING.value: {
        CampaignPhase.REPORTING.value,
        CampaignPhase.FAILED.value,
    },
    CampaignPhase.REPORTING.value: {
        CampaignPhase.SUCCEEDED.value,
        CampaignPhase.FAILED.value,
    },
    CampaignPhase.SUCCEEDED.value: {CampaignPhase.FROZEN.value},
    CampaignPhase.FAILED.value: set(),
    CampaignPhase.CANCELLED.value: set(),
    CampaignPhase.FROZEN.value: set(),
}


class BenchmarkStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()

    def create(self, campaign: Mapping[str, Any]) -> dict[str, Any]:
        campaign_id = identity(campaign.get("campaign_id"), "campaign id")
        campaign_root = self._campaign_root(campaign_id)
        with self._locked(campaign_id):
            if campaign_root.exists():
                raise invalid(
                    "benchmark_campaign_already_exists",
                    "Benchmark campaign already exists.",
                    phase="store",
                    detail={"campaign_id": campaign_id},
                )
            campaign_root.mkdir(parents=True)
            (campaign_root / "results").mkdir()
            (campaign_root / "receipts").mkdir()
            now = utc_now()
            state = {
                "schema": "zyra.live-benchmark-store-state/v1",
                "campaign_id": campaign_id,
                "phase": str(campaign.get("phase") or CampaignPhase.PLANNED.value),
                "revision": 1,
                "cancel_requested": False,
                "frozen": False,
                "campaign_digest": digest(campaign),
                "created_at": now,
                "updated_at": now,
                "cell_states": {
                    identity(item.get("cell_id"), "cell id"): {
                        "phase": CellPhase.PENDING.value,
                        "attempt": 0,
                        "lease": None,
                        "result_digest": "",
                        "updated_at": now,
                    }
                    for item in campaign.get("cells") or []
                },
                "journal_head": "0" * 64,
                "journal_length": 0,
            }
            atomic_json(campaign_root / "campaign.json", campaign)
            self._write_state(campaign_id, state)
            self._append_journal(
                campaign_id,
                state,
                event_type="campaign-created",
                payload={"campaign_digest": state["campaign_digest"]},
            )
            return self.state(campaign_id)

    def state(self, campaign_id: str) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        with self._locked(selected):
            return self._load_json(self._campaign_root(selected) / "state.json")

    def campaign(self, campaign_id: str) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        with self._locked(selected):
            return self._load_json(self._campaign_root(selected) / "campaign.json")

    def transition(
        self,
        campaign_id: str,
        phase: CampaignPhase | str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        target = CampaignPhase(str(phase)).value
        with self._locked(selected):
            state = self._load_state(selected)
            self._require_mutable(state)
            current = str(state["phase"])
            if target == current:
                return state
            allowed = _CAMPAIGN_TRANSITIONS.get(current, set())
            if target not in allowed:
                raise invalid(
                    "benchmark_campaign_transition_invalid",
                    "Campaign phase transition is not allowed.",
                    phase="store",
                    detail={"current": current, "target": target},
                )
            state["phase"] = target
            self._bump(state)
            self._write_state(selected, state)
            self._append_journal(
                selected,
                state,
                event_type="campaign-transitioned",
                payload={"from": current, "to": target, "reason": reason},
            )
            return self._load_state(selected)

    def lease(
        self,
        campaign_id: str,
        cell_id: str,
        *,
        worker_id: str,
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        selected_cell = identity(cell_id, "cell id")
        selected_worker = identity(worker_id, "worker id")
        ttl = bounded_integer(
            ttl_seconds,
            "lease TTL seconds",
            minimum=1,
            maximum=24 * 60 * 60,
        )
        with self._locked(selected):
            state = self._load_state(selected)
            self._require_mutable(state)
            if state["phase"] != CampaignPhase.RUNNING.value:
                raise invalid(
                    "benchmark_campaign_not_running",
                    "Cell can only be leased while campaign is running.",
                    phase="store",
                )
            cell = self._cell_state(state, selected_cell)
            if cell["phase"] in _TERMINAL_CELL_PHASES:
                raise invalid(
                    "benchmark_cell_terminal",
                    "Completed benchmark cell cannot be leased.",
                    phase="store",
                    detail={"cell_id": selected_cell, "phase": cell["phase"]},
                )
            existing = cell.get("lease")
            if existing and not lease_expired(existing):
                raise invalid(
                    "benchmark_cell_already_leased",
                    "Benchmark cell has an active lease.",
                    phase="store",
                    detail={
                        "cell_id": selected_cell,
                        "lease_id": existing["lease_id"],
                        "worker_id": existing["worker_id"],
                    },
                )
            now = datetime.now(UTC)
            lease = {
                "lease_id": new_identity("lease"),
                "cell_id": selected_cell,
                "worker_id": selected_worker,
                "issued_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(seconds=ttl))
                .isoformat()
                .replace("+00:00", "Z"),
                "attempt": int(cell["attempt"]) + 1,
            }
            lease["lease_digest"] = digest(lease)
            cell["phase"] = CellPhase.LEASED.value
            cell["attempt"] = lease["attempt"]
            cell["lease"] = lease
            cell["updated_at"] = utc_now()
            self._bump(state)
            self._write_state(selected, state)
            self._append_journal(
                selected,
                state,
                event_type="cell-leased",
                payload=lease,
            )
            return dict(lease)

    def start_cell(
        self,
        campaign_id: str,
        cell_id: str,
        *,
        lease_id: str,
        worker_id: str,
    ) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        selected_cell = identity(cell_id, "cell id")
        with self._locked(selected):
            state = self._load_state(selected)
            self._require_mutable(state)
            cell = self._cell_state(state, selected_cell)
            lease = self._validate_lease(
                cell,
                lease_id=lease_id,
                worker_id=worker_id,
            )
            if cell["phase"] != CellPhase.LEASED.value:
                raise invalid(
                    "benchmark_cell_not_leased",
                    "Benchmark cell cannot start from its current phase.",
                    phase="store",
                    detail={"cell_id": selected_cell, "phase": cell["phase"]},
                )
            cell["phase"] = CellPhase.RUNNING.value
            cell["updated_at"] = utc_now()
            self._bump(state)
            self._write_state(selected, state)
            self._append_journal(
                selected,
                state,
                event_type="cell-started",
                payload={"cell_id": selected_cell, "lease_id": lease["lease_id"]},
            )
            return dict(cell)

    def complete_cell(
        self,
        campaign_id: str,
        cell_id: str,
        *,
        lease_id: str,
        worker_id: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        selected_cell = identity(cell_id, "cell id")
        with self._locked(selected):
            state = self._load_state(selected)
            self._require_mutable(state)
            cell = self._cell_state(state, selected_cell)
            lease = self._validate_lease(
                cell,
                lease_id=lease_id,
                worker_id=worker_id,
            )
            if cell["phase"] != CellPhase.RUNNING.value:
                raise invalid(
                    "benchmark_cell_not_running",
                    "Benchmark cell must be running before completion.",
                    phase="store",
                    detail={"cell_id": selected_cell, "phase": cell["phase"]},
                )
            if result.get("cell", {}).get("cell_id") != selected_cell:
                raise invalid(
                    "benchmark_result_cell_mismatch",
                    "Benchmark result belongs to a different cell.",
                    phase="store",
                )
            result_digest = digest(result)
            result_path = self._result_path(selected, selected_cell)
            if result_path.exists():
                raise invalid(
                    "benchmark_result_already_exists",
                    "Benchmark cell result cannot be overwritten.",
                    phase="store",
                    detail={"cell_id": selected_cell},
                )
            atomic_json(result_path, result)
            cell["phase"] = CellPhase.ADMITTED.value
            cell["result_digest"] = result_digest
            cell["lease"] = None
            cell["updated_at"] = utc_now()
            self._bump(state)
            self._write_state(selected, state)
            self._append_journal(
                selected,
                state,
                event_type="cell-completed",
                payload={
                    "cell_id": selected_cell,
                    "lease_id": lease["lease_id"],
                    "result_digest": result_digest,
                },
            )
            return dict(cell)

    def fail_cell(
        self,
        campaign_id: str,
        cell_id: str,
        *,
        lease_id: str,
        worker_id: str,
        failure: Mapping[str, Any],
        retryable: bool,
    ) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        selected_cell = identity(cell_id, "cell id")
        with self._locked(selected):
            state = self._load_state(selected)
            self._require_mutable(state)
            cell = self._cell_state(state, selected_cell)
            lease = self._validate_lease(
                cell,
                lease_id=lease_id,
                worker_id=worker_id,
                allow_expired=True,
            )
            cell["phase"] = (
                CellPhase.PENDING.value if retryable else CellPhase.FAILED.value
            )
            cell["lease"] = None
            cell["updated_at"] = utc_now()
            failure_value = canonicalize(failure)
            self._bump(state)
            self._write_state(selected, state)
            self._append_journal(
                selected,
                state,
                event_type="cell-failed",
                payload={
                    "cell_id": selected_cell,
                    "lease_id": lease["lease_id"],
                    "retryable": retryable,
                    "failure": failure_value,
                },
            )
            return dict(cell)

    def request_cancel(self, campaign_id: str, *, reason: str) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        with self._locked(selected):
            state = self._load_state(selected)
            self._require_mutable(state)
            state["cancel_requested"] = True
            state["cancel_reason"] = str(reason)[:4096]
            self._bump(state)
            self._write_state(selected, state)
            self._append_journal(
                selected,
                state,
                event_type="campaign-cancel-requested",
                payload={"reason": state["cancel_reason"]},
            )
            return self._load_state(selected)

    def recover_expired_leases(self, campaign_id: str) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        with self._locked(selected):
            state = self._load_state(selected)
            self._require_mutable(state)
            recovered: list[dict[str, Any]] = []
            for cell_id, cell in state["cell_states"].items():
                lease = cell.get("lease")
                if not lease or not lease_expired(lease):
                    continue
                if cell["phase"] not in {
                    CellPhase.LEASED.value,
                    CellPhase.RUNNING.value,
                }:
                    continue
                recovered.append(
                    {
                        "cell_id": cell_id,
                        "lease_id": lease["lease_id"],
                        "worker_id": lease["worker_id"],
                        "attempt": lease["attempt"],
                    }
                )
                cell["phase"] = CellPhase.PENDING.value
                cell["lease"] = None
                cell["updated_at"] = utc_now()
            if recovered:
                self._bump(state)
                self._write_state(selected, state)
                self._append_journal(
                    selected,
                    state,
                    event_type="expired-leases-recovered",
                    payload={"leases": recovered},
                )
            return {
                "schema": "zyra.live-benchmark-lease-recovery/v1",
                "campaign_id": selected,
                "recovered_count": len(recovered),
                "recovered": recovered,
                "state_revision": state["revision"],
                "verified_at": utc_now(),
            }

    def pending_cells(self, campaign_id: str) -> tuple[str, ...]:
        state = self.state(campaign_id)
        return tuple(
            cell_id
            for cell_id, cell in sorted(state["cell_states"].items())
            if cell["phase"] == CellPhase.PENDING.value
        )

    def results(self, campaign_id: str) -> tuple[dict[str, Any], ...]:
        selected = identity(campaign_id, "campaign id")
        state = self.state(selected)
        output: list[dict[str, Any]] = []
        for cell_id, cell in sorted(state["cell_states"].items()):
            if cell["phase"] != CellPhase.ADMITTED.value:
                continue
            result = self._load_json(self._result_path(selected, cell_id))
            observed = digest(result)
            if observed != cell["result_digest"]:
                raise invalid(
                    "benchmark_result_digest_mismatch",
                    "Stored benchmark result was modified after admission.",
                    phase="store",
                    detail={
                        "cell_id": cell_id,
                        "expected": cell["result_digest"],
                        "observed": observed,
                    },
                )
            output.append(result)
        return tuple(output)

    def freeze(
        self,
        campaign_id: str,
        *,
        report_digest: str,
        evidence_index_digest: str,
    ) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        with self._locked(selected):
            state = self._load_state(selected)
            if state["phase"] != CampaignPhase.SUCCEEDED.value:
                raise invalid(
                    "benchmark_campaign_not_succeeded",
                    "Only a succeeded campaign can be frozen.",
                    phase="store",
                )
            incomplete = sorted(
                cell_id
                for cell_id, cell in state["cell_states"].items()
                if cell["phase"] != CellPhase.ADMITTED.value
            )
            if incomplete:
                raise invalid(
                    "benchmark_campaign_cells_incomplete",
                    "Campaign cannot freeze with incomplete cells.",
                    phase="store",
                    detail={"cell_ids": incomplete},
                )
            state["phase"] = CampaignPhase.FROZEN.value
            state["frozen"] = True
            state["report_digest"] = report_digest
            state["evidence_index_digest"] = evidence_index_digest
            state["frozen_at"] = utc_now()
            self._bump(state)
            self._write_state(selected, state)
            self._append_journal(
                selected,
                state,
                event_type="campaign-frozen",
                payload={
                    "report_digest": report_digest,
                    "evidence_index_digest": evidence_index_digest,
                },
            )
            return self._load_state(selected)

    def verify_journal(self, campaign_id: str) -> dict[str, Any]:
        selected = identity(campaign_id, "campaign id")
        path = self._campaign_root(selected) / "journal.jsonl"
        state = self.state(selected)
        previous = "0" * 64
        count = 0
        if not path.exists():
            raise invalid(
                "benchmark_journal_missing",
                "Benchmark transaction journal is missing.",
                phase="store",
            )
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    raise invalid(
                        "benchmark_journal_json_invalid",
                        "Benchmark journal contains invalid JSON.",
                        phase="store",
                        detail={"line": line_number},
                    ) from error
                declared = str(entry.pop("entry_digest", ""))
                if entry.get("previous_digest") != previous:
                    raise invalid(
                        "benchmark_journal_chain_broken",
                        "Benchmark journal previous digest is invalid.",
                        phase="store",
                        detail={"line": line_number},
                    )
                observed = digest(entry)
                if declared != observed:
                    raise invalid(
                        "benchmark_journal_entry_tampered",
                        "Benchmark journal entry digest is invalid.",
                        phase="store",
                        detail={"line": line_number},
                    )
                previous = observed
                count += 1
        if previous != state["journal_head"] or count != state["journal_length"]:
            raise invalid(
                "benchmark_journal_state_mismatch",
                "Benchmark state does not match the transaction journal.",
                phase="store",
                detail={
                    "journal_head": previous,
                    "state_head": state["journal_head"],
                    "journal_length": count,
                    "state_length": state["journal_length"],
                },
            )
        receipt = {
            "schema": "zyra.live-benchmark-journal-verification/v1",
            "valid": True,
            "campaign_id": selected,
            "entry_count": count,
            "journal_head": previous,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _append_journal(
        self,
        campaign_id: str,
        state: dict[str, Any],
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        entry = {
            "schema": "zyra.live-benchmark-journal-entry/v1",
            "campaign_id": campaign_id,
            "sequence": int(state["journal_length"]) + 1,
            "event_type": event_type,
            "payload": canonicalize(payload),
            "state_revision": state["revision"],
            "previous_digest": state["journal_head"],
            "created_at": utc_now(),
        }
        entry["entry_digest"] = digest(entry)
        path = self._campaign_root(campaign_id) / "journal.jsonl"
        with path.open("ab") as handle:
            payload_bytes = (
                json.dumps(entry, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            handle.write(payload_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        state["journal_head"] = entry["entry_digest"]
        state["journal_length"] = entry["sequence"]
        self._write_state(campaign_id, state)

    def _validate_lease(
        self,
        cell: Mapping[str, Any],
        *,
        lease_id: str,
        worker_id: str,
        allow_expired: bool = False,
    ) -> Mapping[str, Any]:
        lease = cell.get("lease")
        if not isinstance(lease, Mapping):
            raise invalid(
                "benchmark_cell_lease_missing",
                "Benchmark cell has no active lease.",
                phase="store",
            )
        selected_lease = identity(lease_id, "lease id")
        selected_worker = identity(worker_id, "worker id")
        if lease.get("lease_id") != selected_lease:
            raise invalid(
                "benchmark_cell_lease_stale",
                "Benchmark cell lease identity is stale.",
                phase="store",
            )
        if lease.get("worker_id") != selected_worker:
            raise invalid(
                "benchmark_cell_lease_owner_mismatch",
                "Benchmark cell lease belongs to another worker.",
                phase="store",
            )
        projection = dict(lease)
        declared = projection.pop("lease_digest", "")
        if declared != digest(projection):
            raise invalid(
                "benchmark_cell_lease_tampered",
                "Benchmark cell lease digest is invalid.",
                phase="store",
            )
        if not allow_expired and lease_expired(lease):
            raise invalid(
                "benchmark_cell_lease_expired",
                "Benchmark cell lease has expired.",
                phase="store",
            )
        return lease

    def _cell_state(self, state: Mapping[str, Any], cell_id: str) -> dict[str, Any]:
        try:
            return state["cell_states"][cell_id]
        except KeyError as error:
            raise invalid(
                "benchmark_cell_not_found",
                "Benchmark cell does not exist.",
                phase="store",
                detail={"cell_id": cell_id},
            ) from error

    def _require_mutable(self, state: Mapping[str, Any]) -> None:
        if state.get("frozen") is True or state.get("phase") == CampaignPhase.FROZEN.value:
            raise invalid(
                "benchmark_campaign_frozen",
                "Frozen benchmark campaign is immutable.",
                phase="store",
            )
        if state.get("phase") in {
            CampaignPhase.FAILED.value,
            CampaignPhase.CANCELLED.value,
        }:
            raise invalid(
                "benchmark_campaign_terminal",
                "Terminal benchmark campaign is immutable.",
                phase="store",
            )

    def _load_state(self, campaign_id: str) -> dict[str, Any]:
        return self._load_json(self._campaign_root(campaign_id) / "state.json")

    def _write_state(self, campaign_id: str, state: Mapping[str, Any]) -> None:
        atomic_json(self._campaign_root(campaign_id) / "state.json", state)

    def _load_json(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise invalid(
                "benchmark_store_record_missing",
                "Benchmark store record does not exist.",
                phase="store",
                detail={"path": str(path)},
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise invalid(
                "benchmark_store_record_invalid",
                "Benchmark store record cannot be decoded.",
                phase="store",
                detail={"path": str(path)},
            ) from error
        return dict(mapping(raw, "benchmark store record"))

    def _campaign_root(self, campaign_id: str) -> Path:
        return resolve_within(identity(campaign_id, "campaign id"), self.root, "campaign")

    def _result_path(self, campaign_id: str, cell_id: str) -> Path:
        return resolve_within(
            Path(campaign_id) / "results" / f"{identity(cell_id, 'cell id')}.json",
            self.root,
            "benchmark result",
        )

    def _bump(self, state: dict[str, Any]) -> None:
        state["revision"] = int(state["revision"]) + 1
        state["updated_at"] = utc_now()

    @contextmanager
    def _locked(self, campaign_id: str) -> Iterator[None]:
        with self._thread_lock:
            yield


def lease_expired(lease: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    return parse_utc(lease.get("expires_at"), "lease expires_at") <= current
