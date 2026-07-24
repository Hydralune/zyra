# M2-S03B-01 terminal PTY viewer preimplementation decision

## Frozen identity

- Slice: `M2-S03B-01`
- Parent: `M2-03B`
- Decision status: `approved_before_production_change`
- Baseline commit:
  `cc92c129ead234a82cf23dad5a1c32e3bf35f06f`
- Parent effective-code baseline:
  `cc92c129ead234a82cf23dad5a1c32e3bf35f06f`
- Implementation commit: `pending`
- Evidence commit: `pending`
- Required conservative effective TypeScript/React floor: `6,000`
- Parent cumulative floor: `17,000`
- Protected predecessor: `M2-S03A-02`

The baseline worktree is clean. This decision is committed before any
production or direct-test change. Effective-code accounting for this slice
will use the exact `baseline_commit..implementation_commit` interval.

## Exact source-to-target decision

| Capability | Role | Source repository and commit | Exact source paths | Source language | Zyra target | Target language | Migration mode | Canonical owner after integration |
|---|---|---|---|---|---|---|---|---|
| Workspace-scoped terminal tabs, reconnect cursor, short connect ticket, resize coalescing, serialized buffer handoff and exited-session removal | `primary_implementation` | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/app/src/context/terminal.tsx`; `packages/app/src/components/terminal.tsx`; `packages/app/src/pages/session/terminal-panel.tsx`; `packages/app/src/pages/session/terminal-panel-v2.tsx`; `packages/app/src/utils/terminal-writer.ts`; `packages/app/src/utils/terminal-websocket-url.ts` | TypeScript/TSX | `apps/web/src/features/terminal/**`; typed terminal client and TaskDetail mount | TypeScript/React | cropped, same-language component/runtime integration | Browser terminal runtime owns only bounded display/tab refs; PTY/session/cursor/output truth remains in the Zyra gateway |
| Dedicated terminal panel availability, hidden/inactive lifecycle, safe fitting and read-only completed/archived projection | `supplementary_implementation` | `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | `frontend/src/components/features/terminal/terminal.tsx`; `frontend/src/hooks/use-terminal.ts`; `frontend/src/services/terminal-service.ts`; `frontend/src/utils/parse-terminal-output.ts` | TypeScript/TSX | `apps/web/src/features/terminal/view/**`; terminal availability and completed-session projection | TypeScript/React | cropped, same-language behavior integration without xterm dependency | Browser view only; it cannot own process state, replay or permission |
| PTY input normalization, ordered write queue, resize propagation, bounded output summary/spill and explicit running/exited/killed/timed-out phases | `supplementary_implementation` | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | `packages/coding-agent/src/tools/bash-interactive.ts`; semantic conformance from `crates/pi-natives/src/pty.rs` | TypeScript primary selected range; Rust conformance range | `apps/web/src/features/terminal/input.ts`; `writer.ts`; `screen.ts`; `backpressure.ts`; terminal protocol projection | TypeScript | bounded same-language semantic integration; Rust PTY implementation is conformance-only and is not copied, loaded or launched | Browser input/output projection only; no process or native runtime ownership |
| Real PTY/ConPTY lifecycle, session/workspace binding, cursor buffer, spill, process-tree termination and crash recovery | `zyra_owned_new_owner` | current slice specification and Python standard-library platform APIs | no external implementation source | Python | `packages/workers/zyra_workers/terminal/**`; `apps/api/zyra_api/terminal_api.py` | Python | new Zyra-owned platform implementation; POSIX `pty`/`os` and Windows ConPTY via `ctypes`; no external binary/package | `TerminalGateway`, `TerminalSessionRegistry` and `TerminalStateStore` |
| Interactive stdin/kill permission and sealed deterministic denial | `existing_primary_owner_integration` | Zyra M1 E02 TypeScript permission runtime at baseline | `packages/runtime/claude-runtime/src/permission/**`; existing Python typed effect port | TypeScript with Python port | terminal gateway mutation path and terminal API receipts | TypeScript/Python | exact-call enforcement and permit claim; no Python decision fallback | `typescript.PermissionCoordinator` |
| Workspace custody, artifact spill and canonical event correlation | `existing_primary_owner_integration` | Zyra M1/M2 runtime at baseline | `packages/workspace/zyra_workspace/**`; `packages/runtime/zyra_runtime/artifacts.py`; canonical SQLite/event spine | Python | terminal gateway, task-scoped API, spill artifact and event projection | Python | direct owner binding and additive terminal projection | `WorkspaceManagerRuntime`, `LocalArtifactStore`, task state and canonical event log |
| Ticket origin/auth/replay conformance | `conformance_only` | Hermes pinned repository state recorded by the parent source graph | `tui_gateway/server.py`; `tui_gateway/ws.py`; `hermes_cli/web_server.py`; associated dashboard auth/reconnect tests | Python | terminal ticket and WebSocket behavior tests | Python/TypeScript tests | conformance only; no production source migration or Hermes process | Zyra ticket authority and gateway remain sole owners |

OpenClaw is excluded forward-only. It will not be read, restored, cited as an
implementation source, tested, imported or introduced as a dependency.

## Complete implementation boundary

The slice will implement one connected production path:

1. A task-scoped HTTP endpoint opens a terminal against the task's canonical
   workspace binding after validating command, cwd, shell, rows/columns,
   session/worker/tool/span identities and permission material.
2. The Zyra gateway opens a real platform PTY. POSIX uses the standard-library
   PTY primitives; Windows uses the documented ConPTY API through `ctypes`.
   No `node-pty`, `pywinpty`, Bun runtime, source repository process or opaque
   native executable is introduced.
3. A short-lived, one-use connection ticket binds task, run, terminal,
   permission session, workspace, origin, protocol version, cursor window and
   expiry. The WebSocket upgrade validates all bindings before exposing
   output or accepting input.
4. The gateway owns ordered output chunks, byte cursors, ANSI-preserving text,
   binary detection, bounded memory, acknowledgement/backpressure state,
   large/binary spill artifacts, exit/kill/crash state and process cleanup.
5. WebSocket input and kill frames are re-authorized by the existing
   TypeScript permission owner. Resize frames are identity-bound, sequenced
   and coalesced; stale resize/input/ack frames are rejected.
6. Viewer disconnect only detaches a connection lease. It does not kill the
   PTY, change task state or discard output. A new one-use ticket reconnects
   from the last acknowledged cursor or receives a deterministic resync/spill
   receipt when the cursor is stale.
7. Terminal lifecycle/output/control facts are emitted to the canonical event
   spine and correlated to command, worker, tool call, span, artifact and
   state mutation identities. Spill artifacts join the task's artifact list.
8. The TypeScript runtime validates every wire frame, maintains bounded screen
   projection and tab references, parses ANSI/OSC state, coalesces writes,
   applies cursor acknowledgements and persists only tab/view preferences.
9. The React workbench exposes create, reconnect, input, resize, kill, tab,
   status, cwd, exit, spill and structured-output states from the real API.
10. Sealed competition mode rejects human terminal creation, stdin and kill
    before approval wait or PTY mutation, records only a denied intervention
    attempt and keeps `human_intervention_count` equal to zero.

Browser/DOM/download control and the cross-view causal trace explorer remain
owned by `M2-S03B-02` and `M2-S03B-03`; this slice only emits the terminal
correlation fields those later slices consume.

## Canonical state custody

| State | Canonical owner | Slice responsibility |
|---|---|---|
| PTY process, OS handles, process tree and terminal dimensions | `TerminalGateway` platform driver | Open, resize, write, wait, terminate and close exactly once |
| Terminal identity, task/run/session/workspace binding and lifecycle | `TerminalSessionRegistry` plus `TerminalStateStore` | Validate transitions and recover persisted nonterminal records as crashed/orphaned after API restart |
| Ordered output cursor, bounded replay window, spill boundary and connection acknowledgement | `TerminalOutputJournal` within the gateway | Sole terminal replay owner; browser has only the last acknowledged cursor |
| Short ticket, origin, protocol version and one-use nonce | `TerminalTicketAuthority` | Issue and consume; never persist bearer material in browser storage |
| Workspace root and task membership | `WorkspaceManagerRuntime` and task store | Resolve internally; physical roots never cross the API |
| Permission decision, ASK continuation and exact execution permit | `typescript.PermissionCoordinator` | Enforce create/input/kill calls and fail closed if unavailable |
| Spill bytes and immutable revision | `LocalArtifactStore` plus task artifact membership | Publish bounded output or binary spill and correlate it to terminal/event identities |
| Terminal event visibility | Canonical runtime event spine / SQLite event log | Persist lifecycle, control, output summary and artifact facts; no terminal-only event database |
| Tab order, selected tab, scroll position and last acknowledged cursor reference | Browser `TerminalTabStore` | Bounded view state only; no process truth, output replay store or permission state |

No existing owner is transferred. Terminal state is a new owner explicitly
allocated by M2-03B. Public additions are versioned and additive. Disabling
the terminal gateway, permission owner, workspace owner, artifact spill owner
or event persistence fails closed; there is no pipe-shell or static-output
fallback.

## Security and failure policy

- Create, input and kill requests bind task, run, session, worker, terminal,
  workspace revision, tool call, command, span and actor. Client-supplied
  workspace paths cannot select a server directory.
- The process environment begins from a restrictive allowlist. Explicit env
  overrides reject secret/credential/token/key/password names and control
  characters. Output redaction removes configured secret values and common
  credential formats before replay, event or artifact persistence.
- Command and terminal input have independent byte/rate budgets. Paste
  bursts, oversized frames, stale sequence numbers and unacknowledged output
  beyond the window trigger deterministic backpressure rather than unbounded
  memory.
- ANSI control sequences are preserved for the terminal parser while OSC
  title/link/file payloads are bounded and sanitized. Device-control,
  clipboard and unsupported private sequences never become browser actions.
- Binary output is not decoded as trusted text. It is summarized, hashed and
  spilled as an artifact; only a replacement marker and artifact reference
  enter the text projection.
- Ticket TTL, nonce, origin, protocol version and all custody identities are
  checked at upgrade. Cross-session, cross-task, replayed, expired and
  stale-version tickets fail before any output is revealed.
- Closing a viewer only closes its WebSocket lease. Explicit kill is a
  separate permission-gated control transition.
- API shutdown terminates and drains all Zyra-owned PTYs before returning.
  A process crash cannot silently leave a persisted session marked running.
- Sealed mode never creates an approval request for human terminal actions and
  never increments successful human intervention.
- Physical workspace paths, environment values, tickets, custody tokens and
  raw credentials are absent from frames, events, artifacts and logs.

## Planned production modules

The TypeScript/React feature will be split by runtime responsibility:

- strict terminal wire contracts, versions and identity validation;
- ticket/connect URL construction and reconnect state machine;
- ordered writer queue and acknowledgement fencing;
- bounded replay/backpressure projection;
- ANSI/CSI/OSC parser and terminal screen/cell model;
- input/key/paste normalization and rate budgeting;
- tab persistence migration, pruning and active-tab selection;
- lifecycle, exit, crash and artifact-spill view models;
- structured output classification and secret-safe rendering;
- resize observation/coalescing and stale-result fencing;
- terminal client/runtime/controller and React workbench.

The Python owner will be split into:

- platform-neutral PTY driver contracts;
- POSIX PTY and Windows ConPTY drivers;
- process-tree lifecycle and shutdown supervision;
- session/lifecycle registry and persisted recovery records;
- ordered output journal, cursor replay and backpressure;
- binary/large-output spill and redaction;
- ticket issue/consume authority;
- WebSocket frame codec and connection lease;
- permission/workspace/event/artifact integration;
- task-scoped HTTP/WebSocket API composition.

Python is necessary platform/control glue and canonical process custody, but it
does not count toward the declared TypeScript/React effective-code floor.

## Direct behavior-test plan

Browser/runtime tests will cover:

- strict frame/version/identity decoding and malformed-frame rejection;
- ANSI colors/styles, erase, cursor movement, wrapping, wide characters,
  alternate screen and bounded OSC handling;
- ordered output writes, cursor acknowledgement, duplicates, gaps, stale
  replay, resync and spill references;
- tab migration, workspace scoping, stable selection, pruning and close
  behavior without process mutation;
- input/key/paste normalization, input budgets and sealed read-only states;
- resize coalescing, resize/input race ordering and stale resize rejection;
- reconnect backoff, one-ticket-per-attempt, abnormal/normal close and owner
  disabled behavior;
- running/exited/killed/timed-out/crashed/permission-pending projections;
- workbench mount, inactive/completed read-only states and structured output.

Backend tests will use real temporary workspaces and PTYs to prove:

- a real interactive shell reports cwd, ANSI output and exit status;
- stdin reaches the PTY, resize reaches the platform driver and kill ends the
  process tree;
- ticket auth, origin, expiry, replay, version and cross-session/task binding;
- reconnect from an exact cursor, stale-cursor resync and viewer close without
  PTY termination;
- concurrent resize/input ordering and duplicate frame rejection;
- child crash, API shutdown and persisted-running recovery;
- large output spill, binary output spill, bounded replay and backpressure;
- output/env/credential redaction across WebSocket, event and artifact;
- permission allow/ask/deny and exact permit behavior for create/input/kill;
- sealed human create/input/kill denial with zero human intervention;
- command/tool/span/artifact/state-mutation correlation in canonical events;
- disabling terminal, permission, workspace, artifact or event owners fails
  closed and causes the corresponding real behavior test to fail.

## Effective-code gate

Only executable TypeScript/TSX production logic and active UI behavior in
`baseline..implementation` may count. Imports, comments, blanks, interfaces,
type-only declarations, static schema/data, CSS/static JSX, tests, fixtures,
generated code, Python, docs, source-ledger entries and adapter-only code are
excluded. UI behavior and UI presentation will be bucketed separately.

The implementation must contain non-zero original-language production from
the opencode primary chain and both OpenHands/OMP supplementary TypeScript
chains. OMP's Rust PTY source remains conformance evidence only; it is not
claimed as a same-language production migration. The slice fails if any active
TypeScript source becomes ledger-only or test-only.

After the implementation commit, the audit must establish at least `6,000`
effective TypeScript/React lines for this slice. Files with more than 500 raw
added lines, effective ratio below 20%, or excluded ratio above 30% receive a
detailed symbol/call-path/reachability review.

## Upgrade-trigger assessment

This slice intentionally introduces a new Zyra-owned local PTY subprocess
owner on the default terminal path. That subprocess is explicitly assigned by
the current unit, so it is not a transfer of an existing canonical owner, but
it still matches the new-subprocess high-risk trigger.

The implementation therefore requires the matching upgraded validation:

- real platform PTY create/input/resize/exit/kill/process-tree behavior;
- API shutdown and fresh-process orphan recovery;
- exact dependency/process/path audit proving no source repository,
  `node-pty`, `pywinpty`, Bun/native sidecar, editable path or second replay
  store;
- a fresh temporary-state API integration run with empty terminal/artifact
  state;
- targeted adjacent workspace, permission, artifact, event-spine and web
  console regression.

It does not require an unrelated full-repository suite at this ordinary slice;
the M2-03 numeric-stage aggregate still owns the full cleanroom and broad
cross-unit regression. If implementation reveals an incompatible public
schema, global permission change, external dependency, new port or owner
transfer, production work stops and this decision is amended and committed
before crossing that additional boundary.
