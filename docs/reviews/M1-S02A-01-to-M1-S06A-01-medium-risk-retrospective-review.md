# M1-S02A-01 至 M1-S06A-01 中风险追溯审查报告

## 1. 结论

审查结论：`FAIL_REMEDIATION_REQUIRED`。

在固定目标提交 `752a2636d2038c20a80bb79d2484f61874e5e796` 上，审查发现两个会阻断普通主路径的 `P1`：

1. CodeWorker 公开 ASK 队列投影与恢复链断裂。仓库自带的单 ASK API 测试已经失败；自有双 ASK 探针在第一次 suspension 后同样查不到 pending request，重试又触发 `restored tool call is missing its scheduled batch`。
2. Browser 已产生并声明一个待交付上下文，但当前 CodeWorker 的 02D provider envelope 没有选中它，导致跨 worker 主路径返回 `409`。

另有一个 `P2` cleanroom 债务：根 `pyproject.toml` 不能安装项目，也没有声明 browser-use/psutil 等实际测试依赖。补装仓库现有 `vendor/browser-use[core]` 后，三个依赖型浏览器失败全部转为通过，证明它们不是产品语义失败；但依赖复现边界仍不合格。

这不等于 M1-S02A-01 至 M1-S06A-01 整体出现了严重执行失控。E01、E02 owner 内部、workspace/gateway、event/provider/backend、06A retrieval/index 等大量行为均通过。失败集中在累计 custody cutover 之后的跨域桥，正是本任务书要追踪的中风险类型。

根据任务书的停止条件，本次审查没有修生产代码、测试或 `execution-state.yaml`。两个 P1 需要用户另行授权修复。

## 2. 固定审查身份与输入门禁

| 项目 | 固定值 |
|---|---|
| scope base | `55d78f0002f9e98f3eef93f33cbb856ed62f7e25` |
| target commit | `752a2636d2038c20a80bb79d2484f61874e5e796` |
| target tree | `7037433dfdc09a3f579c095880eec64a608a5377` |
| S06A implementation ancestry | `b1086e064731ee87baa030e3d3de30405acc6972`，已包含 |
| S06A evidence ancestry | `f98b3005feba7b51408f80654057709799a62929`，已包含 |
| slice 数 | `33` |
| 文档输入数 | `109` |
| cleanroom | detached exact-target worktree；未复制原 `.venv`、`node_modules`、缓存、数据库或 artifact |

109 份输入的路径、字节数、SHA-256、读取范围和 target binding 见 `input-document-manifest.json`。除 LangGraph 文档按项目规则完整读取第 12 节至 EOF 外，其余列入清单的文件均从开头读到 EOF；所有必读范围完成。

根目录计划、slice、taskbook 和 source graph 不属于 `zyra` Git 仓库，清单以 `unversioned-workspace-root-snapshot-2026-07-20` 和 SHA-256 固定；`zyra/**` 输入绑定到本次 target commit。

## 3. 33 个 slice 普查

完整逐 slice 实现/evidence/aggregate commit 普查见 `slice-census.json`，数量校验为 `33/33`。摘要如下：

| 范围 | slice 数 | 历史收口 | 本次状态 |
|---|---:|---|---|
| M1-S02A-01 至 M1-S02D-02 | 8 | 早期实现、局部 review fix，后由 R01 custody cutover 累计覆盖 | E01 主路径通过；02D 跨 Browser provider handoff 失败 |
| M1-S03A-01 至 M1-S03D-02 | 8 | `3367825` 修复、`476bc372` evidence | owner 内 E02/MCP/Skill/Agent 通过；公开 ASK transport 失败 |
| M1-S04A-01 至 M1-S04D-02 | 8 | `66c883c` 修复、`dbff7f19` evidence | Browser 局部链大部分通过；Browser → 02D 累计链失败 |
| M1-S05A-01 至 M1-S05B-02 | 4 | `59a0581` 修复、`8e0662f` evidence | workspace/gateway 通过；与 ASK continuation 的交叉入口仍受 P1 影响 |
| M1-S05C-01 至 M1-S05D-02 | 4 | `11740ae` 修复、`67acc5cf` evidence | event/provider/backend 生命周期与 failover 通过 |
| M1-S06A-01 | 1 | `b1086e0` implementation、`f98b300` evidence | retrieval/index 全部针对性探针通过 |

### R01 累计基线

本次没有把 R01 的不同验收性质混写：

- execution-01：`80d8feef` implementation、`f07fd239` evidence，independent PASS。
- execution-02：`9de572bb` implementation、`bad500f6` evidence、`454a22d` 用户接受；它不是 independent PASS。
- execution-03：`62c514d` implementation、`c723a20` independent evidence，independent PASS。
- execution-04：`fb23f8a` implementation、`2c2c216` evidence、`6242f338` independent rereview，independent PASS。

历史报告只用于建立 accepted chain；本次结论来自更晚 target 的实际行为探针，不用历史 PASS 替代当前验证。

## 4. 对早期 subagent 规划的判断

文档普查确认：M1-03A 至 M1-05D 的父级 unit 仍保留固定角色/固定 prompt 的 subagent 矩阵；M1-06A 已采用“按需启动、必须使用 `gpt-5.6-sol xhigh`”的笼统规则。02A–02D 未发现同等强度、会显著左右本次判断的固定角色矩阵。

本次证据支持的结论是：

- 固定拆分确实存在集成盲区风险。多个 owner 内部套件和局部验收均很强，但累计切换 canonical owner 后，两条旧有公共跨域测试没有被持续纳入目标提交的回归选择。
- 03、04、05 的聚合审查曾经实际抓到并修复过 scheduler command、MCP projection、cleanroom、workspace 原子性、gateway 绕行、event sidecar 生命周期等跨 slice 问题。这说明聚合层能缓解早期固定拆分的风险，但不能保证之后的 R01 大规模 custody cutover 不重新引入回归。
- 没有 durable delegation transcript 能把两个 P1 直接归因给某个 subagent，也不能证明 gpt-5.5 是原因。因此不能把“固定 subagent 规划是可信风险因素”写成“某模型或某 subagent 已被证实导致缺陷”。
- 模型版本本身没有表现出可识别的故障特征。当前失败是确定性的状态投影、checkpoint 恢复和跨域 provider-envelope 接线问题；换模型不会自动修复。

所以，用户先前的直觉基本正确：模型版本风险较小，早期固定分工造成的跨边界漏检风险更值得针对性审查。本次审查也确实在该方向发现了实质问题，但问题不是全范围普遍失效。

## 5. State custody 与默认可达性

完整 owner、持久化/恢复、默认入口、fallback、disable 和探针状态见 `state-custody-and-reachability-matrix.json`。关键裁决如下：

| 状态域 | 当前 canonical owner | 默认入口 | fallback/disable | 结果 |
|---|---|---|---|---|
| query/session/turn/tool/budget/compact/provider | TypeScript E01/E04 | CodeWorker API → TS runtime | 无 Python query/policy fallback；disable fail-closed | PASS |
| permission policy/decision/journal | TypeScript E02 | TS capability host | Python 只应做 approval transport projection | **P1 FAIL** |
| MCP/Skill/Plugin/Command/AgentTool | TypeScript E02/E03/E04 | capability/command/AgentTool | 无 Python child loop/canonical fallback | owner 内 PASS |
| Browser session/context/action/observability | Zyra Python browser modules | BrowserWorker API/runtime | permission/process loss fail-closed | 局部 PASS，跨 02D **P1 FAIL** |
| workspace | `zyra_workspace` | workspace API 与 worker attachment | 无 unmanaged root fallback | PASS |
| sandbox gateway | `zyra_runtime.sandbox_gateway` | Code/Browser/MCP gateway | policy/path/process/artifact fail-closed | PASS |
| runtime event spine | Node TypeScript event spine | worker ingress/API | 无 log parsing fallback | PASS |
| provider / backend | TS provider plane / Python backend registry，明确分权 | provider port / backend dispatch | partial stream reconcile-only | PASS |
| retrieval / code index | `zyra_memory` / `zyra_code_index` | retrieval/index API | stale generation fenced；disable 503 | PASS |

## 6. Cleanroom 与探针结果

### 6.1 环境和构建

- Bun `1.2.15` 按 `bun.lock` 全新安装，14 个包。
- TypeScript typecheck 通过。
- Bun 和 Node 双构建通过。
- Python `3.13.9` 全新 venv，pytest `9.1.1`。
- 根 editable install 失败：setuptools 检测到多个顶层包；根 `pyproject.toml` 也没有依赖声明。
- pytest 首次使用用户 profile temp 目录时被 Windows sandbox 拒绝；显式把 `--basetemp` 放进 cleanroom 后，E01 的 23 项全部通过。该项属于环境隔离，不是产品失败。

### 6.2 六组探针

| 组 | 结果 | 关键覆盖 |
|---|---|---|
| G1 TypeScript runtime | Bun `423/423`；Python `23/23` | 默认 TS owner、disable、checkpoint、lost ACK、并发、真实进程 fault、CAS/corruption |
| G2 Permission/MCP/Skill/Agent | Bun `808/808`；Python `11` tests + `14` subtests 通过；公开 ASK 两项失败 | owner 内规则/恢复通过；公共 transport/continuation 断裂 |
| G3 Browser 跨域 | 归一后 `65` tests + `2` subtests 通过，`1` 产品失败 | session、message、action、watchdog、artifact、process；Browser → 02D 失败 |
| G4 Workspace/Gateway | `71` tests + `9` subtests 全通过 | stale base、lease/epoch、回滚、路径、进程、artifact、两个 worker 入口 |
| G5 Event/Provider/Backend | Python `21/21`；Node provider `11/11`；Node event `22/22` | handler drain、A/B 生命周期、DB key rotation、partial stream、failover |
| G6 Retrieval/Index | `14` tests + `8` subtests 全通过 | worker kill、lease/generation fence、receipt、无 substring fallback、invalidation、disable 503 |

G3 最初另有三个缺依赖失败。向新 venv 显式安装仓库现有 `vendor/browser-use[core]` 后，这三项 `3/3` 通过，故从产品失败中剔除，并单列为 `MR-P2-001` cleanroom/packaging 债务。

一次诊断性 Bun 调用错误地运行了要求 Node `node:sqlite` 的 event package；该命令不是 package 声明的 runner。随后严格使用两个 package 的 `npm test`/Node 命令，provider `11/11`、event `22/22` 均通过，后者为权威结果。

## 7. Findings

### MR-P1-001：公开 CodeWorker ASK 投影与恢复断裂

仓库自带测试：

`tests/integration/test_api_control_commands.py::ApiControlCommandTests::test_code_worker_permission_approval_resumes_exact_parked_call_via_api`

预期第一次 `409 permission_suspended` 后可查询 1 个 pending request；实际是 0。

自有双 ASK 探针进一步观察到：

- attempt 1：HTTP 409，`permission_suspended`，2 个工具均产生 failure signal；公开 pending/all request 均为空；无 Python policy fallback。
- attempt 2：HTTP 409，`typescript_runtime_error`，消息为 `restored tool call is missing its scheduled batch`。
- 两个计数副作用均未执行。

fail-closed 避免了未授权副作用，但普通交互审批主路径不可用。任务书明确规定：若第二 ASK 阻断正常连续 batch，则定 P1；当前甚至第一 ASK 的公共 transport 就已阻断，因此不能保留为 P2 已知债务。

### MR-P1-002：Browser disclosure 未进入当前 CodeWorker provider envelope

隔离复跑：

`tests/integration/test_browser_message_state_compression_integration.py::BrowserMessageStateCompressionIntegrationTests::test_api_browser_checkpoint_is_delivered_to_real_02d_provider_once`

Browser 调用成功并报告 `pending_count=1`，但 CodeWorker 返回 409。`selected_source_ids`、`provider_request_ids`、`provider_turn_ids` 和 `event_ids` 均为空，同时出现：

- `provider_context_missing:<browser-disclosure-id>`
- `provider_envelope_event_missing`

隔离进程中仍然稳定失败，排除了前序测试污染。该缺陷让 Browser-local 成功与下游 CodeWorker 失败并存，是典型累计 cross-slice 回归。

### MR-P2-001：根 Python 依赖/安装边界不可复现

根 `pyproject.toml` 没有 dependencies，`pip install -e .` 因 flat-layout 多顶层包失败。Browser live/process 测试需要的 browser-use/psutil 只能通过额外安装仓库 vendor snapshot 才出现。这个问题没有造成上述两个 P1，但使 cleanroom 和发布复现依赖隐含环境状态。

## 8. 修复范围建议（未执行）

后续如获授权，应把修复限制在三个明确边界：

1. 先恢复 TypeScript ASK → Python public transport queue 的原子投影，并使现有单 ASK 测试通过；再修 exact restored batch scheduling，增加同一逻辑批次双 ASK、进程重启、过期、拒绝、丢 ACK、部分副作用 fence 测试。
2. 把 Browser context claim 重新接到当前 TypeScript CodeWorker provider envelope 和 canonical event ids，保留 exactly-once selection 与失败回滚。
3. 建立权威 Python workspace/install/dependency 清单，使全新 checkout 不依赖预填 `.venv`。

修复后至少重跑 G2、隔离 Browser → 02D、相邻 G1/G4、exact-target cleanroom，并做一次独立复审。P1 修复前不得把本范围标记为“追溯复验通过”。

## 9. 变更与提交边界

本次只新增审查报告、结构化 evidence、raw manifest/matrix/probe 和 reviewer-owned probe 脚本；没有修改 production、测试或 `docs/milestones/execution-state.yaml`。

根目录 taskbook、milestone 文档和 source graph 不属于 `zyra` Git 仓库，本次没有改它们。审查 evidence 应以独立 `zyra` commit 提交；该 commit 只表示审查结果被固定，不表示 P1 已修复。
