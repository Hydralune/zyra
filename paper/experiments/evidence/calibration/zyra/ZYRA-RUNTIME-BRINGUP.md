# ZYRA 运行时启动与模型链路验证（2026-09-08）

本文件记录"让 ZYRA 在 SWE-bench Verified 上与 OpenHands 做同任务对照"的运行时启动进展。所有结论均来自实际执行，未伪造。

## 已完成（实证）

### 1. ZYRA 环境就绪清单
| 组件 | 状态 | 证据 |
|---|---|---|
| Python | 3.12.7（`/d/Python/python.exe`） | `python --version` |
| node | v22.23.2 | `node --version`，`--experimental-strip-types` 可用 |
| bun | 1.2.15 | `node_modules/.bin/bun.exe --version` |
| ZYRA CLI | 可加载 | `bun apps/cli/src/index.ts --version` 返回 `zyra.cli-version.v1 version=0.1.0` |
| ZYRA 核心 Python 包 | 可 import（sys.path 方式） | `zyra_orchestration`、`zyra_workspace` 均 OK |
| dist 编译版 | `apps/cli/dist/zyra.js`（1MB）、`dist/code-worker/main.js`、`dist/code-worker-node/main.js` | 均已存在 |
| deepseek provider | Python `provider_dispatch.py` + TS `deepseek.ts` 均内置 | `base_url=https://api.deepseek.com`, model `deepseek-v4-flash` |

### 2. ZYRA 模型调用链路已打通（真实模型调用）
用 `LiveProviderDispatchRuntime.dispatch_marker` 发起一次真实 deepseek 调用，结果：
```
MARKER_DISPATCH_OK
provider: deepseek  model: deepseek-v4-flash
http_status: 200
prompt_tokens: 109  completion_tokens: 26
latency_ms: 971  cost_usd: 2.254e-05
```
调用链路：Python `dispatch_marker` → `ProviderControlPlaneClient` → spawn node 子进程（`stdio-server.ts`）→ deepseek API → 200。

关键坑：`dispatch_marker` 之前必须先把 `.env.deepseek.local` 的 `DEEPSEEK_API_KEY` 写入 `os.environ`（node 子进程继承 Python 环境），否则报 `credential_blocked`（secret_ref_unresolved）。

## ZYRA 执行架构（源码级确认）

完整链路：
```
CLI (bun/node, apps/cli/dist/zyra.js)
  → HTTP API (FastAPI, apps/api/zyra_api/main.py, 19797 行)
  → task_graph (控制平面, run_task_graph + completion_gate)
  → execute_code_worker_operator (code_worker_adapter.py)
  → CodeWorkerRuntime (Python, code_worker_runtime.py)
      → TypeScriptClaudeQueryEngine (apps/code-worker, dist 编译版)
          → ProviderControlPlaneClient → node stdio-server → deepseek
```

## 关键对照点（论文核心论点的实证基础）

ZYRA 的完成门 `_evaluate_delivery_completion`（`code_worker_adapter.py:286`）docstring：
> "Fail closed on objective delivery evidence before a model may stop."

检查项：`required_paths_present`、`expected_file_contents_match`、`workspace_mutation_observed`、`behavioral_verification_passed`、`final_response_present`、`delivery_contract_bound`。

对照 OpenHands 的停止条件（`local_conversation.py` run loop）：只认"模型主动调用 finish 工具"或"迭代硬上限"，无产物级收敛判定。

## 剩余工作（未完成，需继续）

1. **让 ZYRA CodeWorker 在 flask 仓库（base_commit 7ee9ceb）执行 pallets__flask-5014**：
   - 配置工作区（`cwd` + `workspace_roots` 绑定 flask 仓库）
   - 构造 SWE-bench goal（problem_statement）
   - 配置 delivery_contract（required_paths = 需修改的源文件）
2. **提取 git diff 作为 model_patch**（`zyra_workspace/git_boundary.py` 支持只读 `diff`）
3. **官方 SWE-bench 评分器独立判分**（`predictions.jsonl` 格式）
4. 若 ZYRA 能收敛而 OpenHands 不能，则构成论文核心论点的同任务对照证据。

## 参考脚本（可复用）

- `scripts/run_zyra_inference_validation.py`：官方"启动隔离 ZYRA + 真实模型 + 提交 sealed CLI 任务"的完整脚本，展示 LocalInferenceCluster + dev_api.py + node dist/zyra.js 的启动方式。
- `scripts/run_embedded_scenario_evidence.py`：纯进程内（不启动 HTTP）的嵌入式运行，但为"排除模型"验证模式。

## 下一步建议（待用户决策）

方案 A：写一个最小 SWE-bench 启动脚本，复用 `run_zyra_inference_validation.py` 的启动链路，把 TASK 换成 flask-5014 的 problem_statement，workspace 换成 flask 仓库。
方案 B：先确认 CodeWorker 是否需要在 Docker 里跑（flask 仓库 + 测试环境），还是可直接在 WSL/Windows 文件系统跑。

## 追加：ZYRA 端到端跑通受阻于核心代码 bug（2026-09-08）

**进展**：通过 `pip install -e . --no-deps --no-build-isolation` 解决了 `zyra_workers` 无法 import 的问题（此前 edge worker 子进程因 `ModuleNotFoundError: No module named 'zyra_workers'` 立即退出，报 `NoSuchProcess`）。ZYRA API 现已能正常启动（`ready: true`）并接受任务提交。

**新阻塞点**：任务执行在 TypeScript event spine 层被拒绝，报错：
```
legacy event 0 (node_updated) failed: event requires causation id
```

**根因（源码级确认）**：
- `packages/core/zyra_core/models.py` 的 `EventRecord` 数据类**没有 `causation_id` 字段**；
- `packages/orchestration/zyra_orchestration/task_graph.py:2037` 的 `_node_event()` 产生 `node_updated` 事件时未设置 causation_id；
- 而 `packages/runtime/runtime-event-spine/src/event-catalog.ts:193` 校验 `node_updated` 事件**强制要求 `causationId`**。

这是 ZYRA Python 事件模型与 TypeScript 事件脊契约的**版本不一致 bug**，非环境配置问题。

**判断**：修复需要给 `EventRecord` 增加 `causation_id` 字段并在所有事件产生点正确传递，属于 ZYRA 核心事件契约的重构。修改 ZYRA 核心代码本身会在论文中构成"为跑通基线而修改被测系统"的方法论披露项，需谨慎决策。

**当前最诚实的状态**：
- OpenHands 基线：✅ 完整跑通，2 个官方判分结果（flask resolved=true，pylint 0/1）
- ZYRA：⏳ 环境全链路就绪（API 可启动、任务可提交、deepseek 模型链路通），但端到端执行被上述核心代码 bug 阻塞

## 追加：causation_id bug 修复尝试（2026-09-08 深夜）

**已完成的修复（6 处）**：
1. `packages/core/zyra_core/models.py`：`EventRecord` 增加 `causation_id: str | None = None` 字段
2. `packages/orchestration/zyra_orchestration/task_graph.py:2037`：`_node_event` 设置 `causation_id=state.root_node_id`
3. `packages/symbolic/zyra_symbolic/control.py:328`：`_node_update_event` 设置 `causation_id=state.root_node_id`
4. `packages/runtime/runtime-event-spine/src/contracts.ts`：`LegacyEventRecord` 增加 `causation_id/causationId` 字段
5. `packages/runtime/runtime-event-spine/src/contracts.ts:normalizeLegacyRecord`：解析 `causation_id`
6. `packages/runtime/runtime-event-spine/src/normalizers.ts:normalizeLegacyEvent`：draft 设置 `causationId`

**独立验证（均通过）**：
- Python `run_task_graph` 产出的所有 `node_updated` 事件 `causation_id` 非空（`root_node_id` 值）
- TS `normalizeLegacyEvent` 能正确提取 `causation_id` → `draft.causationId`

**但端到端仍失败**：`legacy event 0 (node_updated) failed: event requires causation id`

**未解之谜**：在 `append_legacy_events` 加 debug print 后，实际提交任务时 print **未触发**，说明实际失败的那个 `node_updated` 事件**没有经过 `append_legacy_events`**（`main.py:9229` 是唯一调用点，但 debug 未命中）。可能的事件路径：事件在进入 `persist_events` 前经过了 SQLite 序列化/反序列化（`store.task_events` 的 SELECT 不含 causation_id 列），或存在另一条事件发送路径。

**诚实判断**：这是一个 ZYRA 核心事件脊的深度 bug，追踪需要完整理解事件从 task_graph → persist_events → event spine 的序列化链路（含 SQLite 存储是否保留 causation_id）。已远超"校准基线实验"范畴。

**方法论说明**：上述 6 处修改属于"修复被测系统（ZYRA）让其能运行"，若最终跑通，论文需如实披露这些修复及其对结果的影响。
