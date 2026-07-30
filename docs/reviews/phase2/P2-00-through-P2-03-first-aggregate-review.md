# P2-00 至 P2-03 首次数字阶段聚合审查

## 裁决

`PASS_AFTER_FIX`

- 审查范围：`P2-00`、`P2-01`、`P2-02`、`P2-02A`、`P2-03`，共
  15 个 slice。
- 聚合基线：`57c0def91a69c851c941e7d4df80519cf881a8c7`。
- 审查前 target：`a8bc199a7d08fa981eef9cb3b1ac5c609b514266`。
- 修复后 target：`1d81143a9e9ab2a3f27ca1051e0bacb33f98a721`。
- 未发现未关闭的 P0/P1 问题。
- 下一数字阶段建议：`USER_DECISION_REQUIRED`。本次审查不授权或自动启动
  `P2-S04-01`。

审查按目标重量分配力度：P2-00 以冻结合同和可重放性为主，P2-01 以
LoopX 包装、桥、控制和 release 边界为主；P2-02/P2-03 才展开 owner、激活门、
memory/symbolic、动态图和 cleanroom 行为审查。LoopX 上游完整源码按
`runtime-assets/vendor-like` 处理，没有把体量当成 Zyra 实现量。

## 数字阶段结论

| 数字阶段 | 结论 | 聚合判断 |
| --- | --- | --- |
| P2-00 | `PASS_AFTER_FIX` | 冻结身份、source role、owner、指标和 readiness 合同成立；修复后 24 个冻结引用可在后续提交上从 Git 冻结快照重放。 |
| P2-01 | `PASS` | 固定完整 LoopX 源码、release locator、durable bridge、single-writer、outbox、控制/restart 和首次任务路径成立；LoopX claim/quota 未越权替代 Zyra lease/budget。 |
| P2-02 | `PASS_AFTER_FIX` | policy contract、唯一 registry、memory continuity 和 neuro-symbolic evidence 路径成立；最强组合仍未激活，激活门现会强制检查固定的五机制全集。 |
| P2-02A | `PASS` | 两个 legacy source pool 当前目标数为 0；独立 clean source 上 retirement 测试 7/7 通过，没有恢复 runtime fallback。 |
| P2-03 | `PASS_AFTER_FIX` | ARG → CARD → AgentPrune → symbolic projector → GraphStateCustody 的显式 validation 主路径成立；修复 Windows clean checkout 的 readiness 换行稳定性后，独立检出 24/24 通过。 |

## 本次发现并关闭的问题

### P1-01 冻结证据读取了当前工作树

`Phase2BaselineVerifier` 和 `FrozenEvidenceIndexer` 原来按当前路径校验
`package.json`、`pyproject.toml` 等冻结引用。后续 slice 的合法修改因此会让
P2-00 的冻结基线反向失效，首次聚合运行表现为 `142 passed, 1 failed,
6 errors`，基线验证有 4 个 digest/size blocker。

修复后：

- `p2_base_commit` 继续只表示阶段入口身份和 lineage；
- 引用字节从首次提交冻结清单的 Git snapshot 读取；
- 原始 Windows CRLF 捕获只有在 frozen size 和 SHA-256 同时精确匹配时才可
  重建；
- 基线清单本身仍按固定 manifest digest fail closed；
- 新增 descendant-worktree 回归。

### P1-02 局部 topology readiness 可能被误当成完整最强组合

P2-S03-04 为隔离 validation 注册了 ARG、CARD、AgentPrune 三个就绪机制，而
冻结 activation contract 要求 LoopX、ARG、CARD、AgentPrune、MaAS 五项。
原激活 gate 只检查 registry record 内已有项，存在未来误激活的门缝。

修复后 `StrongestProfileActivationGate` 强制精确匹配固定五机制全集。当前
三层 topology validation 仍可显式运行，但缺少 LoopX/MaAS 时 default
activation 必定拒绝；正常 run 继续固定在
`phase1_deterministic_baseline`。

### P1-03 P2-03 父级证据绑定了不存在的 base

父级 review 和 machine summary 中的
`7d2658635460163478d88848e79f7ecc650a9274` 不是 Git object。现已改为与
P2-S03-01 一致的真实父级 base
`2ab79c0a6cd862b9ddfd650d6bccd1fe610b4d60`。同时修正
`P2-S02-02A` 为正式 slice id `P2-S02A-01`。

### P1-04 clean checkout 的 CRLF 使三层 topology 全部降级

三个 runtime 对 `mechanism-readiness.json` 使用工作树原始字节哈希。主工作树
中的 LF 能通过，但 Windows 干净检出为 CRLF 后，ARG、CARD、AgentPrune 都会
报告 readiness mismatch 并 fail closed。三个 digest helper 现统一把 CRLF
规范化为 Git canonical LF，并有同时覆盖三层的回归测试。

## 15 个 slice 聚合门

| Slice | 聚合结果 | 说明 |
| --- | --- | --- |
| P2-S00-01 | `PASS_AFTER_FIX` | 冻结 reconciliation 可跨 descendant commit 重放。 |
| P2-S00-02 | `PASS` | source role、ADR、owner、metric/evidence contract 验证有效。 |
| P2-S00-03 | `PASS_AFTER_FIX` | readiness 仍按 input-precheck fail closed；冻结 evidence index 不再依赖当前工作树。 |
| P2-S01-01 | `PASS` | pinned/offline release runtime 和 source integrity 通过。 |
| P2-S01-02 | `PASS` | durable bridge、outbox、single-writer、restart owner 边界通过。 |
| P2-S01-03 | `PASS` | control/restart、API 和 Web LoopX feature 通过。 |
| P2-S01-04 | `PASS` | v0.2.13 embedded source、deep doctor 和 cross-version 边界通过。 |
| P2-S02-01 | `PASS` | fixed policy contracts 与 activation baseline 通过。 |
| P2-S02-02 | `PASS_AFTER_FIX` | registry/shadow validation 保留；完整五机制 activation gate 已补强。 |
| P2-S02-03 | `PASS` | memory continuity、symbolic projector、adversarial evidence 路径通过；handoff id 已修正。 |
| P2-S02A-01 | `PASS` | retirement verifier 为 valid，current legacy target count 为 0，clean source 通过。 |
| P2-S03-01 | `PASS_AFTER_FIX` | ARG 行为有效；clean-checkout readiness digest 已稳定。 |
| P2-S03-02 | `PASS_AFTER_FIX` | CARD 条件残差有效；clean-checkout readiness digest 已稳定。 |
| P2-S03-03 | `PASS_AFTER_FIX` | AgentPrune 空间/时间剪枝有效；clean-checkout readiness digest 已稳定。 |
| P2-S03-04 | `PASS_AFTER_FIX` | composer、disable/mutation、冲突/replay、churn/oscillation 和 custody commit 通过；父级 base 已修复。 |

## 验证摘要

- Phase 2 baseline：`valid=true`，24 个引用，0 finding。
- Phase 2 policy contracts：`valid=true`，target 为
  `1d81143a9e9ab2a3f27ca1051e0bacb33f98a721`，最强组合未激活。
- unit/contract：69 passed。
- readiness/registry/activation：24 passed。
- 最终 topology/memory/symbolic：41 passed。
- LoopX 分组：24 passed，包括 source integrity、embedded source、bridge、
  restart、control 和 release doctor。
- legacy retirement：主工作树 7 passed；clean source 7 passed。
- 独立 clean source：确认 Python 模块从独立检出路径导入，P2-00/P2-03
  关键门 24 passed。
- Web LoopX feature：3 passed；Web TypeScript typecheck 和 375-module
  production build 通过。
- ledger audit：`ok=true`、`errors=0`、`blockers=0`；982 条 warning 是既有
  inventory/覆盖债务，不作为本范围实现量或通过证据。
- `git diff --check` 和相关 Python compile 通过。

完整命令和结果见
`docs/reviews/evidence/phase2/P2-00-through-P2-03/1d81143/commands.json`。

## 代码量与高风险边界

聚合 diff 的普通 rename-aware shortstat 为 5,882 files、845,911 additions、
773,918 deletions。为了避免 rename detection 在超大删除集上不稳定，机器证据
按 `--no-renames` 分类为 5,886 files、845,932 additions、773,939 deletions。

关键桶：

- Zyra production/config 候选：106 files，34,455 additions，1,536
  deletions；
- tests：45 files，13,501 additions，509 deletions；
- LoopX runtime-assets/vendor-like：1,926 files，720,835 additions，12 个
  binary；
- retired `vendor/**` 与 `vendor-runtimes/**`：3,684 files，771,117
  deletions，7 个 binary；
- docs/evidence：103 files，62,750 additions；
- generated/data/manifests：5 files，12,276 additions。

这些是 changed-line 分类，不把上游源码、删除量、证据 JSON、manifest、测试或
数据计入 Zyra 深度实现。

## 保留的 P2 说明

1. 没有重跑全仓库 pytest 或完整 13-gate release CI。原因是本次范围按 15 个
   slice 的直接及相邻门禁、独立 clean source、release doctor、retirement 和
   Web 产品路径验收；P2-S02A-01 已记录的跨阶段 broad-suite 债务仍保留，未伪装
   成全仓 PASS。
2. 仓库外 `G:\agent-zoo\docs\milestones\execution-state.yaml` 仍把 verified
   head 记为 `d0afb45`，并把 P2-S03-04 写为未授权候选。这与当前 Git 完成证据及
   用户本次给出的完成事实不一致。通用任务书禁止本次自动改写未授权状态或推进
   下一阶段，因此未修改该根目录文件；进入 P2-04 前应由用户明确授权并单独同步。

## 证据

- `docs/reviews/evidence/phase2/P2-00-through-P2-03/1d81143/aggregate-review.json`
- `docs/reviews/evidence/phase2/P2-00-through-P2-03/1d81143/gate-matrix.json`
- `docs/reviews/evidence/phase2/P2-00-through-P2-03/1d81143/bucket-summary.json`
- `docs/reviews/evidence/phase2/P2-00-through-P2-03/1d81143/commands.json`

