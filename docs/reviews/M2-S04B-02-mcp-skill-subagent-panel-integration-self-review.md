# M2-S04B-02 MCP / Skill / Subagent Panel Integration 自审

## 1. 冻结边界与结论

- slice baseline：`2d3d8c16075f1dda298831578cd339478f63afad`
- preimplementation decision：`d17bcdee550db9742abe975672a7a58c12675bb4`
- initial implementation：`d153c0e48f417fe8c073fdac7429d643508265f4`
- first real-owner fix：`e8e7d8e49d15a211da27f04e56f380201e01d524`
- authority-fence fix：`48fd5c6e2e16c2dedf5be608512148002aa9da54`
- staged skill-supply fix / final source target：`21b1165df4fbb941391d97f4aac304c13c7e312e`
- slice、M2-04B 父级、M2-04 数字阶段结论：通过

初版实现完成三个 panel 后，独立复审发现 mutation 仍可停留在测试 transport、测试可直接写入
canonical projection、direct slash 的 sealed mode 未绑定任务状态、elicitation secret 进入原始命令文本。
第一轮问题在 `e8e7d8e...` 内关闭。第二轮复审继续发现 API sealed 仍可被 payload
覆盖、transient secret 仍可能被 retained request/retry 持有、skill supply admission 未绑定
同一 staged snapshot，以及 subagent physical attempt/lease/nonce/idempotency fence 仍不够严格。
这些问题分别在 `48fd5c6...` 与 `21b1165...` 内关闭，并重跑 source-tree 与 exact-commit
cleanroom 验证。最终实现中：

- panel/controller 只消费 `CanonicalProjectionStore`，不拥有 MCP、skill 或 subagent truth；
- mutation 进入 Python API / command owner，再进入 TypeScript E02/E03 owner；
- receipt 只证明接收，update/invoke/steer/kill 等效果必须由后续 canonical owner state 结算；
- direct slash、API 与 panel control 将 canonical task sealed 与调用输入合并；调用输入只能升级、
  不能把 canonical sealed 降级，且拒绝在 owner mutation 前完成；
- elicitation secret 只存在于 transient structured `argumentOverrides`，不进入命令文本、argv、history、
  palette、receipt content、panel projection、retained request 或 retry；
- skill supply admission 对 staged proposed descriptors 作 owner 审批，并只提交完全相同的 scan id/digest；
- subagent control 同时绑定 logical attempt、physical attempt/lease、worker binding、nonce 与 idempotency。

## 2. 来源裁决与内化落位

| 角色 | 来源 | 迁移方式 | Zyra 落位 |
| --- | --- | --- | --- |
| primary | `claude-code-best` | TypeScript/TSX retained-control-flow adapt、cropped migration、same-language integration | `apps/web/src/features/{mcp,skills,subagents}/**`、`packages/commands/**`、E02 MCP/Skill command owner |
| supplementary | `opencode` | bounded TypeScript catalog/hierarchy integration | MCP/skill catalog、subagent hierarchy |
| supplementary | `hermes-agent` | 预裁决的 bounded Python-to-TypeScript semantic port | MCP auth presence/redaction、skill provenance/hash/supply-chain |
| conformance | Agent Framework、AgentScope | tests only | approval/history、worker lifecycle |
| reference | browser-use、Oh My Pi | tests/review only | close/restore/disable、agent/task drill-down |

每个状态域只有一个 production owner。OpenCode/Hermes 没有取得 session、MCP、skill、subagent、
permission 或 credential custody；API、command handler 与 WorkerPool 是 Zyra-owned integration。
不存在父目录来源仓库、npm link、editable path 或外部 source process 依赖。OpenClaw 没有 ledger entry、
源码读取或运行路径，保持 `excluded_forward_only`。

## 3. 真实主路径与状态责任

### MCP

Web admission 严格绑定 task/run/session/server/owner/revision；auth projection 只保留 presence/expiry；
catalog cursor 绑定 canonical capability revision；elicitation 验证 schema 与 request identity。`/mcp`
enable/disable/reconnect/refresh/auth-refresh/elicit 由 E02 coordinator 调用真实 MCP connection/client owner。
真实 stdio MCP server 行为测试证明 catalog、重连、auth、elicitation 与 disconnect，而不是固定 contract。

### Skill

Web 侧承担 Markdown/resource/tool-scope、hash/provenance、dependency graph、supply-chain admission 与 staged
approval。`/skills update` 和 `/skills invoke` 经 permission operation 进入真实 `SkillCoordinator`；
update 先对 `loadSkillsFromSkillsDir(..., commit: false)` 的 proposed descriptors 作 owner admission，再提交
同一 scan id；磁盘在 admission 回调内变化时不会把未批准的新 snapshot 混入 commit。update 只有
registry revision/hash 改变后结算，invoke 只有 exact-bound owner invocation 出现后结算。

### Subagent

`/tasks` 是 E03 `SubagentTaskStore` 的 canonical hierarchy/lifecycle projection。`/agents steer/kill`
校验 child/logical attempt/physical attempt/lease/worker binding/parent/owner/revision/nonce/idempotency，
并通过一次性 owner grant 进入新鲜 E02 query identity；E03 仍持有原 parent-session owner。exact
binding 在 lost-ack lookup 之前验证，kill 的 physical lease fence 阻止 late result，exact replay
只返回既有结果，nonce 与 idempotency 均不能重绑到另一个 control request。control query 禁用
retrieval、checkpoint restore、permission queue 与 background drain，避免旧 checkpoint、隐式审批
或异步 drain 取得第二控制路径。

### UI 与 sealed

`WorkbenchRuntime` 持有三个 disposable controller，并从 canonical selected task 派生 sealed mode；
permission/task 变化会同步所有 panel。`/mcp`、`/skills`、`/agents`、`/tasks` 是 hybrid commands：
只读在 sealed mode 可用，mutation 确定性拒绝并在 PermissionConsole 记录一次 attempt、零 mutation、
零 human intervention。overlay 只在目标 workbench 真正挂载后 focus，不制造成功状态。

## 4. 动态可达性、失败路径与断开即失败

- Web panel → command runtime → API → Python command owner → TypeScript E02/E03 owner → canonical projection
  是测试覆盖的真实路径。
- E02 focused owner tests 使用真实 stdio MCP server 与真实 SkillCoordinator；API cutover 测试执行真实
  `/skills update`，并验证 sealed `/mcp disable` 在 owner 前拒绝。
- WorkerPool API 全套验证真实 OMP admission、physical lease、fanout、parent cancel、steer/kill、
  exact replay、sealed read/mutation boundary。
- controller、canonical store、MCP connection owner、skill owner、OMP gate、lease store 或 execution gate
  被 disable 时，对应行为显式失败；没有 test JSON rewrite、localStorage、direct fetch 或备用 store 接管。
- secret-bearing projection 被拒绝；OMP checksum failure 同时给出 expected/observed digest，便于因果审计。

## 5. 有效行数分桶

精确逐文件分桶：

- `docs/reviews/evidence/M2-S04B-02/effective-lines-slice.json`
- `docs/reviews/evidence/M2-S04B-02/effective-lines-parent.json`
- `docs/reviews/evidence/M2-S04B-02/effective-lines-stage.json`

| 区间 | raw additions | production runtime | UI behavior | presentation | types | schema/data | adapter-only | tests | effective | 门禁 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| M2-S04B-02 | 22,826 | 14,861 | 450 | 1,196 | 2,167 | 316 | 159 | 3,184 | **15,311** | 8,000 |
| M2-04B | 37,354 | 23,484 | 529 | 1,622 | 4,081 | 2,315 | 171 | 3,831 | **24,013** | 16,000 |
| M2-04 | 71,097 | 39,061 | 1,153 | 2,867 | 6,510 | 10,802 | 722 | 6,687 | **40,214** | 31,000 |

tests、fixtures、Markdown、JSX/static presentation、interfaces/types、command descriptor/data、ledger、
generated、adapter-only 与 comments/blanks 均为零 credit；vendor-like/source-pool 为 0。slice 分组：
Claude primary raw 16,108 / effective 12,545；OpenCode supplementary 1,223 / 1,016；Hermes bounded
semantic port 1,100 / 1,000；Zyra owner integration 994 / 750。Python production 使用 AST/token
分桶，不再被误归为 generated。大文件触发列表逐文件保存在 audit JSON。

## 6. 验证

### Source tree

- focused real E02 command owner：3/3；
- commands：29/29，137 assertions；
- focused MCP/skill/subagent workbench：24/24，167 assertions；
- focused skill staged admission/custody：24/24；
- focused E03 routing/lease：41/41；
- Python E02 API cutover：1/1；
- Python WorkerPool API：8/8；
- Python 邻接：24 pass + 16 subtests；permission console API：2/2；
- root TypeScript typecheck、Web typecheck/build、`git diff --check`：通过；
- ledger：7 entries、missing target 0，`--write`/`--check` aligned。

### Exact final-source cleanroom

从 `21b1165...` 创建 detached worktree 到
`G:/agent-zoo/.tmp/m2-s04b02-cleanroom-21b1165-20260725`，执行 frozen lockfile install：

- Web + commands + MCP + Claude runtime：78 files，1,546/1,546，3,190 assertions；
- 全仓 TypeScript typecheck 与 Web production build：通过；
- Python E02 API cutover：1/1；
- Python WorkerPool API：8/8（真实 stdio/OMP/physical-worker path）；
- manifest root/parent runtime dependency matches：0。

外部 `.venv` 仅作为测试解释器，不提供运行时源码；`PYTHONPATH` 显式指向 cleanroom package roots，
pytest 使用 cleanroom-local `--basetemp`。cleanroom worktree 保持 clean。

### 已知非门禁残余

宽 legacy `tests/integration/test_api_control_commands.py` 在最终 source tree 为 9 pass / 16 fail，
不计为通过。失败主要来自已切换 E02 canonical route 后仍调用旧 direct `/tools`/inventory contract，
以及已移除的 `build_productized_claude_runtime_contracts`；其中 `/help` HTTP 409 已在数字阶段 baseline
复现。当前 slice 的新增 API mutation/read paths 由 focused E02 cutover 与 WorkerPool suite 全绿覆盖。
该 legacy suite 需要后续按 canonical E02 contract 迁移，不能用它否定或伪装本 slice 的真实 owner tests。

## 7. 状态、事件与交付边界

- frontend truth：`CanonicalProjectionStore`
- command/permission：`CommandSurfaceRuntime` + backend permission runtime
- MCP/Skill owner：TypeScript E02 runtimes
- Subagent owner：E03 `SubagentTaskStore` / control protocol / physical lease store
- Python API/commands/workers：Zyra-owned admission、bridge、dispatch 与 projection
- panel local state：selection/query/page/expanded/in-flight view operation，不持久化 canonical truth
- causal keys：task/run/session/owner/revision/attempt/lease/nonce/idempotency/request/receipt/event/checkpoint
- root source runtime dependency：0

根目录 `docs/milestones/execution-state.yaml` 不属于 Zyra Git；只在最终 evidence commit 后单独更新。

## 8. 赛题证据触达

- `REQ-CLOSE-01`：viewer close/detach 不停止 owner，reopen 从 canonical truth 恢复。
- `REQ-MEM-01`：skill resource/provenance 与 M2-04B-01 context/memory panel 共用 selector truth。
- `REQ-TRACE-01`：MCP、skill、subagent receipt/event/checkpoint/result 可回溯到 owner 和 physical attempt。
- `SCORE-COMPAT`：typed MCP tools/resources/prompts/auth/elicitation 与 skill/subagent lifecycle。
- `SCORE-UX`：command-to-panel handoff、分页、禁用原因、reconnect、sealed warning、lifecycle receipt。
