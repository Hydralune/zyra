from __future__ import annotations

from zyra_orchestration.deployment.code_worker_adapter import (
    _public_runtime_events,
    _public_session_projection,
)


def _assistant_event(phase: str, **values: object) -> dict[str, object]:
    return {
        "schema": "zyra.provider-assistant-presentation/v1",
        "phase": phase,
        "delta_kind": "assistant_text",
        "stream_id": "provider:dispatch-1",
        "assistant_message_id": "message:assistant:provider:dispatch-1",
        "provider_sequence": 3,
        **values,
    }


def test_public_projection_admits_only_bounded_redacted_assistant_text() -> None:
    projected = _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="answer api_key=sk-private-provider-key",
            segment_index=4,
        )
    )

    assert projected is not None
    assert projected["phase"] == "assistant_text_delta"
    assert projected["presentation_text"] == "answer api_key=<redacted>"
    assert projected["content_persisted"] is False
    assert "content" not in projected
    assert "sk-private-provider-key" not in str(projected)


def test_public_projection_rejects_private_or_malformed_stream_frames() -> None:
    assert _public_session_projection(
        {"phase": "message_delta", "content": "private model token"}
    ) is None
    assert _public_session_projection(
        {"phase": "model_stream_frame", "content": "private reasoning"}
    ) is None
    assert _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="x" * 1_025,
        )
    ) is None
    assert _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="answer",
            stream_id="",
        )
    ) is None
    malformed_counter = _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="answer",
            segment_index="not-an-integer",
        )
    )
    assert malformed_counter is not None
    assert malformed_counter["segment_index"] == 0


def test_public_projection_admits_reasoning_under_the_same_custody() -> None:
    """The reasoning branch binds its own `content`; it must not read the
    assistant branch's local, which never runs for a reasoning phase."""

    projected = _public_session_projection(
        {
            "schema": "zyra.provider-assistant-presentation/v1",
            "phase": "reasoning_delta",
            "delta_kind": "reasoning",
            "stream_id": "provider:dispatch-1:reasoning",
            "assistant_message_id": "message:assistant:provider:dispatch-1",
            "provider_sequence": 2,
            "segment_index": 3,
            "content": "weighing api_key=sk-private-provider-key",
        }
    )

    assert projected is not None
    assert projected["phase"] == "reasoning_delta"
    assert projected["delta_kind"] == "reasoning"
    assert projected["presentation_text"] == "weighing api_key=<redacted>"
    assert projected["content_persisted"] is False
    assert "content" not in projected
    assert "sk-private-provider-key" not in str(projected)


def test_public_projection_rejects_malformed_reasoning_frames() -> None:
    base = {
        "schema": "zyra.provider-assistant-presentation/v1",
        "phase": "reasoning_delta",
        "delta_kind": "reasoning",
        "stream_id": "provider:dispatch-1:reasoning",
        "assistant_message_id": "message:assistant:provider:dispatch-1",
    }
    # An empty delta, an over-long chunk, a missing stream and a foreign
    # delta_kind are each refused rather than coerced.
    assert _public_session_projection({**base, "content": ""}) is None
    assert _public_session_projection({**base, "content": "x" * 1_025}) is None
    assert _public_session_projection({**base, "content": "ok", "stream_id": ""}) is None
    assert _public_session_projection(
        {**base, "delta_kind": "assistant_text", "content": "ok"}
    ) is None
    # A non-delta reasoning phase must not carry chunk content.
    assert _public_session_projection(
        {**base, "phase": "reasoning_started", "content": "leaked"}
    ) is None


def _host_event(phase: str, *, delta_kind: str, **values: object) -> dict[str, object]:
    """Reproduce one host EventRecord for a provider presentation frame.

    The host records the provider frame on the `query_session` envelope and adds
    a `typescript_runtime` sibling that carries no `schema` -- only custody
    metadata.  That sibling is what used to take reasoning frames down with it.
    """

    return {
        "run_id": "run-1",
        "task_id": "task-1",
        "node_id": "node-1",
        "event_type": "agent_message",
        "payload": {
            "query_session": {
                "schema": "zyra.provider-assistant-presentation/v1",
                "phase": phase,
                "delta_kind": delta_kind,
                "stream_id": "provider:dispatch-1",
                "assistant_message_id": "message:assistant:provider:dispatch-1",
                "sequence": 7,
                **values,
            },
            "typescript_runtime": {
                "phase": phase,
                "canonical_owner": "typescript",
                "runtime_id": "zyra-typescript-claude-runtime",
            },
        },
    }


def test_reasoning_frames_survive_the_host_custody_sibling() -> None:
    """A reasoning frame must not be dropped by its own custody envelope.

    The `typescript_runtime` sibling has no `schema`, so projecting it yields
    None; the loop treated that as "drop the whole event" and discarded the
    valid `query_session` copy beside it.  Only phases listed as custody-only
    are reduced instead, and reasoning was missing from that list.
    """

    # Only a delta carries chunk content; a started/ended frame that carries
    # content is correctly refused.
    frames = [
        ("reasoning_started", {}),
        ("reasoning_delta", {"content": "why"}),
        ("reasoning_ended", {}),
    ]
    for phase, extra in frames:
        projected = _public_runtime_events(
            [_host_event(phase, delta_kind="reasoning", **extra)],
        )
        assert len(projected) == 1, phase
        session = projected[0]["payload"]["query_session"]
        assert session["phase"] == phase
        assert session["delta_kind"] == "reasoning"
        # The sibling is reduced to custody metadata, never a second envelope.
        sibling = projected[0]["payload"]["typescript_runtime"]
        assert "schema" not in sibling


def test_reasoning_delta_carries_its_text_through_the_host_path() -> None:
    projected = _public_runtime_events(
        [_host_event("reasoning_delta", delta_kind="reasoning", content="6*15+4*7=118")],
        persist_presentation_text=True,
    )

    assert len(projected) == 1
    assert projected[0]["payload"]["query_session"]["presentation_text"] == "6*15+4*7=118"


def test_assistant_text_host_path_is_unchanged_by_the_reasoning_fix() -> None:
    """The assistant path already worked; widening the custody set must not
    change it, and must not let the sibling become a second content envelope."""

    projected = _public_runtime_events(
        [_host_event("assistant_text_delta", delta_kind="assistant_text", content="118")],
        persist_presentation_text=True,
    )

    assert len(projected) == 1
    session = projected[0]["payload"]["query_session"]
    assert session["phase"] == "assistant_text_delta"
    assert session["presentation_text"] == "118"
    assert "schema" not in projected[0]["payload"]["typescript_runtime"]


def test_public_event_projection_never_carries_host_custody_extras() -> None:
    """The `typescript_runtime` sibling is reduced to custody keys.

    It is host-generated metadata, not a second content envelope: its arbitrary
    keys must never cross into the public event, and it must not be mistaken for
    the payload that carries the presentation text.
    """

    projected = _public_runtime_events(
        [
            {
                "event_id": "event-assistant-1",
                "task_id": "task-1",
                "run_id": "run-1",
                "payload": {
                    "query_session": _assistant_event(
                        "assistant_text_delta",
                        content="bounded answer",
                        segment_index=7,
                    ),
                    "typescript_runtime": {
                        "runtime_id": "zyra-typescript-claude-runtime",
                        "protocol": "zyra.claude-runtime.v1",
                        "canonical_owner": "typescript",
                        "phase": "assistant_text_delta",
                        "scope": "parent_session",
                        "parent_session_id": "session-1",
                        "effective_session_id": "session-1",
                        "untrusted_extra": "must not cross",
                    },
                },
            }
        ]
    )

    assert len(projected) == 1
    sibling = projected[0]["payload"]["typescript_runtime"]
    assert "untrusted_extra" not in sibling
    assert "schema" not in sibling
    # The text comes from the query_session envelope, not the sibling.
    assert projected[0]["payload"]["query_session"]["presentation_text"] == "bounded answer"


def test_public_event_projection_keeps_bounded_text_by_default_and_can_opt_out() -> None:
    """Presentation text is retained by default; a task may still opt out.

    The low-entropy default made every multi-round run look empty: the
    narration existed but was never kept, so a reader saw tool calls and an
    answer and nothing the agent actually said.
    """

    events = [
        {
            "event_id": "event-assistant-1",
            "task_id": "task-1",
            "run_id": "run-1",
            "payload": {
                "query_session": _assistant_event(
                    "assistant_text_delta",
                    content="bounded answer",
                    segment_index=7,
                ),
            },
        }
    ]

    # Default: the narration is kept.
    projected = _public_runtime_events(events)
    assert len(projected) == 1
    session = projected[0]["payload"]["query_session"]
    assert session["presentation_text"] == "bounded answer"
    # Digest custody is untouched and the raw content never crosses.
    assert session["content_persisted"] is False
    assert "content" not in session

    # An explicit opt-out still produces nothing.
    assert _public_runtime_events(events, persist_presentation_text=False) == []


def test_public_event_projection_bounds_persisted_text_per_stream() -> None:
    """An over-long answer keeps a bounded prefix and says what was elided."""

    chunk = _assistant_event(
        "assistant_text_delta",
        content="x" * 1_024,
        segment_index=1,
    )
    # 100 KiB of chunks against a 64 KiB per-stream allowance.
    events = [
        {
            "event_id": f"event-assistant-{index}",
            "task_id": "task-1",
            "run_id": "run-1",
            "payload": {"query_session": {**chunk, "assistant_message_id": f"m-{index}"}},
        }
        for index in range(100)
    ]

    projected = _public_runtime_events(events, persist_presentation_text=True)

    retained = [
        event["payload"]["query_session"]
        for event in projected
        if event.get("schema") != "zyra.presentation-retention-summary/v1"
    ]
    kept = [session for session in retained if "presentation_text" in session]
    elided = [session for session in retained if "presentation_text" not in session]
    summaries = [
        event
        for event in projected
        if event.get("schema") == "zyra.presentation-retention-summary/v1"
    ]
    kept_bytes = sum(
        len(session["presentation_text"].encode("utf-8")) for session in kept
    )
    assert kept_bytes == 64 * 1_024
    assert len(elided) == 36
    # Every elided frame still keeps its lifecycle and custody fields, so only
    # the redundant text copy was dropped, not the record of what happened.
    assert all(session["content_persisted"] is False for session in elided)
    assert all(session["stream_id"] == "provider:dispatch-1" for session in elided)
    # The elision is reported rather than silently swallowed.
    assert len(summaries) == 1
    assert summaries[0]["dropped_presentation_bytes"] == 36 * 1_024
    assert summaries[0]["per_stream_limit_bytes"] == 64 * 1_024
