# M1-S03B-01 MCP client runtime foundation review

- Slice: `M1-S03B-01`
- Parent: `M1-03B`（父级尚未完成，下一切片仍为 `M1-S03B-02`）
- Base commit: `17aaacfb5695727768b91b6bbe5ef95df689d651`
- Implementation commit: `786729b2ba955177b9371c23dc130857f5d70d0f`
- Review date: `2026-07-11`
- Disposition: **slice complete; parent in progress**

## 1. 结论

本切片完成了 Zyra-owned MCP client runtime foundation。核心实现已拆入
`packages/integrations/zyra_integrations/mcp/**`，并进入既有 02C
`ToolRegistryRuntime` / `ToolExecutionRuntime`、03A `ToolPermissionRuntime`、
02D compact restore、CodeWorker 和 `apps/api` 主路径。实现不依赖 MCP SDK、
sidecar、Docker、外部服务或根目录来源仓库。

三个必须同时成立的链路均已形成真实行为：

1. config/connection：来源优先级、project approval、dedupe、disabled、
   needs-auth、failed、reconnecting、list-changed、stale cleanup、重启恢复；
2. capability projection：tools/resources/prompts 分页发现，投影进统一 tool
   registry，工具调用经过 03A exact one-shot grant，大 structured/binary 结果
   进入 artifact；
3. auth/control/restore：OAuth/XAA、refresh、needs-auth cache、DPAPI credential
   custody、elicitation、default-deny sampling、long task poll/cancel/reconnect、
   instructions delta 与 compact 后恢复。

最终门禁为：全仓 `552/552` tests 通过；MCP 单测 `111/111`；MCP live 集成
`6/6`；第三轮独立对抗复审 `12/12 PASS`、`0` blocking findings；严格 ledger、
reachability、test-quality、evidence-graph、boundary、cleanroom 和 unit-review 均为
`0` error / `0` blocker。

## 2. 执行单元目标覆盖矩阵

| 验收项 | 状态 | 证据 | 验证 | 阻断 |
| --- | --- | --- | --- | --- |
| MCP config provenance、precedence、dedupe、project approval、disabled/stale cleanup | 完成 | `mcp/config.py`、`mcp/store.py` | `test_mcp_config_store.py` | 否 |
| connection lifecycle、health、failed/needs-auth/reconnecting/terminal failure | 完成 | `mcp/connection.py`、`mcp/transport.py` | `test_mcp_transport_protocol.py`、`test_mcp_adversarial_lifecycle.py` | 否 |
| 严格 initialize/version/capability negotiation | 完成 | `mcp/protocol.py`、`mcp/connection.py`、`mcp/transport.py` | 未知版本拒绝、negotiated HTTP header、动态 sampling capability 测试 | 否 |
| tools/resources/prompts 分页发现与 list-changed | 完成 | `mcp/capabilities.py`、`mcp/connection.py` | capability、HTTP integration、fake server smoke | 否 |
| MCP tool 投影到 02C registry/executor | 完成 | `mcp/projection.py`、`zyra_runtime/tools.py`、`executor.py` | CodeWorker permission integration | 否 |
| 所有动态/MCP 工具通过 03A permission | 完成 | registry-owned `DynamicToolProvenance`、exact one-shot grant | metadata 篡改、错误 grant、解绑 handler、grant replay 测试 | 否 |
| large structured/binary 输出预算与 artifact 外置 | 完成 | `mcp/output.py`、`LocalArtifactStore` | `test_mcp_capabilities_output_tasks.py` | 否 |
| OAuth/XAA/refresh/revoke/needs-auth 与独立 credential custody | 完成 | `mcp/auth.py`、`mcp/credentials.py` | auth/sampling 与 credential protection 测试 | 否 |
| elicitation 原请求 resolve/cancel/timeout | 完成 | `mcp/elicitation.py`、connection request handler、MCP API | 双 waiter、精确 idempotency、HTTP resolve 测试 | 否 |
| sampling default-deny、cap、timeout/cancel | 完成 | `mcp/sampling.py` | `test_mcp_auth_sampling.py` | 否 |
| long-running task poll/result/cancel/reconnect | 完成 | `mcp/tasks.py`、dynamic transport resolver | transport replacement/no-replay 对抗测试 | 否 |
| instructions delta 与 compact restore | 完成 | `mcp/instructions.py`、`code_worker_runtime.py` | live API/CodeWorker compact restore | 否 |
| API 与 `/mcp` diagnostics/control | 完成 | `mcp_api.py`、`main.py` | facade、real HTTP、control command tests | 否 |
| fake MCP server smoke 能真实调用工具 | 完成 | `scripts/smoke_mcp_runtime.py` | smoke 输出 `remote_tool_calls=1`，grant replay 拒绝 | 否 |
| 每个父级必裁决来源项有 disposition/target/test/main path | 完成 | `mcp/source_audit.py`、ledger seed | 62/62 aligned，9 source-audit tests | 否 |
| 最低 8,000 行生产内化代码 | 完成 | 20,552 production Python additions；保守核心 18,147 | 三类 numstat 与 buckets gate | 否 |
| vendor/source-pool 失败线 | 完成 | `vendor/**`、`vendor-runtimes/**` diff 为 0 | numstat、submission boundary | 否 |
| 干净目录、动态可达、断开即失败、语义效果 | 完成 | physical archive、API/worker/tool/event/artifact 路径、12 项对抗测试 | cleanroom 102 unit + 6 integration + smoke + boundary | 否 |

父级 `M1-03B` 的 16,000 行最低线在本切片的保守生产口径下已超过，但父级仍不
标完成；`M1-S03B-02` 的集成目标和验收仍必须独立执行。

## 3. 主路径与状态归属

| 能力 | Zyra owner | 真实入口 | 状态 / event / artifact 接点 | 断开效果 |
| --- | --- | --- | --- | --- |
| 配置与审批 | `McpConfigStore` + `McpRuntimeStateStore` | API add/approve/disable、runtime config load | atomic revision/CAS/journal；`MCP_CONFIG_CHANGED` | 无配置或未审批 server 无法连接 |
| 连接与协议 | `McpConnectionRuntime` | API connect/reconnect、worker runtime | durable snapshot；`MCP_CONNECTION_CHANGED` | terminal failure 令 health=false 且撤 catalog |
| capability catalog | `McpCapabilityCatalog` | initialize、list/list-changed | catalog generation/digest；`MCP_CAPABILITIES_CHANGED` | reconnect failure 后 worker 不再看到旧工具 |
| 工具执行 | `McpToolProjectionRuntime` + 02C executor | CodeWorker 与 `/tools` | exact provenance/grant；`MCP_TOOL_RESULT`；artifact refs | 无 grant、错 grant、replay 均为零远端调用 |
| auth/credential | `McpAuthRuntime` + `FileCredentialVault` | MCP auth API、connect needs-auth | public state 仅 opaque ref/digest；`MCP_AUTH_CHANGED` | 无安全 codec 或无 token 时 fail closed |
| elicitation | `McpElicitationQueue` | server `elicitation/create` + API resolve | durable revision/idempotency fingerprint；`MCP_ELICITATION` | collision 为 409，新 waiter 保持 pending |
| sampling | `McpSamplingRuntime` | server `sampling/createMessage` | cap/timeout/audit；default deny | 无 callback/policy 不 advertise 且不执行 |
| long task | `McpTaskLifecycleRuntime` | task-returning tool call | task state/progress；`MCP_TASK_UPDATED` | reconnect 后切换 transport，原 call 不重放 |
| context restore | `McpInstructionsRuntime` + CodeWorker session | worker projection / compact restore | `runtime_state["mcp_runtime"]`；`MCP_INSTRUCTIONS_CHANGED` | 断开模块会改变恢复后 context |
| HTTP control | `McpApiFacade` + 03A custody | `/mcp/**` | Permission state/event + MCP state/event | 无 custody/ASK 未批准/重用 grant 均拒绝 |

Credential material 不进入 MCP state、event、API body 或 CodeWorker snapshot。Windows
默认使用 current-user DPAPI 并绑定 credential reference entropy；非 Windows 未注入
authenticated codec 时，仅无凭据 runtime 可启动，首次凭据读写 fail closed。

## 4. 来源内化账本

精确的 62 条逐文件裁决在 `mcp/source_audit.py`，并由
`scripts/sync_mcp_source_ledger.py` 同步为 `owner_unit=M1-03B` 的 ledger 条目。结果：

| 来源 | 条目数 | 主要迁移机制 | Zyra 目标 |
| --- | ---: | --- | --- |
| `claude-code-best` | 43 | config/client/auth/transport/tool/resource/output/instructions/elicitation/commands | `mcp/**`、API、CodeWorker、02C/03A |
| `agent-framework` | 2 | lifecycle owner queue、pagination、sampling guard、long task；skills contract | connection/capabilities/tasks/sampling；skills runtime 留 03C |
| `opencode` | 8 | transport/catalog/auth/session tool projection/config | transport/capabilities/auth/credentials/projection/config |
| `agentscope` | 4 | config/tool adapter contract；gateway 只作边界参考 | config/models/projection；gateway 不进入 runtime |
| `hermes-agent` | 5 | lifecycle/OAuth/401 dedupe/elicitation；拒绝 default-allow sampling | connection/auth/credentials/elicitation/sampling |

Disposition：`active=24`、`adapter=18`、`contract-only=7`、`reference-only=6`、
`deferred=7`，`issues=0`。active/adapter 才声明 runtime ownership；contract/reference
不用于关闭行为门禁。主要 deferred 项有明确后继 owner：interactive MCP UI/editor/browser
adapter 属 `M2-04A`，可运行 MCP skills 属 `M1-03C`，gateway/server-direction 能力不属于
本 client foundation。Claude `src/skills/mcpSkills.ts` 是 generated/no-op stub，保持
reference-only，未被伪标 active。

迁移策略是机制拆解和 Zyra 语义重建，不保留上游目录、入口、依赖图或状态模型。删除
`zyra_integrations.mcp` 会直接破坏 API、CodeWorker、tool permission、compact restore
和对应 live integration；删除来源仓库不会影响运行。

## 5. 对抗审查

首轮独立复审发现 12 个真实阻断；全部修复后第三轮逐项复现为 `12/12 PASS`：

| 原阻断 | 修复与复现证据 | 最终 |
| --- | --- | --- |
| 可变 ToolSpec metadata 绕过 permission | 深冻结/防御快照 + registry provenance；篡改仍零远端 | PASS |
| config name/canonical server ID 混用 | 双向唯一解析，歧义 fail closed | PASS |
| long task reconnect 持旧 transport | 每次 poll/result/cancel 动态解析 current transport | PASS |
| elicitation API 不回原 server request / idempotency 碰撞 | Condition waiter + bounded timeout + server/session/request/revision/action/content/actor fingerprint | PASS |
| Streamable HTTP server request 写锁死锁 | 每个 JSON-RPC message 独立并发 POST，不持锁消费整段 SSE | PASS |
| 接受未知协议版本/HTTP header 不更新 | supported allowlist + negotiated header | PASS |
| 宣称未实现 roots/draft elicitation/sampling | capabilities 从已安装 handler 动态生成 | PASS |
| credential vault 为 base64 明文 | Windows DPAPI；tamper/authentication 测试；其它平台 fail closed | PASS |
| 重启把失败状态变成空健康 | FAILED 保留；旧 CONNECTED 恢复为 RECONNECTING/live=false | PASS |
| API 可绕过权限启动进程/访问 URL，stdio/PATH 与 HTTP DNS rebind | 03A custody + exact one-shot；绝对 executable + 空 args/env；精确公网 IP-literal URL，无 DNS/redirect | PASS |
| terminal failure 不影响 connection health | terminal error 异步移交 owner，FAILED + health=false | PASS |
| reconnect failure 仍投影旧 catalog | 开始 reconnect/failed/needs-auth/terminal 即撤 catalog | PASS |

第三轮审查另独立运行 `117` 个 MCP tests 与 `112` 个 permission regressions，均通过，
blocking findings 为 `0`。

## 6. 测试与审计证据

开发工作区：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
# 552 tests, 498.942s, OK

.\.venv\Scripts\python.exe -m unittest discover -s tests\unit -p "test_mcp*.py" -q
# 111 tests, OK

.\.venv\Scripts\python.exe -m unittest \
  tests.integration.test_mcp_codeworker_permission_integration \
  tests.integration.test_mcp_api_control_restore -q
# 6 tests, OK

.\.venv\Scripts\python.exe scripts\smoke_mcp_runtime.py
# ok=true, remote_tool_calls=1, grant_replay=permission_required

.\.venv\Scripts\python.exe -m unittest tests.unit.test_mcp_source_audit -q
# 9 tests, OK; 62/62 source decisions exist in the review workspace
```

全仓测试出现的 Windows asyncio/browser pipe `ResourceWarning` 不改变退出码或断言，来自
既有 browser integration 的清理日志；本切片未修改该路径，记录为非阻断的 M1-04/M3
process-audit 观察项。

审计：

```powershell
python scripts/sync_mcp_source_ledger.py --check
# aligned=true, decisions=62, owner_groups_changed=0

python scripts/verify_internalization_ledger.py \
  --base 17aaacfb5695727768b91b6bbe5ef95df689d651 --cached \
  --unit M1-03B --minimum-effective-lines 8000 --fail-on-shortfall
# 使用 bundled seed；0 errors, 0 blockers, line_count_ok=true

python scripts/verify_submission_boundary.py
# passed

python scripts/zyra_integration_ledger.py --ledger-path \
  packages/integrations/zyra_integrations/data/internalization_ledger_seed.json \
  unit-review --owner-unit M1-03B \
  --base 17aaacfb5695727768b91b6bbe5ef95df689d651 \
  --minimum-effective-lines 8000 --fail-on-error
# audit/line buckets/schema/evidence/custody/mutation/boundary/reachability/
# cleanroom/semantic-effects/test-quality/acceptance 全部 passing
```

### Physical cleanroom

从 implementation commit 执行 `git archive`，解压到
`tmp/cleanroom-M1-S03B-01-786729b`。归档不含 `.git`、`.venv`、原工作区 `tmp`、
来源仓库、editable link 或 cache；以空 `PYTHONPATH`、`PYTHONNOUSERSITE=1`、
`PYTHONDONTWRITEBYTECODE=1` 运行：

- MCP runtime behavior unit：`102/102`；
- MCP live integration：`6/6`；
- `scripts/smoke_mcp_runtime.py`：passed；
- `scripts/verify_submission_boundary.py`：passed。

`test_mcp_source_audit.py` 刻意检查五个工作区来源仓库并调用开发环境 CLI，因此归入
development source-review evidence，不伪装成 cleanroom runtime test；其余行为测试均不读取
根目录来源仓库。

## 7. 有效行数分桶

依据 `git diff --numstat 17aaacfb... HEAD`：

| 桶 | 新增 | 删除 | 是否计入 8,000 行最低线 |
| --- | ---: | ---: | --- |
| production Python（`apps/**` + `packages/**`，排除 data/docs） | 20,552 | 42 | 是 |
| 保守 MCP operational（排除 models/source-audit/init） | 15,945 | - | 是 |
| 保守主路径集成（API/02C/03A/worker） | 2,202 | - | 是 |
| 保守核心合计 | 18,147 | - | 是 |
| tests | 6,409 | 0 | 否 |
| ordinary scripts | 632 | 0 | 否 |
| ledger data/seed | 6,280 | 1,805 | 否 |
| docs/runtime README | 51 | 0 | 否 |
| vendor / vendor-runtimes | 0 | 0 | 失败线为零，已满足 |
| commit raw | 33,923 | 1,847 | 仅审计，不作为有效口径 |

`verify_internalization_ledger` 的 broad effective count 为 `27,592`，其中包含测试/脚本，
故不用于证明最低线；完成判定采用上表 production 与保守核心口径。

## 8. 赛题证据校准

本切片推进但不关闭：

- `REQ-CLOSE-01`：增加真实 MCP tool/control/permission/recovery step 能力；尚无 sealed
  2,000-transition live run，不能关闭；
- `REQ-TRACE-01`：增加 config/connection/auth/elicitation/task/tool/instructions 的 canonical
  event、permission decision 与 artifact 因果接点；UI 消费证据仍由 M2/M3 owner；
- `REQ-MEM-01`：instructions delta 在 compact 后恢复并真实影响后续 CodeWorker context；
  分布式检索/唤醒完整门禁仍属 06A-C/07C/08。

本切片没有关闭任何 100 分赛题门禁，也不以代码行数、ledger 或 fake server 替代 live
场景、多模型、真实端边云、2,000 transitions、异常恢复或 UI 因果轨迹。

## 9. 已知限制与后继 owner

以下均不阻断本 foundation，但不得被误报为父级或阶段完成：

1. `M1-S03B-02` 仍需完成父级后续 integration 验收；
2. interactive MCP panel/editor/browser adapter 属 `M2-04A`；
3. runnable MCP skill loading 属 `M1-03C`，不能使用 Claude no-op stub 关闭；
4. ordinary HTTP facade 的 dynamic stdio 只允许绝对 executable 且 args/env 为空；dynamic
   HTTP 只允许精确公网 IP-literal URL。域名 endpoint 需由受信配置源或后续 pinned connector
   提供，不能通过 permission 扩大 deployment policy；
5. 非 Windows credential persistence 需要显式注入 authenticated OS/KMS codec；默认拒绝
   凭据读写，不做可逆编码 fallback；
6. ledger/evidence graph 的 shared-target warnings 表示多个来源机制被综合进同一 Zyra-owned
   模块，不是多个 runtime owner；精确来源和替代边界已在 62 条 decision 中记录。

本次没有发现仍影响切片目标的未完成项；阻断数为 `0`。
