from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


E02_RECEIPT_VERSION = "zyra.e02-typescript-permission-receipt/v1"


class TypeScriptPermissionReceiptError(RuntimeError):
    """Raised when a TypeScript decision receipt cannot authorize a physical effect."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TypeScriptPermissionBinding:
    run_id: str
    task_id: str
    session_id: str
    session_revision: int
    worker_request_id: str
    tool_call_id: str
    tool_name: str
    tool_namespace: str
    server_name: str
    operation: str
    workspace_root: str
    arguments_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "session_revision": self.session_revision,
            "worker_request_id": self.worker_request_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_namespace": self.tool_namespace,
            "server_name": self.server_name,
            "operation": self.operation,
            "workspace_root": self.workspace_root,
            "arguments_digest": self.arguments_digest,
        }


@dataclass(frozen=True, slots=True)
class TypeScriptExecutionPermit:
    permit_id: str
    decision_id: str
    request_id: str
    binding: TypeScriptPermissionBinding
    policy_revision: int
    mode_revision: int
    receipt_digest: str
    physical_arguments_digest: str
    canonical_owner: str = "typescript"

    @property
    def arguments_digest(self) -> str:
        return self.binding.arguments_digest


class TypeScriptPermissionReceiptPort:
    """Narrow TypeScript-to-Python effect port.

    The port does not select allow/deny/ask and does not retain a policy, rule
    index, approval queue, or continuation state machine.  It only verifies an
    already committed TypeScript allow receipt against the physical call and
    consumes the resulting permit once at the final side-effect boundary.
    """

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        session_id: str,
        worker_request_id: str,
        workspace_root: str | Path,
    ) -> None:
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.session_id = str(session_id)
        self.worker_request_id = str(worker_request_id)
        self.workspace_root = str(Path(workspace_root).resolve())
        self._issued: dict[str, TypeScriptExecutionPermit] = {}
        self._consumed: set[str] = set()
        self._lock = threading.RLock()

    def accept(
        self,
        decision_value: Mapping[str, Any],
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        namespace: str,
        server_id: str,
        operation: str,
    ) -> TypeScriptExecutionPermit:
        decision = dict(decision_value)
        owner = _text(decision, "canonicalOwner", "canonical_owner")
        if owner != "typescript":
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_owner_invalid",
                "physical execution requires a canonical TypeScript decision receipt",
            )
        if _text(decision, "effect") != "allow":
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_not_allowed",
                "only a committed TypeScript allow receipt can reach the physical executor",
            )
        decision_id = _text(decision, "decisionId", "decision_id")
        if not decision_id:
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_identity_missing",
                "TypeScript permission receipt is missing decision identity",
            )
        binding = _mapping(decision, "requestBinding", "request_binding")
        typescript_arguments_digest = str(binding.get("arguments_digest") or "")
        if not typescript_arguments_digest:
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_arguments_digest_missing",
                "TypeScript permission receipt is missing its canonical arguments digest",
            )
        expected = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "tool_call_id": str(tool_call_id),
            "tool_name": str(tool_name),
            "namespace": str(namespace),
            "server_id": str(server_id),
            "operation": str(operation),
        }
        for key, value in expected.items():
            actual = str(binding.get(key) or "")
            if actual != value:
                raise TypeScriptPermissionReceiptError(
                    "e02_receipt_binding_mismatch",
                    f"TypeScript permission receipt binding mismatch for {key}",
                )
        final_digest = _text(decision, "finalArgumentsDigest", "final_arguments_digest")
        if not final_digest or not hmac.compare_digest(
            final_digest,
            typescript_arguments_digest,
        ):
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_arguments_mismatch",
                "TypeScript decision and request binding disagree on the authorized arguments",
            )
        final_arguments_value = decision.get(
            "finalArguments",
            decision.get("final_arguments"),
        )
        if not isinstance(final_arguments_value, Mapping):
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_final_arguments_missing",
                "TypeScript permission receipt is missing the authorized final arguments",
            )
        final_arguments = dict(final_arguments_value)
        if not _json_transport_equal(final_arguments, dict(arguments)):
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_physical_arguments_mismatch",
                "physical arguments do not match the TypeScript-authorized final arguments",
            )
        physical_arguments_digest = canonical_digest(dict(arguments))
        workspace = str(Path(str(binding.get("workspace_root") or "")).resolve())
        if workspace != self.workspace_root:
            raise TypeScriptPermissionReceiptError(
                "e02_receipt_workspace_mismatch",
                "TypeScript permission receipt belongs to another workspace",
            )
        session_revision = _non_negative_integer(binding.get("session_revision"), "session revision")
        policy_revision = _non_negative_integer(
            decision.get("policyRevision", decision.get("policy_revision", 0)),
            "policy revision",
        )
        mode_revision = _non_negative_integer(
            decision.get("modeRevision", decision.get("mode_revision", 0)),
            "mode revision",
        )
        receipt_digest = canonical_digest(decision)
        permit_id = "e02-port-" + canonical_digest(
            {
                "decision_id": decision_id,
                "tool_call_id": tool_call_id,
                "typescript_arguments_digest": typescript_arguments_digest,
                "physical_arguments_digest": physical_arguments_digest,
                "receipt_digest": receipt_digest,
            }
        )[:40]
        permit = TypeScriptExecutionPermit(
            permit_id=permit_id,
            decision_id=decision_id,
            request_id=_text(decision, "continuationRequestId", "continuation_request_id"),
            binding=TypeScriptPermissionBinding(
                run_id=self.run_id,
                task_id=self.task_id,
                session_id=self.session_id,
                session_revision=session_revision,
                worker_request_id=self.worker_request_id,
                tool_call_id=str(tool_call_id),
                tool_name=str(tool_name),
                tool_namespace=str(namespace),
                server_name=str(server_id),
                operation=str(operation),
                workspace_root=self.workspace_root,
                arguments_digest=typescript_arguments_digest,
            ),
            policy_revision=policy_revision,
            mode_revision=mode_revision,
            receipt_digest=receipt_digest,
            physical_arguments_digest=physical_arguments_digest,
        )
        with self._lock:
            prior = self._issued.get(permit_id)
            if prior is not None and prior != permit:
                raise TypeScriptPermissionReceiptError(
                    "e02_receipt_collision",
                    "TypeScript receipt permit identity collided with different content",
                )
            self._issued[permit_id] = permit
        return permit

    def validate_and_consume(
        self,
        call: Any,
        permit: TypeScriptExecutionPermit,
        _execution_context: Any,
    ) -> bool:
        with self._lock:
            issued = self._issued.get(permit.permit_id)
            if issued is None or issued != permit or permit.permit_id in self._consumed:
                return False
            binding = permit.binding
            if (
                str(getattr(call, "run_id", "")) != binding.run_id
                or str(getattr(call, "task_id", "")) != binding.task_id
                or str(getattr(call, "tool_call_id", "")) != binding.tool_call_id
                or str(getattr(call, "tool_name", "")) != binding.tool_name
                or not hmac.compare_digest(
                    canonical_digest(dict(getattr(call, "arguments", {}) or {})),
                    permit.physical_arguments_digest,
                )
            ):
                return False
            self._consumed.add(permit.permit_id)
            return True

    def metadata(self) -> dict[str, str]:
        with self._lock:
            return {
                "canonical_permission_owner": "typescript",
                "python_permission_role": "typed-physical-effect-port",
                "python_policy_fallback": "false",
                "e02_port_issued": str(len(self._issued)),
                "e02_port_consumed": str(len(self._consumed)),
            }


def canonical_digest(value: Mapping[str, Any] | list[Any] | str | int | float | bool | None) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_transport_equal(left: Any, right: Any) -> bool:
    """Compare values exactly as the JSON transport represents them.

    JavaScript serializes integral floats as integers and normalizes negative
    zero.  This structural comparison keeps booleans distinct from numbers,
    accepts those JSON-number normalizations, and otherwise requires identical
    arrays, object keys, and scalar values.  The TypeScript digest remains the
    canonical binding; this function only proves that the Python physical call
    carries the same JSON value returned in the signed decision.
    """

    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        if isinstance(left, float) and not _finite(left):
            return False
        if isinstance(right, float) and not _finite(right):
            return False
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if set(left) != set(right) or any(not isinstance(key, str) for key in left):
            return False
        return all(_json_transport_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
            return False
        return len(left) == len(right) and all(
            _json_transport_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return False


def _finite(value: float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}


def _text(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        selected = value.get(key)
        if isinstance(selected, str):
            return selected
    return ""


def _mapping(value: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        selected = value.get(key)
        if isinstance(selected, Mapping):
            return dict(selected)
    return {}


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeScriptPermissionReceiptError(
            "e02_receipt_revision_invalid",
            f"TypeScript permission receipt has an invalid {label}",
        )
    return value
