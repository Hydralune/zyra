# M1-R01 Execution 01: Runtime Core TypeScript Cutover Review

- Status: PASS
- Base commit: `e7b204d682315a28c922a1a61a219ccb276fe3c8`
- Evidence commit: `SELF` (the commit containing this review)
- Scope: `execution-01-runtime-core-typescript-cutover.md`
- Review date: 2026-07-14

## 1. Custody verdict

The default CodeWorker path now starts `apps/code-worker/src/main.ts` and delegates canonical query execution to `@zyra/claude-runtime`.

| State or behavior | Canonical owner after cutover |
| --- | --- |
| Query loop and turn progression | TypeScript |
| Session lifecycle and exact TypeScript snapshot | TypeScript |
| Tool registry, schema validation, batching, stable call IDs | TypeScript |
| Tool-result budget and context compact/restore | TypeScript |
| Model SSE parsing, retry and fallback selection | TypeScript |
| Permission decision authority | Python `ToolPermissionRuntime` |
| Tool side effects | Python tool gateway |
| Durable event, artifact and compatibility projection | Python |
| Python query-engine fallback | None |

The runtime protocol is versioned JSONL (`zyra.claude-runtime.v1`) with independent ordered sequences and correlation IDs. Process failure, protocol drift, disabled runtime components, exhausted model retries and pending permission all fail closed.

## 2. Main-path evidence

The remediation introduced the formal TypeScript workspace and package under:

- `apps/code-worker/src/main.ts`
- `packages/runtime/claude-runtime/src/**`
- `packages/workers/zyra_workers/typescript_claude_runtime.py`

The old `apps/code-worker/src/main.mjs` inspection entrypoint was removed. `CodeWorkerRuntime` selects `TypeScriptClaudeQueryEngine` by default and has no fallback to `ZyraClaudeQueryEngine`.

Behavior covered by the target matrix:

- Multi-turn session lifecycle, snapshots and restore.
- Read-only concurrency and serial mutating batches.
- Same-target mutation conflict protection.
- Stable Claude/opencode/API tool-use identifiers.
- Schema rejection before Python side effects.
- Permission suspension and exact approved-call resume.
- Large result externalization and watchdog routing.
- Forced and threshold compact/restore.
- Real local HTTP SSE parsing, retry, fallback-model selection and provider-produced tool plans.
- Exhausted provider attempts stop before mutation.
- Component disconnects stop before mutation.
- API inventory and control surfaces expose the TypeScript owner.

## 3. Verification evidence

### TypeScript runtime

```text
node --experimental-strip-types --test packages/runtime/claude-runtime/test/protocol.test.ts packages/runtime/claude-runtime/test/runtime.test.ts
9 passed
```

### Custody audit

```text
.venv\Scripts\python.exe scripts\verify_typescript_runtime_custody.py --json
ok=true; findings=[]; python_query_engine_fallback=false
```

### Incremental and adjacent regression matrix

```text
.venv\Scripts\python.exe -m pytest -q <execution-01 integration/API/scenario matrix>
63 passed, 9 subtests passed
```

### Cleanroom

A temporary directory received only `apps/**`, `packages/**`, `scripts/**`, `tests/**`, `skills/**` and root runtime configuration. It did not receive `.git`, caches, `vendor/**`, `vendor-runtimes/**`, or any `G:\agent-zoo` source repository.

```text
custody audit: PASS
TypeScript tests: 9 passed
Python core behavior: 25 passed, 7 subtests passed
```

The temporary cleanroom was removed after the run.

## 4. Effective-line buckets

Counts are gross additions relative to the base worktree, with new untracked files counted explicitly.

| Bucket | Added lines | Acceptance treatment |
| --- | ---: | --- |
| Production runtime | 3,739 | Effective |
| TypeScript canonical runtime subset | 2,584 | Effective |
| Python host/projection and existing-path wiring subset | 1,155 | Effective only for gateway/projection responsibilities |
| Tests | 937 | Verification only |
| Custody audit script | 110 | Audit tooling only |
| Runtime configuration | 55 | Configuration only |
| Docs and review | Reported separately | Not production |
| Vendor/source-pool | 0 | Excluded |

The deletion of the 880-line `main.mjs` inspection sidecar is not counted as new implementation.

## 5. Anti-pseudo-internalization checks

- No runtime code was added under `vendor/**`, `vendor-runtimes/**`, `source-pool/**` or equivalent.
- The cleanroom does not require `../claude-code-best` or another root source repository.
- Source manifests and ledgers are not used as behavioral completion evidence.
- Disabling the TypeScript runtime or required runtime components changes behavior and fails closed.
- Python remains authoritative only for permission, side effects, durability and projections; it does not schedule turns, batches, retries, compact decisions or result budgets.
- The real SSE test proves the provider plan replaces the request's scripted plan.
- Permission approval proves the same stable parked call resumes rather than a newly generated call.

## 6. Residual risks and next boundary

- Bun was not installed in the execution environment. The formal TypeScript source was exercised through Node 22 `--experimental-strip-types`; Bun remains the preferred launcher when present.
- Historical Python query/runtime implementations still exist in the repository but are not the default path and are not a fallback. Their deletion or reclassification belongs to execution-02 and must not alter the custody established here.
- The full repository test suite was not repeated. The execution-01 target matrix, adjacent API/scenario regression set and cleanroom were run instead, matching the bounded remediation scope.
