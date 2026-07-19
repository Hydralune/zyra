# M1-R01 E04 独立复审报告（候选 2340b79 / evidence 2c74e21）

- 审查时间：2026-07-19T03:24:35Z
- implementation commit/tree：`2340b792d184bc7625ba15fe7e9d958c3daa4c66` / `27b5f17e87ee204acdc51240f1d5731fddab616d`
- candidate evidence commit/tree：`2c74e21ec35ab29fe67a11b24a5f4d90de64cbfc` / `7d0efc2d4686148026f6d86f3e8a2062b1bf8f73`
- reviewer nonce digest：`1b86f66a19265218a7615c2c395459d3af732749f84f518bf1559e3521caf973`
- 结论：**FAIL**

## 阻塞 finding

### E04-IR2-P0-001：G0 与权威状态源的 verified identity 冲突

`execution-04-baseline-receipt.json` 及候选复制件把 `verified_zyra_head/tree` 写成 G0 tooling commit `c62af85...` / tree `ed8643...`；任务书和 `execution-state.yaml` 指定的受保护 verified baseline 是 `299b708...` / tree `897924...`。候选 `candidate-gate-result.json` 又写回后者，导致同一 candidate 的 exact identity 自相矛盾。违反 R0，属于直接 FAIL。

复现：

```powershell
Get-Content -Raw G:\agent-zoo\docs\remediations\M1-R01-claude-source-custody\manifests\execution-04-baseline-receipt.json | ConvertFrom-Json | Select-Object verified_zyra_head,verified_zyra_tree,zyra_baseline_commit,zyra_baseline_tree
Get-Content -Raw docs\reviews\evidence\M1-R01-v4\execution-04\candidate-gate-result.json | ConvertFrom-Json | Select-Object verified_zyra_head,candidate_commit,candidate_tree
```

### E04-IR2-P0-002：credited source range 不含所声明的成熟控制流

- `e04-source-001` 选取 `src/QueryEngine.ts:1211-1258`，只包含 `ask` 参数与类型声明；函数体在 1273–1320 行，真正的 `new QueryEngine -> submitMessage -> finally persist read cache` 在 1274–1319 行。
- `e04-source-015` 选取 `src/tools/AgentTool/runAgent.ts:248-323`，同样只包含参数与类型声明；真正的 child context/tool/permission/run lifecycle 从 329 行开始。
- `e04-source-012`、`013`、`014` 也在完成 discovery、fork execution 或 hook replacement 控制流之前截断，却取得完整 `adapted_migration` recovery credit。

这违反 R2/R3 的 executable source、retained control flow 和反 semantic-only mapping 条件。

### E04-IR2-P0-003：target anchor 失真且存在 generic target 机械映射

`e04-target-015` 冻结的候选区间仍为 `run-agent.ts:85-93`，但候选 `runAgent` 已位于 103–116 行；候选 provenance 工具只确认 symbol 在文件任意位置出现，没有确认 symbol 位于 credited range。另有以下记录共享同一整类范围和同一 candidate fingerprint，未给出各自 retained sub-symbol：

- `e04-source-001/002 -> QueryLifecycleRuntime:229-1065`
- `e04-source-005/006 -> ToolExecutionRuntime:146-713`
- `e04-source-012/013 -> TypeScriptSkillRuntime:25-394`

这正是任务书 7.4 明示的“多条 source range 机械轮转到 generic target”，违反 R2/R3。

### E04-IR2-P1-004：技能/插件迁移方法未进入真实控制流

- `TypeScriptSkillRuntime.loadSkillsFromSkillsDir` 只执行传入 callback，没有迁入 `readdir -> inaccessible handling -> directory filtering -> parse/register` 控制流。
- `TypeScriptSkillRuntime.executeForkedSkill` 只检查 abort 后调用 `runChild(childInput)`，没有迁入 bounded child context 构建、agent identity、tool/permission scope 或 terminal settlement。
- `SkillReloadRuntime.commitAtomicReplacement` 在候选 production 中无调用边；真实 `commit` 仍直接调用 `registry.commitRevision`。

存在性测试和 disable marker 不能证明动态可达或源码迁移，违反 R2、R3、R8。

## 八域结论

| 能力域 | verdict | 原因 |
| --- | --- | --- |
| query loop | FAIL | source-001 非 executable；001/002 generic whole-class anchor |
| compact/restore | FAIL | exact candidate identity 已失效，不能保留条件通过 |
| tool orchestration | FAIL | 005/006 共用 whole-class anchor，未区分 run/partition retained body |
| permission | FAIL | source-to-target 记录未绑定各自实际 evaluator sub-flow |
| MCP | FAIL | exact candidate identity 已失效，不能保留条件通过 |
| skill/plugin/command | FAIL | 薄转发和未接入 static helper 取得 recovery credit |
| agent/subagent | FAIL | source-015 非 executable，target-015 range 不含 symbol |
| isolation/control | FAIL | exact candidate identity 已失效，不能保留条件通过 |

## 未运行项

R0/R2/R3 已触发直接 FAIL，因此没有把实现方预生成的 terminal、mutation、build 或 cleanroom PASS 当作 reviewer-owned 证据。精确 detached candidate 的一次预检因该目录没有依赖而返回 `typescript_runtime_unavailable`；这不是产品 finding，独立 cleanroom、五故障点、全量 mutation 和默认五入口必须在修复形成新 candidate 后重新使用 reviewer-owned identity 执行。

## 逐字终态回答

> 当前 E04 candidate 是否已经通过真实源码迁移、成熟语义保留、默认产品主路径、正确跨语言职责和独立 cleanroom 交付，充分达到了用户定义的核心补救目标？

否。候选存在 exact identity 冲突、非 executable source credit、失真的 target range、generic target 机械映射以及未进入真实调用链的薄迁移方法，不能通过独立审查。
