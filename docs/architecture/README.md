# Architecture Notes

M0 establishes the system spine:

- every run is identified by `run_id`
- every user task is identified by `task_id`
- every graph task node is identified by `node_id`
- every runtime event is identified by `event_id`
- runtime state is replayable from JSONL events

Later milestones should extend these contracts instead of replacing them.

M0-M3 are the system spine, not the final system weight. M4 has attached the first MemoryFabric/compact/trajectory subsystem. M5-M6 must continue attaching substantial internalized modules to these contracts:

- memory, compaction, trajectory replay, checkpoint/retrieval, and skill memory
- resource scheduling, worker manifests, sandbox/gateway boundaries, fault injection, and recovery
- connected frontend views for task graph, event timeline, worker state, artifacts, diff/browser/terminal output, commands, and live requirement changes

Architecture reviews should reject additions that only create empty contracts without connecting real runtime behavior.
