# M1-S08-02 main-path hardening integration — preimplementation decision

Date: 2026-07-23

Status: frozen before production implementation

## Commit boundaries

- `baseline_commit`: `8065bac109a3bed9ba01e0e92392fec4d05bfca3`
- `implementation_commit`: pending; it will contain production integration code and direct tests
- `evidence_commit`: pending; it will contain the critical review, machine evidence and final state closeout

The baseline is the verified M1-S08-01 evidence head. M1-S08-01 and all earlier protected slices are read-only inputs. This decision document is excluded from effective production lines. No production file for M1-S08-02 was changed before this decision was frozen.

## Planning sufficiency decision

The slice has enough real product responsibility to support its 7,000 effective production-line floor without filler. At the baseline, the M1 hardening product exposes one foundation HTTP scenario, one real GraphStateStore disconnect probe and live CodeWorker disconnect evidence. It does not yet provide the six required integration scenarios, the complete owner-disable matrix, an executable M1 exit decision, exact clean-directory verification, long-horizon evidence admission, real tier/provider attestation, or a stable M2 handoff artifact.

Those missing responsibilities are not new implementations of the 02A–07C runtimes. They are the final productized integration and release-control layer assigned to unit 08. The layer will invoke public owner ports, correlate canonical events/revisions/causation, disconnect real entrypoints, reject simulated or incomplete claims, persist only derivative evidence, and expose the result through the existing hardening API/CLI. It will not fabricate transitions, provider traffic, edge isolation, approvals or task success to satisfy a gate.

## Source, language and migration decision

`migration_mode` is `audit_and_hardening_only`. No new upstream runtime implementation is authorized and no language normalization will occur. Existing canonical owners keep the source language already adjudicated by their slices. The integration layer is Zyra-owned Python because `zyra_evaluation` and the public API composition root are Python; it is a consumer/challenger, not a second owner.

| Audited mechanism | Source role | Source path/language | Existing Zyra owner/language | M1-S08-02 use | Owner retained |
| --- | --- | --- | --- | --- | --- |
| query/session/tool/permission/MCP/skills/subagent/compact | primary | `claude-code-best/src/query*`, `src/tools/**`, `src/services/**`, `src/tasks/**`, `src/commands/**` / TypeScript, TSX | `packages/runtime/claude-runtime`, `apps/code-worker`, `packages/integrations/claude-mcp` / TypeScript | real scenario and disconnect audit only | Claude-derived TypeScript runtimes/stores |
| durable runtime event spine | primary | selected `opencode` session/event mechanisms / TypeScript | `packages/runtime/runtime-event-spine` / TypeScript | causation, stream and disable audit | RuntimeEventSpine |
| provider catalog, wire adapters, retry/fallback | primary/supplementary per 05D | selected `opencode`, Claude and OMP mechanisms / TypeScript | `packages/runtime/provider-control-plane` / TypeScript | live evidence admission and disconnect audit | ProviderControlPlane |
| memory retrieval/index and code index | primary/supplementary | AgentScope and selected OMP mechanisms / Python, TypeScript | `packages/memory`, `packages/code_index`, retrieval packages / Python, TypeScript | semantic-effect scenario and disable audit | existing memory/index stores |
| curator and procedure memory | primary/supplementary | Hermes and selected OMP mechanisms / Python, TypeScript | memory curator and skill-memory runtimes / Python, TypeScript | write/validate/commit/restore scenario and disconnect audit | existing curator/procedure owners |
| worker lease and dynamic topology | primary/supplementary | AgentScope lifecycle, selected OMP tasks, Zyra topology / Python, TypeScript | WorkerPoolStore and GraphStateCustody / Python | failure/reroute and adversarial topology audit | WorkerPoolStore, GraphStateCustody |
| watchdog, fault injection and recovery | primary/supplementary | browser-use watchdog, Zyra recovery, selected OMP supplement / Python, TypeScript | scheduler fault/recovery runtimes / Python | stall/failure/recovery scenario and disconnect audit | existing fault/recovery owners |
| checkpoint identity, lineage, pending/committed writes, exact resume | narrow primary semantic source | selected LangGraph checkpoint/loop semantics / Python | Zyra graph/recovery stores / Python | conformance and negative-boundary probes only | Zyra stores/recovery runtime |
| StateGraph, Pregel, channels, reducers, ToolNode, stream/SDK/server | reference/rejected/deferred | LangGraph framework surface / Python, TypeScript | no default-path owner | forbidden dependency/call audit only | none granted |
| TaskTool/PAL, Mnemopi, provider fallback, Hashline | supplementary/conformance/reference individually | selected `oh-my-pi` mechanisms / TypeScript, Rust | previously adjudicated task/memory/provider/patch owners / TypeScript, Rust, Python | at least two real semantic-effect attestations; no source import | existing Zyra owners |
| OpenClaw | excluded forward only | no source access | none | absence audit only | none |

No row grants a new primary source, a second canonical owner or cross-language migration credit. If implementation reveals that a public owner port is absent, the hardening layer will report the missing reachability; it will not reimplement the owner in Python.

## Product module boundary

The implementation will extend `packages/evaluation/zyra_evaluation/m1_hardening` by behavior:

- `integration_contracts`: executable scenario/evidence contracts, identity/revision/causation validation and stable serialization; validation logic, not a duplicated canonical schema.
- `integration_scenarios`: six real API scenario plans, transport execution, response/event correlation, semantic-effect comparison and fail-closed stage evaluation.
- `owner_matrix`: complete required-owner inventory, public entrypoint resolution, dependency ordering and coverage checks.
- `owner_probes`: reversible real-owner disconnect handles and runtime/API disable receipts; no mock-only completion credit.
- `live_evidence`: real local/independent-edge/cloud and OpenAI-compatible/Anthropic-compatible wire-evidence admission, including endpoint/transport/stream/tool-result anti-simulation checks.
- `benchmark`: same-run canonical action/transition admission, sealed-autonomy correlation, requirement-change causation and targeted envelope budget comparison.
- `cleanroom`: exact-commit clean-directory, package/path/process/cache/build-context and sibling-repository dependency verification with reproducible command receipts.
- `exit_gate`: parent cumulative line gate, six-scenario matrix, LangGraph boundary, disable matrix, competition evidence and residual-blocker decision.
- `handoff`: M2-facing stable API/event/artifact/control-command contract generated from executed evidence and current production reachability.
- existing `service`, `api`, `cli`, `reporting` and `store`: compose the new gates, expose execution/status endpoints and persist a digest-linked derivative report.

This is one cohesive hardening product. Modules are split by separate behavior and failure responsibility, not to manufacture file or line count.

## Main-path reachability

The production call path will be:

```text
POST /hardening/m1/integration
  -> M1HardeningApi
  -> M1IntegrationService / real HTTP scenario executor
  -> existing task, query, tool, permission, MCP, skill, memory,
     worker, event, provider and recovery owner ports
  -> scenario + disconnect + live-evidence + exit gates
  -> digest-linked hardening report and M2 handoff artifact
```

The CLI will invoke the same service. Each scenario must create or mutate real task/session state through public ports, then read canonical task/events/artifacts. A stage passes only when response identity, revision movement, causation and semantic effect agree. Disconnect execution must change behavior or cause the declared explicit failure and must restore the owner. Static inventory, import smoke, health-only output and fixture replay cannot satisfy scenario or disconnect completion.

## Six scenario decision

1. Query/session/context/tool: a new task reaches the TypeScript CodeWorker owner, executes a low-risk tool and emits session/tool/artifact evidence.
2. Permission: a dangerous tool produces ASK/DENY evidence, cannot create the side effect before permission, and an exact approval or deterministic denial/recovery changes the path.
3. MCP: a configured MCP tool/resource/prompt crosses auth and elicitation boundaries into the task context; missing auth/elicitation resolution fails closed.
4. Skill/memory/compact: a real invocation writes procedure/outcome memory, compact/restore changes the next worker context, and the disabled memory/restore path is observably different.
5. Subagent/worker/recovery: a background/subagent task acquires a worker lease, a real injected failure enters recovery, and reroute/resume changes route/checkpoint state.
6. Stream/provider failover: stream stall or backend unavailability causes bounded retry/fallback/failover and leaves runtime-event/recovery/provider evidence without duplicate side effects.

Where the repository lacks credentials or independent infrastructure, the code will retain an explicit blocker rather than relabel loopback or fixture evidence as real.

## State custody and safety

Canonical task/session, permission, memory, graph, worker lease, provider, recovery, workspace and artifact state remain with their existing stores. The new layer may persist only derivative integration run/report/handoff records under the M1 hardening artifact root. Every record includes baseline/target commit, canonical identifiers, evidence pointers and content digest. It cannot mutate canonical state except through existing public API/control/worker ports.

Disconnect probes are reversible and dependency ordered. They capture the exact callable/configuration revision, run a baseline, disconnect one owner, require explicit failure or material semantic difference without alternate-owner masking, restore in `finally`, and verify the post-restore baseline. Dirty-worktree and destructive Git operations are prohibited. Secret values are never stored in reports; only redacted endpoint identity and cryptographic evidence digests are admitted.

## High-risk and verification decision

This is the last sibling slice, numerical-stage aggregate review and M1 exit checkpoint. It therefore requires the parent-level broad regression, full ledger/source-to-target audit and exact-commit cleanroom even if production changes remain inside the existing hardening owner. Public route additions are backward compatible and derivative state does not transfer canonical ownership.

Validation will include:

1. direct unit behavior for contracts, six-stage evaluation, owner matrix, disconnect ordering/restore/fallback rejection, live-evidence anti-simulation checks, benchmark admission, cleanroom result classification, exit policy and handoff integrity;
2. real API integration for all locally executable scenario paths and the hardening route/CLI;
3. adjacent QueryEngine/session, permission, MCP, skill-memory/compact, subagent/worker/recovery, provider/event-stream and graph-custody regressions;
4. all applicable repository tests and packaging/build checks;
5. a clean copy containing only `zyra`, with no sibling source repositories, editable links, external build contexts, runtime source paths, caches or residual databases;
6. an effective-line audit over `8065bac109a3bed9ba01e0e92392fec4d05bfca3..<implementation_commit>`, plus cumulative parent audit from the 08-01 baseline.

The exit gate will distinguish implementation completeness from environment-backed competition evidence. M1/M1-08 and the execution-state YAML will advance only if every mandatory gate has real evidence. Otherwise the implementation and review may be committed, but the authoritative next entry remains 08-02 with concrete blockers.

## Explicit non-goals

- no new QueryEngine, permission evaluator, MCP client, memory store, scheduler, provider plane, worker lifecycle or recovery planner;
- no LangGraph runtime/framework dependency;
- no OpenClaw source, package, process, path or restored repository;
- no simulated edge/cloud, loopback provider or recorded fixture promoted to active-real;
- no generated transitions, repeated no-op actions, logs, heartbeats or projector duplicates counted toward 1,000/2,000;
- no source graph, ledger, report text, test code, DTO repetition, adapter-only code or manifest data counted toward effective production lines.
