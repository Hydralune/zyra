# M1-08 数字阶段聚合与 M1 里程碑退出审查（2026-07-23）

## 结论

**M1-S08-02、M1-08 数字阶段和整个 M1 严格通过，可以交付 M2。**

最终实现目标为 `068d13358e08967947d13c593d447ee21b72ae51`。退出策略给出：

- `decision=ready_for_m2`
- `29` 项 attestation 全部 passed，失败 `0`
- handoff ready：true
- unresolved requirements：`[]`
- limitations：`[]`
- exit bundle digest：`5849cef791d32e3c185ae5c020cac51852c6ee890cf0a9fd9f34e1b712d85dcb`
- handoff digest：`1702823771d1c434410d3fcc852718a5cef519d8153ff7757f593cd8eb4eb81c`

本审查替代早期针对 `8bd19b0...` 的 blocked 退出结论。早期审查没有被抹去：其 owner、edge/cloud、
provider、sealed benchmark、低熵、材料和 source-pool blocker 均由后续提交逐项关闭，并在本轮最终 policy 中重验。

## 冻结身份与范围

| 项目 | 提交 |
| --- | --- |
| 整个 M1 baseline | `68587549447cacfdbf7992387823b5af6f7f9cf3` |
| M1-08 baseline | `44da53ad8ea909147709857e358b7d16e39f6313` |
| M1-S08-02 baseline | `8065bac109a3bed9ba01e0e92392fec4d05bfca3` |
| 早期 blocked review | `aecc6889ab072896d00c1ad962f3a18a174bfea9` |
| live/benchmark 收口 | `d334b9d2b1fbeb231d5a3ebfe0d7dbe61c517931` |
| API/runtime owner 收口 | `68fdefd50e08d3d3d7f8509d8db17f425d31e1a8` |
| 完整集成验收目标 | `0049362ccf0d48e355f1a555449d75b03fcdacb0` |
| 最终 implementation | `068d13358e08967947d13c593d447ee21b72ae51` |

审查覆盖 M1-08 两个 slice、M1-01A 至 M1-07C 的默认后端主路径、canonical owner、来源角色、
断开即失败、exact resume、依赖边界、有效行数、cleanroom、live 三层执行、双 provider、长程 sealed benchmark、
动态稀疏/低熵、恢复与 M2 handoff。

## 父级主路径与 canonical owner

六类真实主路径均通过：query/session/tool、dangerous permission、MCP、skill-memory-compact、
subagent/lease/recovery、provider/event/watchdog failover。

18 个唯一 owner 全部完成真实 disable -> expected failure/material difference -> restore：

- QueryEngine/session、tool loop、workspace、code index、sandbox gateway；
- permission、MCP；
- memory retrieval、memory curator、skill-memory restore；
- physical worker、edge worker、checkpoint recovery、layered route、graph custody；
- provider control plane、runtime event spine、watchdog。

状态 owner 仍保持唯一：TypeScript QueryEngine/E02 permission/MCP/provider/event、Zyra workspace/sandbox、
retrieval/curator/skill-memory stores、worker lease/checkpoint/recovery/immutable graph stores。Python bridge 没有取得第二
逻辑 owner。LangGraph 只保留 narrow recovery conformance，没有 StateGraph/Pregel production owner。

完整集成 run `m1-integration-7762b39cd124baf91e7f` 共 6 scenarios、66 steps、24 assertions、
18 disconnects，全部通过。provider failover 是真实 loopback HTTP/SSE 3×529 后 fallback model 成功，
watchdog observation 对 run/task/node scope fail closed。

## M1 赛题硬证据

| 门禁 | 最终证据 | 判定 |
| --- | --- | --- |
| 两个跨领域高完成度任务 | runtime integrity 与 verification/requirement traceability，各 500 actions/500 artifacts | pass |
| 单 run >=1,000 actions / >=2,000 transitions | 1,017 / 2,030 | pass |
| sealed autonomous / 人工 0 | intervention、manual resume、manual edit 均 0 | pass |
| local + isolated edge + cloud | terminal local、非 loopback tcp+hmac edge process、OpenAI cloud | pass |
| 两个 provider/wire/model | Anthropic `/v1/messages` + Claude；OpenAI `/v1/responses` + GPT | pass |
| 动态稀疏/低熵及对照 | `low-entropy` child gate 通过；full-connect/full-text/static-route 对照进入 admitted benchmark | pass |
| 异常、需求变化、节点失效恢复 | 5 fault recoveries、1 requirement change、2 topology mutations | pass |
| canonical 因果与消息预算 | causal-evidence、long-horizon-progress、communication ablation 全通过 | pass |
| 算法伪代码、复杂性、可视化因果轨迹 | sealed receipt 绑定 algorithm materials 与 causal Mermaid/JSON | pass |
| owner 断开与 cleanroom | 18/18；最终 target archive 无越界依赖或残留 | pass |

sealed run `m1-final-068d133-20260723a` receipt digest：
`9220887742cc5e2ac3a9187f93477e0138db8a3e6ea6a1e5f7b9ae197c13d86a`。
有效进度率和最终约束满足率均为 1.0，duplicate ratio 为 0。

provider/tier receipts 来自同日 live run `m1-final-68fdefd-20260723a`，均为非 simulated。
后续改动只触及 API 编排暴露、集成一致性、failover 和 watchdog scope，未改变 live-probe/tier owner；
manifest 绑定 live receipt、event、provider、tier 与 line evidence digest，因此复用范围可审计。

## Source-to-target 与严格内化

- Claude-derived QueryEngine/tool/permission/MCP/skill/agent/compact 控制流已经裁剪到
  `packages/runtime/claude-runtime/**`、`packages/integrations/claude-mcp/**`、`apps/code-worker/**`，
  由 Zyra 接管构建、schema、事件、权限、错误、恢复和行为测试。
- browser-use、AgentScope、Hermes、Oh My Pi、OpenHands、opencode、LangGraph 等继续遵循单 owner 与
  supplementary/conformance 边界；没有并行迁入同义 runtime。
- OpenClaw 仅保留受保护历史 provenance，继续 `excluded_forward_only`，没有运行依赖或前向义务。
- whole-M1 历史 `130,176` 行 `vendor-runtimes/**` 以 0 有效行排除并由提交 `aecc688...` 保护；
  从保护边界至最终 target 新增未保护 vendor/source-pool 为 0，cleanroom runtime reference 为 0。

内化证据由真实 API/runtime/worker/event/artifact/control path 触发，并由 18-owner disable matrix 证明
“断开即失败”；ledger、manifest 或固定 health response 没有代替行为。

## 有效行数分桶

| 范围 | effective production | 下限 | raw additions | vendor/source-pool | 结果 |
| --- | ---: | ---: | ---: | ---: | --- |
| M1-S08-01 | 9,193 | 9,000 | 12,341 | 0 | pass |
| M1-S08-02 | 12,778 | 7,000 | 18,509 | 0 | pass |
| M1-08 parent | 21,926 | 16,000 | 30,796 | 0 | pass |
| whole M1 | 285,538 | 不以规模替代证据 | 1,128,967 | 130,176 protected / 0 unprotected | pass |

M1-S08-02 分桶为 production raw 15,693、tests 1,950、docs 866；
generated/data/vendor-like/adapter-only/mock-fixture 均为 0。父级 production raw 27,243、tests 2,324、docs 1,229。

## 验证与复封

完整最终验收在 `0049362...` 上运行约 34 分钟：

- runner exit code 0，API 正常停止；
- 6 场景和 18 owner disconnect 全通过；
- 29 个顶层 gate 均为 passed；
- 当前及相邻 foundation/integration/CodeWorker/watchdog：`76 passed, 3 subtests passed`；
- cleanroom 中 25 unit 与 15 main-path tests 通过。

随后 `068d133...` 只给冻结 catalog gate 增加 evidence pointer 及对应单测，runtime、场景定义、owner probe 均未变。
按项目的风险分层验证规则，仅重跑受影响项并建立 continuity gate：

- diff digest：`4157dbf43c9cdb0f0eb918e14ad596bd8495c2b09479e36b9170d070af1911a5`
- exact-target archive digest：`412597069bd21ac9265df0b62684b80955bdfee106b8393c840a791bb4eb6621`
- exact-target cleanroom digest：`416e8e10dfec8783366d088011ed3d33886eab95f057ce1987d0531df6b6c8b3`
- compile passed；受影响 unit `25 passed in 5.60s`
- catalog evidence count 1；line、cleanroom、continuity、handoff、admission、exit 均 passed
- delta reseal digest：`f318141ddfd85be0a93a9cca935e07c2d32aa56e56f3e326f89343d66e1140ec`

没有必要、也没有执行第二次与改动无关的 34 分钟完整验收；证据明确区分完整 run 与 exact-target 增量复封。

## 最终状态与提交边界

Zyra evidence commit 形成后：

- `M1-S08-02` 进入 protected completed range；
- `M1-08` 数字阶段关闭；
- M1 里程碑关闭；
- M1 -> M2 handoff 标记 ready；
- 根目录权威 `execution-state.yaml` 推进到其声明的 M2 下一入口。

`G:/agent-zoo/docs/milestones/execution-state.yaml` 位于 Zyra Git 之外，不能被 evidence commit 自动包含，
必须在提交后独立更新并在最终交付中说明。

结构化证据见
[`evidence/M1-08-and-M1-exit-review-2026-07-23.json`](evidence/M1-08-and-M1-exit-review-2026-07-23.json)。
