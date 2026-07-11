# M1-S03C-02 Skill Runtime Loader Integration — Critical Review

- Slice: `slice-03c-02-skill-runtime-loader-integration.md`
- Parent: `unit-03c-skill-runtime-loader.md`
- Baseline: `deb3061a062b49cc696d0396a6e3ecd921071987`
- Implementation commit: `85136f9`
- Review date: 2026-07-11
- Verdict: **slice complete; parent M1-03C complete**

## 1. Outcome

S03C-02 connects the S03C-01 loader foundation to the real CodeWorker,
session, API, permission, plugin, MCP and compact paths. The default worker
path is now:

`CodeWorkerRuntime -> SkillToolProjectionRuntime -> ToolExecutionRuntime / M1-03A -> SkillRuntime -> session checkpoint -> 02D compact projection`

The package-control path is:

`POST /tasks/{task}/skill-updates -> M1-03A exact decision -> SkillUpdateRuntime -> atomic filesystem transaction -> registry/plugin publication -> task checkpoint`

The integration does not invoke an upstream CLI, source checkout, sidecar,
Docker image, npm package, pip package or dynamic import to make a skill
decision. Claude Code, OpenCode, Hermes and Agent Framework mechanisms were
split and rewritten around Zyra's existing tool identity, event, permission,
artifact and checkpoint contracts.

Actual forked child execution remains owned by M1-03D. S03C-02 emits a typed,
deny-only child scope and immutable fork handoff without exposing the body to
the parent. Remote marketplace/search remains deferred. Active MCP skills are
accepted only as an M1-03B capability/resource projection.

## 2. Goal coverage matrix

| Slice / parent requirement | Status | Production evidence | Behavioral evidence | Blocking |
|---|---|---|---|---|
| Real CodeWorker/session tool path | Complete | `tool_projection.py`, `code_worker_runtime.py`, `executor.py`, `claude_query_engine_runtime.py` | projected `skill`, `read_skill_resource`, `list_skills`; real permission suspension and worker event/checkpoint tests | No |
| One shared SkillRuntime for handler and 03A hook | Complete | `ToolExecutionContext.runtime_services`; query engine consumes the projected runtime | identity assertion plus real 03A allowed-tools tests | No |
| Inline body/attachment disclosure changes the query session | Complete | `disclosure.py`, `task_integration.py`, `session_integration.py` | prepare/commit/abort lease test, one disclosure per invocation, no body in checkpoint | No |
| Query terminal lifecycle | Complete | `SkillToolProjectionRuntime.finalize_successful_query`, CodeWorker success path | inline invocation becomes completed with causal event/artifact refs; fork remains pending | No |
| Allowed-tools enforced by M1-03A | Complete | shared runtime permission hook and projected exact-grant provenance | outer ASK before execution; inner deny-only policy; disabled policy tests in S03C-01 | No |
| Body/resource lazy disclosure and containment | Complete | `tool_projection.py`, S03C-01 loaders | declared resource reads exact revision; traversal/missing/tamper fail closed | No |
| Event/session/checkpoint and 02D compact split | Complete | `compact_integration.py`, `outcome_commit.py`, `compact_bridge.py`, API lifecycle route | active refs restore necessary input; terminal outcomes persist immutable refs only; body never serialized | No |
| Causal terminal outcome | Complete | `SkillOutcomeCommitRuntime` and API evidence port | forged refs rejected; real task event/artifact IDs accepted | No |
| Plugin skills/commands/hooks provenance | Complete | `plugin_integration.py`, `sources/plugin.py`, `composition.py` | command resolution, declarative hook load, provenance, disabled rejection | No |
| Plugin disable/reload/last-good | Complete | persistent `.zyra/skill-control/plugin-state.json`, atomic snapshot swap | invalid manifest preserves last good; disable excludes current and future compositions | No |
| M1-03B MCP skill projection only | Complete | `mcp_discovery.py`, `mcp_integration.py` | exact capability revision/digest/resource bytes; request-scoped source cannot leak to later composition | No |
| Local install/update/rollback/control | Complete | `update_runtime.py`, `update_integration.py`, API route | real ASK -> persisted pending -> approval -> resume; publication failure rolls filesystem back | No |
| Update/revocation invalidates active state | Complete | rebuilt registry compares immutable refs, revokes changed invocations and removes hook/policy leases | update integration and S03C-01 revoked restore tests | No |
| Fork tool scope | Complete as M1-03D handoff | `fork_scope.py`, fork request projection | no parent grants/body; allow/deny intersection and immutable request | No; execution is correctly deferred |
| Remote search / marketplace | Explicitly deferred | health/search response reports local-only and deferred owner | API control-surface assertions | No |
| Disable-to-fail / disconnect | Complete | `disable_skill_runtime`, component health gates and source audit | API disabled flag, missing resource, permission suspension and source audit tests | No |
| Clean directory | Complete | commit archive `85136f9` | 78 tests, 1 Windows symlink skip; zero runtime references to root source repositories | No |
| S03C-02 minimum 8,000 production LOC | Complete | full production net 9,184; conservative net 8,945 | required numstat buckets below | No |
| Parent minimum 16,000 production LOC | Complete | S03C-01 9,078 + S03C-02 conservative 8,945 = 18,023 | both slice review records | No |

## 3. Main-path evidence

| Capability | Upstream mechanism | Zyra target | Runtime entry / semantic effect | Test surface |
|---|---|---|---|---|
| SkillTool projection | Claude `SkillTool`, OpenCode skill tool | `tool_projection.py` | three provenanced dynamic tools enter the actual ToolExecution registry before the query loop | `SkillToolMainPathTests` |
| Session disclosure | Claude process prompt/attachments and session hooks | `disclosure.py`, `task_integration.py`, `session_integration.py` | exact inline body is leased to one worker request, committed by event IDs, then not repeated | `TaskAndCompactIntegrationTests` |
| Permission ceiling | Claude/OpenCode allowed tools | projected runtime + M1-03A hook adapter | downstream calls are denied unless admitted by every active skill ceiling and 03A | foundation allowed-tools tests + CodeWorker ASK test |
| Query completion | Claude query/session lifecycle | CodeWorker + `finalize_successful_query` | successful query fires post hooks, terminalizes inline invocations and records evidence/artifact refs | projection lifecycle test |
| Compact split | Claude compact invoked-skills restore | `compact_integration.py`, `compact_bridge.py` | active input refs and terminal outcome refs are separated; bodies are reloaded, never checkpointed | compact/terminal tests |
| Plugin capabilities | Claude plugin commands/hooks/options and OpenCode plugin loader | `plugin_integration.py`, `composition.py` | typed commands and declarative hooks enter active composition; disable state survives process recreation | plugin tests |
| MCP skill discovery | Claude builder contract and Agent Framework MCPSkillsSource | `mcp_discovery.py`, `mcp_integration.py` | M1-03B server snapshot/resource read produces an exact-digest read-only source | MCP projection and no-leak tests |
| Package control | Hermes manager lifecycle and OpenCode install concepts | `update_runtime.py`, `update_integration.py`, API | trusted-local root, preflight, exact 03A approval, transaction, reload, rollback, persistent pending state | direct runtime and real HTTP API tests |
| Outcome handoff | Claude compact outcome reference; 06C boundary | `outcome_commit.py` | task-owned evidence/artifact IDs become immutable outcome projection; no second memory store | API lifecycle tests |

Deleting or disconnecting `SkillToolProjectionRuntime` removes the executable
tools from CodeWorker. Disconnecting the shared runtime service makes the
handler-created invocation invisible to the M1-03A hook. Disabling the skill
runtime makes the API fail closed. Removing package publication or plugin state
persistence causes the update/disable behavioral tests to fail. These are
semantic disconnects, not import-only or health-only checks.

## 4. State custody

| State | Owner | Persistence / restore boundary |
|---|---|---|
| skill registry, body/resource versions, allowed-tools, invocation | M1-03C `SkillRuntime` | existing task/session checkpoint as `skill_runtime_state` |
| projected CodeWorker tool handlers | M1-03C `SkillToolProjectionRuntime` | request-local; handlers/callbacks are never serialized |
| permission requests, decisions and execution grants | M1-03A `ToolPermissionRuntime` | deployment-owned permission state and custody token |
| pending local package update | M1-03C `SkillUpdateRuntime` | task metadata `skill_update_runtime_state`, digest protected |
| plugin disabled controls | M1-03C plugin integration | workspace `.zyra/skill-control/plugin-state.json`, atomic replace |
| task/session identity and canonical events | existing Zyra task store / EventRecord | SQLite checkpoint and event log |
| compact context | M1-02D with M1-03C typed references | active references only; exact body revalidated on restore |
| terminal outcome/evidence | M1-03C projection, downstream M1-06C | immutable refs/digests/policy only; no body or mutable tool grant |
| MCP connection/auth/capability state | M1-03B | 03C consumes typed snapshots/resources and owns no second MCP state |
| forked child execution | M1-03D | 03C owns handoff and deny-only tool scope, not execution |

LLM output does not decide permissions, package updates, plugin disable,
version invalidation or compact restore. All are deterministic Zyra-owned state
machines and exact-digest checks.

## 5. Source-to-target dispositions

The executable ledger is `default_skill_source_decisions()` in
`source_audit.py`. It contains 28 decisions: 17 active, 5 adapter, 1
contract-only, 1 reference-only and 4 deferred. S03C-02 changes the previously
open items as follows; the remaining S03C-01 decisions retain their prior
reviewed disposition.

| Source | Disposition | S03C-02 target / reason |
|---|---|---|
| Claude `SkillTool/{SkillTool,types}.ts` | active | `tool_projection.py`, real CodeWorker dynamic tool entry |
| Claude `loadPluginCommands/loadPluginHooks` | active | typed plugin command and declarative hook runtime |
| Claude `mcpPluginIntegration.ts` | adapter | plugin/MCP provenance boundary; all network custody stays in 03B |
| Claude `mcpSkillBuilders.ts` | adapter | exact index/body/resource projection through 03B |
| Claude `mcpSkills.ts` | deferred | upstream generated/no-op stub; not claimed as active |
| Agent Framework `MCPSkillsSource` | adapter | source lifecycle adapted to Zyra registry and M1-03B resource port |
| OpenCode command index | active | session command/invocation relation and API path |
| OpenCode plugin install | adapter | trusted-local, permissioned, rollback-safe package control; no remote marketplace |
| Hermes `skill_manager_tool.py` | active | install/update/rollback/enable/disable/revoke state machine |
| Hermes skills hub | deferred | remote marketplace/product distribution belongs to M3 |
| Claude plugin agents | contract-only | actual subagent execution remains M1-03D |
| Claude SkillTool UI | deferred | M2-01A console owner |

No deferred stub, marketplace inventory, Markdown body or source ledger record
is counted as production implementation. The active MCP path is the real 03B
projection, not the Claude stub.

## 6. Adversarial review and fixes

The safety review initially blocked completion. Every blocking finding was
resolved before `85136f9`:

1. The projection handler and QueryEngine permission hook used separate
   `SkillRuntime` instances. `ToolExecutionContext.runtime_services` now makes
   one request-owned instance authoritative.
2. Inline invocations had no production terminal edge. CodeWorker now
   terminalizes them only after a successful, non-suspended query and commits
   causal event/artifact refs; fork handoffs remain pending.
3. Pending package updates were process-local. Digest-protected snapshots now
   restore the exact request and re-run preflight before approved execution.
4. Filesystem commit and runtime publication were not one observable
   transaction. Publication failure now rolls the filesystem back and rebuilds
   from restored content.
5. Update invalidation was reported but not enacted. Changed active revisions
   are now revoked, policy unbound and hook leases cleaned before publication.
6. Plugin disable state was private and ephemeral. It is atomically persisted
   and excluded from both current and future compositions.
7. MCP external sources were held by a product-root singleton and could leak
   across requests. Worker/API composition is now request scoped.
8. API update failures did not return the one-time permission custody envelope,
   preventing a legitimate resume. The pending response now returns it without
   persisting it.
9. Permission events in update receipts retained dataclass objects and broke
   JSON serialization. They are normalized at the integration boundary.
10. Pending disable/rollback resumed through the install state machine. Resume
    now dispatches by original action.
11. Rollback of a first install could rebuild a composition with a missing
    target root. The rebuilder now removes absent roots.

No blocking finding remains. Actual M1-03D child execution and M3 marketplace
distribution are explicit downstream scope, not hidden incompleteness in this
slice.

## 7. Verification

### Passing gates

- Compile: `python -m compileall` over API/runtime/skills/workers/tests — passed.
- Slice and adjacent regression:
  `python -m unittest tests.unit.test_skill_runtime_foundation tests.unit.test_commands_skills tests.integration.test_skill_runtime_loader_integration tests.integration.test_mcp_codeworker_permission_integration tests.integration.test_api_control_commands`
  — **82 run, 1 skipped** in 64.824s. The skip is Windows symlink creation
  privilege; the NTFS junction containment test passes.
- New integration module alone — **13 passed**.
- Source/runtime audit — 28 decisions, 0 errors, 0 blockers.
- Cleanroom archive from `85136f9`:
  foundation + S03C-02 + MCP/permission + API — **78 run, 1 skipped** in
  66.756s.
- Cleanroom contains none of `claude-code-best`, `opencode`, `browser-use`,
  `OpenHands`, `agent-framework` or `dive`; runtime external-source path hits:
  **0**. Two denylist literals in `source_audit.py` intentionally assert that
  `../claude-code-best` and `../opencode` must not appear as dependencies.

### Full-repository command

The required `python -m unittest discover -s tests` was run and returned exit
1 after 552.8s. It is **not reported as passing**. The output identified the
existing Windows Browser page-readiness failure plus legacy clean-copy,
M1-01B/source-extract and evidence/line-count assertions tied to earlier frozen
baselines. The S03C-02 and adjacent suites above pass independently, including
the real HTTP API, CodeWorker permission and MCP paths. This is the same class
of aggregate Browser/source-audit limitation recorded by S03C-01; no S03C-02
targeted failure was observed.

## 8. Effective-line audit

Baseline: `deb3061a062b49cc696d0396a6e3ecd921071987`.

| Bucket | Added | Deleted | Net | Counts toward minimum |
|---|---:|---:|---:|---|
| `apps packages skills scripts` full production diff | 9,292 | 108 | 9,184 | Yes, subject to conservative exclusions |
| Conservative production core | — | — | 8,945 | Yes |
| `tests` | 648 | 14 | 634 | No; verification only |
| `vendor vendor-runtimes` | 0 | 0 | 0 | No; failure line remains clean |
| generated/data/runtime-assets/docs | 0 | 0 | 0 | No |

The conservative count excludes 205 lines of integration error taxonomy, 16
export-only lines and 18 net source-audit ledger lines from the full production
net. It does not count tests, documentation, manifests, Markdown skill bodies,
fixtures, generated data, vendor/source-pool material or adapters without
runtime custody. The remaining 8,945 lines implement executable state
transitions, projection, disclosure, plugin/MCP custody, package transaction,
checkpoint, API and failure handling.

Parent conservative total: S03C-01 `9,078` + S03C-02 `8,945` = **18,023**,
above the parent minimum of 16,000.

## 9. Competition and delivery calibration

This slice advances but does not close:

- `REQ-CLOSE-01`: CodeWorker can invoke a governed skill and continue through
  deterministic session/update lifecycle, but sealed multi-agent live runs are
  owned by later units.
- `REQ-TRACE-01`: invocation, permission, disclosure, update, event and artifact
  refs are causally linked; the final UI trace remains M2/M3 work.
- `REQ-MEM-01`: compact input and terminal outcome are versioned and body-free;
  reusable cross-session procedure memory remains M1-06C.

No competition requirement is marked closed. This is backend infrastructure
for later long-horizon, heterogeneous and recoverable live evidence.

## 10. Residual debt and final verdict

Non-blocking downstream debt:

- M1-03D must consume the fork request and tool scope for actual isolated child
  execution; parent body/grant inheritance remains prohibited.
- M1-06C must consume immutable outcomes for reusable procedure memory without
  copying body/resource/policy ownership.
- M2 must expose real skill/plugin/permission/compact events in the console.
- M3 owns remote marketplace/distribution and the aggregate clean-copy/source
  audit modernization.

There is no runtime dependency on a root source repository, vendor runtime,
external process or new third-party package. Main-path reachability, semantic
permission effects, failure recovery, cleanroom behavior and both line-count
thresholds are demonstrated. **M1-S03C-02 and parent M1-03C are complete.**
