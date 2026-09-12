"""The durable protocol frame trace must stay bounded.

The trace is written out in full on every checkpoint, so an unbounded list
makes each save progressively more expensive.  One real long-horizon run grew
it to 137 MB, after which the agentic loop stopped advancing: provider calls
kept succeeding while task state and the event spine stood still.  These tests
pin the bound and the fact that trimming does not renumber what remains.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package in ("packages/workers", "packages/runtime", "packages/orchestration"):
    candidate = str(ROOT / package)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from zyra_workers.typescript_claude_runtime import (  # noqa: E402
    _PROTOCOL_FRAME_TRACE_LIMIT,
    TypeScriptClaudeQueryEngine,
)


class _TraceHarness:
    """Minimal stand-in exposing only what the trace helper touches."""

    def __init__(self) -> None:
        self._protocol_frame_trace: list[dict[str, object]] = []
        self._protocol_frame_trace_base_index = 0

    append = TypeScriptClaudeQueryEngine._append_protocol_frame_trace


def test_trace_is_capped_and_indices_keep_counting() -> None:
    harness = _TraceHarness()
    total = _PROTOCOL_FRAME_TRACE_LIMIT * 3

    for index in range(total):
        harness.append({"sequence": index})

    assert len(harness._protocol_frame_trace) == _PROTOCOL_FRAME_TRACE_LIMIT
    # Indices keep counting across trimming, so a reader can tell where the
    # retained window sits in the full sequence.
    assert harness._protocol_frame_trace[-1]["trace_index"] == total
    assert (
        harness._protocol_frame_trace[0]["trace_index"]
        == total - _PROTOCOL_FRAME_TRACE_LIMIT + 1
    )


def test_trace_below_the_cap_is_untouched() -> None:
    harness = _TraceHarness()

    for index in range(10):
        harness.append({"sequence": index})

    assert [item["trace_index"] for item in harness._protocol_frame_trace] == list(
        range(1, 11)
    )
    assert harness._protocol_frame_trace[0]["sequence"] == 0
