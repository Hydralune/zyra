# M1-S03B-02 MCP client runtime integration review

## 1. Review identity

| Field | Value |
| --- | --- |
| Slice | `M1-S03B-02` |
| Parent unit | `M1-03B` |
| Slice baseline | `7a8a0a0031cc36565ab8ee6640974aba16e8d014` |
| Parent-unit baseline | `17aaacf` |
| Implementation commit | `16717f2` |
| Review date | `2026-07-11` |
| Disposition | complete |

The slice is complete. MCP client capabilities are now part of the default CodeWorker, API, command, permission, event, checkpoint, restore, and recovery paths. The implementation does not execute against a parent-directory source repository, a vendored runtime, a fixed health response, or a fixture-only server.

## 2. Critical review conclusion

No completion-blocking defect remains after implementation and validation.

The first full-suite attempt reached the existing live browser tests but exceeded a 901-second command limit. It was not counted as a pass. The same suite was rerun with a sufficient time budget and passed all 555 tests in 927.511 seconds. This distinction is retained because a timeout is not equivalent to successful regression evidence.

Residual risks are non-blocking for this slice:

- UI-native MCP management remains owned by M2; this slice exposes API and dynamic command surfaces rather than claiming a completed web console.
- Skill and AgentTool composition remains owned by M1-03C and later subagent slices.
- Remote OAuth/provider-specific interoperability remains provider integration work; this slice owns refresh state transitions and safe persistence, not every provider implementation.
- MCP protocol evolution still requires compatibility tests when the negotiated protocol version changes.

## 3. Target coverage matrix

| Slice target | Status | Evidence | Blocking |
| --- | --- | --- | --- |
| Integrate MCP lifecycle into the default runtime path | complete | `mcp/main_path.py`, `mcp/bootstrap.py`, `mcp/runtime.py`, `code_worker_runtime.py` | no |
| Project tools, resources, prompts, and resource templates | complete | `mcp/resource_projection.py`, dynamic tool handlers, prompt-backed slash commands | no |
| Route MCP mutations through permission policy | complete | typed `McpControlRuntime`, API mutation authorizer, approved/denied integration tests | no |
| Persist causal MCP events through Zyra event storage | complete | `mcp/causality.py`, `mcp/event_commit.py`, API event sink | no |
| Persist and restore session-safe MCP state | complete | `mcp/session_bridge.py`, CodeWorker checkpoint and restore integration | no |
| Exclude credentials and live transports from snapshots | complete | checkpoint validator and hard rejection paths | no |
| Provide deterministic recovery planning | complete | `mcp/recovery.py`, runtime recovery entry point | no |
| Expose runtime controls and prompt commands | complete | `mcp/control.py`, `zyra_commands/mcp_control.py`, `/commands`, `/mcp`, task commands | no |
| Use a real independent stdio server in behavior tests | complete | `tests/support/fake_mcp_server.py`, integration test, smoke test | no |
| Prove clean-submission execution | complete | `scripts/verify_mcp_cleanroom.py` | no |
| Meet slice effective production minimum | complete | conservative `8,718 >= 8,000` | no |
| Keep vendor/source-pool additions at zero | complete | vendor numstat empty | no |

## 4. Main-path and state-custody evidence

| Capability | Runtime entry | Zyra state owner | Event/control attachment | Behavior evidence |
| --- | --- | --- | --- | --- |
| MCP bootstrap | CodeWorker request projection and API runtime dependency | `McpConnectionRuntime` plus Zyra runtime configuration | bootstrap and connection transitions | MCP integration and smoke tests |
| Tools | CodeWorker tool projection and API `/tools` | projected tool registry and session identity | permission mutation plus tool causal events | real stdio echo call |
| Resources/templates | worker context/resource projection and typed control | resource projection bundle | permission-gated external read | integration resource read |
| Prompts | dynamic `/commands` registry and task command dispatcher | prompt projection bundle | typed prompt-get control | integration prompt expansion |
| Auth refresh | MCP API typed control | refresh state only; credentials excluded from snapshots | permission-gated auth mutation | control restore tests |
| Session checkpoint | CodeWorker checkpoint path | `McpSessionBridge` persisted through Zyra store | checkpoint validation and diff | main-path integration tests |
| Session restore | CodeWorker session-open path | Zyra checkpoint/event storage | restore receipt and causal linkage | main-path restore test |
| Recovery | runtime `plan_recovery` | deterministic recovery plan | recovery actions and committed signals | unit/runtime coverage |

If the new main-path, projection, control, session bridge, or event committer modules are disconnected, the real MCP integration tests fail because CodeWorker context no longer receives MCP capabilities, prompt commands disappear, resource operations lose their permission path, or checkpoint/restore loses MCP state. This is dynamic reachability, not an import-only assertion.

## 5. Source-to-target disposition

The existing 62-row MCP source ledger remains aligned. No row was marked complete merely because a source file exists. The slice consumes the already-reviewed decisions and moves their behavior into Zyra-owned modules.

| Source family | Migrated mechanism | Zyra target | Strategy | Disposition |
| --- | --- | --- | --- | --- |
| `claude-code-best` MCP client/runtime patterns | client lifecycle, tools/resources/prompts, auth and control semantics | `packages/integrations/zyra_integrations/mcp/**` | semantic migration into Zyra state, permission, events, and checkpoints | `zyra_module_migrated` |
| `opencode` MCP/session/command patterns | durable session projection and dynamic prompt commands | MCP runtime plus `packages/commands/zyra_commands/**` | adapted to Zyra command registry and session identity | `zyra_module_migrated` |
| MCP protocol contracts | typed transport/control payload boundaries | existing transport plus new control/projection modules | contract-compatible implementation; not a copied runtime | active contract |
| Parent-directory source repositories | none at runtime | none | rejected by cleanroom and submission-boundary checks | reference-only |
| `vendor/**` and `vendor-runtimes/**` | none | none | prohibited for completion | zero additions |

## 6. Effective line-count audit

Baseline: `7a8a0a0031cc36565ab8ee6640974aba16e8d014`.

| Bucket | Added | Deleted | Completion accounting |
| --- | ---: | ---: | --- |
| `apps/**`, `packages/**`, `scripts/**` raw production/verification | 8,896 | 128 | raw only |
| Verification scripts excluded conservatively | 178 | 62 | excluded from production minimum |
| Conservative production internalization | 8,718 | 66 | counts toward slice minimum |
| `tests/**` | 904 | 19 | validation only |
| `vendor/**`, `vendor-runtimes/**` | 0 | 0 | prohibited bucket remains zero |
| Whole implementation commit | 9,800 | 147 | not used as production minimum |

The conservative slice result is `8,718 >= 8,000`. M1-03B also passes its combined parent-unit review from baseline `17aaacf` with minimum `16,000`; the prior slice's conservative production count was 18,147 before adding this slice.

No generated data, source map, ledger record, Markdown asset, mock-only code, fixture-only code, or vendor-like source pool is included in the conservative production count.

## 7. Validation evidence

| Command or suite | Result |
| --- | --- |
| MCP unit discovery | 111 tests passed |
| Existing MCP integration suite | 6 tests passed |
| `tests.integration.test_mcp_main_path_integration` | 3 tests passed against independent stdio server |
| Claude productization integration | 8 tests passed |
| CodeWorker context/compact/API integration | 5 tests passed |
| CodeWorker integration discovery | 15 tests passed |
| API control commands | 20 tests passed |
| Full `unittest discover -s tests` | 555 tests passed in 927.511 seconds |
| `scripts/smoke_mcp_runtime.py` | passed; one real remote tool call plus resource/prompt/task behavior |
| `scripts/verify_mcp_cleanroom.py` | passed in a clean copied `zyra` tree with source repositories absent |
| `scripts/verify_submission_boundary.py` | passed |
| `scripts/sync_mcp_source_ledger.py --check` | 62 decisions aligned, zero owner-group changes |
| Strict slice ledger audit | `9,800` raw audited additions, minimum `8,000`, audit passed |
| M1-03B parent unit review | 20 findings, 0 errors, 0 blockers, passed |

## 8. Anti-fake-internalization review

| Failure mode | Finding |
| --- | --- |
| Thin adapters delegate core decisions to an upstream runtime | not found; projection, control, persistence, causality, and recovery execute in Zyra modules |
| Parent repository required at runtime | not found; cleanroom explicitly removes source repositories |
| Fixture-only/fixed health evidence | not found; fake server is a separate stdio process with live protocol interaction and mutable catalogs |
| Permission only logs decisions | not found; resource, prompt, auth, and tool mutations are actually allowed or rejected |
| Checkpoint persists credentials/live connections | not found; validator rejects unsafe snapshot content |
| Event log is detached from real activity | not found; API sink commits causal MCP events through the existing SQLite event store |
| Dynamic command is static documentation | not found; prompt discovery changes the runtime command registry and task dispatcher |
| Vendor/source-pool code is counted | not found; vendor diff is empty |
| Tests rely on residue/cache | not found in cleanroom path; the copied repository runs the core integration and smoke suites |

## 9. Competition evidence and downstream ownership

This slice advances but does not close the following requirement evidence:

| Requirement | Evidence change | Status after slice |
| --- | --- | --- |
| `REQ-CLOSE-01` | MCP actions now produce real state, tool, permission, checkpoint, restore, and recovery transitions | advanced; live 2,000-transition scenario remains downstream |
| `REQ-TRACE-01` | causal MCP events attach session, task, worker request, tool/resource/prompt, and mutation identities | advanced; full visual causal trace remains M2/M3 |
| `REQ-MEM-01` | MCP session state participates in safe checkpoint/restore and subsequent context projection | advanced; long-horizon benchmark remains downstream |
| Runtime heterogeneity evidence | independent stdio process participates in the real worker/API path | advanced; real local/edge/cloud dispatch remains M3 evidence work |

No competition gate is claimed closed solely by this infrastructure slice. The slice supplies runtime evidence required by later live-task, trace UI, autonomous benchmark, and delivery gates.

## 10. Completion decision

`M1-S03B-02` is complete and `M1-03B` is complete. The next execution entry is `M1-S03C-01`, the Skill runtime loader foundation. Root-level `docs/milestones/execution-state.yaml` must be updated only after this review is committed as Zyra evidence.
