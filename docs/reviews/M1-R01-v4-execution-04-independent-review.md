# M1-R01 v4 Execution 04 独立审查

- 最终裁决：`FAIL`
- 审查对象：implementation `a2c8b8e894f558974018d33416e010b95c7e6f65` / tree `93460d639b3b539fd0253be70d488c7fffa41252`
- 实现证据：`d1401f6ff980ede26d61968a5f20c5c90fc072ea` / tree `f0dcd07bbf0aa362c9816159592404888235ac19`
- 受保护基线：`299b708d3559da7a5da1f9d6d55d2d1f1b155249`
- reviewer nonce digest：`b940f0e6ea1837093be7ef0b8d54a3e971be08d76cb70bab3df1de8e08bb964f`

## 裁决摘要

候选不能进入 M1-S05C-01。审查确认四个 blocker，其中两个直接击穿 R0/R6 硬门禁：冻结的 schema v4 Python owner census 不合法；terminal crash matrix 没有杀死和重启真实 TypeScript owner，反而显式断言不得重启。实现方 candidate gate 因未校验 census 的强制字段、只接受单进程 probe 的自报布尔值而错误给出 PASS。

冻结的两条回归测试独立重跑为 `2 passed`，这仅说明旧默认路径仍可完成，不足以抵扣 manifest、真实 crash/restart、cleanroom 和 evidence 分桶失败。

## Findings

### E04-IR-P0-001 — schema v4 G0 Python owner census 全量缺少强制字段

- 等级：P0 / 自动 FAIL。
- source：`G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/schema-v4.md`，`Python owner census` 合同。
- target：`G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-04-python-owner-census.jsonl`，records `e04-python-0001` 至 `e04-python-1619`。
- state/effect：G0 owner inventory、default reachability、tests/callsites 可核验性和 candidate gate 的 Python owner 结论。
- 事实：1,619/1,619 条记录缺 `tests`，1,619/1,619 条记录缺 `callsites`。文件 hash 与冻结 hash 一致，说明问题在 G0 本身而非候选篡改。
- gate 缺口：`scripts/remediation/verify_m1_r01_e04_candidate.py::python_owner_report` 只读取三个 logical-owner boolean 和 path hash，没有校验 schema 强制字段。
- 复现：

  ```powershell
  $p='docs/remediations/M1-R01-claude-source-custody/manifests/execution-04-python-owner-census.jsonl'
  $r=@(Get-Content $p | ForEach-Object { $_ | ConvertFrom-Json })
  @($r | Where-Object { -not ($_.PSObject.Properties.Name -contains 'tests') }).Count
  @($r | Where-Object { -not ($_.PSObject.Properties.Name -contains 'callsites') }).Count
  ```

- 为什么违反核心目标：缺少测试和调用点后，静态 census 无法证明 Python retained port 没有动态 logical owner/fallback，也不能执行 reviewer-owned 全量 owner 核查。
- 返回阶段：废弃当前 candidate，回到 E04 G0 冻结阶段，修正生成器和 G0 verifier，重新生成并重新冻结全部相互绑定的 v4 manifests；schema-v4 明确禁止在原候选上事后修改 G0。

### E04-IR-P0-002 — terminal recovery 没有真实进程 crash/restart

- 等级：P0 / 自动 FAIL。
- source contract：`cross-language-commit-protocol.md` 的真实 owner kill/restart 与至少两个 restart epoch；E04 独立审查任务书 6.3、R6。
- target：`scripts/remediation/probe_m1_r01_e04.py::{resume,lost_ack,duplicate_ack}`、`scripts/remediation/verify_m1_r01_e04_candidate.py::execute_evidence`、`packages/workers/zyra_workers/typescript_claude_runtime.py`、`packages/runtime/claude-runtime/src/stdio.ts`。
- state/effect：checkpoint revision、terminal result identity、terminal ACK、tool effect receipt 和跨进程 restore。
- 事实：reviewer 重跑 `resume` 得到 `runtime_relaunches: 0`、`first_revision: 0`、`second_revision: 0`。probe 使用 mock 并要求 `Popen` 调用次数为 0；candidate matrix 只有 resume/lost-ack/duplicate-ack/disable 四种模式，没有任务书要求的五个 terminal crash/disconnect 时点，也没有两个 restart epoch。
- 复现：

  ```powershell
  .\.venv\Scripts\python.exe scripts\remediation\probe_m1_r01_e04.py resume
  ```

- 为什么违反核心目标：单进程缓存命中不能证明 TypeScript canonical journal 能在进程死亡后恢复，也不能证明 effect exactly-once、revision 单调、stable identity 或 stale writer rejection。
- 返回阶段：E04 slice-04a terminal protocol closure 与 slice-04f cumulative gate；增加真实外部进程 kill/restart、frame capture、两次以上 epoch 和全部五个 terminal fault point，再由新 candidate 独立复审。

### E04-IR-P1-003 — source-free cleanroom 使用原工作树环境

- 等级：P1 / R7 FAIL。
- target：`scripts/remediation/cleanroom_m1_r01_e04.py:14`、`:16-17`、`:76-84`、`:99-108`。
- state/effect：candidate install/build/test/probe 的依赖隔离与 cleanroom 可复现性。
- 事实：cleanroom 位于原仓库 `.tmp` 下，命令直接调用 `G:/agent-zoo/zyra/node_modules/bun/bin/bun.exe` 和 `G:/agent-zoo/zyra/.venv/Scripts/python.exe`；脚本没有创建 fresh Python environment，也没有从 OS 级访问边界阻止读取原工作树或根来源仓库。
- 复现：检查实现方 `cleanroom-result.json` 的 command argv，或读取上述脚本常量。
- 为什么违反核心目标：通过结果仍可能依赖原工作树 node_modules、venv `.pth`、site-packages 或残留，不能证明提交后的独立交付。
- 返回阶段：E04 slice-04f cleanroom/gate；使用仓库外新隔离目录、fresh locked install/venv、显式 denied-path probe，并记录 cwd、环境和访问失败证据。

### E04-IR-P1-004 — audit/gate tooling 被计入 production

- 等级：P1 / R8 evidence FAIL。
- target：`docs/reviews/evidence/M1-R01-v4/execution-04/line-buckets.json`。
- state/effect：有效实现分桶和累计反缩水审计。
- 事实：六个 `scripts/remediation/*e04*` audit/probe/gate/cleanroom 脚本共新增 2,679 行，被计入 `production.added=5111`，没有独立 audit-tooling/validation-infrastructure bucket。
- 复现：

  ```powershell
  git diff --numstat 299b708d3559da7a5da1f9d6d55d2d1f1b155249..a2c8b8e894f558974018d33416e010b95c7e6f65 -- scripts/remediation
  ```

- 为什么违反核心目标：candidate gate 基础设施不能充当运行时产品实现；该分桶掩盖了真实 production 与验证工具的比例。
- 返回阶段：E04 slice-04f evidence/gate；独立重算 production TypeScript、Python port、adapter、test、audit tooling、docs/data/generated/vendor-like 等桶，并重新核对累计绝对线。

## R0-R9 结果

| Gate | Verdict | 说明 |
|---|---|---|
| R0 identity/baseline | FAIL | candidate/tree/worktree 身份一致且 clean；G0 census schema 不合法。 |
| R1 default path | FAIL | 两条 frozen regressions 通过；强制七入口未在自动 FAIL 后全量重跑，terminal-order 合同失败。 |
| R2 source recovery | FAIL | 18 records 可解析，但 invalid G0/candidate gate 不能授予 credited PASS；人工抽样未在自动 FAIL 后继续。 |
| R3 migration authenticity | FAIL | 未完成 24 个 full-file samples，不能 PASS。 |
| R4 reachability/disconnect | FAIL | candidate mutation evidence 未覆盖 reviewer-owned 真实 process disconnect/restart。 |
| R5 Python owner | FAIL | census 1,619 条均缺 tests/callsites，无法完成全量动态 owner 裁决。 |
| R6 cross-language commit | FAIL | 无真实 TypeScript kill/restart、无两个 restart epoch、terminal fault matrix 不完整。 |
| R7 build/cleanroom | FAIL | frozen regressions 通过；实现方 cleanroom 复用原工作树 venv/node_modules。 |
| R8 effective implementation | FAIL | 2,679 行 audit tooling 错计 production；累计绝对线未在可信分桶上重算。 |
| R9 protection boundary | FAIL | 未修改受保护基线；因前述硬门禁失败，不能恢复主线。 |

## 八域 verdict

八个域均为 `FAIL_NOT_CREDITED`：query/session lifecycle、compact/restore、tool loop/budget、permission、MCP、skills/plugins、subagent、isolation/resume/kill。这个裁决不等价于逐域断言代码全无价值；它表示 invalid G0、未完成 reviewer full-file sampling 和缺失真实 disconnect/restart 证据时，任何域都不能获得任务书定义的 credited PASS。

## Reviewer-owned sampling 与未运行项

nonce 派生的域顺序已记录在 `reviewer-metadata.json`。由于 E04-IR-P0-001 在 R0 即构成自动 FAIL，未继续执行 24 个 source/target full-file 人工样本、八域断开扩展、七个默认入口全量复跑、exact-candidate 全构建、frozen mutation corpus 和独立 cleanroom。任务书允许 FAIL 报告列明未运行项，但这些未运行硬门禁也独立禁止 PASS。

## 状态与修复边界

本次 review 只新增 reviewer report/evidence，没有修改 production、tests、G0 manifests、candidate gate 或实现方 evidence。按任务书，当前 candidate 必须标记 `independent_review_failed`，入口回到 E04 `ready_for_fix`，`verified_zyra_head` 保持不变；修复必须发生在新的实现窗口并形成新 candidate，不能把本次 reviewer 当作修复后同候选的独立审查者。
