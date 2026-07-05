# Memory

M4 turns this package into Zyra's long-horizon memory, context compact, and trajectory replay layer.

## Runtime Modules

- `SQLiteStore`: durable task checkpoints, event log rows, memory records, and compact records.
- `MemoryFabric`: working/episodic/semantic/skill memory ingestion, group-aware context compaction, retrieval, and replay frame construction.
- `models`: memory records, compact policy/result, and trajectory frame schemas.

## Internalized Patterns

- `claude-code-best`: compact boundary, preserved tail, tool result budget, and session-memory restore ideas.
- `hermes-agent`: protect head/tail and compress the middle trajectory.
- `agent-framework`: group-aware compaction strategy so tool calls and results are kept as atomic units.
- `agentscope`: dependency-light approximate token counting/chunk sizing.
- `openclaw`: memory flush before compaction and compact artifacts while retaining the full event log.
- `langgraph`: checkpoint/store separation between durable state and replayable event history.

## Entrypoints

- `GET /tasks/{task_id}/memory`
- `POST /tasks/{task_id}/memory/ingest`
- `POST /tasks/{task_id}/memory/compact`
- `GET /tasks/{task_id}/trajectory`
- `GET /tasks/{task_id}/compactions`
- Slash commands: `/memory`, `/compact`, `/context`

## Verification

```bash
python scripts/verify_m4.py
python -m unittest tests.unit.test_memory_fabric
```
