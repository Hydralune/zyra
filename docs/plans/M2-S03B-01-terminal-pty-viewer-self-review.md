# M2-S03B-01 terminal PTY viewer critical self-review

## Verdict and immutable interval

- Slice: `M2-S03B-01`
- Baseline: `f54446ec7bcbec16f189c8f50d957bd67e76e94b`
- Implementation:
  `1bb50e7cf3f5f784ba11a4d107f1ec7730b7b9d8`
- Verdict: pass for the ordinary-slice gate and the matching
  new-subprocess upgrade gate.
- Conservative effective TypeScript/React: `6,315`, above the required
  `6,000`.
- Raw implementation interval: `15,688` additions and `3` deletions.

The implementation is one connected, task-scoped path:

`TaskDetail -> TerminalWorkbench -> TerminalRuntime -> typed TaskApi ->
ticketed WebSocket/HTTP API -> TerminalApiService ->
TerminalSessionRegistry -> platform PTY`.

It is not a static terminal mock. The integration suite opens an actual
platform PTY in a fresh temporary workspace and drives input, resize, output,
exit, tree termination, WebSocket replay and shutdown/recovery behavior.

## Source-role judgment and internalization

| Source | Frozen role | Selected mechanism | Zyra-owned result |
|---|---|---|---|
| `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | primary TypeScript implementation | workspace terminal tabs, reconnect cursor, writer/resize and session projection | strict contracts, browser runtime, reconnect, resize, tabs, diagnostics, protocol audit and transcript under `apps/web/src/features/terminal/**` |
| `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | supplementary TypeScript implementation | terminal panel lifecycle and safe completed/read-only projection | render, structured output and the React workbench |
| `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | supplementary TypeScript implementation | interactive input, ordered writes, bounded screen and backpressure | `input.ts`, `writer.ts`, `screen.ts` and `backpressure.ts` |
| `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` Rust PTY | conformance only | PTY input/resize/exit/tree-kill behavior | real-platform behavior tests only; no Rust source, native bundle or process |
| `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | conformance only | origin/auth/reconnect negative behavior | ticket/WebSocket tests only; no Hermes gateway or token owner |

The selected mechanisms were decomposed into Zyra modules and connected to
Zyra identities, permissions, workspaces, event persistence, artifact
membership, task API and UI. No upstream entry point or state store remains
in the runtime. OpenClaw was not read, restored, referenced or depended on.

The source ledger has five `M2-S03B-01` rows. Its synchronizer validates that
every target exists at the exact implementation commit and records
`root_source_runtime_dependency=false` and `external_pty_package=false`.

## Canonical custody and failure closure

| State or decision | Canonical owner |
|---|---|
| PTY/ConPTY process, handles, dimensions and process tree | Python platform driver selected by `spawn_pty` |
| terminal binding, lifecycle and persisted recovery | `TerminalSessionRegistry` plus `TerminalStateStore` |
| ordered cursor, replay window, redaction and spill boundary | `TerminalOutputJournal` |
| ticket nonce, origin, protocol and one-use consumption | `TerminalTicketAuthority` |
| create/input/kill allow, ask, deny and exact permit | existing TypeScript `PermissionCoordinator` |
| task workspace and revision | `WorkspaceManagerRuntime` |
| spill artifact revision and task membership | `LocalArtifactStore` and canonical task state |
| lifecycle/control/output causality | canonical event spine |
| selected tab, order, scroll and acknowledged-cursor reference | browser `TerminalTabStore`; no process/output truth |

There is no alternate pipe-shell, source-repository process, static-output or
Python permission-decision fallback. The tests prove that disabling the
terminal owner or permission owner fails closed, a denied/ask create never
spawns, malformed creation is rejected before spawn, creation-event failure
cleans the process/registry/state, and persisted live records recover as
orphaned instead of silently remaining `running`.

Closing a browser tab or WebSocket detaches only the viewer. Process
termination occurs solely through the explicit permission-gated kill path or
runtime shutdown. This separation is asserted in both TypeScript runtime and
real WebSocket tests.

## Security and protocol review

- Connect tickets are short-lived, origin-bound, version-bound and one-use.
  A rejected origin does not consume a valid ticket. Ticket issuance is
  serialized per connect generation, and closing a tab cancels a pending
  attempt and generation-fences stale sockets.
- Every server frame is version/binding/cursor validated. Duplicate and stale
  client sequences do not reach the PTY; output gaps cause deterministic
  resync rather than repainting untrusted data.
- Environment names containing credential/token/key/password material are
  rejected before spawn. Output secrets split across multiple PTY reads are
  held until they can be redacted safely. Binary output is redacted before
  artifact persistence, then represented by a bounded marker/reference.
- Server input uses a `128 KiB/s` token rate and `256 KiB` burst; oversized
  traffic receives a retryable `429`. Browser key/paste normalization and
  byte budgets are an additional projection guard, not the canonical guard.
- ANSI/CSI/OSC parsing is streaming and bounded. Only sanitized `http` or
  `https` hyperlinks are exposed; device, clipboard and unsupported controls
  never become browser actions.
- `ASK` on input/kill returns the exact permission request as retryable.
  The workbench forwards the permit ID for create, input and kill. Sealed
  human create/input/kill rejects before approval waiting or PTY mutation.

## Real behavior and upgraded subprocess evidence

The implementation commit passed:

| Command | Result |
|---|---|
| `python -m pytest tests/unit/test_terminal_pty_runtime.py tests/integration/test_terminal_pty_platform_integration.py tests/integration/test_terminal_websocket_integration.py -q` | `31 passed` |
| `bun test ./apps/web/test/terminal-pty-viewer.test.ts` | `17 passed`, `117 assertions` |
| `bun x tsc -p packages/core/typed-api-client/tsconfig.json` | pass |
| `bun x tsc -p apps/web/tsconfig.json` | pass |
| `bun run build:web` | pass; production bundle contains `213` modules |
| `python -m compileall apps/api/zyra_api packages/workers/zyra_workers/terminal` | pass |
| adjacent workspace/permission/artifact/event-spine pytest set | `30 passed, 14 subtests passed` |
| 01B workbench plus 03A artifact/diff Web regression | `64 passed`, `237 assertions` |

The real-platform tests cover:

- fresh temporary terminal and artifact state;
- actual PTY create, input, resize, output and exit;
- actual bounded process-tree kill;
- invalid spawn rejection before process creation;
- fresh API composition owning an actual platform PTY;
- real WebSocket upgrade, replay, ack, input, resize, ping and detach;
- rejected origin, one-use ticket and future/stale-cursor resync;
- state recovery and shutdown cleanup.

Production terminal paths were scanned for `node-pty`, `pywinpty`, `winpty`,
parent-source paths, npm links, pip editable paths, Docker/dynamic imports and
OpenClaw; there were zero matches. No package manifest, lockfile,
requirements, `pyproject.toml` or Dockerfile changed. A post-test
`Get-Process` audit found zero recent `cmd`/`ping` processes. Windows process
tree and shutdown proof rests on the real PTY tests, not on inaccessible
system-wide command-line inspection.

## Effective-code and anti-padding audit

The exact Git added-line audit reports:

| Bucket | Lines | Credit |
|---|---:|---|
| executable TypeScript production runtime | 5,943 | yes |
| active React UI behavior | 372 | yes |
| CSS/static JSX presentation | 633 | no |
| TypeScript declarations | 1,081 | no |
| schema/DTO/static data | 5 | no |
| Python PTY/API production | 4,583 | real production, but excluded from the TypeScript/React floor |
| TypeScript/Python tests and fixtures | 2,860 | no |
| comments/blanks/docs classification | 211 | no |
| adapter-only | 0 | no |
| vendor/source-pool | 0 | no |
| **conservative effective TypeScript/React** | **6,315** | **pass** |

The audit tool reports the Python production rows in its generic
out-of-scope/generated bucket because the gate deliberately accepts only
TypeScript/React. Those `4,583` lines are reviewed Python production, not
generated or vendored code; they receive zero floor credit.

Detailed review for every file with more than 500 raw added lines:

| File | Raw/effective | Symbol and reachable call-path judgment |
|---|---:|---|
| `apps/api/zyra_api/main.py` | 528/0 | terminal route recognition, owner composition, workspace/permission/event/artifact bindings and shutdown reset; reached by the real HTTP/WebSocket API tests; Python is excluded from the floor |
| `ansi.ts` | 633/530 | `AnsiStreamParser` and unsafe-control stripping feed `TerminalScreen` through `TerminalRuntime`; fragmented CSI/OSC tests cover the stream path |
| `contracts.ts` | 925/670 | strict binding/status/frame parsers and command encoder sit on every TaskApi/socket boundary; 247 declaration and 5 schema lines were excluded |
| `runtime.ts` | 757/644 | `TerminalRuntime` owns connect generations, ordered replay, ACK, resize/input and explicit kill; mounted by `TerminalWorkbench` and exercised through a real fake-transport behavioral path |
| `screen.ts` | 1,127/969 | `TerminalScreen` applies parser operations to cells, cursor, alternate screen and bounded scrollback; rendering/search tests consume the resulting projection |
| `terminal-workbench.tsx` | 603/372 | `TaskDetail -> TerminalWorkbench -> TerminalRuntime -> TaskApi/socket`; 193 presentation and 37 declaration lines were excluded. Its excluded ratio is above 30%, so handlers for create/connect/input/resize/kill/tab reconciliation were inspected individually and are reachable; static markup receives no credit |
| `terminal-pty-viewer.test.ts` | 973/0 | 17 direct behavior cases; entirely test-only |
| `drivers.py` | 931/0 | POSIX stdlib PTY and Windows ctypes ConPTY/job-object implementations behind `spawn_pty`; real create/input/resize/tree-kill tests; Python receives no floor credit |
| `runtime.py` | 1,037/0 | `TerminalSession` and `TerminalSessionRegistry` state machine, permission, replay and recovery; unit and real-platform API tests; Python receives no floor credit |
| `websocket.py` | 572/0 | frame codec, upgrade and connection lease around the registry; two real WebSocket integration tests; Python receives no floor credit |
| `test_terminal_websocket_integration.py` | 539/0 | real loopback WebSocket evidence; test-only |
| `test_terminal_pty_runtime.py` | 943/0 | canonical transition, permission, ticket, redaction, recovery, rate and disable evidence; test-only |

All counted production files have an effective ratio above 20%. The only
file over the 30% excluded threshold is the workbench, addressed above.
There is no ledger-only, generated, fixture-only, adapter-only or
vendor-shaped production credit.

## Ledger regression disclosure

The five new rows pass:

- synchronizer `--check`;
- exact implementation-commit target existence;
- `5` entries with `0` schema errors;
- the six ledger contract tests.

The broader two-file ledger run produced `16 passed, 2 failed`. Both failed
assertions reproduce unchanged at the exact baseline and implementation
commits:

1. a historical `M1-02B` query still expects the retired
   `packages/workers` target even though current-policy normalization maps it
   to `packages/runtime`;
2. the pre-existing seed contains two `source_repo=zyra` policy-validation
   errors, so a test expecting the entire seed audit to be clean fails.

The failures are therefore not regressions from the five terminal rows.
Changing protected historical rows or unrelated assertions to make this
slice green would violate the current scope. The remaining ledger tests pass
when those two baseline-known cases are deselected (`10 passed, 2
deselected`), and the contract file passes (`6 passed`).

## Residual risks and deferred validation

- Windows ConPTY creation temporarily adjusts inherited standard handles
  under a process-global lock because a redirected parent can otherwise
  invalidate pseudo-console startup. Restoration is `finally`-guarded and
  real-platform tests pass, but higher concurrency/load validation belongs to
  the M2-03 aggregate.
- The standard-library WebSocket handler is intentionally integrated into
  the existing API process; sustained fan-out and long-duration backpressure
  performance remain aggregate/exit concerns.
- Full cleanroom, broad cross-unit regression and packaging are deliberately
  deferred to the M2-03 numeric-stage aggregate as required by the slice.
  This slice ran the matching subprocess/path/dependency/fresh-state and
  adjacent owner upgrade checks instead.

None of these residual items provides an alternate state owner, weakens a
required failure path or blocks this slice.
