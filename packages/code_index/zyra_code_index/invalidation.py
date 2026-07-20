from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .models import CodeIndexBuildResult, StaleWorkspaceError
from .runtime import CodeIndexRuntime


class WorkspaceTransactionResolver(Protocol):
    def require_transaction(self, transaction_id: str) -> Any:
        ...

    def list_transactions(self, workspace_id: str) -> Sequence[Any]:
        ...


@dataclass(frozen=True, slots=True)
class PatchIndexReceipt:
    transaction_id: str
    workspace_id: str
    disposition: str
    changed_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]
    generation: int = 0
    build: CodeIndexBuildResult | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "workspace_id": self.workspace_id,
            "disposition": self.disposition,
            "changed_paths": list(self.changed_paths),
            "deleted_paths": list(self.deleted_paths),
            "generation": self.generation,
            "build": self.build.to_dict() if self.build else None,
            "reason": self.reason,
        }


class WorkspacePatchIndexBridge:
    """Dereference canonical patch transactions and invalidate CodeIndex.

    Workspace patch events may intentionally carry only transaction/receipt IDs.
    This bridge resolves the committed transaction from WorkspaceIntegrationStore
    before it derives paths and revisions.  Startup reconciliation scans the
    durable transaction list, so loss of an in-process event cannot strand the
    index.
    """

    def __init__(
        self,
        resolver: WorkspaceTransactionResolver,
        runtime_factory: Callable[[str], CodeIndexRuntime],
    ) -> None:
        self.resolver = resolver
        self.runtime_factory = runtime_factory

    def apply_transaction(
        self,
        transaction_id: str,
        *,
        rebuild: bool = True,
    ) -> PatchIndexReceipt:
        transaction = self.resolver.require_transaction(transaction_id)
        workspace_id = str(getattr(transaction, "workspace_id", ""))
        if not workspace_id:
            raise ValueError("workspace transaction has no workspace_id")
        phase = str(getattr(transaction, "phase", "")).casefold()
        if "commit" not in phase:
            return PatchIndexReceipt(
                transaction_id=transaction_id,
                workspace_id=workspace_id,
                disposition="ignored",
                changed_paths=(),
                deleted_paths=(),
                reason=f"transaction phase is not committed: {phase or 'unknown'}",
            )
        changed, deleted = self._paths(transaction)
        runtime = self.runtime_factory(workspace_id)
        if runtime.source.identity.workspace_id != workspace_id:
            raise StaleWorkspaceError("runtime factory returned another workspace")
        build = runtime.invalidate_patch(
            transaction_id=transaction_id,
            changed_paths=changed,
            deleted_paths=deleted,
            rebuild=rebuild,
        )
        if build is None:
            state = runtime.store.workspace_state(workspace_id)
            return PatchIndexReceipt(
                transaction_id=transaction_id,
                workspace_id=workspace_id,
                disposition="duplicate" if not rebuild else "recorded",
                changed_paths=changed,
                deleted_paths=deleted,
                generation=int(state.get("active_generation") or 0),
                reason="transaction invalidation already recorded" if not build else "",
            )
        return PatchIndexReceipt(
            transaction_id=transaction_id,
            workspace_id=workspace_id,
            disposition="rebuilt",
            changed_paths=changed,
            deleted_paths=deleted,
            generation=build.generation,
            build=build,
        )

    def apply_event(self, event: Mapping[str, Any], *, rebuild: bool = True) -> PatchIndexReceipt:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else event
        transaction_id = str(payload.get("transaction_id") or "")
        if not transaction_id:
            raise ValueError("workspace patch event has no transaction_id")
        return self.apply_transaction(transaction_id, rebuild=rebuild)

    def reconcile_startup(
        self,
        workspace_id: str,
        *,
        rebuild_once: bool = True,
    ) -> tuple[PatchIndexReceipt, ...]:
        runtime = self.runtime_factory(workspace_id)
        pending: list[Any] = []
        for transaction in self.resolver.list_transactions(workspace_id):
            phase = str(getattr(transaction, "phase", "")).casefold()
            transaction_id = str(getattr(transaction, "transaction_id", ""))
            if "commit" not in phase or not transaction_id:
                continue
            if not runtime.store.has_invalidation(workspace_id, transaction_id):
                pending.append(transaction)
        receipts: list[PatchIndexReceipt] = []
        for transaction in pending:
            transaction_id = str(getattr(transaction, "transaction_id"))
            receipts.append(self.apply_transaction(transaction_id, rebuild=False))
        if pending and rebuild_once:
            build = runtime.reconcile_invalidations()
            if build is not None:
                receipts.append(
                    PatchIndexReceipt(
                        transaction_id="startup-coalesced",
                        workspace_id=workspace_id,
                        disposition="rebuilt",
                        changed_paths=build.changed_paths,
                        deleted_paths=build.deleted_paths,
                        generation=build.generation,
                        build=build,
                        reason=f"coalesced {len(pending)} committed transactions",
                    )
                )
        return tuple(receipts)

    @staticmethod
    def _paths(transaction: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        changed: set[str] = set()
        deleted: set[str] = set()
        results = getattr(transaction, "path_results", ()) or ()
        for result in results:
            logical = str(
                getattr(result, "logical_path", "")
                or (result.get("logical_path") if isinstance(result, Mapping) else "")
                or ""
            )
            if not logical:
                continue
            operation = str(
                getattr(result, "operation", "")
                or (result.get("operation") if isinstance(result, Mapping) else "")
                or ""
            ).casefold()
            after_hash = str(
                getattr(result, "after_hash", "")
                or (result.get("after_hash") if isinstance(result, Mapping) else "")
                or ""
            )
            if "delete" in operation or not after_hash:
                deleted.add(logical)
            else:
                changed.add(logical)
        if not results:
            logical_paths = getattr(transaction, "logical_paths", ()) or ()
            changed.update(str(item) for item in logical_paths if str(item))
        return tuple(sorted(changed)), tuple(sorted(deleted))
