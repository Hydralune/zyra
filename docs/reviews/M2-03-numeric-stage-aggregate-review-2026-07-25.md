# M2-03 numeric-stage aggregate critical review — 2026-07-25

## Verdict

M2-03 passes the numeric-stage aggregate after one bounded repair. The final
implementation and cleanroom target is
`2bc9604685cd7024825cf94a34c3fc507921d661`.

Scope:

- `M2-S03A-01` artifact custody/catalog/viewers;
- `M2-S03A-02` diff/patch review integration;
- `M2-S03B-01` terminal PTY viewer;
- `M2-S03B-02` browser artifact/control viewer;
- `M2-S03B-03` causal trace cross-view integration.

The final default console path connects typed API transport, artifact/diff,
terminal, browser, timeline/topology and causal trace views. It is not a set of
disconnected static panels. The exact archive cleanroom passes all Web tests,
the production build and 15 applicable Python API/runtime files.

## Critical finding and resolution

The first aggregate cleanroom constructed the production typed client and found
that `task.terminals.ticket` was registered as a mutation without a receipt.
`ProtocolCatalog.assertComplete()` correctly rejected the catalog. This made
the M2-S03B-01 ticket route unreachable through the default typed client even
though its narrower terminal tests passed.

Resolution in `2bc9604`:

- the route uses `taskTerminalReceipt` and `receipt: required`;
- the API response contains a typed ticket-issuance receipt with exact scope,
  cursor, expiry, correlation/causation and only the SHA-256 digest of the
  secret ticket;
- the Python `TerminalSessionRegistry`/ticket authority remains canonical;
- unit, platform integration, typed client and cleanroom aggregate tests pass.

No other P0/P1 finding remained. No source role, canonical state owner, default
permission/scheduler/recovery policy, dependency, process or port changed.

## Source-to-target and role audit

| Slice | Primary | Supplementary | Conformance/reference | Entries |
| --- | ---: | ---: | ---: | ---: |
| M2-S03A-01 | 1 | 2 | 2 | 5 |
| M2-S03A-02 | 1 | 2 | 0 | 3 |
| M2-S03B-01 | 1 | 2 | 2 | 5 |
| M2-S03B-02 | 1 | 2 | 1 | 4 |
| M2-S03B-03 | 1 | 1 | 1 | 3 |

Totals: 20 unique entries, 14 production entries, 70 production bindings, 6
non-production entries, 12 conformance/reference bindings, 0 missing targets
at the final commit and 0 forward OpenClaw entries. No slice has more than one
primary or more than two supplementary sources.

The trace slice does not recount inherited opencode/OpenHands/browser-use panel
implementations. It counts only new typed focus consumers and its Zyra/OMP
trace modules. Hermes remains conformance-only. OpenClaw remains
`excluded_forward_only`.

## State custody across M2-03

| State | Canonical owner | M2 Web responsibility |
| --- | --- | --- |
| task/run/checkpoint/events | API SQLite/task state and canonical event spine | typed read and immutable canonical projection |
| workspace bytes/patch transactions | WorkspaceManager/PatchTransaction runtimes | verified ranges, review model, typed transaction receipts |
| artifact bytes/revisions | LocalArtifactStore plus task artifact state | MIME/redaction/hash/range/cache/preview admission |
| PTY process/session/tickets | TerminalSessionRegistry/platform PTY/ticket authority | ANSI screen, tabs, replay/backpressure, explicit controls |
| browser process/session/actions/history | BrowserRuntimeRegistry and BrowserWorker owners | typed target/action/DOM/artifact/health projection and controls |
| scheduler/worker/recovery | existing Python scheduler/worker/recovery owners | timeline/topology/trace projection and typed focus |
| permission | existing permission runtime/PermissionCoordinator boundary | exact permit/denial receipts and display |
| causal trace view state | no canonical owner; disposable `CausalTraceController` | filter/fold/window/selection/pins only |

Disable/close tests prove that UI-local resources can be removed without
stopping a task, while removal of a claimed projector/runtime changes behavior
or fails closed. No view is a second canonical state or replay owner.

## Direct effective-code closure

The common frozen AST/scanner was run directly across the parent and numeric
stage intervals:

| Scope | Interval | Raw + / - | Effective | Minimum | Margin |
| --- | --- | ---: | ---: | ---: | ---: |
| M2-S03B-03 | `ca1d1b1..2bc9604` | 9,389 / 4 | 5,904 | 5,500 | 404 |
| M2-03B | `cc92c129..2bc9604` | 43,825 / 24 | 21,152 | 17,000 | 4,152 |
| M2-03 | `1833319a..2bc9604` | 80,786 / 142 | 40,309 | 32,000 | 8,309 |

The direct M2-03 classification contains 38,235 executable production lines
and 2,074 active UI-behavior lines. Presentation, declarations, schema/DTO,
Python owner integration, tests/fixtures, docs/comments, generated content,
adapter-only and vendor/source-pool material receive zero TypeScript/React
credit. The result is not an arithmetic sum or raw-numstat claim.

## Exact cleanroom

- Source: `git archive 2bc9604685cd7024825cf94a34c3fc507921d661`.
- Location: `G:/agent-zoo/.tmp/m2-s03b-03-cleanroom-2bc9604-02`, outside
  the Zyra package workspace so Bun cannot reuse parent workspace resolution.
- Dependency install: `bun@1.2.15 install --frozen-lockfile`; local
  `node_modules/.bin/bun.exe` verified.
- Web: 187 passed, 0 failed, 1,208 assertions across all 13 Web test files.
- Typecheck/build: passed, 243 modules bundled.
- Python: 78 passed across 15 applicable files, each file in an isolated
  process with a cleanroom-local pytest base.
- Manifest scan: 0 external `file:`, `link:`, editable, absolute parent-source
  or `../source-repo` dependencies.
- Trace/runtime scan: 0 source-repository-name dependencies and 0 OpenClaw
  matches.

The nested-cleanroom and combined-Python attempts are retained as diagnostic
evidence, not counted as passes. The nested path exposed Bun workspace upward
resolution. The combined Python process exposed shared environment/singleton
order sensitivity in older suites. Moving the archive outside the workspace
and executing each Python file in a fresh process produced the final green
reproducible result.

## Requirement review

M2-03 materially advances `REQ-TRACE-01`, `REQ-FAULT-01`, `REQ-TOPO-01`,
`REQ-EDGE-01`, `REQ-CLOSE-01`, `SCORE-UX` and `SCORE-ROBUST` through real API,
worker, event, artifact and control behavior. It does not close milestone-level
competition gates that require:

- two high-completion cross-domain live tasks;
- one sealed autonomous run with at least 2,000 effective canonical
  transitions and zero human intervention;
- real local/edge/cloud dispatch and multi-provider/model evidence;
- formal sparse/full/static topology and communication ablations;
- live visual capture, performance evidence, packaging rehearsal and final
  submission materials.

Those remain later M2/M3 exit work. They are not deferred implementation debt
inside M2-03, and none is falsely represented as complete here.

## Ledger/tooling limitations

The M2-03 slice-scoped synchronizers and target checks are green. The focused
ledger contract test is 6/6. The legacy generic verifier remains non-green for
three protected negative-audit string literals in M1 hardening code and reads a
stale protected `tmp/internalization_ledger.json`; the broad unit suite retains
two protected baseline expectations. This review makes no green claim for
those generic checks. The current 20 rows, source-role caps, target existence,
language custody, dependency boundary and cleanroom were checked directly.

## Completion boundary

The Zyra evidence commit is created after this report and its machine evidence
are staged. Root `G:/agent-zoo/docs/milestones/execution-state.yaml` is then
updated to record M2-S03B-03, parent M2-03B and numeric stage M2-03 closure and
to advance the next slice. The root is not a Git repository, so that state file
is outside the Zyra evidence commit.
