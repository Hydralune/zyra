# M2-03 五切片依任务书复审与修复报告

日期：2026-07-25

审查范围：`M2-S03A-01`、`M2-S03A-02`、`M2-S03B-01`、`M2-S03B-02`、`M2-S03B-03`

审查类型：用户指定的数字阶段聚合复审；依照
`G:/agent-zoo/docs/执行单元完成后通用审查任务书.md` 执行。

## 结论

**PASS（发现问题并修复后通过）。**

- 冻结阶段基线：`1833319acdb8a09fac3438b12356da0e9c78d6bb`。
- 本次复审起点：`d98a5235aa9fc4ee93c9cdd6cd8ddb4c285008bb`。
- 本次代码修复与 cleanroom 精确目标：
  `da656829174eb521a331e7dbb1b21bf6c70a7a49`。
- 未解决阻断问题：`0`。
- 五个 slice 的父级目标、状态 owner、默认主路径、失败路径、动态可达性、
  来源角色、有效行数和适用测试在修复后均满足门禁。
- 本报告不把 M2-03 判作关闭里程碑级 live/benchmark/部署证据；这些门禁仍由
  后续 M2/M3 计划承担。

## 审查中发现并已修复的问题

| ID | 严重度 | 首次引入提交 | 问题与语义影响 | 修复 |
| --- | --- | --- | --- | --- |
| M2-03-F01 | P1 | `1bb50e7cf3f5f784ba11a4d107f1ec7730b7b9d8` | `TerminalOutputJournal` 先按内部 chunk 切分再脱敏，配置 secret 跨 256/64K 边界时会泄漏；短 secret 被替换成更长文本后，Web 严格协议会因文本字节数超过 cursor 区间而拒绝；原始 secret 的摘要也仍被发往浏览器。 | 在完整可发布材料上做长度保持脱敏，再同步切分 raw/safe 字节；摘要改为 sanitized material；增加短 secret、跨 chunk secret、摘要不可反推原始值测试。 |
| M2-03-F02 | P1 | `1bb50e7cf3f5f784ba11a4d107f1ec7730b7b9d8` | PTY 读取或内部 chunk 可能截断 UTF-8 code point；短二进制数据的说明文本可能长于原始 cursor 区间，两种帧都会被 Web 合同拒绝。 | 保留尾部不完整 UTF-8、按 code-point 边界切块；无效 UTF-8 转为 binary spill；binary placeholder 受原始字节预算约束；增加跨 PTY read、跨 256 字节边界和 1 字节 binary 回归。 |
| M2-03-F03 | P1 | `ae2a37849cf1099c03e62e7228bfecb478c2cd8a` | Web 和 API 都把 deleted-side 的 `\ No newline at end of file` 误当成 new-side 无换行，导致 patch apply 改变目标文件 EOF 语义。 | 按 marker 紧邻的 diff side 判定新文件是否有尾换行；增加正反向 Web apply 和真实 `WorkspaceEditPort` 提交测试。 |
| M2-03-F04 | P1 | `25b58af8aca0efd6650b2a391eab08f625117b77` | navigate 请求会静默剥离 URL 凭据后继续执行；page-derived `javascript:`、`data:` 或带凭据观测 URL 可进入可点击 `<a href>`。 | navigate 在客户端 fail closed 拒绝 embedded credentials；观测 URL 只有无凭据 HTTP(S) 才生成外链，其余仅显示文本；增加控制拒绝与外链安全测试。 |
| M2-03-F05 | P2/证据阻断 | B01 evidence `7555de96cb8ec9fcc6b7f429f7fe91bcb2add060`；B02 evidence `ca1d1b1eee0ef04451cc619ddc2b3f944f30f99e` | 两份 evidence 缺少非空 `language_custody`，标准 fail-closed 语言托管校验无法运行。B01 的 `source_decisions` 也缺少 `migration_mode`。 | 补齐三个 active implementation role 的生产路径、预期语言、最小新增行和 migration mode；B02 显式绑定预实现决策第 3 节的跨语言例外。五份 evidence 的机器门禁全部通过。 |
| M2-03-F06 | P3 | `a9967d69c98215b996e1f7fd5448339adf115386`、`8984aa80596dc29097b13b8dad4723464bd1af57` | 两份冻结决策文档的 Markdown hard-break 尾空格使阶段区间 `git diff --check` 失败。 | 改为无尾空格的分段元数据，不改变决策事实。 |

先前数字阶段审查已在 `2bc9604685cd7024825cf94a34c3fc507921d661`
修复 terminal ticket receipt 解析；本次重新验证该路径，没有回退。

## 仍未解决的阻断问题

无。

## 非阻断风险

- 这五个 slice 提供控制台主路径和赛题证据载体，但不独自关闭两个跨领域 live
  任务、单 run 2,000 个有效 transition、真实 local/edge/cloud、多模型兼容、
  稀疏/全连接/静态拓扑消融、部署彩排和最终提交材料。
- cleanroom 的完整仓库仍保留历史 provenance、审计器和旧阶段中的 OpenClaw
  字符串；M2-03 diff 中 OpenClaw 命中为 `0`，没有源码、包、进程、路径或运行
  依赖。本次没有读取、恢复或使用 OpenClaw 来源仓库。
- `vendor/browser-use/pyproject.toml` 中存在一条被注释的上游本地开发依赖示例；
  它不是 active dependency，M2-03 没有 manifest/lockfile 变更，也没有新增
  parent-source path。

## 目标覆盖矩阵

| Slice | 主要目标 | 默认主路径与失败路径 | 结论 |
| --- | --- | --- | --- |
| M2-S03A-01 | artifact custody、catalog、range、viewer、security | TaskDetail → typed API → artifact API/store；digest、revision、range、secret、active content、disable 均 fail closed | PASS |
| M2-S03A-02 | diff/patch review、虚拟化、permission、transaction/rollback | artifact → diff API → permission → `WorkspaceEditPort`；stale hash、ask/deny、sealed、rollback failure 和 disable 可观测 | PASS，修复 EOF 换行 |
| M2-S03B-01 | real PTY、ticket/WebSocket、cursor/replay、terminal panel | terminal API → registry/platform PTY → journal → WebSocket → viewer；permission、origin、ticket、cursor、backpressure、kill、recovery 均有行为测试 | PASS，修复输出协议 |
| M2-S03B-02 | BrowserWorker observability、artifact、history、bounded controls | canonical browser owners → observability envelope → Web projection；navigate/retry/stop 只经 typed control，sealed/timeout/disable 无 fallback | PASS，修复 URL 安全 |
| M2-S03B-03 | typed causal index、critical path、cross-view navigation | canonical projection → trace projector/index → timeline/topology/terminal/browser/artifact/diff；missing/late/quarantine/disable 显式 | PASS |

## 内化审查

### 上游源码核对

复审没有只读 ledger 或摘要；直接核对了以下上游实现符号和调用链：

- opencode：
  `ContentCache` 的 byte/entry LRU，
  `context/terminal.tsx` 的 workspace terminal session/clone/update/exit 生命周期，
  `review-panel-v2-state.ts` 的 transient/persisted 状态分界。
- OpenHands：
  `LocalFileStore.write` 的 temp + `os.replace` 原子写，
  `FileDiffViewer` 的 old/diff/new 模式和按需 fetch，
  `Terminal`/`useTerminal` 生命周期，
  `BrowserPanel`/`BrowserSnapshot` 组合。
- browser-use：
  `BrowserStateSummary`、`BrowserStateHistory.get_screenshot`、
  `ActionResult`、`AgentHistory`/`AgentHistoryList`、
  `ScreenshotService.store_screenshot/get_screenshot`。
- oh-my-pi：
  `Patcher.apply/prepare/commit` 的 preflight/commit 分界，
  `Recovery.tryRecover` 的 merge/remap/session-chain，
  `RpcClient` 的 pending request/correlation/event listener，
  `runInteractiveBashPty` 的 PTY lifecycle。
- Hermes 仅用于 auth/origin/reconnect 负向 conformance，没有生产代码迁移配额或
  canonical owner。

### Source-to-target 裁决

| 域 | Primary | Supplementary | Zyra 正式落位与 owner |
| --- | --- | --- | --- |
| artifact | opencode TS | OpenHands TS/Python | `apps/web/src/features/artifacts/**`；`apps/api/zyra_api/artifact_api.py`；`zyra_runtime.artifacts`/`LocalArtifactStore` |
| diff review | opencode TS | OpenHands TS、oh-my-pi TS | `apps/web/src/features/diff-review/**`；`diff_review_api.py`；workspace/permission/artifact canonical owners |
| terminal | opencode TS | OpenHands TS、oh-my-pi TS | `apps/web/src/features/terminal/**`；`zyra_workers.terminal`；`TerminalSessionRegistry`/`TerminalOutputJournal` |
| browser viewer | browser-use Python，经已批准的有界跨语言语义迁移 | OpenHands TS、oh-my-pi TS | `apps/web/src/features/browser/**`；既有 BrowserWorker/session/action/observability owners |
| causal trace | Zyra canonical typed projection | oh-my-pi TS | `apps/web/src/features/trace/**`；canonical projection store 仍是唯一事实 owner |

每个域仍是一个 canonical owner；supplementary 只补齐局部机制，没有第二套
runtime/store/reducer。正式代码落在 `apps/**`、`packages/**`，没有通过
`../opencode`、`../OpenHands`、`../browser-use`、`../oh-my-pi` 或外部 CLI
执行核心决策。

### 动态可达性、语义效果与断开即失败

- 五个 viewer 都由生产 TaskDetail/workbench 路由挂载，不依赖 fixture replay。
- artifact/diff/terminal/browser control 经真实 API、store、workspace、
  permission、worker、event 和 artifact owner 运行。
- real platform PTY、WebSocket ticket/origin/cursor、真实 workspace patch commit、
  BrowserWorker 单次委托和 scheduler fault/recovery causal trace 均在测试中执行。
- artifact reader、diff service、terminal permission/runtime、browser control
  binding、trace projector/index/navigation/controller 被禁用时，相应测试显式失败，
  不会由 fallback 掩盖。

## 有效行数审查

五个 slice 的历史冻结实现区间仍逐个通过。父级与数字阶段按本次最终代码目标
重新做 direct diff，不把子项算术相加：

| Scope | Interval | Raw + / - | Effective | Minimum | Margin |
| --- | --- | ---: | ---: | ---: | ---: |
| M2-S03A-01 | `1833319a..9ca93e34` | 15,640 / 118 | 9,077 | 7,500 | 1,577 |
| M2-S03A-02 | `5dec6715..ae2a3784` | 17,593 / 4 | 10,084 | 7,500 | 2,584 |
| M2-S03B-01 | `f54446ec..1bb50e7c` | 15,688 / 3 | 6,315 | 6,000 | 315 |
| M2-S03B-02 | `7e9483c0..25b58af8` | 14,351 / 1 | 8,935 | 5,500 | 3,435 |
| M2-S03B-03 | `ca1d1b1e..2bc9604` | 9,389 / 4 | 5,904 | 5,500 | 404 |
| M2-03B parent | `cc92c129..da656829` | 46,290 / 28 | 21,190 | 17,000 | 4,190 |
| M2-03 numeric | `1833319a..da656829` | 83,248 / 143 | 40,346 | 32,000 | 8,346 |

本次 review-fix 直接区间 `d98a5235..da656829` 是 `+398/-30`：有效
TypeScript/React behavior `40` 行；UI presentation `10`、声明 `2`、Python
canonical-owner 修复 `113`、tests `232`、comments/blank `1` 均不计入该 TS
floor。`adapter-only`、generated-data、vendor/source-pool 有效贡献均为 `0`。

## 来源语言 custody

标准 `verify_source_language_custody.py` 对五份 evidence 全部通过：

| Slice / source role | 目标生产语言 | 冻结区间新增生产行 |
| --- | --- | ---: |
| A01 opencode primary | TypeScript | 4,810 |
| A01 OpenHands supplementary TS | TypeScript | 3,247 |
| A01 OpenHands supplementary owner | Python | 1,975 |
| A02 opencode primary | TypeScript | 6,305 |
| A02 OpenHands supplementary | TypeScript | 2,916 |
| A02 oh-my-pi supplementary | TypeScript | 2,323 |
| B01 opencode primary | TypeScript | 2,238 |
| B01 OpenHands supplementary | TypeScript | 1,109 |
| B01 oh-my-pi supplementary | TypeScript | 1,724 |
| B02 browser-use primary exception | TypeScript | 4,806 |
| B02 OpenHands supplementary | TypeScript | 1,384 |
| B02 oh-my-pi supplementary | TypeScript | 2,087 |
| B03 Zyra primary | TypeScript | 6,141 |
| B03 oh-my-pi supplementary | TypeScript | 3,202 |

B02 的 Python → TypeScript 有界语义迁移由
`M2-S03B-02-preimplementation-decision-section-3` 预先授权；没有用跨语言机械
转写替代 source-role 裁决。

## 测试与验证

### 本地相邻与全 Web

- 7 个 slice 直接 Python 文件：`66 passed`。
- terminal owner/platform/WebSocket 相邻：`33 passed`。
- typed client + 全 Web：`205 passed, 0 failed, 1,257 assertions`。
- `typecheck:web`：通过。
- `build:web`：通过，`243` modules bundled。
- 五个 source-ledger sync `--check`：全部 aligned，missing target `0`。
- `git diff --check`：本次 evidence 修复后通过。

### 精确 cleanroom

- Source：`git archive da656829174eb521a331e7dbb1b21bf6c70a7a49`。
- Location：`G:/agent-zoo/.tmp/m2-03-followup-da656829`，位于 Zyra
  workspace 之外。
- Dependency：`bun@1.2.15 install --frozen-lockfile`，使用 cleanroom-local
  `node_modules/.bin/bun.exe`。
- Web：`205 passed, 0 failed, 1,257 assertions`。
- Python：15 个适用文件逐进程隔离，`81 passed, 0 failed`。
- Typecheck/build：通过，`243` modules bundled。
- M2-03 manifest/lockfile 变更：`0`。
- M2-03 diff-bound production parent-source path：`0`。
- M2-03 diff-bound OpenClaw：`0`。

测试覆盖真实状态 mutation、workspace commit/rollback、permission denial、
real PTY、WebSocket auth/replay、BrowserWorker delegation、fault/recovery、
cross-view navigation 和 disable mutation，不以 import smoke、静态 UI 或固定
contract 返回替代行为。

## 赛题需求与评分回归矩阵

| Requirement / score | 本阶段证据 | 判定 |
| --- | --- | --- |
| REQ-TRACE-01 | typed causal index、critical path、跨 terminal/browser/artifact/diff/timeline/topology 导航 | advances |
| REQ-FAULT-01 | real scheduler fault/recovery 进入 trace；terminal/browser crash/reconnect 显式 | advances |
| REQ-TOPO-01 | trace 与现有动态 topology typed identity 联动 | advances |
| REQ-EDGE-01 | local PTY、browser worker、artifact/workspace 边界进入控制台 | advances；不等同真实三层 dispatch 闭门 |
| REQ-CLOSE-01 | viewer close 只释放 transient view，不杀 worker/session | advances |
| SCORE-UX | artifact/diff/terminal/browser/trace 高完成度交互主路径 | advances |
| SCORE-ROBUST | stale、gap、timeout、sealed deny、permission、rollback、crash/reconnect | advances |
| live/2,000 steps/ablation/multi-model/deploy | 不属于五个 slice 可独立关闭的证据 | open |

## 提交与状态边界

- Zyra 代码修复提交：
  `da656829174eb521a331e7dbb1b21bf6c70a7a49`。
- 本报告、结构化 evidence 和 B01/B02 language custody 进入随后独立的 Zyra
  evidence commit。
- `G:/agent-zoo` 根目录不是 Git 仓库；根级
  `docs/milestones/execution-state.yaml` 只能在 evidence commit 哈希确定后更新，
  不属于 Zyra commit。
- 已完成 slice 列表、`completed_through` 和 `next_slice` 不改变；只追加本次
  用户指定复审的 fix/evidence 事实并推进 `verified_zyra_head`。

## 下一步

保持 `M2-S04A-01` 为下一执行入口；不要回改本次已复审通过的五个 slice，除非
后续明确发现新的回归或用户再次授权复审。
