# FE-G00B 终端生命周期与物理动作主路径收口：增量自审

## 结论

FE-G00B 在 `237bc123f45b0e2326edd1512539c6fce7146201` 基线上完成，验收结论为
**PASS**。本片只收口 FE-S04 激活复核发现的两个服务端 contract 差异：revision-fenced
正常 disable，以及 permission-approved typed action 的 BackendRegistry HTTP dispatch。

本片没有创建 TypeScript terminal listener、没有增加 endpoint/frame/schema、没有把整个
CodeWorker callable 序列化到远端，也没有声明 `real_terminal_dispatch_claimed` 或
`real_edge_dispatch_claimed`。FE-S04 仍需用户重新明确授权。

## Owner 与实现自审

| 检查项 | 结果 | 证据 |
|---|---|---|
| definition owner | PASS | disable 仍只经 `POST /backends` 和 `BackendRegistryStore`；expected revision 必填 |
| disable identity | PASS | existing owner、generation、capability digest、endpoint、runtime、kind、location 必须完全一致 |
| drain fence | PASS | `current_leases > 0` 拒绝 disable；成功写入 definition `enabled=false` 和 canonical health `disabled` |
| initialization | PASS | 首次 disabled 继续拒绝；首次 enabled 仍需 listener health identity/capability attestation |
| permission owner | PASS | action port 只接受已消费/允许的 safe permission receipt；policy 决策仍由 TypeScript permission runtime 完成 |
| scheduler/lease owner | PASS | action 通过 `BackendSelectionRequest` 和 `WorkerDispatchRouter.dispatch_payload` 取得 canonical lease/envelope |
| transport owner | PASS | 复用既有 `HttpBackendTransport`、frame/digest、cancel/failover 和 dispatch journal 语义 |
| callable boundary | PASS | `dispatch_callable(worker.run)` 在 router boundary 排除全部 terminal definition；不序列化 Python callable |
| action boundary | PASS | file read/write/edit/delete、search、shell、artifact typed operation 可按 capability 选择 terminal |
| result parity | PASS | Gateway 路径同时返回 backend action receipt 与 `zyra.gateway-execution-receipt.v1`，保留 permission consumption identity |
| secret/root projection | PASS | action metadata 使用 allowlist；capability token 不进入 payload/receipt；用户 receipt 不返回 workspace/artifact root |
| sealed | PASS | sealed/formal task 不安装 action port，port execution-mode fence 对强制调用也 fail closed |
| listener/CLI claim | PASS | 未新增 listener、CLI lifecycle 或真实 terminal claim；这些仍属于 FE-S04 |

## 生命周期收口

`_validate_terminal_registration` 现在区分初始化注册与既有 generation 的关闭更新：

```text
first enabled
  -> loopback/capability/health attestation
  -> revision-fenced register

existing enabled
  -> drain externally
  -> active leases == 0
  -> same owner/generation/token/transport identity
  -> revision-fenced enabled=false
  -> registry health=disabled
```

disable 不要求已停止 accepting 的 listener 再通过 health-ready 检查；否则正常 drain 后无法安全
注销。它也不允许借 disable 变更 endpoint 或接管 generation。stale revision 不自动重试、不覆盖
新 definition。

## 物理动作主路径收口

新增 `BackendRegistryActionDispatchPort`，但它不拥有 permission、workspace 或 placement：

```text
CodeWorker typed ToolCall
  -> existing permission/policy consume
  -> BackendRegistryActionDispatchPort
  -> BackendSelectionRequest (terminal-only eligible set)
  -> WorkerDispatchRouter.dispatch_payload
  -> HttpBackendTransport
  -> terminal action result
  -> backend action receipt + Gateway execution receipt
```

operation 固定为 `tool.<tool_name>`，payload 固定为 `zyra.terminal-action/v1`，返回固定要求
`zyra.terminal-action-result/v1` 且 tool-call identity 不得变化。该 payload 是现有 dispatch
payload 的 typed 内容，不是新增 transport protocol 或 endpoint。

普通 CodeWorker 的完整 `worker.run` 仍在 runtime host 执行。router 对 callable 全局加入
registry-owned terminal ids exclusion，避免远端只收到两字段占位 payload 后被当作 native
`CodeWorkerRun`。只有 typed action lane 可以选 terminal。

## 真实行为、负向与 mutation 证据

1. 真实 `BackendDispatchServiceRuntime` 绑定 loopback HTTP，和 action port 共享真实 SQLite
   BackendRegistry；file/search/shell/artifact actions 获得 terminal lease、envelope 和 transport receipt。
2. API CodeWorker binding 从产品 artifact-root registry 发现 terminal action port，并完成真实 HTTP
   dispatch；没有 registration 或完整 provider route 时不安装 port，保持旧本地路径。
3. SandboxGateway 在原 permission consume 后 dispatch；同一 shell 去掉 delegation port 时真实在
   managed workspace 写入 marker，保留 delegation 前后可观察差异。
4. direct ToolExecutor 的 permission-approved web search 使用真实一次性 TypeScript permit 后 dispatch；
   未授权 action 不会越过原 permission owner。
5. worker callable 即使 preferred backend 指向 terminal 也从未尝试它；action lane 可选择同一 definition。
6. sealed port `available=false`，强制 dispatch 返回 execution-mode exclusion；sealed/recovery 相邻回归通过。
7. 首次 disabled、active lease、stale revision、owner/generation/token mismatch 全部拒绝；drained
   same-generation disable 成功并可从 list/health 观察 canonical disabled 状态。

## 验证记录

- contract + real action tests：`7 passed`（包含 API product binding、HTTP、permission、mutation）。
- terminal/backend/router/failover/sealed/SandboxGateway 相邻回归：最终合并命令 `27 passed`。
- targeted `compileall`：通过。
- `git diff --check`：通过。

一个旧 `test_api_control_commands.py` 用例仍断言 permission-suspended tool plan 的
`query_plan_ok=true`。本片在临时移除全部 API action-port 注入后单独复跑，仍得到
`query_plan_ok=false`，证明该断言是当前基线行为差异而非 FE-G00B 回归；临时对照改动与调试输出
均未保留。其余同文件两个已知旧 endpoint/response 断言也不作为本 Gate 的 owner contract。

## 分桶

| bucket | 增量 | 说明 |
|---|---:|---|
| production | 10 existing + 1 new file | lifecycle authority、typed action port、CodeWorker/SandboxGateway 接线、callable exclusion |
| test | 2 existing + 1 new file | lifecycle 负向、real HTTP/action/API binding、permission、sealed 与 mutation |
| docs | 4 files（含机器证据） | terminal contract、historical divergence resolution、本自审、JSON evidence |
| runtime-assets | 0 | 无 vendor、listener 或参考仓库复制 |
| generated | 0 tracked | compile/test cache 不入 commit |
| data | 0 | 无 dataset/checkpoint |
| adapter-only | 0 | port 有真实产品接线、HTTP 行为与 mutation 证据 |
| mock/fixture | unit + loopback service | 完成条件由真实 registry、lease、HTTP transport、permission port 和 workspace mutation 验证 |

## 后继边界

- 本片没有实现 capability-prefix TypeScript listener、CLI/daemon start/drain/disable/close、kill/restart、
  root attestation 或发布离线证据；这些仍是 FE-S04/FE-S06 的工作。
- FE-S04 现在只是下一候选。此前授权在 activation stop 时已经消耗，必须由用户重新明确授权，
  并以本片 verified HEAD 作为新 base 才能开始。

机器可读证据见 `docs/product-entry/evidence/FE-G00B-terminal-lifecycle-dispatch-remediation.json`。
