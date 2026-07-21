# M1-S06C-02 Skill Memory / Compact Restore Integration Review

Date: 2026-07-21

Review level: slice closeout and parent `M1-06C` cumulative closeout. The required `M1-06`
numeric-stage aggregate review remains the next authority entry and is not claimed here.

Baseline: `92185ba0a93e6e7468966b84807a517fec336b2c`

Implementation commit: `dec6086b06146cbbcb47a7c71348c205856db01b`

Review-fix commit: `a8d3b8049df771e69337dbb5f9e4f562e8e9a952`

Final review target and cleanroom target: `a8d3b8049df771e69337dbb5f9e4f562e8e9a952`

## 1. Conclusion

**修复后通过。** The slice connects the 06C foundation to real 03C, 02B/02D, 06A, 06B,
QueryEngine, CodeWorker, BrowserWorker and task API paths. It runs three identical-history and
identical-budget fidelity lanes, emits deterministic failures and downstream directives, restores
semantic continuity after resume, and never turns historical skill memory into execution authority.

The final detached cleanroom passed full configured TypeScript typecheck, Bun and Node builds, 68
TypeScript behavior tests, 18 isolated Python main-path/adjacent tests, ledger synchronization and
the current dependency scan. Conservative effective production is `7,209` lines; with 06C-01's
`8,186`, parent `M1-06C` reaches `15,395` lines. No unresolved blocker remains within this slice.

## 2. Findings fixed during implementation and review

### Implementation-time adversarial fixes

- Cross-language projection digests originally omitted the source worker/epoch metadata needed by
  the Python validator. The TypeScript export and Python reconstruction now share the same semantic
  digest input; corruption still fails before context mutation.
- A replayed Browser checkpoint was initially compared as if its mutable applied state were part of
  the immutable projection identity. Replay now binds only the projection digest while checkpoint
  state advances independently and exactly-once seeding remains intact.
- Browser projection metadata used camelCase while the consumer initially expected snake_case.
  Both contract spellings are normalized at the boundary.
- 06A `claim_delivery` was initially given an unsupported top-level `procedures` argument. 06B
  provenance now travels through the existing metadata and reference-only recovery checkpoint,
  preserving the 06A journal schema and owner.
- Static reachability omitted the already implemented dynamic Browser POST route. The colocated
  `ZYRA_DYNAMIC_API_ROUTES` declaration now exposes it to the ledger auditor.
- A real CodeWorker export initially reused CodeWorker tool names as Browser authority. The export
  now carries only explicitly declared `browser_allowed_tools`; the default is an empty authority
  projection, and every Browser action is still decided by the Browser TypeScript permission gate.

### Review-fix commit

The source ledger incorrectly stored Zyra target runtime names as upstream symbols and omitted
explicit source/target language, upstream callsite and upstream-test evidence. Commit `a8d3b80`
records verified upstream symbols for Claude Code, OMP and Hermes, accurately records that the
pinned Claude snapshot has no collocated compact/SkillTool tests, and preserves the Zyra behavior
tests as conformance evidence. Reachability remained `3/3` after regeneration.

## 3. Acceptance matrix

| Requirement | Runtime and test evidence | Status |
|---|---|---|
| Three comparable fidelity lanes | `ProviderRestoreFidelityRuntime` runs text, extractive+refs and experimental snapcompact over identical history/budget and records recall, token/image cost, latency and compatibility | PASS |
| Goal/constraint/change/evidence/version/policy/tool atomic recall | Lane comparison and attachment selection retain all named fact classes; tool call/result pairs are atomic | PASS |
| Vision/frame/gateway/provider fallback | Unsupported vision, missing/corrupt frame, gateway image loss and provider switch emit explicit failure/fallback receipts | PASS |
| Snapcompact remains experimental | Default off; disabling its adapter removes only image behavior and leaves the text/ref baseline operational | PASS |
| Current 03C authority | Real filesystem-backed 03C reload tightens tool scope and then revokes the skill; historical outcome memory cannot restore either right or body | PASS |
| CodeWorker same-session effect | Real `ClaudeRuntimeCore` compaction applies the integrated projection and emits the next-provider context contract | PASS |
| BrowserWorker same-session effect | Productized Browser runtime consumes the digest-validated projection, seeds 02D context once, persists its checkpoint and still uses the TypeScript permission gate | PASS |
| 06A + 06B composition | Real memory/code retrieval and validated `ReusableProcedureRuntime.context` results enter one bounded projection and reference-only checkpoint | PASS |
| Resume continuity | Checksummed semantic manifests restore archive/projection/retrieval/fidelity/failure state without embedding canonical memory or execution authority | PASS |
| Failure and downstream signals | Deterministic retry/circuit/reroute/replan/deny-resume records feed context, routing, recovery and control directives for 07C/08 | PASS |
| Disable/disconnect semantics | Disabling 06C removes outcome/restore effects while real 03C invocation remains; disabling 03C prevents fresh authority and cached execution | PASS |
| Dynamic reachability | Three productized ledger decisions are all reachable; exact event/API/runtime bindings are asserted | PASS |
| Clean submission boundary | Final cleanroom has zero parent-source, npm-link, editable-path, external Docker or OpenClaw runtime matches | PASS |
| Slice/parent production minimum | `7,209 >= 7,000`; `8,186 + 7,209 = 15,395 >= 15,000` | PASS |

## 4. Main paths and semantic effects

```text
03C SkillCoordinator current resolve/invoke
  -> immutable skill outcome evidence (no executable body)
  -> 06C outcome memory + 06A memory/code + 06B validated procedure projection
  -> identical-history fidelity comparison
  -> existing 02D compact boundary and 02B context assembly
  -> next CodeWorker provider context
  -> task metadata projection
  -> productized BrowserWorker 02D context seed
  -> Browser TypeScript permission decision for every action
```

The default Browser handoff transfers context only. An empty exported Browser tool scope is valid
and does not authorize anything; an explicitly exported tool scope must be a subset of the current
Browser request scope, and any intersection with current denied tools fails before context mutation.

The recovery composition is similarly reference-only. It stores memory query IDs/digests,
procedure query IDs/digests and selected IDs, but no memory rows, skill bodies, registry snapshot,
workspace content or index dump. A corrupted digest, foreign task/session/run, epoch regression,
missing provenance or lost canonical checkpoint produces a deterministic failure and never silently
falls back to execution authority.

## 5. Internalization and source decisions

| Role | Source/language | Migration mode and target | Canonical owner preserved |
|---|---|---|---|
| Primary | `claude-code-best` `c57f5a2`, TypeScript | Cropped retained TypeScript compact/SkillTool lifecycle in `@zyra/skill-memory-runtime`; bounded Python context port only at Browser boundary | 03C skill authority; 02B context; 02D compact |
| Supplementary | `oh-my-pi` `c6b83c1`, TypeScript | Cropped TypeScript compact modes/frame protocol and default-off ablation | 06C fidelity comparison only |
| Supplementary | `hermes-agent` `44ddc55`, Python | Bounded projection/context adaptation into real 06A/06B stores and Browser context | `ReusableProcedureStore`, `SQLiteStore.memory_records` |
| Experimental | snapcompact | Default-off image lane; no active-real or visual-effectiveness claim | None |
| Forward excluded | OpenClaw | No read, source row, test duty, code, path or runtime dependency | None |

The production code is decomposed into Zyra modules rather than an upstream directory shape:

- `authority-runtime.ts`: current-policy receipts, version/trust/body digest comparison and revoked/
  tightened fail-closed decisions;
- `fidelity-runtime.ts`: three-lane comparison, Unicode/CJK-safe fact recall, frames and fallbacks;
- `retrieval-composition-runtime.ts`: bounded 06A/06B/outcome/archive composition;
- `resume-continuity-runtime.ts`: semantic checkpoint manifests and resume-loss guards;
- `continuity-failure-runtime.ts`: deterministic failure/circuit ledger and downstream claims;
- `consumer-directive-runtime.ts`: context/routing/recovery/control/audit contracts;
- `integration-runtime.ts`: one orchestration owner embedded in `SkillMemoryApplication` snapshots;
- `skill_memory_context.py`: cross-runtime digest validation, 02D Browser context seed, checkpoint,
  replay and terminal delivery state;
- QueryEngine, BrowserWorker and API glue: real main-path registration, persistence and events.

No external package, CLI, sidecar, Docker image or root source repository makes a compact, skill,
permission, retrieval or recovery decision at runtime.

## 6. State custody

| State | System of record / commit boundary | Restore and test evidence |
|---|---|---|
| Current skill version/trust/policy/tools | 03C `SkillCoordinator` registry and invocation journal | Real reload tighten/revoke test; 06C stores only digests/receipts |
| Outcome/archive/fidelity/integration/failure | `SkillMemoryApplication` inside the E01 checksummed TypeScript snapshot | Snapshot checksum, same-scope restore, resume-loss and disable tests |
| Compact boundary and context epoch | Existing 02D compact/restore runtime and 02B assembly | Real QueryEngine compaction and next-context event |
| Canonical memory and 06A retrieval | `SQLiteStore.memory_records` plus 06A query/delivery journals | Real temporary SQLite retrieval and delivery settle/repair tests |
| Reusable procedure | `ReusableProcedureStore` and 06B curator evidence | API/runtime query, provenance, idempotency and validation tests |
| Browser restored context | API task metadata checkpoint plus `BrowserSkillMemoryContextRuntime` delivery receipt | Productized Browser run, replay, corruption, epoch and tool-scope tests |
| Downstream retry/reroute/replan contract | 06C failure snapshot and events; execution remains 07C/08-owned | Circuit/resume-loss tests and consumer directive assertions |

All stores use deterministic IDs/digests and explicit prepare/apply/commit/release or checkpoint
boundaries. LLM output is not used to decide permission, authority, fallback, resume validity,
failure routing or compact ownership.

## 7. Effective line review

Final range `92185ba..a8d3b80`:

- `whole_commit_added`: `9,663` (`41` deleted);
- `raw_production_candidate`: `7,652` before export-only and comment/blank exclusions;
- export-only `index.ts` / `__init__.py`: `19`, excluded;
- tests: `1,085`, excluded;
- ledger synchronization script: `379`, excluded;
- ledger seed/data: `547` added / `9` deleted, excluded;
- blank/comment-only production additions: `424`, excluded conservatively;
- `conservative_effective_production`: **`7,209`**;
- generated, docs, runtime assets, vendor/source-pool, mock/fixture-only, dead/unreachable and
  unconnected thin-adapter code counted as production: `0`;
- minimum: `7,000`; shortfall: `0`.

The repository linecount tool reports `raw_added=9,663`, `effective_added=9,116`,
`excluded_added=547`, `shortfall=0`. That broader tool includes tests/tooling in its effective
surface, so the production-only `7,209` value is the acceptance count.

Parent cumulative production is `8,186` (06C-01) + `7,209` = **`15,395`**, above the `15,000`
parent minimum. Line count does not substitute for the behavior evidence above.

## 8. Tests and verification

Main worktree:

- `bun run typecheck`: pass for all configured TypeScript workspaces;
- targeted TS runtime/E01/skill-memory suite: `68 passed`, `350` assertions;
- targeted Python Browser/reachability/06A/06B suite: `18 passed`;
- source ledger sync: aligned, `3` decisions;
- reachability contract: `3/3`, `0` unreachable;
- current-diff forbidden dependency matches: `0`;
- `git diff --check`: pass.

Final exact detached cleanroom `a8d3b8049df771e69337dbb5f9e4f562e8e9a952`:

- `bun install --frozen-lockfile`: pass with a cleanroom-local cache;
- full configured TypeScript typecheck: pass;
- Bun build: pass, `243` modules, `5.0 MB`;
- Node build: pass, `243` modules, `5.0 MB`;
- TypeScript behavior tests: `68 passed`, `350` assertions;
- Python imported `zyra_memory` and `zyra_workers` only from the detached worktree;
- isolated Python tests: `18 passed in 36.39s`;
- ledger sync: aligned, `3` decisions;
- linecount gate: pass;
- forbidden runtime dependency matches: `0`;
- generated local Bun cache was removed and detached `git status --short` was empty.

Default-path, clean-state, disconnect, corruption, permission/tool-scope, fallback, resume-loss and
procedure-provenance behaviors were executed. A full repository suite was not run because this is a
slice/parent closeout, not the required `M1-06` numeric-stage aggregate; the full applicable suite,
global ledger/source-to-target audit and historical adjacent failures remain mandatory at that next
review layer.

## 9. Adjacent known failures

Exploratory protected-suite failures are not regressions from this range. They reproduce on baseline
`92185ba`: old 02D tests expect unconnected Python `restore_integration_ok`/
`runtime_state_checkpoint_ok` metadata; the HTTP-SSE test suspends on the current permission policy;
old static/live Browser tests call gateway-owned actions without an installed SandboxGateway; and
one Browser-message provider-selection assertion was already recorded by 06C-01. This slice does
not reintroduce Python as compact owner or bypass the gateway/permission runtime to make stale tests
green. These facts must be classified in the `M1-06` aggregate review.

## 10. Competition requirements

| Requirement | Change from this slice | Gate status |
|---|---|---|
| `REQ-MEM-01` | Real compact/restore, retrieval-driven next context, cross-worker checkpoint and exact semantic resume evidence added | Advanced; overall competition gate remains partial |
| `SCORE-ALGO` (10) | Deterministic three-lane fidelity comparison, authority revalidation and recovery contracts now have implementation and ablation evidence | Advanced; no final score claimed |
| `REQ-FAULT-01` | Resume loss, provider/gateway/frame faults and deterministic retry/reroute/replan contracts are exposed for 07C/08 | Downstream contract only; gate remains partial |

This infrastructure does not claim to close the two cross-domain live runs, 2,000 canonical
transitions, dynamic topology, real edge/cloud dispatch, multi-provider compatibility or final
submission evidence gates.

## 11. Commit and state boundary

- Baseline: `92185ba0a93e6e7468966b84807a517fec336b2c`.
- Implementation: `dec6086b06146cbbcb47a7c71348c205856db01b`.
- Review-fix: `a8d3b8049df771e69337dbb5f9e4f562e8e9a952`.
- Final review/cleanroom target: `a8d3b8049df771e69337dbb5f9e4f562e8e9a952`.
- Review/evidence commit: created after this report.
- Root `G:/agent-zoo/docs/milestones/execution-state.yaml` is outside the Zyra Git repository and
  is updated only after the review/evidence commit exists.

## 12. Next step

Run the required `M1-06` numeric-stage aggregate review before entering `M1-07`. It must use the
full numeric-stage baseline, classify the protected adjacent failures above, run the applicable
cross-unit suite and full ledger/source-to-target audit, and update evidence if it finds/fixes any
cross-unit defect. No slice-local blocker remains.
