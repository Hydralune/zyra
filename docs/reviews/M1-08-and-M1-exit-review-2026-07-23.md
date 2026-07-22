# M1-08 数字阶段聚合与 M1 里程碑退出审查（2026-07-23）

## 结论

本次审查在监控到 `M1-S08-02` 的 fail-closed 记录后接管，冻结原审查触发提交
`c464dc0162f20a9f0d0f41931c60446110573ec2`，并按
`docs/执行单元完成后通用审查任务书.md` 同时执行 M1-08 数字阶段聚合审查与 M1 里程碑退出审查。

结论分成两层，不能互相替代：

- **工程修复与 M1-08 聚合验证：通过。** 两个修复提交补齐了真实 owner 断开矩阵、错误透传、状态重置、
  source-to-target 账本迁移和 OpenClaw 前向排除策略。最终实现目标
  `8bd19b099b6256e46cd42809324fbc9666af2c7d` 的 exact-commit cleanroom 通过。
- **M1-S08-02、M1-08 和整个 M1 的完成判定：阻断。** 真实 edge/cloud、多 provider wire/model、两个跨领域
  live task、sealed 零人工长程 run、2,000 个有效 canonical transitions、动态稀疏/低熵对照等正式证据仍不存在。
  `execution-state.yaml` 因此继续保持 `completed_through: M1-S08-01`，不得关闭 08-02 或向 M2 交付成功 handoff。

## 冻结身份与审查范围

| 项目 | 提交 |
| --- | --- |
| 整个 M1 基线 | `68587549447cacfdbf7992387823b5af6f7f9cf3` |
| M1-08 基线 | `44da53ad8ea909147709857e358b7d16e39f6313` |
| M1-S08-02 基线 | `8065bac109a3bed9ba01e0e92392fec4d05bfca3` |
| 原 08-02 实现目标 | `e7105fdefebe4723c53bd58a3f0353b73744ece0` |
| fail-closed 监控触发提交 | `c464dc0162f20a9f0d0f41931c60446110573ec2` |
| owner 矩阵修复 | `b6a4b9f642a0c999dec59f05bcd7fc66a9116c76` |
| 账本与最终 cleanroom 目标 | `8bd19b099b6256e46cd42809324fbc9666af2c7d` |

审查覆盖 M1-08 两个 slice 的累计实现、M1-01A 至 M1-07C 的默认后端主路径、状态 owner、来源角色、
断开即失败、依赖边界、有效行数、全量账本以及里程碑赛题证据。已完成 slice 的历史实现和结论未被改写；
修复以当前 M1-08 退出层的前向兼容、执行矩阵和账本 migration 落位。

## 批判式发现与修复

| 等级 | 发现 | 处置 |
| --- | --- | --- |
| P0 | 旧 disconnect 执行器按启发式选择步骤并以空 payload 重放，不能证明被验收 owner 真正断开 | `DisconnectRequirement` 现在强制 `exercise_step_id`；每个 probe 重放真实场景前缀和目标请求，校验稳定错误或语义差异 |
| P0 | 断开矩阵遗漏 MCP owner，且 cleanroom 曾以 `execute_disconnects=False` 运行 | 唯一 owner catalog 扩为 18 项；六类场景映射所有 owner，cleanroom 真实执行矩阵 |
| P0 | edge probe 实际只获得 `worker_location=local`，若继续记成功会伪造真实 edge 证据 | receipt 显式暴露物理位置；没有 edge endpoint 时返回 `edge_live_dispatch_unavailable` 并阻断，不再把本机 fanout 冒充 edge |
| P0 | `RLock` 跨 runner thread 持有、场景复用 session/task、worker lease 未清理，导致 restore 超时和 custody 冲突 | probe 使用独立 task/session 前缀；注册真实 reset handler；父子 lease 在场景结束时释放 |
| P1 | TypeScript 宿主没有转发部分 kill-switch 环境变量，permission/MCP 错误被 stdio 清理路径吞掉 | 明确传递 query/tool/MCP/permission/isolation/skill-memory/compact/provider 开关；stdio 先写 `runtime.error` 再收尾 |
| P1 | sandbox `file_read` 没有经过 gateway enabled 检查；API wrapper 会把 runtime-event/watchdog/provider owner 错误掩成普通响应 | 所有 sandbox owned operation 统一检查 owner；API 将 owner 失败映射为稳定 503/error code |
| P1 | provider、runtime event、watchdog 场景没有命中各自真实控制面；部分响应字段导致伪语义差异 | 场景调用真实 provider health、event GET 和 fault ingress；比较使用有界语义投影，不以无关 response digest 充数 |
| P1 | 全量 ledger 仍指向已淘汰的 `main.mjs`、Python query/permission/MCP owner，并把 OpenClaw 当作前向必需来源 | migration 将历史来源事实映射到当前 TypeScript owner；OpenClaw 仍保留历史 provenance，但从 required source set 移除 |
| P1 | ledger 的原始字符串扫描把 deny-list 定义和历史 remediation 脚本误判为运行期 `../source` 依赖 | Python 使用 AST 识别可执行字面量并排除静态 deny-list；remediation source-access 工具不作为产品运行时扫描。exact cleanroom 的独立边界扫描仍是硬门禁 |

修复没有引入 Python decision fallback，也没有把 ledger、健康检查或固定 contract 当成 owner 行为。禁用当前
Zyra-owned 模块会在对应场景产生明确失败或可解释语义变化。

## 主路径、owner 与断开结果

六类主路径为 query/session/tool、dangerous permission、MCP、skill-memory-compact、subagent-recovery、
provider/event/watchdog failover。18 个唯一 owner 覆盖：

- QueryEngine/session、tool loop、workspace、code index、sandbox gateway；
- permission、MCP；
- memory retrieval、memory curator、skill-memory restore；
- physical worker、edge worker、checkpoint recovery、layered route、graph custody；
- provider control plane、runtime event spine、watchdog。

结果是 **17 个当前可执行 owner 完成真实 disable -> expected failure/material difference -> restore**。第 18 个
`edge-worker` 没有假通过：实测 `observed_worker_locations=["local"]`，因此以
`edge_live_dispatch_unavailable` 保持正式 blocker。MCP 在 MCP tool surface 上断开，permission 在真实 command
surface 上断开；二者不再共享一个未命中 owner 的替代探针。

主要状态托管继续保持唯一 owner：TypeScript QueryEngine/E02 permission/MCP、Zyra workspace/sandbox、
retrieval/curator/skill-memory stores、worker lease/checkpoint/recovery/immutable graph stores、TypeScript provider
control plane 和 runtime-event spine。Python bridge 只承担物理 I/O、持久化或 API 接入的已裁决边界，不取得第二
逻辑 owner。

## Source-to-target 与内化判断

- `claude-code-best` 的 QueryEngine、tool、permission、MCP、skill/agent/compact 控制流已裁剪进入
  `packages/runtime/claude-runtime/**`、`packages/integrations/claude-mcp/**` 和 `apps/code-worker/**`，由 Zyra
  构建、状态、事件、权限、错误和行为测试接管。
- browser-use、AgentScope、Hermes、Oh My Pi、OpenHands、opencode、LangGraph 等来源继续遵循已裁决的单 owner
  和 supplementary/conformance 边界。LangGraph 只保留 narrow recovery conformance；没有 StateGraph/Pregel
  production owner。
- OpenClaw 历史 ledger/provenance 保留，但为 `excluded_forward_only`，不再产生源码读取、迁移、适配、测试或
  运行依赖义务。
- whole-M1 diff 中仍有 `130,176` 行 `vendor-runtimes/**` 历史 source-pool。它们全部按 0 有效行排除，且
  cleanroom 的运行期引用为 0；现有 line auditor 对“里程碑基线以来出现 vendor/source-pool”仍给 blocker，
  所以不得用 281,708 条非 vendor 有效行覆盖这一治理债务。

严格 ledger 审计最终为 `0 errors / 0 blockers / 695 warnings`。其中 M1 有 487 条 warning，全部属于未连接的
planned/candidate 行：284 个尚未物化目标和 203 个待补 notice；connected M1 warning 为 0。它们没有被提升为
active，也没有被计算为完成。

## 有效行数分桶

| 范围 | 有效 production | 下限 | raw production | test | docs | vendor/source-pool | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 审查修复 `c464dc0..8bd19b0` | 694 | 0 | 848 | 264 | 0 | 0 | pass |
| M1-S08-02 `8065bac..8bd19b0` | 8,659 | 7,000 | 10,843 | 794 | 468 | 0 | pass |
| M1-08 `44da53a..8bd19b0` | 17,842 | 16,000 | 22,431 | 1,168 | 831 | 0 | pass |
| whole M1 `6858754..8bd19b0` | 281,708 | 不以规模替代证据 | 567,477 | 78,784 | 139,428 | 130,176（计 0） | source-pool blocker |

有效 production 按 AST/脚本分类扣除了 import、空行、注释、literal/docstring、schema/DTO、signature、type/interface、
测试、文档、data、mock 和 vendor-like 内容。M1-08 的最低线已满足，但代码规模不抵扣 live/benchmark 门禁。

## 验证证据

最终 exact-commit cleanroom target 为 `8bd19b099b6256e46cd42809324fbc9666af2c7d`：

- archive digest：`b4fb4036138fbcf291fc318244a3061d5ea857f928dbde500ac1bc0dbe1f1061`；
- receipt digest：`0e3b08b84ed250c75a5f68926f8b0551cfbb8d93dccc27ac6f07a694080612c2`；
- 6,807 archive members，6,532 files，9 个 workspace packages；
- residual、outside link、symlink、forbidden production reference、limitation 均为 0；source worktree clean，cleanup 成功；
- `compileall` 通过；M1 hardening unit `13 passed`；完整主路径 `15 passed in 850.45s`。

其它定向证据：

- owner 场景逐组验证覆盖 query/sandbox、permission、MCP、skill-memory、provider/event/watchdog、recovery；
- Python 当前修复路径 `22 passed`；provider Node `12 passed`；相关 TypeScript E04/E02/protocol `127 passed`；
- ledger migration/forward exclusion/AST scanner `4 passed`；ledger 控制面相关组 `38 passed, 1 failed`；
- 严格 ledger 命令通过：`audit_ok=True, errors=0, blockers=0`，M1-08 line floor 通过。

已披露但不归因于本轮 diff 的相邻基线：旧 delegated MCP reachability 测试仍要求已经不存在的
`POST /mcp/servers/{server_id}/connect`（同组 38 项通过）；旧 TypeScript permission suite 还有 raw digest prefix 与
autonomous MCP allow/deny 两个历史断言不匹配（同次 124 项通过）。本轮没有为了这些旧断言恢复被淘汰 owner 或
放宽权限策略。

## M1 正式退出 blocker

| 门禁 | 当前证据 | 判定 |
| --- | --- | --- |
| 两个及以上高完成度跨领域 live task | 没有 admission-ready live envelope | blocked |
| 单 run >=1,000 有效 actions / >=2,000 canonical transitions | 没有 sealed 长程 receipt | blocked |
| sealed autonomous、人工干预 0 | 没有正式 policy/run 证据 | blocked |
| 真实 local + 隔离 edge + cloud dispatch | local 已验证；edge 明确不可用；cloud 未验证 | blocked |
| 两个不同 wire dialect/provider/model | provider 控制面代码通过，真实双 provider 请求/流式/tool-result 证据缺失 | blocked |
| 动态稀疏拓扑、低熵及 static/full-connect/full-broadcast 对照 | 没有正式 baseline/ablation 报告 | blocked |
| 异常、需求变化、节点失效的 live 恢复 | 定向行为测试通过，正式长程 live evidence 缺失 | blocked |
| 消息预算与 p50/p95/max、route density、broadcast、bytes/transition 指标 | 没有正式长程指标 envelope | blocked |
| 核心算法伪代码、复杂性分析、可视化因果轨迹 | M1 退出材料尚未形成可验收提交 | blocked |
| 历史 vendor/source-pool 治理 | 不计行数且无运行依赖，但 whole-M1 line audit 仍显式 blocker | blocked |

## 状态与排期

根目录 `G:/agent-zoo/docs/milestones/execution-state.yaml` 不属于 Zyra Git。本审查不修改其完成状态：

- `completed_through` 保持 `M1-S08-01`；
- `next_slice` 保持 `slice-08-02-main-path-hardening-integration.md`；
- `M1-S08-02` 不进入 protected completed range；
- M1->M2 handoff 继续携带 blocked 状态。

距离 2026-09-15 官方提交截止日约 54 个日历日。下一次 08-02 尝试必须优先补真实 edge/cloud/provider、两个
live task 和 sealed 2,000-transition 证据，再执行相同 admission/exit policy；不能继续用本地 cleanroom 重复通过
来替代外部 evidence。上述证据齐备后，还需重新运行 exact-target cleanroom、whole-M1 source-pool 处置审查和
最终退出矩阵，才可更新权威 YAML。

结构化证据见
[`evidence/M1-08-and-M1-exit-review-2026-07-23.json`](evidence/M1-08-and-M1-exit-review-2026-07-23.json)。
