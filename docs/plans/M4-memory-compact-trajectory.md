# M4 Memory, Compact, and Long Trajectory Self-Check

## Status

M4 is implemented as a runnable Zyra subsystem, not as schema-only memory placeholders.

## Internalized Sources

| Source | Internalized Pattern | Zyra Path |
| --- | --- | --- |
| `claude-code-best/src/services/compact` | compact boundary, preserved recent tail, tool-result budget, session-memory restore contract | `packages/memory/zyra_memory/fabric.py` |
| `hermes-agent/trajectory_compressor.py` | protect head/tail and summarize the middle trajectory | `packages/memory/zyra_memory/fabric.py` |
| `agent-framework/docs/decisions/0019-python-context-compaction-strategy.md` | group-aware compaction and tool call/result atomicity | `packages/memory/zyra_memory/fabric.py` |
| `agentscope/src/agentscope/rag/_chunker/_approx_token_chunker.py` | dependency-light approximate token counting | `packages/memory/zyra_memory/fabric.py` |
| `openclaw/docs/concepts/compaction.md` and `docs/tools/trajectory.md` | memory flush before compact, full transcript retained, trajectory bundle framing | `packages/memory/zyra_memory/fabric.py`, `apps/api/zyra_api/main.py` |
| `langgraph/libs/checkpoint` and `checkpoint-sqlite` | checkpoint/store split for durable state and replayable events | `packages/memory/zyra_memory/sqlite_store.py` |

## Delivered Runtime Surface

- `MemoryFabric.refresh_task_memory()` writes working, episodic, semantic, and skill memory records from checkpoint state, event log, artifacts, worker traces, and skill invocations.
- `MemoryFabric.compact_context()` flushes memory before compacting, preserves initial goal, constraints, requirement changes, failure events, route/constraint/evaluation decisions, tool-call groups, and the final tail.
- Large tool or agent-message payloads are moved to compact source artifacts and represented by artifact refs.
- `MemoryFabric.replay_trajectory()` builds timeline frames for task state, topology route, tool calls, requirement changes, fault injection, verification, workers, and artifacts.
- `SQLiteStore` now persists `memory_records` and `compact_records`.
- API endpoints expose memory, compaction, and trajectory replay: `/tasks/{task_id}/memory`, `/memory/ingest`, `/memory/compact`, `/trajectory`, `/compactions`.
- Slash commands `/memory`, `/compact`, and `/context` use MemoryFabric rather than only session metadata.
- The static console includes Memory and Trajectory panels backed by the new API.

## Verification

Validated by:

```bash
python scripts/verify_m4.py
python -m unittest tests.unit.test_memory_fabric tests.unit.test_web_console_static
python -m unittest tests.integration.test_api_control_commands.ApiControlCommandTests.test_memory_fabric_endpoints_ingest_compact_and_replay
```

`scripts/verify_m4.py` checks four memory layers, replay search, compact preservation of requirement/failure/tool events, raw artifact externalization, persisted compact records, and trajectory replay.

## Critical Review

M4 now has a real runnable subsystem, but it is still a first-stage integration implementation:

- Retrieval is lexical and approximate-token based; semantic vector retrieval or SQLite FTS can be added in the second stage.
- Compact summaries are deterministic structured summaries, not LLM-generated summaries yet. This is acceptable for first-stage stability, but a future MemoryCurator worker should be able to call an LLM/provider for richer summarization.
- Frontend trajectory replay is a connected static panel. The full M6 console should add richer filtering, topology playback, artifact previews, and step-by-step replay controls.
- Historical failure memory is now persisted and replayable; M5 should connect it into ResourceScheduler and recovery policy scoring.
