# Architecture Notes

M0 establishes the system spine:

- every run is identified by `run_id`
- every user task is identified by `task_id`
- every graph task node is identified by `node_id`
- every runtime event is identified by `event_id`
- runtime state is replayable from JSONL events

Later milestones should extend these contracts instead of replacing them.
