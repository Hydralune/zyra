# M0-M3 Heavyweight Goal Self-Check

This review checks M0-M3 against the current project rule: Zyra must become a heavyweight, complete first-stage agent system, not a thin wrapper or demo.

## Conclusion

M0 and M1 are acceptable as intentionally light foundation milestones. They created schema, event log, API, web shell, task graph, and checkpoint boundaries before heavy runtime work.

M2 is acceptable only as the start of heavyweight integration, not as deep code internalization completion. It did land large source snapshots and real runtime boundaries:

- `vendor/claude-code-best` and `vendor/browser-use` are inside `zyra`, not parent-directory dependencies.
- `apps/code-worker` reads the vendored Claude Code runtime and exposes health, inventory, and QueryEngine contract data.
- `CodeWorkerRuntime` runs a contract-backed tool loop with lifecycle events, read-only batching, write serialization, tool result budgets, context compaction artifacts, permissioned tools, and trace artifacts.
- `BrowserWorkerRuntime` integrates browser-use action metadata, static browser execution, live browser-use session operations, and optional browser-use Agent execution.
- Slash commands, skills, tools, permissions, artifacts, trace, checkpoint, and context session control are connected to the API and tests.

M3 is acceptable as a symbolic-control milestone, not as a major code-volume milestone. It connected `ConstraintKeeper`, `TopologyRouter`, structured low-entropy messages, requirement-change replan, failure-recovery routing, and trace evaluation to the main task graph.

The concern about low non-vendor code volume is valid. The audit script currently reports a project dominated by vendor code, with non-vendor Zyra code still in the low tens of thousands of lines. This is not a reason to reject M0-M3 retroactively. M4 has since delivered a real first memory subsystem, but the warning remains active for M5/M6: the next milestones must perform deeper runtime, scheduler, recovery, and UI internalization rather than adding more small glue layers.

## What Was Intentional

- M0/M1 were deliberately light so the project had stable IDs, schema, persistence, and control-plane contracts before absorbing large upstream modules.
- M2 prioritized landing full local source snapshots and runtime boundaries instead of immediately rewriting or scattering upstream code across Zyra packages.
- M3 prioritized the赛题要求中的神经符号协同、动态拓扑、运行中需求变更和节点失效恢复路径，因此代码量增长不应与 M2 一样大。

## What Was Insufficient

- M2 documentation could be read as if source inventory and sidecar contracts already count as full internalization. They do not.
- Several Claude Code capabilities remain at inventory/contract level: full QueryEngine execution, permission handlers, SkillTool, AgentTool/subagent, MCP client, and full compact restore.
- Browser-use is more deeply connected than Claude Code in live/agent paths, but many browser-use watchdog, memory, sandbox, and judge capabilities are still not first-class Zyra modules.
- `packages/memory` and `packages/scheduler` are still mostly placeholders relative to the final first-stage target.
- The frontend remains a connected shell, not the final control console expected by M6.

## Corrections Added

- Added `scripts/audit_m0_m3_internalization.py` so future agents can re-run this review and see line counts, required path checks, and known internalization debt.
- Added this self-check document so M2/M3 are not mistaken for completed deep internalization.
- Updated project docs to point M4-M7 at concrete source-to-target internalization responsibilities.

## Required Follow-Through

M4 has converted the current context/session/compact traces into a first real memory subsystem: MemoryFabric, four-layer memory records, compact records, API endpoints, and trajectory replay. It still carries debt for richer retrieval, MemoryCurator, skill memory body loading, and compact restore into worker context.

M5 must convert M3 routing, M4 memory/failure records, and M2 runtime debt into a real scheduler and recovery subsystem: worker manifests, resource selection, local/docker/cloud simulation, sandbox/gateway boundaries, fault injection, watchdogs, and recovery policies.

M6 must convert the static console into the first-stage control console: graph, topology, event timeline, worker state, artifacts, diff/browser/terminal output, slash command panel, and live requirement-change input.

M7 should freeze and productize, not become the first stage where heavy integration finally happens.
