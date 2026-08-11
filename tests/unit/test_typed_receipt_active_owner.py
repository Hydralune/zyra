from __future__ import annotations

import sqlite3
from http import HTTPStatus

import pytest

from apps.api.zyra_api.typed_transport import (
    ReceiptReservation,
    TypedReceiptStore,
    TypedRequestContext,
    TypedTransportError,
)


def _context(request_id: str) -> TypedRequestContext:
    return TypedRequestContext(
        request_id=request_id,
        api_version="1.0",
        client="zyra-cli",
        operation="task.resume",
        contract="zyra.task-mutation.v1",
        typed=True,
        authenticated=True,
    )


def test_live_receipt_owner_cannot_be_stolen_only_because_row_is_old(tmp_path) -> None:
    database = tmp_path / "api.sqlite3"
    store = TypedReceiptStore(database)
    headers = {"Idempotency-Key": "task-resume-live-owner"}
    payload = {"requested_by": "test"}
    first = store.begin(
        context=_context("request_live_owner_first"),
        headers=headers,
        operation="task.resume",
        path="/tasks/task_0123456789ab/run",
        payload=payload,
    )
    assert isinstance(first, ReceiptReservation)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE typed_api_receipts SET created_at = 0 WHERE receipt_id = ?",
            (first.receipt_id,),
        )

    with pytest.raises(TypedTransportError) as captured:
        store.begin(
            context=_context("request_live_owner_duplicate"),
            headers=headers,
            operation="task.resume",
            path="/tasks/task_0123456789ab/run",
            payload=payload,
        )
    assert captured.value.status == HTTPStatus.CONFLICT
    assert captured.value.code == "receipt_pending"
    assert captured.value.details["active_owner"] is True

    # A fresh store models a restarted daemon: no process-local owner
    # survived, so the bounded stale-row recovery path remains available.
    restarted = TypedReceiptStore(database)
    recovered = restarted.begin(
        context=_context("request_after_restart"),
        headers=headers,
        operation="task.resume",
        path="/tasks/task_0123456789ab/run",
        payload=payload,
    )
    assert isinstance(recovered, ReceiptReservation)
    restarted.commit(
        recovered,
        status=HTTPStatus.OK,
        body={"task": {"task_id": "task_0123456789ab"}},
        binding={"task_id": "task_0123456789ab"},
    )
