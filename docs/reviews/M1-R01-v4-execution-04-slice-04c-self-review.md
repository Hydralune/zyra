# M1-R01 E04-C 增量批判式自审

## 结论

`E04-C Permission + MCP Source Recovery` 在实现提交
`102761e6264849e27d056931db6b1bde4c3dfcc2` 上通过增量验收。本结论只关闭
04C 的 permission 与 MCP 两个能力域，不完成 E04，不恢复 `M1-S05C-01`，也不
替代 E04 专用独立终审。

## 上游源码恢复与 Zyra 接管

- `e04-source-008`：从 `claude-code-best` 的 `handleCoordinatorPermission` 裁剪
  自动检查顺序。Zyra 的本地 hooks 先执行；只有 hooks 未决时才调用 advisory
  classifier；交互式 ASK 由 durable continuation 继续处理。终端 UI 提示和全局
  hook 状态被删除。
- `e04-source-009`：把 `checkRuleBasedPermissions` 的 deny/ask/allow 优先级接入
  `PermissionEvaluator`、canonical identity、standing grant 与 policy revision。
  修复显式 hook `allow` 被后续默认策略覆盖的缺口。
- `e04-source-010`：把 `connectToServer` 的 policy -> transport -> initialize ->
  initialized notification -> ready 顺序接入 `McpConnectionRuntime`。关闭或失败后的
  再连接会重建 transport、递增 owner epoch 并重新绑定监听，避免 owner epoch 与
  物理 transport epoch 脱节。
- `e04-source-011`：把 `ensureConnectedClient` 接入 `McpClientRuntime`。catalog 和
  capability 请求必须持有 live initialized client；连接被清空时通过 canonical
  connection owner 重建，失败时返回 typed terminal failure，不创建逻辑 client。

逐 range 的 source fingerprint、候选 target fingerprint、裁剪分支与实现提交见
`docs/reviews/evidence/M1-R01-v4/execution-04/slice-04c/target-provenance-report.jsonl`。
G0 不可变清单未改写。

## 默认路径、状态 owner 与反回退

- permission 默认闭包为 `CodeWorkerApplication.runCapabilityApiPort ->`
  `E02RuntimeCoordinator -> PermissionEvaluator -> PermissionHookRuntime`；状态由
  permission decision/journal/continuation store 持久化。
- MCP 默认闭包为 `runCapabilityApiPort -> E02RuntimeCoordinator ->`
  `McpClientRuntime -> McpConnectionRuntime`；连接、catalog、request journal 和
  effect receipt 均使用 Zyra 的 TypeScript owner。
- 旧 E01 permission 对抗测试不再传入伪造的 capability stub，而是实际打开和关闭
  `TypeScriptCapabilityRuntime`；此前分配给 04C 的 3 个回归已经关闭。
- `ZYRA_DISABLE_E04_PERMISSION_SOURCE_RUNTIME` 与
  `ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME` 会杀死真实默认能力路径；无 Python permission
  或 MCP 逻辑决策回退。

## 对抗、失败与变异结果

- permission 覆盖 allow/ask/deny、hook failure、审批续接、source disable；MCP 覆盖
  lifecycle、connect failure、resume、cleared-client reconnect、source disable。
- MCP 还通过真实本地 stdio child、loopback HTTP/SSE、旧 Node client 和 Python API
  cutover 回归，证明不是 fixture-only 或静态 catalog。
- `e04-mutation-domain-04/05` 修改后均通过 typecheck，但精确 killer
  `e04-permission-disable`、`e04-mcp-disable` 失败；恢复后通过并回到记录的 SHA-256，
  无 backup 残留。
- 若关闭/失败后复用旧 transport 而不递增 owner epoch，新增重连断言失败；若 catalog
  或 capability 调用绕过 live-client guard，cleared-client 测试失败。

## 有效行数分桶

- production TypeScript：新增 116 行、删除 34 行；承担 permission 顺序与 effect、
  MCP live-client guard、transport rebuild、epoch 和监听责任。
- test：新增 585 行、删除 62 行；其中新增 04C 行为测试 516 行，旧 E01 fixture 改为
  真实 runtime 69/62 行。
- validation tooling：新增 22 行；只承担可逆 mutation operator，不计入产品运行时。
- generated、data-as-code、vendor-like/source-pool、adapter-only、mock-only：0 行；
  测试内 in-process transport 仅作 typed fixture，不计有效 production。

## 验证与未关闭事项

typecheck、61 个 runtime/API/permission/对抗测试、11 个 MCP custody/live transport
测试、1 个 Python API cutover 集成测试和 6 个 Node MCP client 测试均通过。G0 复核
仍为 18 个 source range、18 个 target、1619 个 Python owner symbol、13 个 mutation
point，且零异常。

skill/plugin/command、agent/subagent、isolation/control 尚由 04D/04E 关闭；八域 E2E、
全 13 mutation、双发行构建、cleanroom、依赖审计、有效行数累计审计与 candidate gate
仍由 04F 强制执行。E04 terminal verdict 只能由专用独立审查任务书在最终 target
commit 上给出。
