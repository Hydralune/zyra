from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..models import ObservationScope, canonical_json, digest_value, utc_now
from .contracts import CommitPhase, IntegrationCommitReceipt


class ObservationCommitError(RuntimeError):
    code = "browser_observation_commit_error"


class ObservationCommitConflict(ObservationCommitError):
    code = "browser_observation_commit_conflict"


@dataclass(frozen=True, slots=True)
class CommitFenceAuditIssue:
    code: str
    path: str
    summary: str
    commit_id: str = ""
    fatal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "summary": self.summary,
            "commit_id": self.commit_id,
            "fatal": self.fatal,
        }


@dataclass(frozen=True, slots=True)
class CommitFenceAuditReport:
    root: str
    scanned_files: int
    valid_receipts: int
    pending_receipts: int
    terminal_receipts: int
    issues: tuple[CommitFenceAuditIssue, ...]

    @property
    def ok(self) -> bool:
        return not any(item.fatal for item in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.commit-fence-audit.v1",
            "root": self.root,
            "scanned_files": self.scanned_files,
            "valid_receipts": self.valid_receipts,
            "pending_receipts": self.pending_receipts,
            "terminal_receipts": self.terminal_receipts,
            "issues": [item.to_dict() for item in self.issues],
            "ok": self.ok,
            "canonical_owner_changed": False,
            "recovery_planner_owner": "M1-07C",
        }


_PHASE_ORDER: dict[CommitPhase, int] = {
    CommitPhase.PREPARED: 0,
    CommitPhase.HISTORY_COMMITTED: 1,
    CommitPhase.ARTIFACTS_COMMITTED: 2,
    CommitPhase.EVENTS_PENDING: 3,
    CommitPhase.EVENTS_COMMITTED: 4,
    CommitPhase.CHECKPOINT_COMMITTED: 5,
    CommitPhase.FAILED: 6,
}


class ObservationCommitFence:
    """Durable delivery fence across history, artifact, event, checkpoint owners.

    This is an outbox receipt, not a cross-store transaction. It makes partial
    delivery explicit and retryable while each existing owner keeps custody.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()

    def prepare(
        self,
        scope: ObservationScope,
        *,
        source_event_ids: Sequence[str],
        action_receipt_ids: Sequence[str],
        artifact_ids: Sequence[str],
        input_metadata: Mapping[str, Any] | None = None,
    ) -> IntegrationCommitReceipt:
        input_digest = digest_value(
            {
                "scope": scope.to_dict(),
                "source_event_ids": list(source_event_ids),
                "action_receipt_ids": list(action_receipt_ids),
                "artifact_ids": list(artifact_ids),
                "input_metadata": dict(input_metadata or {}),
            }
        )
        commit_id = f"browser-observation-{scope.key}-{input_digest[7:23]}"
        with self._guard:
            existing = self.get(scope, commit_id)
            if existing is not None:
                if existing.input_digest != input_digest:
                    raise ObservationCommitConflict(
                        f"observation commit {commit_id!r} changed input digest"
                    )
                return existing
            receipt = IntegrationCommitReceipt(
                scope=scope,
                commit_id=commit_id,
                input_digest=input_digest,
                phase=CommitPhase.PREPARED,
            )
            self._write(receipt)
            return receipt

    def advance(
        self,
        receipt: IntegrationCommitReceipt,
        phase: CommitPhase,
        *,
        history_head_digest: str | None = None,
        event_ids: Sequence[str] | None = None,
        artifact_ids: Sequence[str] | None = None,
        recovery_input_ids: Sequence[str] | None = None,
        error: str = "",
    ) -> IntegrationCommitReceipt:
        with self._guard:
            current = self.get(receipt.scope, receipt.commit_id)
            if current is None:
                raise ObservationCommitConflict("observation commit receipt is missing")
            if current.input_digest != receipt.input_digest:
                raise ObservationCommitConflict("observation commit input digest changed")
            if current.phase == CommitPhase.FAILED and phase != CommitPhase.FAILED:
                raise ObservationCommitConflict("failed observation commit cannot advance")
            if (
                phase != CommitPhase.FAILED
                and _PHASE_ORDER[phase] < _PHASE_ORDER[current.phase]
            ):
                raise ObservationCommitConflict(
                    f"observation commit cannot regress from {current.phase} to {phase}"
                )
            updated = replace(
                current,
                phase=phase,
                history_head_digest=(
                    current.history_head_digest
                    if history_head_digest is None
                    else history_head_digest
                ),
                event_ids=(
                    current.event_ids
                    if event_ids is None
                    else tuple(dict.fromkeys(str(item) for item in event_ids if str(item)))
                ),
                artifact_ids=(
                    current.artifact_ids
                    if artifact_ids is None
                    else tuple(dict.fromkeys(str(item) for item in artifact_ids if str(item)))
                ),
                recovery_input_ids=(
                    current.recovery_input_ids
                    if recovery_input_ids is None
                    else tuple(
                        dict.fromkeys(
                            str(item) for item in recovery_input_ids if str(item)
                        )
                    )
                ),
                error=error,
                revision=current.revision + 1,
                updated_at=utc_now(),
            )
            self._write(updated)
            return updated

    def fail(
        self,
        receipt: IntegrationCommitReceipt,
        error: BaseException | str,
    ) -> IntegrationCommitReceipt:
        message = (
            str(error)
            if isinstance(error, str)
            else f"{type(error).__name__}: {error}"
        )
        return self.advance(receipt, CommitPhase.FAILED, error=message)

    def acknowledge_events(
        self,
        scope: ObservationScope,
        commit_id: str,
        *,
        committed_event_ids: Sequence[str],
    ) -> IntegrationCommitReceipt:
        """Acknowledge the EventLog owner only after its append succeeds.

        The fence records the exact event identities it expected before the API
        transaction began.  A caller cannot close the event phase with a
        partial append or with an unrelated batch that merely happens to be
        non-empty.
        """

        with self._guard:
            current = self.get(scope, commit_id)
            if current is None:
                raise ObservationCommitConflict("observation commit receipt is missing")
            if current.phase in {
                CommitPhase.EVENTS_COMMITTED,
                CommitPhase.CHECKPOINT_COMMITTED,
            }:
                return current
            if current.phase != CommitPhase.EVENTS_PENDING:
                raise ObservationCommitConflict(
                    f"event acknowledgement requires events_pending, got {current.phase}"
                )
            committed = frozenset(
                str(item) for item in committed_event_ids if str(item)
            )
            missing = tuple(item for item in current.event_ids if item not in committed)
            if missing:
                raise ObservationCommitConflict(
                    "event acknowledgement is missing expected identities: "
                    + ", ".join(missing[:10])
                )
            return self.advance(current, CommitPhase.EVENTS_COMMITTED)

    def acknowledge_checkpoint(
        self,
        scope: ObservationScope,
        commit_id: str,
    ) -> IntegrationCommitReceipt:
        """Close a delivery only after TaskState checkpoint persistence succeeds."""

        with self._guard:
            current = self.get(scope, commit_id)
            if current is None:
                raise ObservationCommitConflict("observation commit receipt is missing")
            if current.phase == CommitPhase.CHECKPOINT_COMMITTED:
                return current
            if current.phase != CommitPhase.EVENTS_COMMITTED:
                raise ObservationCommitConflict(
                    "checkpoint acknowledgement requires events_committed, "
                    f"got {current.phase}"
                )
            return self.advance(current, CommitPhase.CHECKPOINT_COMMITTED)

    def get(
        self,
        scope: ObservationScope,
        commit_id: str,
    ) -> IntegrationCommitReceipt | None:
        path = self._path(scope, commit_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return _receipt_from_mapping(value)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ObservationCommitError(
                f"invalid observation commit receipt {commit_id!r}: {exc}"
            ) from exc

    def list(
        self,
        *,
        task_id: str = "",
        scope: ObservationScope | None = None,
        phases: Sequence[CommitPhase] = (),
    ) -> tuple[IntegrationCommitReceipt, ...]:
        roots = (
            (self.root / scope.task_id / scope.key,)
            if scope is not None
            else tuple(
                path
                for task_root in self.root.iterdir()
                if task_root.is_dir() and (not task_id or task_root.name == task_id)
                for path in task_root.iterdir()
                if path.is_dir()
            )
        )
        allowed = frozenset(phases)
        output: list[IntegrationCommitReceipt] = []
        for root in roots:
            for path in root.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    receipt = _receipt_from_mapping(value)
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if scope is not None and receipt.scope != scope:
                    continue
                if allowed and receipt.phase not in allowed:
                    continue
                output.append(receipt)
        return tuple(
            sorted(output, key=lambda item: (item.updated_at, item.commit_id))
        )

    def pending(
        self,
        *,
        task_id: str = "",
        scope: ObservationScope | None = None,
    ) -> tuple[IntegrationCommitReceipt, ...]:
        terminal = {CommitPhase.CHECKPOINT_COMMITTED, CommitPhase.FAILED}
        return tuple(
            item
            for item in self.list(task_id=task_id, scope=scope)
            if item.phase not in terminal
        )

    def audit(
        self,
        *,
        task_id: str = "",
        scope: ObservationScope | None = None,
    ) -> CommitFenceAuditReport:
        """Validate durable receipt files, monotonic phases and path custody.

        Unlike ``list``, auditing does not silently skip corrupt JSON.  The
        report is used by the commits API so restart diagnostics can distinguish
        an intentionally pending delivery from a damaged receipt.
        """

        issues: list[CommitFenceAuditIssue] = []
        receipts: list[IntegrationCommitReceipt] = []
        seen: set[tuple[str, str]] = set()
        paths = self._candidate_paths(task_id=task_id, scope=scope)
        for path in paths:
            relative = str(path.relative_to(self.root)).replace("\\", "/")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, Mapping):
                    raise TypeError("receipt JSON root is not an object")
                receipt = _receipt_from_mapping(value)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                issues.append(
                    CommitFenceAuditIssue(
                        code="commit_receipt_corrupt",
                        path=relative,
                        summary=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            identity = (receipt.scope.key, receipt.commit_id)
            if identity in seen:
                issues.append(
                    CommitFenceAuditIssue(
                        code="commit_receipt_duplicate",
                        path=relative,
                        commit_id=receipt.commit_id,
                        summary="commit identity appears in more than one receipt file",
                    )
                )
                continue
            seen.add(identity)
            receipts.append(receipt)
            if path.stem != receipt.commit_id:
                issues.append(
                    CommitFenceAuditIssue(
                        code="commit_path_identity_mismatch",
                        path=relative,
                        commit_id=receipt.commit_id,
                        summary="receipt file name differs from its commit identity",
                    )
                )
            expected_parent = self.root / receipt.scope.task_id / receipt.scope.key
            if path.parent.resolve() != expected_parent.resolve():
                issues.append(
                    CommitFenceAuditIssue(
                        code="commit_scope_path_mismatch",
                        path=relative,
                        commit_id=receipt.commit_id,
                        summary="receipt is stored outside its task/scope custody path",
                    )
                )
            minimum_revision = _PHASE_ORDER[receipt.phase]
            if (
                receipt.phase != CommitPhase.FAILED
                and receipt.revision < minimum_revision
            ):
                issues.append(
                    CommitFenceAuditIssue(
                        code="commit_phase_revision_regressed",
                        path=relative,
                        commit_id=receipt.commit_id,
                        summary=(
                            f"phase {receipt.phase} requires revision at least "
                            f"{minimum_revision}, got {receipt.revision}"
                        ),
                    )
                )
            if (
                receipt.phase != CommitPhase.FAILED
                and
                _PHASE_ORDER[receipt.phase]
                >= _PHASE_ORDER[CommitPhase.HISTORY_COMMITTED]
                and not receipt.history_head_digest
            ):
                issues.append(
                    CommitFenceAuditIssue(
                        code="commit_history_head_missing",
                        path=relative,
                        commit_id=receipt.commit_id,
                        summary="committed history phase lacks a durable head digest",
                    )
                )
            if receipt.phase in {
                CommitPhase.EVENTS_PENDING,
                CommitPhase.EVENTS_COMMITTED,
                CommitPhase.CHECKPOINT_COMMITTED,
            } and not receipt.event_ids:
                issues.append(
                    CommitFenceAuditIssue(
                        code="commit_event_identity_missing",
                        path=relative,
                        commit_id=receipt.commit_id,
                        summary="event delivery phase lacks expected event identities",
                    )
                )
        terminal = sum(1 for item in receipts if item.terminal)
        return CommitFenceAuditReport(
            root=str(self.root),
            scanned_files=len(paths),
            valid_receipts=len(receipts),
            pending_receipts=len(receipts) - terminal,
            terminal_receipts=terminal,
            issues=tuple(issues),
        )

    def projection(
        self,
        *,
        task_id: str = "",
        scope: ObservationScope | None = None,
    ) -> dict[str, Any]:
        receipts = self.list(task_id=task_id, scope=scope)
        phases: dict[str, int] = {}
        for item in receipts:
            phases[str(item.phase)] = phases.get(str(item.phase), 0) + 1
        pending = tuple(
            item
            for item in receipts
            if item.phase not in {CommitPhase.CHECKPOINT_COMMITTED, CommitPhase.FAILED}
        )
        return {
            "schema": "zyra.browser-observability.commit-fence.v1",
            "receipt_count": len(receipts),
            "pending_count": len(pending),
            "failed_count": phases.get(str(CommitPhase.FAILED), 0),
            "phases": dict(sorted(phases.items())),
            "pending": [item.to_dict() for item in pending[-100:]],
            "owners": {
                "history": "BrowserHistoryStore",
                "artifacts": "LocalArtifactStore",
                "events": "EventLog",
                "checkpoint": "SQLiteStore/TaskState",
            },
            "atomic_across_owners": False,
            "idempotency_fence": True,
            "audit": self.audit(task_id=task_id, scope=scope).to_dict(),
        }

    def _candidate_paths(
        self,
        *,
        task_id: str = "",
        scope: ObservationScope | None = None,
    ) -> tuple[Path, ...]:
        if scope is not None:
            roots = (self.root / scope.task_id / scope.key,)
        elif task_id:
            task_root = self.root / task_id
            roots = (
                tuple(path for path in task_root.iterdir() if path.is_dir())
                if task_root.is_dir()
                else ()
            )
        else:
            roots = tuple(
                scope_root
                for task_root in self.root.iterdir()
                if task_root.is_dir()
                for scope_root in task_root.iterdir()
                if scope_root.is_dir()
            )
        return tuple(
            sorted(
                path
                for root in roots
                if root.is_dir()
                for path in root.glob("*.json")
                if path.is_file()
            )
        )

    def _path(self, scope: ObservationScope, commit_id: str) -> Path:
        if not commit_id.startswith("browser-observation-"):
            raise ValueError("invalid observation commit identity")
        return self.root / scope.task_id / scope.key / f"{commit_id}.json"

    def _write(self, receipt: IntegrationCommitReceipt) -> None:
        path = self._path(receipt.scope, receipt.commit_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        payload = (canonical_json(receipt.to_dict()) + "\n").encode("utf-8")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _receipt_from_mapping(value: Mapping[str, Any]) -> IntegrationCommitReceipt:
    scope_value = value["scope"]
    scope = ObservationScope(
        run_id=str(scope_value["run_id"]),
        task_id=str(scope_value["task_id"]),
        node_id=str(scope_value.get("node_id") or ""),
        browser_session_id=str(scope_value["browser_session_id"]),
        canonical_session_id=str(scope_value.get("canonical_session_id") or ""),
        worker_request_id=str(scope_value["worker_request_id"]),
    )
    return IntegrationCommitReceipt(
        scope=scope,
        commit_id=str(value["commit_id"]),
        input_digest=str(value["input_digest"]),
        phase=CommitPhase(str(value["phase"])),
        history_head_digest=str(value.get("history_head_digest") or ""),
        event_ids=tuple(str(item) for item in value.get("event_ids", ())),
        artifact_ids=tuple(str(item) for item in value.get("artifact_ids", ())),
        recovery_input_ids=tuple(
            str(item) for item in value.get("recovery_input_ids", ())
        ),
        error=str(value.get("error") or ""),
        revision=int(value.get("revision") or 0),
        created_at=str(value.get("created_at") or utc_now()),
        updated_at=str(value.get("updated_at") or utc_now()),
    )
