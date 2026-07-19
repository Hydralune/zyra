# M1-R01 v4 Execution 04 独立复审（Reviewer B）

## 裁决

**FAIL（P0）**。

候选的默认产品路径、terminal protocol、跨语言提交、15 点 mutation、Python owner、构建测试和 source-free cleanroom 均通过；但 `18` 条 Claude primary credited mapping 中有 `3` 条把语义相近的新实现或薄包装器申报为 retained upstream control flow。E04 的核心目标是恢复并内化上游成熟 TypeScript/TSX 控制流，这一门禁不能由行为测试或 cleanroom 成功抵扣。

审查绑定：

- implementation commit：`3d4a00d62e47264cc4ac8678de41af497be7aec8`
- implementation tree：`af2f174ae9607fbcaebd33a691f1e1c5451fcadb`
- evidence commit：`960eb829f04799ff3ee797fe32f5c9912358f56e`
- evidence tree：`f22112e56c310ecf6a70e26c517d3b573578cad3`
- baseline：`299b708d3559da7a5da1f9d6d55d2d1f1b155249`
- reviewer run：`e04-irb-f356e4ada5165b888f8885a9`
- reviewer binding digest：`57a2328722c1cdfcde90ee7c01f9d7747dad3f4ba73e41750f5dcc6878936873`

## P0 findings

### E04-IRB-F001：skill loader 的 retained-flow credit 落在薄回调包装器

- source：`claude-code-best@c57f5a29e88e9a814bea47abeb9a0a6f725dc102/src/skills/loadSkillsDir.ts::loadSkillsFromSkillsDir`，`407-480`
- credited exact target：`packages/runtime/claude-runtime/src/skills/runtime.ts::TypeScriptSkillRuntime.loadSkillsFromSkillsDir`
- default edge：`SkillCoordinator.reloadNow -> TypeScriptSkillRuntime.loadSkillsFromSkillsDir`

上游 selected body 自身执行目录枚举、entry directory/symlink 判断、`SKILL.md` 读取、frontmatter/path 解析、command 构造和过滤。credited target 只执行：

```ts
const scan = await operations.discover();
operations.validate(scan);
return operations.register(scan);
```

实际扫描和解析仍由候选前已有的 `SkillReloadRuntime.scan` 承担。候选 diff 只是把已有 `reload.scan/commit` 套进三个 callback，并增加只验证 callback 相序的测试。这证明动态行为可用，但不证明上游 loader 控制流进入 credited exact target。

复现：

```powershell
git diff 299b708d3559da7a5da1f9d6d55d2d1f1b155249..3d4a00d62e47264cc4ac8678de41af497be7aec8 -- packages/runtime/claude-runtime/src/skills/runtime.ts packages/runtime/claude-runtime/src/skills/coordinator.ts
```

违反任务书 §7.3、§7.4、§8.1，candidate gate contract §6.2/§6.3，以及 schema v4 §6.1 的 exact executable symbol 要求。

### E04-IRB-F002：`QueryEngine.ask` 被降成 admission wrapper

- source：`claude-code-best@c57f.../src/QueryEngine.ts::ask`，`1211-1320`
- credited exact target：`packages/runtime/claude-runtime/src/query/lifecycle-runtime.ts::QueryLifecycleRuntime.ask`
- state effect：query admission

selected source 包含 QueryEngine 构造、`yield* engine.submitMessage(...)`、输入/abort 流和 `finally` 中的 prompt-suggestion cache 恢复。manifest 的 crop reason 也明确要求保留 input、loop 和 failure ordering。target 仅 stringify prompt 后调用 `this.admit(...)`；没有 submit/yield、loop、failure route 或 finally cleanup。因此这是新的 admission wrapper，而不是申报 source range 的 adapted migration。

复现：

```powershell
git diff 299b708d3559da7a5da1f9d6d55d2d1f1b155249..3d4a00d62e47264cc4ac8678de41af497be7aec8 -- packages/runtime/claude-runtime/src/query/lifecycle-runtime.ts
```

### E04-IRB-F003：compact provenance 指向只产出 plan 的错误 exact symbol

- source：`claude-code-best@c57f.../src/services/compact/autoCompact.ts::autoCompactIfNeeded`，`241-351`
- credited exact target：`packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts::CompactionSourceCustodyRuntime.autoCompactIfNeeded`

上游 selected function 执行 threshold 判定、session-memory-first、legacy compact fallback、失败计数和 circuit breaker。credited exact target 只返回 `AutoCompactionPlan` 与 `phaseTrace`；真正的 session-memory/legacy 执行在另一方法 `packages/runtime/claude-runtime/src/compact/context-runtime.ts::ContextRuntime.autoCompactIfNeeded`。邻接 call edge 可以证明行为闭合，但不能修正被冻结的 exact candidate symbol/retained anchor。

复现：

```powershell
git diff 299b708d3559da7a5da1f9d6d55d2d1f1b155249..3d4a00d62e47264cc4ac8678de41af497be7aec8 -- packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts packages/runtime/claude-runtime/src/compact/context-runtime.ts
```

## 门禁结果

| 门禁 | 结果 | 独立证据 |
|---|---|---|
| R0 identity / G0 | PASS | baseline 未变；18 source、18 target、15 mutation、1619 Python owner；verifier exit `0` |
| R1 default paths | PASS | source stdio、built Bun、built Node、Python bridge、API、E02、E03 全部 exit `0` |
| R1 terminal order | PASS | 三个 stdio 入口均 35 frames、16 checkpoints、1 terminal、0 post-terminal request |
| R2/R3 source recovery | **FAIL** | 18 条全量人工核验，15 PASS / 3 FAIL；见 F001-F003 |
| R4 disconnect/mutation | PASS | 15/15 killed；恢复 SHA 全一致；0 residual backup |
| R5 Python owner | PASS | 1619/1619 retain_port；logical advance/policy/fallback 均为 0 |
| R6 cross-language commit | PASS | 5 类提交及 duplicate ACK、lost ACK、stale writer、corrupt checkpoint 通过 |
| R7 build/test | PASS | isolated frozen install、typecheck、双 build、TS 与 Python integration 全通过 |
| R7 cleanroom | PASS | 8/8 commands；independent tooling；无 vendor/source sibling、dirty path 或旧 cache |
| R7 dependency | PASS | 无 runtime path/editable/link/dynamic-source/opaque black-box dependency |
| R8 line buckets | PASS | production +2570/-487；test +2961/-112；无 E04 gross LOC 门槛或历史缩水 |
| R9 mainline protection | PASS | 未改 execution-state，未恢复 M1-S05C-01，未回写其它 slice 历史事实 |

cleanroom 的首次 sandbox 内执行因网络被拒而无法安装 pytest；按规则使用 scoped network approval 原命令重跑后完整通过。mutation 首轮使用 system Python 时缺 pytest，15 个 mutant 均已 killed 且 hash 全恢复，但四个 restored baseline 无法启动；切换到候选本地 `.venv` 后原 corpus 15/15 完整通过。这两项均作为审查环境过程记录，不计为 candidate finding。

## 返回修复范围

返回 E04 source-recovery 实现阶段，至少需要：

1. query：迁入/裁剪 `QueryEngine.ask` 的真实 submit/loop/failure/finally 控制流，或将当前 wrapper 降为 `reimplemented` 零信用并补合格 primary chain。
2. compact：让 frozen provenance 指向真正执行 session-memory-first/fallback/failure 的精确 target，并确保该 body 是 retained migration；不能只靠 plan method 取得信用。
3. skill：迁入/裁剪真实目录发现、读取、frontmatter 解析和注册控制流；三个 callback 的 wrapper 不得独占 source-recovery credit。
4. 修复后重新冻结/校验受影响 exact target fingerprint、retained anchors、tests 和 mutation，再交由新的独立 reviewer 复审。

Reviewer B 未修改 production、tests、G0 manifests 或根目录 `execution-state.yaml`。

## 最终问题

> 当前 E04 candidate 是否已经通过真实源码迁移、成熟语义保留、默认产品主路径、正确跨语言职责和独立 cleanroom 交付，充分达到了用户定义的核心补救目标？

**否。** 动态行为、跨语言职责和独立交付已经通过，但三条 Claude primary credited mapping 没有在申报的精确目标符号中保留上游成熟控制流，无法无保留 PASS。
