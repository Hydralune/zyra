# M1-S03C-01 Skill Runtime Loader Foundation — Critical Review

- Slice: `slice-03c-01-skill-runtime-loader-foundation.md`
- Parent: `unit-03c-skill-runtime-loader.md`
- Baseline: `3fc23e5`
- Implementation commit: `f9d60b8`
- Review date: 2026-07-11
- Verdict: **slice complete; parent M1-03C remains open for S03C-02**

## 1. Outcome

This slice replaces the former metadata-only skill registry with a Zyra-owned, session-aware runtime. The default path is now:

`SkillRegistry -> SkillBodyResourceLoader -> SkillAllowedToolsPolicy -> SkillInvocationRuntime -> InvokedSkillState -> SkillSessionBridge -> 02D/CodeWorker`

The read-only downstream path is:

`InvokedSkillState -> immutable SkillCompactReference / SkillOutcomeProjection -> 02D compact restore / M1-06C consumer`

The implementation does not execute an upstream CLI, sidecar, Docker image, npm package, pip package, or root-workspace source repository. Plugin command/hook integration, MCP-backed skills, marketplace/update approval, and actual forked child execution remain assigned to S03C-02 and M1-03D rather than being falsely marked complete.

## 2. Goal coverage matrix

| Slice requirement | Status | Production evidence | Behavioral evidence | Blocking |
|---|---|---|---|---|
| Source decisions for Claude, OpenCode, Hermes, Agent Framework | Complete | `packages/skills/zyra_skills/source_audit.py` contains 28 explicit decisions | `SkillRuntimeAuditor` reports 28 decisions, 0 errors, 0 blockers | No |
| Managed/user/project/add-dir/plugin/MCP source and provenance model | Complete for loader foundation; MCP execution deferred | `models.py`, `sources/base.py`, `sources/filesystem.py`, `sources/plugin.py`, `sources/mcp.py`, `precedence.py` | precedence, project trust, plugin source and source audit tests | No; MCP active execution belongs to S03C-02 |
| Listing separated from body/resource disclosure | Complete | `attachments.py`, `body_loader.py`, `resource_loader.py`, `search.py` | listing does not increment body/resource load counters; body/resource exact-digest tests | No |
| Strict frontmatter and filesystem boundary | Complete | `frontmatter.py`, `path_security.py`, `revision_builder.py` | duplicate key, YAML alias/tag/merge, unknown security field, traversal, absolute path, NTFS junction and digest tamper tests | No |
| Versioned registry, precedence, tombstones, atomic reload/rollback | Complete | `registry.py`, `revision_store.py`, `change_detector.py`, `reload.py`, `atomic_update.py` | concurrent readers, invalid reload last-good preservation, exact lifecycle rollback, content-hash change detection | No |
| Allowed-tools enters the real M1-03A permission gate | Complete | `policy.py`, `invocation_permission.py`, `permission/evaluator.py`, API permission hook wiring | real `PermissionPolicyEvaluator` denies namespace/server bypass; restored ceiling remains deny-only and cannot issue a grant | No |
| Safe-property bootstrap allow is narrow and deterministic | Complete | `admission.py`, `PreauthorizedBuiltinSkillPermission` | product-owned immutable context expansion only; project/plugin/MCP require 03A decision; no downstream grant transfer | No |
| Inline invocation changes messages, attachments, policy, hooks and state | Complete | `invocation.py`, `attachments.py`, `hooks.py`, `state.py`, `session_bridge.py` | inline mutation, hook lease restore/cleanup, terminal lifecycle, API task checkpoint tests | No |
| Forked invocation hands off to M1-03D contract | Complete as a reference-only handoff | `subagent_contract.py`, `invocation.py` | durable fork request contains immutable refs and no body/grant | No; child execution is deliberately not claimed |
| 02D compact restore uses session-scoped versioned loader | Complete | `compact_bridge.py`, `compact_restore_runtime.py`, `claude_query_engine_runtime.py` | exact ref restore, revocation failure, missing resolver failure, project trust remains untrusted | No |
| CodeWorker receives restored skill content on the real request path | Complete | API `_task_skill_worker_messages`, `code_worker_runtime.py`, query runtime restore resolver | `POST /tasks/{id}/skills` followed by `POST /tasks/{id}/workers/code` observes the exact skill message | No |
| M1-06C receives immutable, non-authoritative outcome projection only | Complete | `SkillOutcomeProjection`, `SkillSessionBridge.memory_handoff` | projection has refs/digests/outcomes, no body/effective-tools/mutable policy | No |
| Disable-to-fail behavior | Complete | runtime/config disable checks in registry, loader, policy and invocation | unit disconnect tests plus `ZYRA_SKILL_RUNTIME_DISABLED=true` API failure | No |
| Clean directory and no root source dependency | Complete | no external imports/process launchers in skill package | commit archive cleanroom: 40 tests, audit 0/0, no `.git` or root source repos | No |
| Minimum 8,000 production LOC | Complete | conservative net counted production: 9,078 | three required `git diff --numstat` buckets recorded below | No |

## 3. Main-path evidence

| Capability | Source mechanism | Zyra target | Live entry / state effect | Test surface |
|---|---|---|---|---|
| Discovery and precedence | Claude `loadSkillsDir`, bundled skills; OpenCode skill loader; Agent Framework providers | `sources/filesystem.py`, `sources/plugin.py`, `registry.py`, `precedence.py` | `GET /skills`, `SkillRuntime.bootstrap/reload_if_changed` publishes atomic generations | `RegistryAndLoaderTests`, API control surface |
| Body/resource disclosure | Claude SkillTool and attachments; Agent Framework resources | `body_loader.py`, `resource_loader.py`, `attachments.py`, `budget_runtime.py` | invocation loads exact body/resource only after registry resolution and budget reservation | lazy-load, tamper, traversal, oversized argument tests |
| Permission ceiling | Claude SkillTool allowed tools; OpenCode skill tool | `policy.py`, `invocation_permission.py`, M1-03A evaluator hook | every guarded tool call receives namespace/server-aware deny-only ceiling before 03A rules/grants | real 03A evaluator and 94-test adjacent suite |
| Session mutation | Claude slash-command path and session hooks; OpenCode command relation | `invocation.py`, `session_bridge.py`, API task metadata | task checkpoint gains invoked state/immutable refs; CodeWorker request gains exact skill message | real API invoke/complete/worker test |
| Compact/restore | Claude compact invoked-skills restore | `compact_bridge.py`, `compact_restore_runtime.py`, query runtime | current query session resolver verifies digest/revocation/trust and restores body | 02D compact tests and cleanroom |
| Plugin-style discovery | Claude/OpenCode plugin loaders | `plugin_runtime.py`, `sources/plugin.py` | plugin skill capabilities enter versioned registry with namespace/provenance | source audit and registry tests |
| Update/rollback | Hermes skill manager lifecycle and Claude change detector | `atomic_update.py`, `revision_store.py`, `change_detector.py`, `reload.py` | validated transaction swaps generation or preserves last-good state | atomic update, rollback and concurrent read tests |

Deleting or disabling `SkillRuntime`, `SkillBodyResourceLoader`, `SkillAllowedToolsPolicy`, or `SkillInvocationRuntime` makes the relevant invocation/API tests fail. Removing the API/CodeWorker/compact wiring makes the live API or 02D integration tests fail rather than merely changing a health response.

## 4. State custody and authority

| State | Sole owner | Persistence / restore boundary |
|---|---|---|
| skill discovery, immutable revision and lifecycle | M1-03C `SkillRegistry` / `SkillRevisionStore` | registry generation plus optional revision lifecycle state |
| invocation state and idempotency | M1-03C `SkillInvocationStateStore` | embedded inside existing task/session checkpoint; no second SQLite store |
| context budget allocations | M1-03C `SkillContextBudgetRuntime` | embedded in the skill runtime checkpoint |
| permission decisions, rule overlays and execution grants | M1-03A `ToolPermissionRuntime` | existing permission state store; skill policy is only a deny ceiling |
| declarative hook leases | M1-03C `SkillHookRuntime` | callbacks are rebuilt from immutable revision metadata; callbacks are not serialized |
| compact/session context | existing 02D session lifecycle plus M1-03C resolver | serialized refs/status/digests only; body is reloaded and reverified |
| forked child execution | M1-03D | S03C-01 emits a durable immutable handoff only |
| skill memory outcome | M1-06C consumer | read-only `SkillOutcomeProjection`; no body/resource/policy store duplication |

Terminal compact references may restore historical context but call policy restore with `activate=false`. This prevents a completed skill from silently reopening its tool ceiling. Trust tier is recomputed from the resolved revision and never accepted from serialized compact input.

## 5. Source-to-target dispositions

The executable source map is `default_skill_source_decisions()` in `source_audit.py`; the auditor verifies mandatory markers, target existence and test surfaces. The decisions are:

| # | Source | Disposition | Target / downstream owner |
|---:|---|---|---|
| 1 | Claude `SkillTool/{SkillTool,types}.ts` | active | skill runtime package + API |
| 2 | Claude `SkillTool/prompt.ts` | reference-only | runtime assets; excluded from production LOC |
| 3 | Claude `SkillTool/UI.tsx` | deferred | M2-01A web console |
| 4 | Claude `skills/loadSkillsDir.ts` | active | filesystem source + registry |
| 5 | Claude `skills/bundledSkills.ts` | active | builtin assets + runtime loader |
| 6 | Claude `utils/attachments.ts` | active | typed/budgeted attachment runtime |
| 7 | Claude `processSlashCommand.tsx` | active | invocation + session bridge |
| 8 | Claude `services/compact/compact.ts` | adapter | versioned compact bridge + 02D runtime |
| 9 | Claude `registerSkillHooks/sessionHooks.ts` | active | declarative hook leases |
| 10 | Claude `skillChangeDetector.ts` | active | content-hash detector + reload coordinator |
| 11 | Claude plugin loader/schema/walk/id/options | active | plugin runtime + plugin source |
| 12 | Claude plugin command/hooks | contract-only | S03C-02 |
| 13 | Claude plugin agents | contract-only | M1-03D |
| 14 | Claude MCP plugin integration | contract-only | S03C-02 |
| 15 | Claude `mcpSkills.ts` | deferred upstream stub | S03C-02 |
| 16 | Claude `mcpSkillBuilders.ts` | contract-only | S03C-02 |
| 17 | Claude remote skill search / DiscoverSkills prompt | deferred upstream stub | local-only search active; remote owner S03C-02 |
| 18 | Agent Framework local providers/FileSkillsSource/Skill | active | frontmatter, path and resource loaders |
| 19 | Agent Framework `MCPSkillsSource` | contract-only | S03C-02 |
| 20 | OpenCode core skill and loader | active | registry + filesystem source |
| 21 | OpenCode skill tool | active | invocation + permission adapter |
| 22 | OpenCode command index | adapter | session bridge + API command path |
| 23 | OpenCode plugin host/skill loader | active | plugin runtime + plugin source |
| 24 | OpenCode plugin install | deferred | S03C-02 approval/update flow |
| 25 | Hermes skill utilities/sync/guard/usage | active | revision, path and change lifecycle |
| 26 | Hermes skill commands/tool | active | local search, invocation and API |
| 27 | Hermes skill manager tool | contract-only | S03C-02 |
| 28 | Hermes skills hub | deferred | S03C-02 remote/install boundary |

No main objective source was silently downgraded. Deferred items are either explicit upstream stubs, interactive UI, marketplace/install authority, MCP execution, or subagent execution owned by a named next unit.

## 6. Adversarial review and fixes

The first adversarial pass blocked completion. The following issues were fixed before the implementation commit:

1. Restored allowed-tools state did not re-enter the real M1-03A gate. Active policies and hook leases are rebuilt from checkpoints, and API/QueryEngine install the ceiling on the current permission adapter.
2. M1-03A hook input dropped tool namespace/server metadata, permitting an MCP/local-name collision. Evaluator hook metadata now preserves namespace, server and request metadata; selector matching treats empty server as local-only.
3. Tombstones could be bypassed with qualified names or immutable refs. Disable names are canonicalized and all resolution modes check the tombstone.
4. Skill messages were stored but not delivered to CodeWorker. The API restores exact refs and constructs real `AgentMessage` objects for `WorkerRequest.messages`.
5. Compact restore accepted trust claims from serialized references. Trust is now recomputed from immutable revision provenance; project/plugin content remains untrusted.
6. Structured compact restore could fall back to a global runtime. It now requires the current session resolver and fails closed when absent.
7. Registry and revision lifecycle publication were separate mutations. `commit_generation` now stages, validates, activates and persists as one rollback-safe transaction under the generation swap lock.
8. Source audit overclaimed grouped/plugin/MCP items and used weak string reachability. Decisions were split by mechanism/owner and production reachability now uses AST call sites.
9. Additional follow-up review found completed compact references reactivating allowed-tools. Invocation status is now carried in the structured reference and terminal restore validates policy without activating it.
10. Change detection initially relied on stat identity. File content is hashed, and symlink directories are surfaced without traversal.

## 7. Verification evidence

### Focused and adjacent behavior

- `python -m unittest -v tests.unit.test_skill_runtime_foundation ...skill API tests`: 40 run, 1 platform skip, suite OK.
- M1-03A permission + commands + 02D compact + CodeWorker permission/query integration: 94 passed.
- Full API control command suite: 21 passed.
- Browser and internalization/line-count gate targeted rerun: 44 passed.
- `SkillRuntimeAuditor`: 28 decisions, 0 errors, 0 blockers; all required production symbols have AST call sites.
- Production boundary scan over `apps,packages,skills,scripts`: exit 0 with 0 blocking findings; broad historical runtime-boundary heuristics remain warnings.
- `compileall`: passed for skill, runtime, worker and API packages.
- `git diff --cached --check`: passed before implementation commit.

The only skip is direct Windows symlink creation without privilege. A real NTFS junction escape test runs and passes, so containment is not evidenced solely by a skipped test.

### Cleanroom

`git archive f9d60b8` was expanded to `tmp/cleanroom-s03c01-f9d60b8` and tested with the repository venv but with the archived project as CWD/PYTHONPATH. The archive has no `.git`, no root source repositories, no uncommitted files and no pre-existing test cache.

- Cleanroom focused/API tests: 40 run, 1 Windows symlink privilege skip, suite OK.
- Cleanroom source/runtime audit: `ok=true`, 0 errors, 0 blockers, 28 decisions.
- No runtime import/path/process dependency on `../claude-code-best`, `../opencode`, `../hermes-agent`, or `../agent-framework`.

### Parent full-suite command

The required `python -m unittest discover -s tests` command was run twice over 594 discovered tests. Both long aggregate runs exited non-zero after the existing real Browser live test hit an 8-second page-readiness timeout on Windows; the second run also emitted Proactor pipe resource warnings. The Browser plus internalization-gate modules were then rerun in isolation: 44 tests passed, including both live Browser scenarios. No persistent skill/permission/compact/API failure was reproduced. This is recorded as a whole-repository test-order/platform stability risk, not hidden as a green full-suite result and not a blocker for S03C-01 behavior.

The legacy integration-ledger strict audit remains globally blocked by three pre-existing workspace findings and 552 planned/candidate warnings. Filtering it to `M1-03C` produced zero M1-03C findings, but the command still reports the global disposition. This slice therefore uses the committed executable 28-item source map and the dedicated `SkillRuntimeAuditor`; it does not rewrite historical seed entries or claim the global ledger is green.

## 8. Effective LOC buckets

Baseline is `3fc23e5`; implementation is `f9d60b8`.

| Bucket | Added | Deleted | Net | Counted toward 8,000 |
|---|---:|---:|---:|---|
| Conservative production (`apps/**`, `packages/**`) after exclusions | 9,267 | 189 | **9,078** | Yes |
| Source audit/map + MCP contract + 03D handoff contract | 955 | 0 | 955 | No; conservative contract/source-map exclusion |
| Runtime assets (`skills/builtin/**`) | 257 | 0 | 257 | No |
| Tests | 1,096 | 12 | 1,084 | No |
| Generated/data/vendor-like/source-pool | 0 | 0 | 0 | No |
| `vendor/**`, `vendor-runtimes/**` | 0 | 0 | 0 | No; failure line remains clean |

The conservative 9,078 net production count excludes the entire `source_audit.py` even though part of it is an executable auditor, plus `sources/mcp.py` and `subagent_contract.py`. It also excludes all Markdown skill bodies/resources/templates and all tests. The slice therefore clears 8,000 without relying on assets, source maps, contracts, fixtures, tests or vendor code.

## 9. Competition requirements and evidence state

| Requirement | Effect of this slice | Status change |
|---|---|---|
| `REQ-MEM-01` | Adds exact invoked-skill compact references, checkpoint restore, digest/revocation validation and context-changing CodeWorker restore | strengthens existing `partial`; does not close benchmark/live evidence |
| `REQ-TRACE-01` | Adds canonical `skill_invoked` events, immutable refs, policy digests, terminal outcome/evidence projection and API/worker traceability | enabling infrastructure only; remains `planned` at matrix level |
| `REQ-CLOSE-01` | Provides deterministic headless admission and deny-only permission integration needed by autonomous runs | no closure; sealed 2,000-transition evidence is owned downstream |

No score item or stage gate is claimed closed. This slice supplies runtime infrastructure; live cross-domain tasks, 2,000 canonical transitions, dynamic topology comparison, real local/edge/cloud dispatch and submission evidence remain with their matrix owners.

## 10. Residual debt and handoff

Non-blocking for this slice, blocking for the parent if left undone:

- S03C-02 must productize plugin commands/hooks, MCP skill discovery/auth/refresh, marketplace/update permission flow, and richer user/project/plugin integration.
- M1-03D must consume `SkillForkRequest` and perform real isolated child execution; the foundation handoff is not child execution.
- M1-06C may consume only the immutable outcome/reference projection and must not create another loader or policy store.
- M2 owns interactive skill UI/approval/session presentation.
- Whole-repository Browser live tests remain sensitive to Windows readiness timing and Proactor cleanup when run inside the full 594-test sequence; isolated live Browser rerun is green.

There is no blocker to marking **S03C-01** complete and advancing the execution entry to **S03C-02**. The parent `M1-03C` must remain incomplete.
