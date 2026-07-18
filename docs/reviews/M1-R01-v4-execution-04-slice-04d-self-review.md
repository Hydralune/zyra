# M1-R01 E04-D 增量批判式自审

## 结论

`E04-D Skill / Plugin / Command Source Recovery` 在实现提交
`4ef6d8c5b68054b3883bc20764165fecfbd0a440` 上通过增量验收。本结论只关闭
04D 的 `skill_plugin_command` 能力域，不完成 E04，不恢复 `M1-S05C-01`，也不
替代 E04 专用独立终审。

## 上游源码恢复与 Zyra 接管

- `e04-source-012`：把 `loadSkillsFromSkillsDir` 的发现到注册顺序接入冻结 target
  `TypeScriptSkillRuntime.loadSkillsFromSkillsDir`。默认 `SkillCoordinator` 必须先经过
  该 custody facade，再由 `SkillSourceRuntime` 完成根目录约束和磁盘发现、
  `SkillFrontmatterRuntime` 完成 frontmatter 验证、`SkillRegistryRuntime` 原子提交
  registry revision。Claude 的进程全局 skill cache、UI 与 telemetry 被删除。
- `e04-source-013`：把 `executeForkedSkill` 接入
  `TypeScriptSkillRuntime.executeForkedSkill`。E02 从已绑定的
  `AgentExecutionContext` 构造带 parent identity、tool scope、turn/cost/sandbox/network
  预算的 child `RuntimeRunInput`，只调用一次 `runChild`，结果和失败均由
  `SkillInvocationJournal` 接管；不存在 inline 或 Python fallback。
- `e04-source-014`：把 `loadPluginHooks` 的“先构造全部 hooks、再清空并一次注册”
  语义接入冻结 target `SkillReloadRuntime.commitAtomicReplacement`、
  `PluginCoordinator.replaceActiveHooks` 与 `PluginHookRuntime.replace`。单插件集成阶段
  只 staging，不改 live hook；全部 active plugin commit 后才做一次 revision CAS 和
  swap。后续 agent/MCP 集成失败时，旧 live hook revision 保持不变。

逐 range 的 source fingerprint、candidate target fingerprint、裁剪分支和转换说明见
`docs/reviews/evidence/M1-R01-v4/execution-04/slice-04d/target-provenance-report.jsonl`。
G0 不可变清单未改写。

## 默认路径、状态 owner 与反回退

- skill 默认闭包为 `CodeWorkerApplication.runCapabilityApiPort ->`
  `E02CapabilityCoordinator -> SkillCoordinator -> TypeScriptSkillRuntime ->`
  `SkillSourceRuntime / SkillRegistryRuntime / SkillInvocationJournal`。
- fork 默认闭包继续进入 `AgentExecutionContext.runChild`，但 child identity、上下文、
  tool scope、turn budget、结果与失败都由 Zyra TypeScript schema 和 journal 接管。
- plugin/command 默认闭包为 `runCapabilityApiPort -> E02CapabilityCoordinator ->`
  `PluginCoordinator / CommandCoordinator`。插件 command 进入 live capability lease；
  hooks 由 `SkillReloadRuntime` CAS 与 `PluginHookRuntime` 单次 replace。
- registry、invocation journal、plugin runtime record、hook revision、command registry
  均进入 snapshot/restore；恢复时先重绑外部 capability，再一次性重建 hook 集合。
- `ZYRA_DISABLE_E04_SKILL_SOURCE_RUNTIME` 和 domain-06 mutation 均切断真实默认 API
  主路径；Skill、Plugin、Command 三个协调器共同 fail closed，不允许 legacy/Python
  逻辑 fallback。

## 实现中发现并关闭的真实缺口

- 旧 plugin hook reload 会在每个 `addHooks/remove` 中立刻修改 live registrations。
  若后续 `addAgents/addMcpServers` 失败，`PluginRuntime` 虽恢复旧 record，但旧 hook
  已被删除。现在失败发生在 staging 阶段，旧 hook 仍可真实 dispatch。
- restore 时 `PluginHookRuntime` revision 从 0 开始，而 coordinator 另持久化旧 revision，
  会触发 revision conflict。现在 hook runtime 从 snapshot revision 恢复，并在成功
  rebind 后单调递增。
- 默认 supply-chain forbidden pattern `child_process.execSync(` 是非法正则，会阻断
  所有 managed plugin。现已转义为有效表达式，真实 capability API plugin fixture
  才能完成发现、扫描、加载与 command dispatch。

## 对抗、失败与变异结果

- `e04-skill-plugin-command` 通过真实 `TypeScriptCapabilityRuntime` 完成磁盘 skill
  discovery、fork invocation、registry reload、plugin discovery、command registry 与
  plugin command dispatch。
- `e04-skill-fork-failure` 证明 child failure 写入 failed journal，且无 inline fallback；
  `e04-skill-hook-failure` 证明成功 reload 单次换代、后续加载失败保留旧 hook、snapshot
  restore 后旧 revision 可重新 dispatch。
- `e04-skill-reload` 证明 registry 与 acknowledged invocation journal 跨 epoch 恢复；
  `e04-skill-disable` 证明断开冻结 target 后默认 open 链直接失败。
- `e04-mutation-domain-06` 变异后仍通过 TypeScript 编译，但精确 killer
  `e04-skill-disable` 失败；恢复后通过并恢复 SHA-256
  `3048960b8513503f3a2f439d8c6cbe59aac4987d647516f8e23d71448e321660`，无 backup
  残留。

## 动态可达性、断开即失败与依赖边界

- 新控制流由 capability API 的 `list_skills`、`skill`、`reload_skills`、
  `list_plugins`、`plugin_command`、`list_commands` 实际触发，不依赖 import smoke、
  manifest 查询或预录轨迹。
- 若删除 `TypeScriptSkillRuntime.loadSkillsFromSkillsDir`，磁盘 skill 无法进入 registry；
  若删除 `executeForkedSkill`，fork test 不再调用 child；若绕开
  `commitAtomicReplacement`，hook failure/restore 的 revision 与旧 handler 断言失败。
- 当前 diff 未新增 npm/pip、动态 import、子进程、端口、Docker、根目录来源仓库路径或
  vendor/source-pool 运行依赖。插件和 skill fixture 全部在测试临时工作区创建并清理。

## 有效行数分桶

- production TypeScript：新增 171 行、删除 45 行；承担 source custody facade、fork
  边界、skill registry reload、plugin hook staging/CAS/swap/restore、command/plugin
  fail-closed 与 provenance metadata。
- test：新增 443 行；覆盖默认 API、成功、失败、reload、restore、disable 与真实状态
  效果。fixture 代码不计 production。
- validation tooling：新增 11 行；只承担可逆 domain-06 mutation operator，不计产品
  运行时。
- generated、data-as-code、vendor-like/source-pool、adapter-only、mock-only：0 行。
  测试临时 Markdown/plugin 文件只作行为输入，不计有效新增源码。

## 验证与未关闭事项

两个 TypeScript 工程 typecheck、04D 5 个行为测试与 E02 全目录 801 个测试均通过。
G0 在最终实现提交上复核为 18 个 source range、18 个 target、1619 个 Python owner
symbol、13 个 mutation point，零异常。

agent/subagent 与 isolation/control 尚由 04E 关闭；八域 E2E、全 13 mutation、双发行
构建、cleanroom、依赖审计、有效行数累计审计与 candidate gate 仍由 04F 强制执行。
E04 terminal verdict 只能由专用独立审查任务书在最终 target commit 上给出。
