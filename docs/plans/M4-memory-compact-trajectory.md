# Historical M0.4 Memory, Compact, and Long Trajectory Self-Check

## Status

This file records the former M4 work. It is now a historical subrecord of the consolidated new `M0: foundation and main-path bootstrap`.

Former M4 is implemented as a runnable Zyra subsystem, not as schema-only memory placeholders.

This historical subrecord should not be used as evidence that the whole memory/skill/retrieval internalization effort is already finished. Former M4 delivered the first real MemoryFabric, compact, persistence, API, and replay path. It did not yet absorb the full mature memory stacks from the source repositories, and it does not remove the new M1 responsibility to keep internalizing large runtime and memory modules.

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
- Frontend trajectory replay is a connected static panel. The full new M2 console should add richer filtering, topology playback, artifact previews, and step-by-step replay controls.
- Historical failure memory is now persisted and replayable; former M5 connected a first scheduler path, and new M1 must deepen it into real resource scheduling and recovery policy scoring.

## Internalization Debt Carried Forward

New M1 must treat these as inputs rather than optional polish:

- Connect MemoryFabric failure history, requirement changes, compact records, and trajectory frames into `ResourceScheduler` and recovery planning.
- Add a real `MemoryCurator` worker path that can decide what to preserve, retrieve, compact, and promote into skill memory.
- Expand skill memory beyond invocation records: load skill bodies/resources, attach allowed tools, and persist reusable procedures from traces.
- Add stronger retrieval backends such as SQLite FTS or a vector adapter behind the current memory interface.
- Make compact restore affect the next worker context, not only produce a compact artifact and metadata.
- Carry the memory/trajectory views into the new M2 console as interactive playback, filtering, artifact preview, and topology correlation.

If later milestones only cite M4 as “memory done” without advancing these items, the first-stage heavy integration target is drifting.
