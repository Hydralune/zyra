# M2-04 数字阶段聚合审查（2026-07-25）

## 审查对象

- baseline：`53b002bac1e97eab23b7553d344da068dd8dd3c9`
- final source target：`21b1165df4fbb941391d97f4aac304c13c7e312e`
- 覆盖：M2-04A permission/command console 与
  M2-04B session/context/memory/provider/MCP/skill/subagent panels
- 依据：`docs/执行单元完成后通用审查任务书.md`

## 聚合结论

通过。默认 task-detail 主路径使用 M2-01B canonical projection truth。mutation 经过 M2-04A
CommandSurfaceRuntime、sealed/permission admission、Python API/owner bridge，最终由 TypeScript E02/E03
owners 产生 canonical state/effect。UI、receipt、测试 transport 和 Python projection 均未取得第二 owner。

数字阶段 exact diff 重算为 40,214 effective production，高于 31,000 门禁。分桶排除
generated、schema/data、adapter-only、tests、docs、static presentation 和 vendor/source-pool；
Python production 使用 AST/token 分类。父级 M2-04B 为 24,013 / 16,000，当前 slice 为
15,311 / 8,000。

## 对抗复审与修复

初版 `d153c0e...` 后独立 reviewer 指出四个 blocker：mutation 仍可由测试 transport 假执行、测试直接
伪造 canonical effect、direct slash 硬编码 non-sealed、elicitation secret 进入 raw command。
`e8e7d8e...` 将 mutation 接到真实 E02/E03 owner，删除测试 state rewrite，统一 canonical sealed
derivation，并以 structured transient argument transport 隔离 secret。第二轮 reviewer 又识别出
payload sealed 覆盖、retained/retry secret、skill admission/commit snapshot 漂移，以及 subagent
physical attempt/lease/nonce/idempotency 重绑风险。`48fd5c6...` 和 `21b1165...` 关闭这些 authority
fence；所有聚合证据随后重新绑定 final source `21b1165...`。

## Exact-commit cleanroom

exact archive：
`G:/agent-zoo/.tmp/m2-s04b02-cleanroom-21b1165-20260725`

- `bun install --frozen-lockfile`：通过；
- Web + commands + MCP + Claude runtime：1,546/1,546，3,190 assertions；
- root TypeScript typecheck + Web production build：通过；
- Python E02 API cutover：1/1；
- Python WorkerPool API：8/8；
- manifest root/parent source runtime dependency：0；
- `git diff --check`：通过。

cleanroom 显式使用现有 `.venv` 作为 Python 测试解释器，不构成源码或 editable dependency；
`PYTHONPATH` 只指向 cleanroom package roots，pytest 使用 cleanroom-local `--basetemp`。

## 跨 unit 回归与残余

exact cleanroom TypeScript aggregate 为 1,546/1,546；source-tree focused commands 29/29、
MCP/skill/subagent workbench 24/24、skill staged admission/custody 24/24、E03 routing/lease 41/41；
新增 Python focused API suites 9/9；邻接 Python 24 pass + 16 subtests，permission console 2/2。

宽 legacy `test_api_control_commands.py` 为 9 pass / 16 fail，不计 pass。其失败主要是旧 direct
`/tools`/inventory 与已移除 productized-contract builder；`/help` 409 可在数字阶段 baseline 复现。
这些旧 contract 待后续迁移，不替代本阶段 exact owner/API/WorkerPool tests，也不被隐藏为绿色。

## Source-to-target / cleanroom boundary

M2-S04B-02 ledger 为 7 entries：Claude primary，OpenCode/Hermes supplementary，
Agent Framework/AgentScope conformance，browser-use/Oh My Pi reference；missing target 0。
OpenClaw 无 entry、无源码读取、无运行路径，保持 `excluded_forward_only`。archive manifest 不含
父目录 source path、npm link、editable dependency 或外部 Docker build context。

## 独立批判式复审

最终 reviewer 对 `53b002b...21b1165` exact interval 再审真实 owner、sealed、secret、skill staged
supply、nonce、idempotency、lease、source role 与证据冻结。最终回执保存在
`docs/reviews/evidence/M2-S04B-02/independent-review.md`；若有 blocker，本报告不得用于执行状态更新。

## 交付

M2-04A/M2-04B 的代码、真实行为测试、有效行数分桶、cleanroom、source ledger、自审和聚合审查齐备。
根目录 `docs/milestones/execution-state.yaml` 在 Zyra evidence commit 与最终 reviewer 回执完成后更新；
它不进入 Zyra Git。
