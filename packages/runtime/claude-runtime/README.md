# Zyra Claude Runtime

This package is the canonical TypeScript owner for the CodeWorker query loop.
It internalizes the Claude-derived query/session/tool/context mechanisms behind
Zyra-owned contracts. It is not a vendored upstream tree and it never imports
from the workspace source repositories.

Owned here:

- query turn and stop-state transitions
- session lifecycle and resumable runtime snapshot
- tool registry snapshot, schema validation and batch scheduling
- tool-result and aggregate context budgets
- automatic compact, reactive compact and post-compact restore state
- versioned JSONL runtime protocol with strict sequence validation

Python remains the host for durable Zyra stores, EventRecord and ArtifactRef
projection, permission enforcement, exact tool side effects and process
lifecycle. The host cannot choose a second query loop or fall back to the old
Python QueryEngine.
