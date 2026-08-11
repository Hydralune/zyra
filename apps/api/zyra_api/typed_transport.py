from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4


API_VERSION = "1.0"
API_VERSION_HEADER = "X-Zyra-Api-Version"
REQUEST_ID_HEADER = "X-Request-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"
RECEIPT_ID_HEADER = "X-Zyra-Receipt-Id"
RECEIPT_REPLAY_HEADER = "X-Zyra-Receipt-Replayed"
CLIENT_HEADER = "X-Zyra-Client"
OPERATION_HEADER = "X-Zyra-Operation"
CONTRACT_HEADER = "X-Zyra-Contract"
TYPED_CLIENT_PREFIX = "zyra-"
MAX_REQUEST_ID_BYTES = 256
MAX_IDEMPOTENCY_BYTES = 256
MAX_CURSOR_BYTES = 4096
RECEIPT_PENDING_TTL_SECONDS = 30.0

_REQUEST_ID = re.compile(r"^request_[A-Za-z0-9][A-Za-z0-9._:-]{2,240}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{2,255}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_RECEIPT_LOCK = threading.RLock()


class TypedTransportError(RuntimeError):
    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = dict(details or {})
        self.retryable = retryable

    def response(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": str(self),
            "details": dict(self.details),
            "retryable": self.retryable,
            "fallback": False,
        }


@dataclass(frozen=True, slots=True)
class TypedRequestContext:
    request_id: str
    api_version: str
    client: str
    operation: str
    contract: str
    typed: bool
    authenticated: bool

    def response_headers(self) -> dict[str, str]:
        return {
            API_VERSION_HEADER: API_VERSION,
            REQUEST_ID_HEADER: self.request_id,
            "Cache-Control": "no-store",
        }


@dataclass(frozen=True, slots=True)
class ReceiptReservation:
    receipt_id: str
    request_id: str
    idempotency_key: str
    operation: str
    request_digest: str
    created_at: float


@dataclass(frozen=True, slots=True)
class ReceiptReplay:
    receipt_id: str
    request_id: str
    idempotency_key: str
    operation: str
    status: HTTPStatus
    body: dict[str, Any]

    def headers(self) -> dict[str, str]:
        return {
            RECEIPT_ID_HEADER: self.receipt_id,
            RECEIPT_REPLAY_HEADER: "true",
        }


ReceiptStart = ReceiptReservation | ReceiptReplay | None


def _bounded_header(value: Any, maximum: int, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        return ""
    if len(rendered.encode("utf-8")) > maximum:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_transport_header",
            f"{label} exceeds {maximum} bytes.",
        )
    if any(character in rendered for character in ("\r", "\n", "\x00")):
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_transport_header",
            f"{label} contains control characters.",
        )
    return rendered


def _new_request_id() -> str:
    return f"request_{uuid4().hex}"


def _new_receipt_id() -> str:
    return f"receipt_{uuid4().hex}"


def typed_request_context(headers: Mapping[str, Any]) -> TypedRequestContext:
    api_version = _bounded_header(headers.get(API_VERSION_HEADER), 64, API_VERSION_HEADER)
    client = _bounded_header(headers.get(CLIENT_HEADER), 128, CLIENT_HEADER)
    operation = _bounded_header(headers.get(OPERATION_HEADER), 128, OPERATION_HEADER)
    contract = _bounded_header(headers.get(CONTRACT_HEADER), 128, CONTRACT_HEADER)
    typed = bool(api_version or client.startswith(TYPED_CLIENT_PREFIX) or operation or contract)
    request_id = _bounded_header(headers.get(REQUEST_ID_HEADER), MAX_REQUEST_ID_BYTES, REQUEST_ID_HEADER)
    if not request_id:
        request_id = _new_request_id()
    elif typed and not _REQUEST_ID.fullmatch(request_id):
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request_id",
            "Typed client request id has an invalid format.",
            details={"request_id": request_id},
        )
    if typed and api_version != API_VERSION:
        raise TypedTransportError(
            HTTPStatus.UPGRADE_REQUIRED,
            "api_version_mismatch",
            f"Zyra API version {api_version or '[missing]'} is not supported.",
            details={
                "actual_version": api_version or None,
                "supported_version": API_VERSION,
                "supported_versions": [API_VERSION],
            },
        )
    if operation and not _OPERATION.fullmatch(operation):
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_operation",
            "Typed client operation has an invalid format.",
            details={"operation": operation},
        )
    required_token = str(os.environ.get("ZYRA_API_AUTH_TOKEN") or "").strip()
    authenticated = not required_token
    if required_token:
        authorization = _bounded_header(headers.get("Authorization"), 8192, "Authorization")
        supplied = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
        authenticated = bool(supplied) and hmac.compare_digest(supplied, required_token)
        if not authenticated:
            raise TypedTransportError(
                HTTPStatus.UNAUTHORIZED,
                "authentication_required",
                "A valid Zyra API bearer token is required.",
            )
    return TypedRequestContext(
        request_id=request_id,
        api_version=api_version or API_VERSION,
        client=client,
        operation=operation,
        contract=contract,
        typed=typed,
        authenticated=authenticated,
    )


def error_context(error: TypedTransportError, headers: Mapping[str, Any]) -> tuple[HTTPStatus, dict[str, Any], dict[str, str]]:
    try:
        request_id = _bounded_header(headers.get(REQUEST_ID_HEADER), MAX_REQUEST_ID_BYTES, REQUEST_ID_HEADER)
    except TypedTransportError:
        request_id = ""
    response_headers = {
        API_VERSION_HEADER: API_VERSION,
        REQUEST_ID_HEADER: request_id or _new_request_id(),
        "Cache-Control": "no-store",
    }
    return error.status, error.response(), response_headers


def canonical_request_digest(
    operation: str,
    path: str,
    payload: Mapping[str, Any],
) -> str:
    material = json.dumps(
        {
            "operation": str(operation),
            "path": str(path),
            "payload": dict(payload),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class TypedReceiptStore:
    def __init__(self, sqlite_file: Path) -> None:
        self._path = Path(sqlite_file)
        # Database age alone cannot prove that a mutation owner died. A
        # long-running request can legitimately outlive the stale-row window,
        # so retain process-local custody for every live reservation. After a
        # daemon restart this set is empty and the existing bounded stale-row
        # recovery remains available.
        self._active_receipt_ids: set[str] = set()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with _RECEIPT_LOCK, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS typed_api_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status_code INTEGER,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    committed_at REAL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_typed_api_receipts_request
                ON typed_api_receipts(request_id, operation)
                """
            )

    def begin(
        self,
        *,
        context: TypedRequestContext,
        headers: Mapping[str, Any],
        operation: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> ReceiptStart:
        if not context.typed:
            return None
        idempotency_key = _bounded_header(
            headers.get(IDEMPOTENCY_HEADER),
            MAX_IDEMPOTENCY_BYTES,
            IDEMPOTENCY_HEADER,
        )
        if not idempotency_key:
            raise TypedTransportError(
                HTTPStatus.BAD_REQUEST,
                "idempotency_key_required",
                f"Typed mutation {operation} requires an Idempotency-Key header.",
            )
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise TypedTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_idempotency_key",
                "Idempotency key has an invalid format.",
            )
        digest = canonical_request_digest(operation, path, payload)
        now = time.time()
        with _RECEIPT_LOCK, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM typed_api_receipts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if str(row["operation"]) != operation or str(row["request_digest"]) != digest:
                    raise TypedTransportError(
                        HTTPStatus.CONFLICT,
                        "receipt_conflict",
                        "Idempotency key was already used for a different mutation.",
                        details={
                            "receipt_id": str(row["receipt_id"]),
                            "previous_operation": str(row["operation"]),
                            "next_operation": operation,
                        },
                    )
                if str(row["state"]) == "committed":
                    body = json.loads(str(row["response_json"] or "{}"))
                    receipt = dict(body.get("receipt") or {})
                    receipt.update(
                        {
                            "request_id": context.request_id,
                            "status": "replayed",
                            "replayed": True,
                        }
                    )
                    body["receipt"] = receipt
                    return ReceiptReplay(
                        receipt_id=str(row["receipt_id"]),
                        request_id=context.request_id,
                        idempotency_key=idempotency_key,
                        operation=operation,
                        status=HTTPStatus(int(row["status_code"])),
                        body=dict(body),
                    )
                receipt_id = str(row["receipt_id"])
                age = now - float(row["created_at"])
                if (
                    receipt_id in self._active_receipt_ids
                    or age <= RECEIPT_PENDING_TTL_SECONDS
                ):
                    raise TypedTransportError(
                        HTTPStatus.CONFLICT,
                        "receipt_pending",
                        "An identical typed mutation is still in flight.",
                        details={
                            "receipt_id": str(row["receipt_id"]),
                            "pending_age_seconds": round(age, 3),
                            "active_owner": (
                                receipt_id in self._active_receipt_ids
                            ),
                        },
                        retryable=True,
                    )
                connection.execute(
                    """
                    UPDATE typed_api_receipts
                    SET request_id = ?, state = 'pending', status_code = NULL,
                        response_json = NULL, created_at = ?, committed_at = NULL
                    WHERE idempotency_key = ?
                    """,
                    (context.request_id, now, idempotency_key),
                )
                self._active_receipt_ids.add(receipt_id)
                return ReceiptReservation(
                    receipt_id=receipt_id,
                    request_id=context.request_id,
                    idempotency_key=idempotency_key,
                    operation=operation,
                    request_digest=digest,
                    created_at=now,
                )
            receipt_id = _new_receipt_id()
            connection.execute(
                """
                INSERT INTO typed_api_receipts(
                    receipt_id, idempotency_key, request_id, operation,
                    request_digest, state, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    receipt_id,
                    idempotency_key,
                    context.request_id,
                    operation,
                    digest,
                    now,
                ),
            )
            self._active_receipt_ids.add(receipt_id)
            return ReceiptReservation(
                receipt_id=receipt_id,
                request_id=context.request_id,
                idempotency_key=idempotency_key,
                operation=operation,
                request_digest=digest,
                created_at=now,
            )

    def commit(
        self,
        reservation: ReceiptReservation | None,
        *,
        status: HTTPStatus,
        body: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if reservation is None:
            return dict(body), {}
        committed_at = time.time()
        receipt = {
            "receipt_id": reservation.receipt_id,
            "request_id": reservation.request_id,
            "idempotency_key": reservation.idempotency_key,
            "operation": reservation.operation,
            "status": "committed",
            "status_code": int(status),
            "replayed": False,
            "committed_at": _iso_timestamp(committed_at),
            "binding": {
                key: value
                for key, value in dict(binding).items()
                if value not in (None, "")
            },
            "response_digest": "",
        }
        response = dict(body)
        response["receipt"] = receipt
        response_digest = hashlib.sha256(
            json.dumps(
                response,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        receipt["response_digest"] = response_digest
        response["receipt"] = receipt
        serialized = json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        with _RECEIPT_LOCK, self._connection() as connection:
            result = connection.execute(
                """
                UPDATE typed_api_receipts
                SET state = 'committed', status_code = ?, response_json = ?, committed_at = ?
                WHERE receipt_id = ? AND state = 'pending'
                  AND request_digest = ? AND operation = ?
                """,
                (
                    int(status),
                    serialized,
                    committed_at,
                    reservation.receipt_id,
                    reservation.request_digest,
                    reservation.operation,
                ),
            )
            if result.rowcount != 1:
                raise TypedTransportError(
                    HTTPStatus.CONFLICT,
                    "receipt_commit_conflict",
                    "Typed mutation receipt could not be committed.",
                    details={"receipt_id": reservation.receipt_id},
                )
            self._active_receipt_ids.discard(reservation.receipt_id)
        return response, {
            RECEIPT_ID_HEADER: reservation.receipt_id,
            RECEIPT_REPLAY_HEADER: "false",
        }

    def abandon(self, reservation: ReceiptReservation | None) -> None:
        if reservation is None:
            return
        with _RECEIPT_LOCK, self._connection() as connection:
            connection.execute(
                "DELETE FROM typed_api_receipts WHERE receipt_id = ? AND state = 'pending'",
                (reservation.receipt_id,),
            )
            self._active_receipt_ids.discard(reservation.receipt_id)

    def lookup(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM typed_api_receipts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row is not None else None


def _iso_timestamp(timestamp: float) -> str:
    seconds = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(timestamp))
    millis = int((timestamp - int(timestamp)) * 1000)
    return f"{seconds}.{millis:03d}Z"


def encode_task_cursor(offset: int, *, status: str, limit: int) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "offset": int(offset),
            "status": str(status),
            "limit": int(limit),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_task_cursor(value: str, *, status: str, limit: int) -> int:
    rendered = _bounded_header(value, MAX_CURSOR_BYTES, "cursor")
    if not rendered:
        return 0
    try:
        padded = rendered + "=" * ((4 - len(rendered) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_invalid",
            "Task cursor is not valid.",
            details={"cause": str(error)},
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or str(payload.get("status") or "") != status
        or int(payload.get("limit") or 0) != limit
    ):
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_scope_mismatch",
            "Task cursor does not match the current query.",
        )
    offset = int(payload.get("offset") or 0)
    if offset < 0:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_invalid",
            "Task cursor offset must be non-negative.",
        )
    return offset


def paginate_tasks(
    tasks: list[dict[str, Any]],
    query: Mapping[str, list[str]],
) -> dict[str, Any]:
    status = str((query.get("status") or [""])[0] or "").strip().lower()
    try:
        limit = int((query.get("limit") or ["100"])[0] or 100)
    except (TypeError, ValueError) as error:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_task_limit",
            "Task list limit must be an integer.",
        ) from error
    limit = min(1000, max(1, limit))
    cursor_value = str((query.get("cursor") or [""])[0] or "")
    offset = decode_task_cursor(cursor_value, status=status, limit=limit)
    filtered = [
        dict(task)
        for task in tasks
        if not status or str(task.get("status") or "").strip().lower() == status
    ]
    filtered.sort(
        key=lambda task: (
            str(task.get("updated_at") or task.get("created_at") or ""),
            str(task.get("task_id") or ""),
        ),
        reverse=True,
    )
    page = filtered[offset : offset + limit]
    next_offset = offset + len(page)
    cursor = (
        encode_task_cursor(next_offset, status=status, limit=limit)
        if next_offset < len(filtered)
        else None
    )
    return {
        "tasks": page,
        "total": len(filtered),
        "cursor": cursor,
    }


def runtime_readiness_payload(
    owner_readiness: Mapping[str, Any] | None = None,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected_owners = (
        "task_store",
        "event_log",
        "checkpoint_store",
        "artifact_store",
        "control_runtime",
        "typed_transport",
    )
    supplied = owner_readiness or {}
    owners = {name: supplied.get(name) is True for name in expected_owners}
    blockers = [name for name in expected_owners if not owners[name]]
    return {
        "ready": not blockers,
        "status": "ready" if not blockers else "blocked",
        "api_version": API_VERSION,
        "owners": owners,
        "blockers": blockers,
        "details": dict(details or {}),
    }
