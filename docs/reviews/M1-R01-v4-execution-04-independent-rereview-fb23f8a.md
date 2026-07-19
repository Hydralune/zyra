# M1-R01 E04 独立复审（fb23f8a）

## 结论

`PASS`。

本次复审绑定 implementation `fb23f8a8355e51046db1e7e85fa35eecebf59e21` / tree `ff1e92473fd36fb61ec68bc984b3f68e4e1257fb`，以及 evidence `2c2c216cc5a1ca148d8bbdbf63226af667a9054e` / tree `7cb2eb4cedc5da55f2cfe03258c9c9133605a2f1`。evidence 的直接 parent 是 implementation；E04 baseline 仍为 `299b708d3559da7a5da1f9d6d55d2d1f1b155249`。

审查者使用全新 32-byte CSPRNG nonce；只保留 digest `edbe3e68f1084f287e7e8949b466b344d203e7751fd99fcfb7e45d12a56d1089`，原始 nonce 已销毁。reviewer-owned run/session/task/request identities 已在真实 `CodeWorkerRuntime.run` 上执行并观察到 TypeScript canonical owner、1 个 tool-effect receipt、1 个 terminal receipt、`close-result-ack-closed`，无 Python fallback。

## 历史三个 P0 finding 的关闭判断

- `E04-IRB-F001` 已关闭。credited exact target 已从通用 callback wrapper 改为 `SkillReloadRuntime.loadSkillsFromSkillsDir`；该 owner 直接串联具体 root discovery、SKILL.md read/frontmatter parse、错误过滤、durable scan、stale revision guard 和 atomic registry replacement，`SkillCoordinator.reloadNow` 直接调用它。`e04-mutation-domain-06` 对该 exact owner 断开后被 killer 捕获，恢复 hash 后 baseline killer 重新通过。
- `E04-IRB-F002` 已关闭。credited exact target 已落到真实默认 `ClaudeRuntimeCore.run`。它在同一 default owner 中构造 E01/query state，执行 model/tool observe/revise loop，catch 路由失败，并在 `finally` 无条件调用 `finishCanonicalQuery`，随后刷新返回 snapshot。`e04-mutation-domain-01` 删除 finally settlement 后被 `e04-query-disable` 捕获。
- `E04-IRB-F003` 已关闭。credited exact target 已落到实际执行的 `ContextCompactionRuntime.autoCompactIfNeeded`，其顺序为 budget/boundary eligibility -> session-memory-first -> legacy compaction -> success reset / failure record。`e04-mutation-domain-02` 断开 source-custody selection 后可编译但被 exact killer 捕获。

## 八域 verdict

| 能力域 | source recovery | 动态默认路径 | disconnect/mutation | verdict |
|---|---:|---:|---:|---:|
| QueryEngine/query loop | PASS | PASS | PASS | PASS |
| session/context/compact | PASS | PASS | PASS | PASS |
| tool orchestration | PASS | PASS | PASS | PASS |
| permission | PASS | PASS | PASS | PASS |
| MCP | PASS | PASS | PASS | PASS |
| Skill/plugin/command | PASS | PASS | PASS | PASS |
| AgentTool/subagent/task | PASS | PASS | PASS | PASS |
| isolation/control | PASS | PASS | PASS | PASS |

18/18 credited mapping 的 source commit/path/hash/symbol/range、candidate exact target、retained control-flow anchor、default closure、state/effect 和测试 ID 均重算通过。18 条均为 `adapted_migration`；reimplementation exceptions 为空，没有 `reimplemented`、`platform_glue`、generic journal 或 semantic-only mapping 获得 recovery credit。按 reviewer seed 排序完成 18 条 source/target 人工检查，并追加八域各一条 mutation 检查，共 26 条样本；详见 `source-recovery-full-audit.json` 与 `source-recovery-samples.jsonl`。

## 动态门禁

- G0 独立重算：18 source ranges、18 target records、15 mutation points、1619 Python owner symbols、8 domains，exit `0`。
- 默认产品入口：TypeScript source stdio、built Bun stdio、built Node stdio、Python CodeWorkerRuntime、API task route、E02 capability port、E03 structured control port，7/7 PASS。stdio 每条均为 35 frames、16 checkpoint requests、1 terminal result、terminal 后 0 新 host request。
- terminal fault：checkpoint-before-ACK、final-checkpoint-before-terminal、lost ACK、Python host disconnect、TypeScript disconnect，5/5 PASS；最少 2 个 restart epochs，effect exactly-once，revision 单调，stable terminal identity，无重复 completion。
- mutation：15/15 可编译且 15/15 killed；恢复后 15/15 baseline killers PASS；0 residual backup。
- build/test：E04 37 passed；全 TypeScript/MCP 1258 passed；受影响 Python/API 34 passed；typecheck、Bun build、Node build 均 PASS。
- Python owner：1619 个冻结 symbol、31 个 path 全量检查；candidate 6 个新增 Python port symbol 均在允许表；0 logical owner、0 fallback、0 unexpected owner、0 missing symbol/path、0 schema error。
- dependency：24 descriptors 与 671 production files；0 forbidden dependency、0 npm link、0 editable/pth、0 runtime source import、0 external source CLI/sidecar、0 external Docker context、0 opaque runtime bundle。

## Cleanroom 与稳定性观察

最终 fresh exact-candidate cleanroom 从 detached `fb23f8a…` 创建，独立安装 pytest `9.1.1`、Bun `1.2.15` 与锁定依赖，移除 vendor/vendor-runtimes/node_modules/dist/cache/state，且不能依赖 sibling source repositories。8/8 commands PASS，`forbidden_exists=[]`、`dirty_paths=[]`。

第一次 fresh cleanroom 的 Python 集合 33/34 通过，唯一失败为 Windows 下 `python_host_terminal_disconnect` 恢复时序；同一 fault 在独立 terminal matrix 已通过。为避免无限重跑，仅执行有界诊断：精确失败用例一次（1/1 PASS）和一次全新完整 cleanroom（8/8 PASS），之后停止重跑。该现象保留为非阻塞稳定性观察，不构成未关闭 blocker。

## R0-R9 与保护边界

R0-R9 全部完成。E04 没有虚假关闭 2,000 transitions、动态图、端边云、多模型、正式 live 场景或交付门禁；没有改写其它完成 slice 的历史事实。审查者没有修改 production、tests、G0 manifests、candidate gate、实现方 evidence 或根 `execution-state.yaml`；主工作树用户已有的 `AGENTS.md` 修改未触碰。

Blocker：无。

未运行的硬门禁：无。

Evidence：`docs/reviews/evidence/M1-R01-v4/execution-04-independent-review-fb23f8a/`。
