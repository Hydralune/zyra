# M1-02A Claude Source Productization Foundation Review

Date: 2026-07-07

## Scope

This review covers the current remediation pass for
`docs/milestones/M1-runtime-memory-scheduler-fault/slice-02a-01-claude-source-productization-foundation.md`.

The repository already contained historical M1-02A/M1-02B/M1-02C work before the
current strict internalization policy was added. This pass therefore treats old
M1-02A productized Claude Code rows as source-pool evidence, not as completed
deep internalization.

## Implemented

- Added a Zyra-owned Claude QueryEngine foundation:
  - `zyra_runtime.claude_query_engine_runtime` is now the default `CodeWorkerRuntime` loop.
  - It executes structured query turns without the Node sidecar by default.
  - It binds query plan normalization, session lifecycle, tool orchestration, tool result budgeting, context compaction, control commands, and runtime state custody to Zyra stores/events/artifacts.

- Added runtime modules used by the default path:
  - `claude_runtime_contracts.py`: sidecar-free source-to-target contract bundle.
  - `claude_query_plan.py`: structured `query_turns`/`tool_plan` planner and audit metadata.
  - `claude_session_lifecycle.py`: snapshot, transcript, resume plan, restore report, interruption detection.
  - `claude_context_window.py`: context blocks, budget selection, compaction artifact, restore payload.
  - `claude_tool_use_runtime.py`: tool-use envelopes, semantic effects, result blocks, batch digests.
  - `claude_control_commands.py`: `/context`, `/compact`, `/cost`, `/doctor`, `/resume`, `/tools`, `/permissions` style control runtime.
  - `claude_runtime_state.py`: state mutation/custody ledger for session/tool/context/artifact/control causality.
  - `claude_clean_runtime.py`: clean-source/default-path/disconnect audit.

- Updated `CodeWorkerRuntime`.
  - Default path uses `ZyraClaudeQueryEngine`; sidecar contracts require explicit `use_sidecar_contracts=True`.
  - Disconnecting `ZyraClaudeQueryEngine` now fails the task with `productized_query_engine_runtime_disabled`.
  - Worker metadata exposes query plan, session lifecycle, context window, tool runtime, control command, clean runtime, and runtime state evidence.
  - `/workers/code/inventory` now reports the Zyra-owned sidecar-free contract bundle instead of the legacy vendor/productized source-pool inventory.

- Retained and strengthened the source-pool correction:
  - `ledger_migrations.py` downgrades legacy M1-02A productized/source-pool rows and the old M0 vendored CodeWorker seed row.
  - `source_extraction.py` no longer treats M1-02A productized source-pool files as connected runtime completion.
  - Foundation probe has clean-source mode and no source-pool fallback by default.

- Added/updated verification:
  - `tests/integration/test_code_worker_clean_productized_runtime.py` covers default path, sidecar non-use, disconnect failure, permission denial, context compaction, session restore, control commands, tool semantics, query plan metadata, and runtime state metadata.
  - `scripts/verify_claude_productization_foundation.py --clean-source` uses an empty source workspace, runs the default CodeWorker path, executes control commands, and checks disconnect evidence.
  - `tests/integration/test_claude_productization_foundation_cli.py` now copies a clean `apps/packages/scripts` project tree without `vendor`, `vendor-runtimes`, or parent source repositories and runs `--clean-source` inside that copy.
  - Browser live-backend tests now treat `browser_use` Python imports as optional live dependencies; static browser behavior remains tested without requiring those imports.

## Objective Matrix

| Requirement | Status | Evidence | Blocking |
| --- | --- | --- | --- |
| Source-to-target裁决 | Done | `claude_productization_foundation.py`, `claude_runtime_contracts.py`, `ledger_migrations.py` | No |
| Source-pool不能作为完成证据 | Done | `policy-matrix --owner-unit M1-02A --fail-on-error` passes | No |
| QueryEngine/tool loop/session 主体产品化 | Done | `ZyraClaudeQueryEngine`, `CodeWorkerRuntime` default path | No |
| 至少一个真实 worker/runtime 入口 | Done | `CodeWorkerRuntime`, `ClaudeProductizationFoundationWorker`, verify script | No |
| Event/artifact 接入 | Done | Query/session/tool/context/control events and snapshot/transcript/resume/trace artifacts | No |
| 目录型上游 source inspection | Done | `_read_source_tree_text()` handles directories | No |
| Legacy M0/M1-02A productized 行降级 | Done | ledger migration tests and policy matrix | No |
| 干净目录/default path/断开即失败 | Done | `verify_claude_productization_foundation.py --clean-source --json` | No |
| 最低 9,000 有效新增生产/脚本行 | Done | cached bucket: 9,025 production/script additions under `apps/packages/skills/scripts`; bucket effective 9,636 | No |

## Main Path Evidence

| Boundary | Primary source | Zyra target surfaces | Runtime/API/CLI entry | Tests |
| --- | --- | --- | --- | --- |
| QueryEngine/query loop | `claude-code-best/src/QueryEngine.ts`, `src/query.ts` | `claude_query_engine_runtime.py`, `claude_query_plan.py`, `claude_runtime_state.py`, `query_session.py` | `CodeWorkerRuntime` default path | `test_code_worker_clean_productized_runtime` |
| Tool loop/budget | `src/Tool.ts`, `src/tools.ts`, `src/services/tools/toolOrchestration.ts` | `tool_loop.py`, `tools.py`, `claude_tool_use_runtime.py`, `executor.py` | `ZyraClaudeQueryEngine` | clean runtime + sidecar/query lifecycle/tool-loop regression tests |
| Permission runtime | `src/hooks/toolPermission` | `permissions.py`, `executor.py`, `claude_tool_use_runtime.py` | permission-gated tool execution | clean permission denial test |
| MCP boundary | `src/services/mcp`, `src/commands/mcp` | `source_extraction.py`, `scaffold.py` | ledger/foundation probe | foundation unit test |
| Commands/control | `src/commands.ts`, command dirs, `claude-reviews-claude` bridge/UI references | `claude_control_commands.py`, `control.py`, `code_worker_runtime.py` | `control_commands` request constraint | clean control command test |
| Context/compact | `src/services/compact`, `sessionRestore.ts` | `claude_context_window.py`, `claude_session_lifecycle.py`, `query_session.py` | context budget + resume plan artifacts | clean context/session tests |
| Skill/subagent | `src/tools/SkillTool`, `src/tools/AgentTool`, `forkSubagent.ts`, `claude-reviews-claude` plugin/swarm references | `scaffold.py`, `runtime_scaffold.py`, `code_worker_runtime.py` | foundation probe | foundation unit test |
| CodeWorker ingress | `src/entrypoints/cli.tsx`, `commands/doctor` | `apps/code-worker`, `code_worker_bridge.py`, `code_worker_runtime.py`, `claude_clean_runtime.py` | verify script and default worker | integration CLI + clean-source verify |

## Source-To-Target Decisions

- `claude-code-best`: primary source repository for M1-02A foundation.
- `claudecode-related/claude-reviews-claude`: reference-only crosswalk.
- `claudecode-related/Dive-into-Claude-Code`: reference-only crosswalk.
- `vendor-runtimes/claude-code-runtime/productized/**`: source-pool evidence only.
- `apps/**`, `packages/**`, `scripts/**`, `tests/**`: valid Zyra-owned evidence when connected to behavior.

## Line Buckets

Commands:

```text
git diff --cached --numstat 55d78f0 -- apps packages skills scripts
git diff --cached --numstat 55d78f0 -- tests
git diff --cached --numstat 55d78f0 -- vendor vendor-runtimes
python scripts/zyra_integration_ledger.py buckets --owner-unit M1-02A --base 55d78f0 --cached --minimum-effective-lines 9000 --json
```

Result:

- Production/scripts under `apps packages skills scripts`: 9,025 added lines / 87 deleted lines.
- Tests by numstat: 615 added lines / 32 deleted lines.
- Bucket split: production 8,812, script 213, test 611, adapter-only 4.
- Vendor/vendor-runtimes: 0 added lines.
- Bucket effective added: 9,636; raw effective added: 9,640.
- Minimum for this slice: 9,000.
- Shortfall: 0.

## Verification

Passed:

```text
python -m unittest tests.unit.test_claude_productization_foundation
python -m unittest tests.integration.test_claude_productization_foundation_cli
python scripts/verify_claude_productization_foundation.py
python scripts/verify_claude_productization_foundation.py --clean-source --json
python scripts/zyra_integration_ledger.py policy-matrix --owner-unit M1-02A --fail-on-error
python scripts/zyra_integration_ledger.py accounting --owner-unit M1-02A --no-entries --fail-on-error
python scripts/zyra_integration_ledger.py buckets --owner-unit M1-02A --base 55d78f0 --cached --minimum-effective-lines 9000 --json
python -m unittest tests.integration.test_api_control_commands
python -m unittest tests.integration.test_claude_productization_foundation_cli
python -m unittest tests.integration.test_browser_worker tests.unit.test_runtime_protocol
python -m unittest tests.integration.test_code_worker_clean_productized_runtime tests.integration.test_code_worker_sidecar tests.integration.test_code_worker_query_session_lifecycle tests.integration.test_code_worker_tool_loop_budget
python scripts/verify_submission_boundary.py
```

Additional pass in the current remediation:

```text
python -m py_compile .\apps\api\zyra_api\main.py .\packages\runtime\zyra_runtime\claude_clean_runtime.py .\packages\runtime\zyra_runtime\claude_query_engine_runtime.py .\packages\workers\zyra_workers\code_worker_runtime.py
python -m unittest tests.integration.test_code_worker_clean_productized_runtime
python -m unittest tests.integration.test_code_worker_sidecar tests.integration.test_code_worker_query_session_lifecycle tests.integration.test_code_worker_tool_loop_budget
python -m unittest tests.unit.test_claude_productization_foundation tests.integration.test_claude_productization_foundation_cli
```

Full-suite review note:

```text
python -m unittest discover -s tests
```

In the 2026-07-07 review run this command timed out after 600 seconds before
emitting any assertion failure. The ledger gate CLI matrix/source-scan test
passes individually but takes about 137 seconds in this environment; unit and
integration discovery therefore exceed the review timeout budget. M1-02A
completion is judged from the targeted foundation, clean-source, ledger,
submission-boundary, scenario, and CodeWorker runtime gates listed above rather
than a fresh full-suite pass in this review.

## Completion Decision

This remediation now passes the current `slice-02a-01` completion gates:

- M1-02A no longer treats productized/source-pool Claude Code files or the legacy vendored CodeWorker inventory as connected runtime completion.
- `CodeWorkerRuntime` default path is backed by Zyra-owned QueryEngine/tool/session runtime modules.
- The CodeWorker API inventory endpoint no longer presents the legacy vendor/source-pool inventory as the default runtime inventory.
- Clean-source verification passes with an empty source workspace and no sidecar contracts.
- Clean project-copy verification passes without `vendor`, `vendor-runtimes`, or parent source repositories.
- Disconnecting the productized QueryEngine fails the task.
- Permission denial, context compaction, session restore, control commands, tool-use semantics, query plan audit, and runtime state custody are covered by behavior tests.
- Effective production/script additions under `apps/packages/skills/scripts` meet the 9,000-line floor, with no vendor/vendor-runtimes additions.

The foundation probe reports no blocking findings or crosswalk warnings after the commands/control and skill/subagent boundaries were tied back to the reference-only `claude-reviews-claude` chapters. Source-pool usage remains unnecessary for the default path.
