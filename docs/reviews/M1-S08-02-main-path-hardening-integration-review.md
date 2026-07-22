# M1-S08-02 main-path hardening integration 批判式自审

日期：2026-07-23

slice baseline：`8065bac109a3bed9ba01e0e92392fec4d05bfca3`

父级 baseline：`44da53ad8ea909147709857e358b7d16e39f6313`

实施前冻结：`0a8518e6b7e115df9e862372cb80447b8020eb7e`

implementation：`e7105fdefebe4723c53bd58a3f0353b73744ece0`

## 结论

本轮完成了 08-02 的集成/审计实现、六条真实 HTTP 主路径、fail-closed 退出策略、精确 commit
cleanroom、有效行数审计以及 M1 -> M2 handoff 数据结构，但 **不能判定 M1-S08-02 完成**，父级
`M1-08` 与 M1 也不能关闭。权威执行状态必须继续指向 08-02，不能更新保护范围。

阻断不是代码规模或 cleanroom：slice 有效 production 为 **8,016/7,000**，父级累计为
**17,201/16,000**；精确 commit `e7105fd...` 的 Git archive 中编译、13 个 08-02 单测和 8 个
集成测试全部通过，且无 cache/database/log 残留、越界链接或兄弟源码仓库运行引用。真正未关闭的是：

1. 17 项 owner disable contract 虽全部注册，但六场景只映射 12 项，本轮没有执行完整最终矩阵；
   workspace、code-index、memory-curator、edge-worker、graph-custody 尚未映射到场景。
2. 没有一条正式、连续、sealed autonomous 的同 run 证据达到 1,000 effective actions 和 2,000
   canonical transitions；单元测试中的计数样本不冒充 benchmark。
3. 没有可接纳的真实 local + isolated edge + cloud 三层 execution receipts，也没有两个真实 provider /
   wire dialect / model 的完整 request-stream-tool-result 证据。
4. 没有动态稀疏拓扑相对 full-connect broadcast、full-text inline、static-route 的低熵对照，也没有两个
   高完成度跨领域 live task 和零人工 fault/recovery 正式闭环。

因此本轮 evidence verdict 是 `implementation_complete_exit_blocked`，不是 `slice_complete`。

## 实施落位

| 产品责任 | Zyra 落位 | 真实入口 / 语义 |
|---|---|---|
| 六场景协议与执行器 | `integration_contracts.py`、`integration_scenarios.py` | query/session/tool、permission、MCP、skill-memory-restore、subagent-recovery、stream-provider-failover 经真实 HTTP API 执行 |
| 集成编排与退出 | `integration_service.py`、`exit_gate.py` | 聚合场景、foundation gate、owner matrix、live evidence、benchmark、cleanroom、line evidence、handoff；任一缺口保持 blocked |
| 正式进度计数 | `benchmark.py` | 排除 heartbeat/log/repaint/replay/no-op/fixture，按 mutation/route/tool/permission/restore/recovery 等 semantic family 计数 |
| live tier/provider evidence | `live_evidence.py` | 验证 endpoint/process/host/isolation/request/route/lease/artifact/digest；loopback 或 simulated edge/cloud 不可晋升 |
| 17-owner inventory | `owner_matrix.py`、`owner_probes.py` | source/symbol/route/event/restore/dependency 解析，可逆 environment disable/restore receipt 和 fallback masking 检查 |
| LangGraph/cross-scenario | `cross_scenario.py` | identity/causation、runtime topology mutation、determinism/conflict/pending-committed/fence 的 fail-closed gate |
| evidence admission | `evidence_admission.py` | digest-bound envelope、origin/kind/commit/run/task 校验、tamper rejection |
| exact-commit cleanroom | `cleanroom.py` | `git archive`、边界扫描、锁定 Bun、离线 workspace 解析、归档内 TEMP/TMP、命令 receipt |
| M2 handoff | `handoff.py`、`release_reporting.py` | surface/source-chain/state-custody/blocker/metrics 的 digest-bound 原子持久化 |
| API / CLI | `api.py`、`cli.py`、`apps/api/zyra_api/main.py` | integration status/run/list/get/execute 与 CLI integration；persisted response digest 与磁盘记录一致 |

本 slice 的 `migration_mode=audit_and_hardening_only`。没有引入新上游实现，没有迁移 OpenClaw，没有改变
02A–07C 的 canonical state owner、transaction、lease、idempotency 或 restore 语义。新增状态只有 derivative
integration report、evidence envelope、cleanroom receipt 和 M2 handoff，由 Zyra evaluation store 保管。

## 六条主路径证据

| 场景 | 本轮已验证语义 | 结果 | 未关闭项 |
|---|---|---|---|
| query/session/context/tool | 新 task/session、context、CodeWorker tool、artifact/revision/event 回读 | cleanroom 通过 | query/tool 两项正式 disconnect 未执行 |
| dangerous permission | 危险写操作进入 ask/deny/block，未授权副作用不落盘 | cleanroom 通过 | permission/sandbox 正式 disconnect 未执行 |
| MCP auth/elicitation | MCP tool/resource/prompt、auth/elicitation 与真实 task mutation 串联 | cleanroom 通过 | tool/permission 正式 disconnect 未执行 |
| skill -> memory -> compact restore | bundled skill 物化到 workspace，skill invocation、memory ingest/mine、compact、前后 `/context` 变化 | cleanroom 通过 | retrieval/skill restore 正式 disconnect 未执行，curator 未映射 |
| subagent/background -> lease -> recovery | fanout、worker lease、typed fault、successor registration、recovery handoff/reroute | cleanroom 通过 | physical/recovery/layered 正式 disconnect 未执行，edge/graph 未映射 |
| stream stall/backend unavailable | API stream/backend failure、retry/failover 与 provider/event evidence | cleanroom 通过 | provider/event/watchdog 正式 disconnect 未执行 |

这里的“通过”只表示场景正常/失败路径和断言通过。测试明确使用
`execute_disconnects=False`，不能把 8 个 cleanroom integration tests 写成完整 disable matrix 证据。

## Disable matrix 批判式结果

`OwnerProbeCatalog` 注册 17/17 contracts，contract validation 通过；但注册不等于动态证据。

- 六场景映射 12 个 probe：query-session、tool-loop、permission-runtime、sandbox-gateway、runtime-event-spine、
  provider-control-plane、memory-retrieval、skill-memory-restore、physical-worker、watchdog、checkpoint-recovery、
  layered-route。
- 未映射 5 个 probe：workspace-runtime、code-index、memory-curator、edge-worker、graph-custody。
- 本轮最终模式 executed capability count 为 0；cleanroom 场景验证关闭了 disconnect execution。
- 当前 production 搜索还发现 workspace、sandbox、runtime-event、provider、memory-curator、watchdog、
  layered-route 和 graph-custody 的 contract flag 未在对应 owner 中形成一致的动态断开路径。单纯把 flag
  名写进 catalog 不构成完成证据。

所以完整矩阵必须在本 slice 后续回补中完成：场景映射、owner 侧真实 fail-closed flag/控制入口、相同请求
baseline -> disable -> stable failure/material difference -> restore -> baseline 的动态 receipt 缺一不可。

## Cleanroom 与依赖边界

最终 receipt：

- target commit：`e7105fdefebe4723c53bd58a3f0353b73744ece0`
- archive SHA-256：`08b2f2b627f9e7eef08ce2e1d5bcfa08562e824b95553a54bb54b00e95468991`
- receipt digest：`529790440634d396830672c916e0d55d474d8d5685261e97a618593fcbc579ed`
- archive/post-command file count：6,530；symlink：0；residual：0；outside link：0；forbidden runtime reference：0；cleanup：成功。
- `packageManager=bun@1.2.15` 与宿主锁定 Bun 版本一致；归档不携带 `node_modules`，9 个 committed
  workspace package 根据归档 manifest 离线物化到临时 resolver tree，不运行 install、不联网。
- `TEMP`、`TMP`、`TMPDIR` 均指向归档内部，并随 cleanroom 删除。

cleanroom 中三条命令：

1. Python compile：通过，3,932 ms。
2. `tests/unit/test_m1_hardening_integration.py`：13 passed，932 ms。
3. `tests/integration/test_m1_hardening_main_path.py`：8 passed，168,751 ms。

旧 foundation 在 Git archive 中没有 `.git`，因此不能重复运行 `git diff` 行数 gate。生产策略现允许非最终
审计显式关闭该 gate，但 `final_completion=True` 禁止关闭；精确 commit 行数由下节独立执行，没有伪造 Git
状态或移除其它 foundation gate。

## 有效行数分桶

slice 区间：`8065bac...e92392fec4d05bfca3..e7105fd...f0353b73744ece0`。

| 桶 | 行数 | 计入最低线 |
|---|---:|---|
| raw additions | 10,707 | 否 |
| production raw | 10,050 | 否 |
| tests | 544 | 否 |
| docs | 113 | 否 |
| blank | 581 | 否 |
| import | 222 | 否 |
| Protocol | 12 | 否 |
| schema/DTO header + fields | 540 | 否 |
| signature continuation | 633 | 否 |
| comment/docstring/literal/pass-only | 46 | 否 |
| generated/data/vendor/source-pool/adapter-only/mock-only | 0 | 否 |
| **conservative effective production** | **8,016** | **是** |

父级区间 `44da53a...6313..e7105fd...44ece0` 为 17,201 有效 production；两个行数 gate 均为
passed、0 finding。行数达标不抵扣上述行为/外部证据 blocker。

## 批判式缺陷发现与修复

1. recovery fault label 与 planner classifier 不一致；把 `worker_unavailable` 接到 `worker_lost`，并修正
   worker/backend route projection 的错误 fallback，恢复 reroute 才能观察真实 owner。
2. bundled skills 最初仍从安装位置读取；改为先物化到 workspace-owned `.zyra/skills/zyra-bundled`，并把
   `permit_id` 传入通用端点。
3. MCP 场景最初只有 capability call；补入真实 task mutation，避免固定 contract 代替主路径。
4. subagent 场景最初没有完整 successor handoff；补入真实 fanout items、typed fault、successor registration
   和 `/recovery/fault-handoff`。
5. integration artifact 在持久化后追加自身路径，导致 API digest 与磁盘不一致；改为计算 digest 前加入
   deterministic path。
6. cleanroom scanner 初版扫描文档/vendor/test 和自身 regex literal；改为生产范围、Python AST runtime
   operation 与真实 path/process argument 检测。
7. Git archive 不含 `node_modules`；初版使用宿主 PATH 且 workspace alias 无法解析。现版本核验锁定 Bun，
   从 committed workspace manifest 离线物化 resolver tree。
8. retrieval/curator TypeScript ports 没有读取统一 `ZYRA_BUN_EXECUTABLE`；已与 E02/CodeWorker 选择规则对齐。
9. cleanroom 最初继承宿主 TEMP，可能受并发窗口残留影响；现把全部临时状态收进归档树。
10. `--no-line-audit` 最初仍被 policy 判为 missing required gate；现非最终显式关闭时同步裁剪策略，最终模式
    仍强制 line gate。
11. 自审发现 17-probe 注册表没有被六场景完整映射，且本轮测试没有执行 disconnect；因此主动阻断 slice
    完成，而没有用 catalog validation 或 cleanroom 通过替代动态矩阵。

## 验证与相邻回归

- exact cleanroom：compile passed；13 unit passed；8 integration passed；receipt status `passed`。
- normal worktree 定向单测：retrieval/curator/cleanroom 25 passed；后续 cleanroom/policy 增量 13 passed。
- recovery + E02 adjacent：13 passed，6 subtests passed，98.97 秒。
- slice line audit：8,016/7,000；父级：17,201/16,000。
- `compileall` 与 `git diff --check` 通过。

本轮早期相邻探测还复现两个不属于当前 owner diff 的既有失败：worker-pool fanout 的第二 child 出现
`retrieval_context_prepare_failed`，相同失败可从 baseline archive 复现；旧 CodeWorker session foundation
collection 缺少 `build_productized_claude_runtime_contracts` import，相同问题也存在于 baseline。它们没有被
fallback 隐藏，但 M1 最终退出前仍须由相应 owner 收口。

未执行全仓无差别测试：本轮已经执行数字阶段要求的完整 cleanroom、当前主路径、恢复/E02 相邻回归与
行数聚合；与当前 diff 无关的长耗时全仓矩阵不能替代仍缺失的正式 live/disable evidence。

## M2 handoff 与权威状态

代码已提供 M2 handoff surface/source-chain/state-custody contract 和原子 store，但 gate 必须携带上述
blocker；M2 不得把 blocked handoff 当成 M1 已关闭。

根目录 `G:/agent-zoo/docs/milestones/execution-state.yaml` 不属于 Zyra Git。本轮不修改它：

- `completed_through` 保持 `M1-S08-01`；
- `next_slice` 保持 `slice-08-02-main-path-hardening-integration.md`；
- 08-02 不进入 protected range；
- M1/M1-08 不关闭。

后续必须从 08-02 原位继续：先回补并执行 17-owner 完整断开矩阵，再接入正式 sealed 1,000/2,000 run、
三层真实 endpoint、双 provider/wire/model、低熵对照和两个跨领域 live task；只有全部 evidence admission 与
exit policy 通过，才能更新权威 YAML。
