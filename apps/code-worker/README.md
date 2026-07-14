# Zyra CodeWorker TypeScript Runtime

The application entrypoint is the canonical in-repository TypeScript runtime for
CodeWorker query execution. It is not an upstream inspection sidecar.

Runtime command:

    bun src/main.ts --stdio

Node 22 strip-types is the supported cleanroom fallback when Bun is unavailable:

    node --experimental-strip-types src/main.ts --stdio

The JSONL boundary is versioned as zyra.claude-runtime.v1. TypeScript owns query
turns, session lifecycle, tool registry and batch decisions, result budgets, and
compact/restore state. Python owns process supervision, durable Zyra stores,
permission enforcement, exact tool side effects, EventRecord projection and
ArtifactRef persistence. Protocol failure or process loss fails the worker; there
is no Python QueryEngine fallback.

The health and contract flags report only the internalized runtime:

    node --experimental-strip-types src/main.ts --health
    node --experimental-strip-types src/main.ts --query-contract
    node --experimental-strip-types src/main.ts --session-contract
    node --experimental-strip-types src/main.ts --tool-loop-contract

No command scans or loads vendor, vendor-runtimes, or a sibling source repository.
