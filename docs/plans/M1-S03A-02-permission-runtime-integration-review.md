# M1-S03A-02 Permission Runtime Integration — Critical Review

## Review status

- Slice: `M1-S03A-02`
- Parent unit: `M1-03A`
- Baseline: `7470c74ca2f3584591659f678b8452108d185ca2`
- Implementation evidence: `544f8ee1e5a99fec80929f60b29464b04e996955`
- Review/evidence record: this file and its containing commit
- Review date: `2026-07-10`
- Slice verdict: **complete**
- Parent-unit verdict: **complete**
- Blocking findings: **none**

The permission foundation is now on the real CodeWorker, BrowserWorker and API
paths.  Allow, deny and ask decisions alter executor reachability; ASK creates a
durable exact-call continuation; approvals are consumed once; DENY and sealed
mode produce recovery inputs; Browser actions are gated before any browser
process or network callback; state can be queried and mutated through a
custody-bound control plane.

This review does not treat the source ledger, source graph, fixed contracts,
mock-only tests or vendored code as implementation evidence.

## Slice and parent objective coverage

| Objective | Status | Evidence | Blocking |
|---|---|---|---|
| Complete-read and source-to-target reconciliation | complete | 58 deterministic decisions in `permission/source_audit.py`; ledger sync is strict and repeatable | no |
| Parent allow/deny/ask runtime | complete | `ToolPermissionRuntime` is required by `ToolExecutionRuntime`; CodeWorker tests execute and block real tools | no |
| Hook/classifier/user approval composition | complete | deployment-owned extension registry, hook rewrite revalidation, advisory classifier, custody-bound user/API resolution | no |
| Exact scope and one-use authority | complete | canonical tool/server/schema/arguments/session/task/run/workspace binding and executor-side grant consumption | no |
| Durable ASK lifecycle | complete | request queue + continuation CAS state + CodeWorker session WAL payload/tombstone records | no |
| API/control/transport integration | complete | structured API facade, control plane, callback/bridge/mailbox projection and slash-command control | no |
| Browser action integration | complete | per-action permission gate on static/live backends; browser agent backend remains fail-closed | no |
| Sealed autonomous policy and recovery | complete | unknown/high-risk ASK becomes deterministic DENY with zero-human recovery input | no |
| Failure, replay and concurrency behavior | complete | duplicate resolve, forged identity, cross-session, expired approval, concurrent claimant and lost-receipt tests | no |
| Event/audit reachability | complete | permission request/decision/grant/mode/recovery events are returned by runtime and queryable through API | no |
| Disable/断开即失败 | complete | disabling runtime, rule store, request queue, decision log or browser gate changes real behavior and produces zero side effects | no |
| Slice 8,000-line production floor | complete | `+12,756/-289` production; conservative core `+9,667` after adapter/audit/export exclusions | no |
| Parent 16,000-line floor | complete | prior conservative foundation `+8,012` + this slice conservative core `+9,667` = `+17,679` | no |
| Clean directory and submission boundary | complete | archive of implementation commit; 141 core tests, strict seed audit and boundary verifier pass | no |

## Main-path evidence

| Source mechanism | Zyra-owned target | Runtime/API entry | State, event or control effect | Behavior evidence |
|---|---|---|---|---|
| Claude permission rules, modes and hook precedence | `packages/runtime/zyra_runtime/permission/**` | `ToolPermissionRuntime.guard` from `ToolExecutionRuntime` | canonical decision, one-use grant, denial recovery | permission unit 87; CodeWorker foundation 23 |
| Claude SDK control/question lifecycle | `permission/control_plane.py`, `api.py`, `transports.py` | `/permissions/**`, `/control`, slash permission commands | create/deliver/resolve/cancel/abort/mode/rule CAS | control/API tests; concurrent resolve winner |
| Claude QueryEngine ASK/resume lifecycle | `permission/continuation.py`, `claude_query_engine_runtime.py`, `claude_session_store.py` | `CodeWorkerRuntime.run` | park/deliver/ready/claim/complete/fail/expire plus WAL payload/tombstone | CodeWorker continuation 17 |
| Bash/PowerShell safety and secret egress patterns | `permission/shell_analysis.py`, `extensions.py` | permission evaluator before shell executor | structured spans, redirections, pipelines, path and secret evidence | shell/extension unit tests |
| browser-use action boundary | `permission/action_gate.py`, `zyra_workers/browser_worker.py` | `BrowserWorkerRuntime.run` | exact action grant, current/target URL binding, zero-action failure | Browser permission 17; Browser base 10 |
| opencode permission question/control patterns | control plane, API facade and continuation runtime | API resolve and CodeWorker resume | exact identity and resolve-once semantics | API control 20; source decision tests |
| AgentScope/Hermes/OpenClaw supplemental patterns | evaluator, control plane, risk and source audit | deterministic Zyra policy only | replacement behavior; no runtime dependency on source repositories | source coverage and behavior tests |

Deleting or disconnecting these targets changes observable behavior:

- without `ToolPermissionRuntime`, no registered CodeWorker tool reaches its handler;
- without the request queue or decision log, ASK/decision persistence fails closed;
- without continuation state, an approved shell call cannot resume;
- without the session WAL, a checkpoint failure loses the raw replay projection;
- without the Browser action gate, BrowserWorker explicitly refuses execution;
- without the control plane/API authority, a response cannot resolve a pending
  request by presenting only public request data.

## State custody and anti-fragmentation

| State | Authoritative owner | Restore/transport rule |
|---|---|---|
| rules, modes, requests, decisions, custody hashes, continuation metadata | `PermissionStateStore` | file-locked CAS; snapshot restore never overwrites a newer revision |
| raw parked tool payload | `CodeWorkerSessionStore` WAL | written before continuation park; tombstoned only after canonical terminal state |
| executor authority | in-memory signed one-use grant store | never serialized; exact validation and atomic consume at executor boundary |
| public permission/event projection | Zyra event store / API response | contains digests and causal IDs, never bearer or raw replay payload |
| API session bearer | transient custody envelope | returned once on session creation; rejected if echoed into task/action data |
| legacy JSON permission compatibility | non-authoritative compatibility surface | cannot approve the new runtime or replace `PermissionStateStore` |

There is one policy implementation, not separate API, worker and browser
approval tables.  API/control routes mutate the same request records consumed by
the worker guard.  Browser actions use the same state owner and grant semantics.

## Adversarial reliability findings resolved

The implementation was not accepted at the first green test run.  Independent
adversarial passes found and closed the following failures:

1. ASK request TTL could leave a permanent PARKED barrier.
2. A resolved-but-unused approval could remain `RESOLUTION_READY` after TTL.
3. A CLAIMED but unconsumed approval could block all different recovery actions.
4. A crash after approval consumption could replay an ambiguous external side
   effect; a durable semantic fence now blocks same-ID and new-ID/same-argument
   calls while allowing a genuinely different recovery action.
5. Continuation payload checkpoint ordering could lose raw replay state; WAL is
   now write-ahead and tombstone cleanup is retryable.
6. Tombstone failure could misreport an already committed tool as failed.
7. Partial batch claims could strand earlier claims without CAS compensation.
8. Browser live startup could occur before permission, and static click approval
   did not bind the resolved target URL.
9. Custody bearers copied into nested CodeWorker/Browser data could leak through
   telemetry; mappings over the scan bound now fail closed instead of ignoring
   tail entries.
10. Reference-only ledger rows were incorrectly marked materialized; they are
    now `candidate/inventoried` and excluded from production line accounting.

The outcome-unknown test executes a real shell side effect, injects process loss
after execution but before receipt settlement, advances the real permission
clock, and proves the marker remains `executed-once` after both replay forms.

## Source-to-target disposition

The audited set contains 58 decisions:

| Disposition | Count | Ledger treatment | Completion meaning |
|---|---:|---|---|
| active | 22 | productized / tested-main-path | Zyra owns the runtime behavior |
| adapter | 27 | productized / tested-main-path | Zyra owns protocol conversion and state integration |
| contract-only | 3 | candidate / inventoried | replacement contract recorded; not counted as materialized code |
| reference-only | 6 | candidate / inventoried | comparison/reference only; not counted as materialized code |
| deferred | 0 | n/a | no source item affecting this slice was deferred |

Coverage includes 47 Claude entries and 11 supplemental entries.  Claude batch
03 permission sources are covered `21/21`; the permission-relevant batch 09
subset is covered `20/70` while non-permission paths remain with their owning
units.  Contract/reference rows carry an explicit replacement, target, test and
owner but do not claim runtime ownership.

## Verification evidence

All commands used the checked-in project code and the repository virtual
environment; no source repository was imported at runtime.

| Command/suite | Result |
|---|---:|
| `python -m unittest discover -s tests -p "test_*.py" -q` | 434 passed, 489.448s |
| permission unit discovery | 87 passed |
| CodeWorker continuation integration | 17 passed |
| Browser permission integration | 17 passed |
| CodeWorker permission foundation | 23 passed |
| Browser base integration | 10 passed |
| API control integration | 20 passed |
| ledger unit/API/CLI regression | 25 passed |
| deterministic source ledger check | 58 decisions aligned |
| strict seed audit | 0 errors |
| `scripts/verify_submission_boundary.py` | passed |
| `git diff --check` / staged diff check | passed |

Cleanroom evidence:

- source: `git archive 544f8ee1e5a99fec80929f60b29464b04e996955`;
- location used: `G:\agent-zoo\.tmp-cleanroom-s03a02-544f8ee\repo`;
- no `.git`, `.venv`, cache, SQLite residue, build artifact or parent source
  repository is present in the archive;
- permission unit 87, CodeWorker continuation 17, Browser permission 17 and API
  control 20 all pass (`141` tests total);
- strict seed audit and submission-boundary verification pass.

## Effective line-count buckets

Baseline-to-implementation numstat:

| Bucket | Additions / deletions | Counts toward slice floor | Rationale |
|---|---:|---|---|
| production (`apps/**`, `packages/**`, excluding seed data) | `+12,756 / -289` | yes | reachable Zyra-owned runtime/API/worker implementation |
| conservative production core | `+9,667` | yes | also excludes API facade, transports, exports and source-audit code |
| tests | `+5,328 / -64` | no | behavior evidence only |
| productized ledger sync script | `+173 / -38` | no | conservatively excluded as auxiliary audit tooling |
| ledger seed data | `+5,574 / -1,263` | no | generated/data projection, never source-line evidence |
| docs/review | excluded | no | evidence narrative only |
| generated/mock/fixture-only | `0` counted | no | no such content supports the production floor |
| vendor/vendor-runtimes | `0` | no | no vendored implementation or source-pool dependency added |

The slice exceeds its 8,000-line minimum even under the conservative core
bucket.  Together with S03A-01's conservative 8,012 lines, the parent reaches
17,679 conservative production lines and exceeds the 16,000-line parent floor.

## Requirement and competition calibration

This slice advances the infrastructure and dynamic evidence for:

- `REQ-CLOSE-01`: real closed-loop execution is permission-gated;
- `REQ-TRACE-01`: permission decisions, grants, recovery and continuation state
  are causally observable;
- loop/robustness/algorithm scoring evidence: sealed recovery, failure
  injection, concurrency and deterministic state transitions.

No final competition gate is claimed closed here.  This slice does not itself
prove two cross-domain live scenarios, 2,000 canonical transitions, real
local/edge/cloud dispatch, multi-model compatibility or final benchmark
complexity evidence.  Those remain with their downstream owners.

## Residual debt and downstream owners

The following are real but non-blocking for M1-03A:

- `PermissionStateStore` canonical transitions and SQLite event projection do
  not share one physical transaction; state is authoritative and events carry
  causal IDs, but a durable cross-store outbox remains an M3 hardening item.
- The mailbox transport is a bounded local projection, not an external message
  service; external delivery belongs to later integration units.
- Browser agent mode intentionally fails closed until per-action execution is
  owned by `M1-04C`.
- MCP, SkillTool and AgentTool integration consume this permission runtime in
  `M1-03B`, `M1-03C` and `M1-03D`.
- Long-running execution lease renewal/heartbeat is a later watchdog concern;
  the current lease exceeds the supported tool timeout and fences ambiguous
  outcomes.
- Permission console interaction and approval UI belong to M2; sealed benchmark
  and packaging enforcement belong to M3.

None of these debts permits a bypass, causes a false completion claim, requires
a root-level source repository at runtime or blocks the next slice.

## Final verdict

`M1-S03A-02` is complete.  With `M1-S03A-01`, parent `M1-03A` is complete.
The next valid execution entry is
`slice-03b-01-mcp-client-runtime-foundation.md`.
