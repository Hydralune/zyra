# FE-G00 终端节点契约

## 1. 身份、范围与 Gate 结论

本文件冻结“CLI 进程同时作为本机 terminal backend listener”所必须遵守的既有 Zyra contract。目标身份固定为：

```text
BackendDefinition.kind     = edge_http
BackendDefinition.location = local
bind host                  = 127.0.0.1 / ::1 loopback
port                       = OS-assigned random port
transport                  = HTTP(S) BackendTransport
runtime owner              = Zyra BackendRegistry + ResourceScheduler
listener owner             = 当前 CLI 进程，或显式 daemon 子进程
```

Gate 初始源码基线为 `931deeff0cedd19a23da65f2c9f58be22c956315`，
三份规格提交为 `a9bc7c1aa1805059b25e713c50abef34598f7b8e`，FE-G00R
实现验证提交为 `fd272ba`（核心实现 `843ef9c`）。现有 backend registry、HTTP
transport、remote control client、workspace attestation 和 Python backend dispatch
service 已经证明主要协议可运行。初始审计发现的四项阻断差异现均已收口：

1. capability secret 曾通过 endpoint projection/transport metadata 泄露；
2. `POST /backends` 曾未限制自注册只能是 `edge_http + local`，且 revision 可省略；
3. sealed run 的 `excluded_backend_ids` 曾未从产品主路径接到选择请求；
4. transport consumer 曾接受缺失 digest 的 frame 并自行补算，无法证明发送方提供了逐帧 digest。

FE-S04 激活复核随后发现正常退出 disable 与产品物理动作 dispatch 两项潜在差异；
FE-G00B 已在基线 `237bc123f45b0e2326edd1512539c6fce7146201` 上完成收口：

5. 首次 disabled 注册继续拒绝；同 owner/generation/token 的既有 terminal definition 可在
   active lease 已清零后以 expected revision 更新为 `enabled=false`；
6. 不可序列化的 `worker.run` 永远排除 terminal；已通过 canonical permission 的 typed
   file/search/shell/artifact action 则可经 `dispatch_payload` 获得 terminal lease、HTTP envelope
   和 transport receipt。

因此本 Gate 当前为 **PASS（contract/reference baseline + FE-G00B remediation + FE-S04
listener）**。FE-S04 已在 `1169797e42a705c74e4201ba0372019db31bbbf1` 基线上实现
TypeScript capability-prefix listener、CLI 进程生命周期、真实 typed action HTTP dispatch、
双 terminal 失效转移和 sealed 零 dispatch，并在安全与跨语言行为测试通过后声明
`real_terminal_dispatch_claimed=true`。这仍不代表真实 edge，`real_edge_dispatch_claimed`
未修改；发布 cleanroom 与最终材料仍属于 FE-S06。

## 2. Canonical owner 与信任边界

| 对象 | canonical owner | terminal listener 可做 | terminal listener 禁止做 |
|---|---|---|---|
| BackendDefinition / registry revision / health | `BackendRegistryStore` | 带 revision 注册自己的 edge_http definition；响应 health/control | 保存第二份 registry、伪造 healthy |
| Placement / selection / lease | `ResourceScheduler` + backend registry policy | 校验 envelope/lease 后执行 | 自选 provider/backend、签发 lease |
| Dispatch envelope | `WorkerDispatchRouter` / backend dispatch owner | 校验 schema/checksum/identity/deadline/idempotency | 修改 route、root、credential projection |
| Workspace/artifact custody | workspace manager + attestation | 只在 envelope 允许的 root 中操作并回传 attestation | 扩大 root、把真实 cwd/root 投影给用户 |
| Frame/event | backend transport + runtime event spine | 按 sequence 输出九类 frame | 把 terminal 文本当 canonical event |
| Permission | permission runtime | 等待/呈现服务端裁决 | 本地批准、增加 skip-permission 模式 |
| Process lifecycle | CLI/daemon process supervisor | bind、drain、resume、shutdown | CLI 退出时默认停止独立 daemon |

LoopX claim、terminal process pid、BackendLease 和 permission custody token 是四类不同 identity，不能互相替代。

### 2.1 当前 HEAD 的 source / test 追踪

| 契约项 | source owner | 既有/后续真实测试入口 |
|---|---|---|
| definition、kind/location、envelope、selection exclusion | `packages/scheduler/zyra_scheduler/backend_registry/models.py`、`registry.py`、`integration.py` | `tests/unit/test_backend_registry.py`；`tests/integration/test_product_entry_sealed_terminal_exclusion.py` |
| registry revision、selection、validation | `packages/scheduler/zyra_scheduler/backend_registry/registry.py`、`store.py`、`policy.py` | `tests/unit/test_backend_registry.py`、`tests/unit/scheduler/test_backend_registry_router_delays.py` |
| HTTP request/frame/sequence/digest/redaction | `packages/scheduler/zyra_scheduler/backend_registry/transport.py` | `tests/integration/test_backend_failover_dispatch.py`；`tests/unit/test_product_entry_terminal_contract.py` |
| cancel/drain/resume/status/generation | `packages/scheduler/zyra_scheduler/backend_registry/remote_control.py`、`apps/cli/src/terminal/server.ts` | `tests/integration/test_backend_failover_dispatch.py`；`apps/cli/test/terminal-node.test.ts` |
| dispatch/control listener | `apps/cli/src/terminal/server.ts`、`actions.ts`；Python reference=`packages/workers/zyra_workers/backend_dispatch_service.py` | `apps/cli/test/terminal-node.test.ts`；`tests/integration/test_product_entry_typescript_terminal_node.py` |
| workspace root 与前后 attestation | `packages/scheduler/zyra_scheduler/backend_registry/workspace_attestation.py`、CLI startup-root containment | `tests/integration/test_product_entry_typescript_terminal_node.py`；TS root/symlink/junction negative tests |
| public registration API | `apps/api/zyra_api/provider_backend_api.py` | `tests/unit/test_product_entry_terminal_contract.py` |
| production selection construction | `apps/api/zyra_api/main.py`、`packages/orchestration/zyra_orchestration/task_graph.py`、backend registry integration | `tests/integration/test_product_entry_sealed_terminal_exclusion.py`；sealed/recovery 相邻回归 |

## 3. Listener 启动、注册和停止时序

```text
CLI/daemon start
  -> resolve allowed workspace/artifact roots
  -> generate process generation + one-time capability token
  -> bind loopback port 0
  -> construct capability-prefixed base URL
  -> POST /backends with expected_revision
  -> GET listener /health and verify backend_id/generation/runtime_worker
  -> enter accepting state
  -> receive dispatch/control calls
  -> drain on shutdown
  -> wait/cancel bounded active dispatches
  -> disable/unregister definition using revision fence
  -> close listener
```

进程启动失败、注册冲突、health identity 不一致或 root attestation 失败时必须 fail closed，不能把 listener 标成 ready。

`zyra` 交互进程退出只关闭由该进程创建的 listener；`zyra daemon` 创建的 listener 由 daemon generation 持有，只有 `zyra daemon stop` 或 crash recovery 才关闭。

## 4. 八个 listener endpoint

所有路径实际位于不可猜测的 capability prefix 之后：

```text
http://127.0.0.1:<random-port>/capability/<capability-token>/health
http://127.0.0.1:<random-port>/capability/<capability-token>/v1/dispatch
...
```

注册到 `BackendDefinition.endpoint` 的是 prefix base URL。后端 HTTP transport 和 `RemoteBackendControlClient` 都以 base URL 拼接以下相对路径；不得改为 Authorization/Cookie，因为 transport 会拒绝或剥离这些敏感 header。

| # | Method / path | 请求 | 成功状态 / 响应 | 失败状态 | canonical source / owner | 必测行为 |
|---:|---|---|---|---|---|---|
| 1 | `GET /health` | 无 body | 200；`backend_id`、`generation`、`runtime_worker`、`accepting`、`draining`、active/terminal counts、capabilities | 503 可表达 unhealthy/degraded；identity mismatch 为 protocol failure | `BackendDispatchServiceRuntime.health`；`RemoteBackendControlClient.health` | generation/backend/worker/capability identity；draining/disabled |
| 2 | `POST /v1/dispatch` | `zyra.backend-transport-request/v1` | 200；JSON/NDJSON/SSE 可解码为 response + frames，必须有 terminal result/error/cancelled | 400 malformed；409 idempotency/lease conflict；422 envelope/protocol；429 capacity；503 unavailable/draining；504 deadline；507 workspace/resource | `HttpBackendTransport` + dispatch runtime | replay、collision、deadline、capacity、root reject、frame order/digest |
| 3 | `GET /v1/dispatches` | 无 body | 200；`{ok:true, dispatches:[safe status...]}` | 503 transport unavailable；协议错误 fail closed | `RemoteBackendControlClient.dispatches` | 无重复 dispatch_id；只返回 safe fields |
| 4 | `GET /v1/dispatches/{dispatch_id}` | path identity | 200；`{ok:true, dispatch:{...}}` | 404 not found；503 unavailable | `RemoteBackendControlClient.status` | generation/status/terminal/reconcile 字段 |
| 5 | `POST /v1/dispatches/{dispatch_id}/cancel` | `{reason}` | 202；state 为 `cancelling`、`cancelled` 或 `reconcile_required` | 400 invalid；404 not found；409 terminal/conflict | remote control + listener runtime | cancel before/after output、重复 cancel、bounded completion |
| 6 | `POST /v1/envelopes/{envelope_id}/cancel` | `{reason}` | 202；同上 | 400/404/409 | remote control + listener runtime | envelope identity 对应唯一 dispatch |
| 7 | `POST /v1/control/drain` | `{reason}` | 202；`{ok:true, active_dispatches:[...]}` | 400 invalid；503 unavailable | remote control + listener runtime | 停止接新任务、保留可调和状态、重复 drain |
| 8 | `POST /v1/control/resume` | `{}` | 200；health payload，`accepting=true` 才算 effective | 409 generation/disabled；503 unavailable | remote control + listener runtime | drain 后恢复、disabled 不得被隐式恢复 |

现有 `packages/workers/zyra_workers/backend_dispatch_service.py` 实现的是无 prefix 的固定路径参考。FE-S04 若获授权，应由 TypeScript listener 在同一进程内实现 prefix 路由，不能直接把无 prefix Python handler 暴露出来。

## 5. Dispatch request、envelope 与 idempotency

### 5.1 HTTP request

```json
{
  "schema": "zyra.backend-transport-request/v1",
  "envelope": { "...": "BackendDispatchEnvelope" },
  "operation": "<non-empty operation>",
  "payload": {},
  "headers": {},
  "input_digest": "<sha256>"
}
```

允许的 transport identity header 仅包括 content type/accept、idempotency、envelope、lease、provider、M0 execution ref 和 user agent 等安全字段；`authorization`、`proxy-authorization`、`cookie`、`set-cookie` 不得通过。

canonical `WorkerDispatchRouter` 计算的 `input_digest` 必须覆盖 `m0_execution_ref + operation + payload`，并显式传给 transport。`BackendTransportRequest` 自身的空值 fallback 只覆盖 `envelope_id + operation + payload`；这是潜在误用危险，终端实现和测试只允许走 canonical router，不直接依赖 fallback。

### 5.2 `BackendDispatchEnvelope`

当前 wire schema 固定为 `zyra.backend-dispatch-envelope/v2`，字段全集：

| 组 | 字段 |
|---|---|
| identity | `schema`、`envelope_id`、`run_id`、`task_id`、`node_id`、`turn_id`、`runtime_worker` |
| placement | `backend_lease_id`、`backend_id`、`backend_kind`、`backend_location` |
| filesystem | `workspace_root`、`artifact_root` |
| provider projection | `provider_route_id`、`provider_route_checksum`、`provider_catalog_revision`、`provider_credential_version`、`provider_credential_fingerprint`、`provider_transport_id` |
| execution | `m0_execution_ref`、`physical_worker_lease_ref`、`idempotency_key`、`deadline_at`、`attempt`、`previous_envelope_id`、`created_at` |
| integrity | `metadata`、`checksum` |

listener 必须在执行前校验：

1. schema、checksum、backend id/kind/location 与当前 definition；
2. lease id、runtime worker、M0 ref、provider projection identity；
3. `deadline_at` 尚未过期；
4. workspace/artifact root 在 listener 启动时冻结的 allow roots 内；
5. `(idempotency_key, input_digest)` 重放返回同一 receipt；相同 key 不同 digest 返回 409；
6. 当前 process generation 仍是注册时的 generation。

## 6. 九类 frame、sequence 与 digest

frame wire shape：

```json
{
  "sequence": 1,
  "kind": "accepted",
  "created_at": 0.0,
  "payload": {},
  "output_observed": false,
  "digest": "<sha256 of all previous fields>"
}
```

| kind | 含义 | `output_observed` 默认/要求 |
|---|---|---|
| `accepted` | 已接收并绑定 dispatch/envelope | false |
| `progress` | 结构化阶段/百分比/状态 delta | false，除非产生不可重试外部效果 |
| `stdout` | worker 标准输出片段 | true |
| `stderr` | worker 错误输出片段 | 通常 false；不能当 error 终态 |
| `artifact` | artifact/evidence ref | true |
| `result` | 成功终态和结构化结果 | true；成功流必须恰有终态 result |
| `error` | 失败终态和 retryability/failure kind | 由已观察输出/效果决定 |
| `cancelled` | 取消终态 | 由已观察输出/效果决定 |
| `heartbeat` | 活性，不计有效长程 step | false |

固定不变量：

- sequence 从 1 开始严格连续；重复、缺口、倒序全部是 protocol error；
- 每帧必须由发送方提供 digest，digest 覆盖 `sequence/kind/created_at/payload/output_observed`；
- receiver 必须比较 supplied digest，不能把“字段缺失后本地补算”视为发送方完整性证据；
- result/error/cancelled 后不得再出现业务 frame；
- stdout/stderr 不可包含 capability token、credential、真实 root/cwd 或 custody token；
- heartbeat、UI repaint 和日志刷新不计比赛有效 step。

## 7. `BackendDefinition` 与注册 API

### 7.1 既有字段

`BackendDefinition` 既有字段为：

```text
backend_id, display_name, kind, location, runtime_worker,
capabilities, endpoint, health_endpoint, command, docker_image,
workspace_policy, limits, enabled, priority,
cost_weight, latency_weight, metadata
```

terminal node 的合法子集固定为：

| 字段 | 约束 |
|---|---|
| `backend_id` | process generation 派生的不可碰撞 identity；重启不得无 fence 复用旧进程身份 |
| `kind` | 只能 `edge_http` |
| `location` | 只能 `local` |
| `runtime_worker` | 必须与 health/envelope 一致 |
| `endpoint` | loopback + random port + capability prefix；不得有 query/userinfo |
| `health_endpoint` | 省略或同 prefix 下 `/health`；不得另指非 loopback host |
| `command` / `docker_image` | terminal 自注册必须为空 |
| `capabilities` | 来自 CLI 实际 operator/tool 能力；health 必须回显 |
| `workspace_policy` / `limits` | 不得比 listener 实际 root/concurrency/timeout 更宽 |
| `enabled` | 完成 bind、注册、health identity 验证前不得为 true/healthy |
| `metadata` | 只放 generation/版本等安全摘要；不得放 capability token、cwd/root、credential |

### 7.2 `POST /backends`

注册入口现在接受 `BackendDefinition`、必填 `expected_revision` 和必填
`terminal_registration={generation, owner_id, capability_token}`，成功返回 HTTP 200，
response schema `zyra.provider-backend-api/v1`，state owner 为
`python.BackendRegistryStore`。终端自注册固定要求：

- `expected_revision` 必填；409 后重新读取 registry revision，绝不 last-write-wins；
- API 必须能证明调用者只能注册/更新自己的 `edge_http + local` generation；
- 禁止 terminal 身份提交 `local_process`、`docker` 或 `cloud_http`；
- endpoint/health endpoint 必须是 loopback capability URL；
- list/detail projection 必须用 opaque endpoint identity，不能返回 token path。

FE-G00R 在 API authority 层强制 `edge_http + local`、无 command/docker、loopback、
`/capability/<token>`、generation/owner、listener health identity/capability 回显和 revision
fence。owner 冲突、非 loopback、错误 generation、错误 capability path、listener 未就绪和
stale revision 均有负向测试。token 只以 digest 进入受保护 metadata。

FE-G00B 增加的唯一 mutation 例外是正常退出 disable：definition 必须已经存在，owner、
generation、capability token digest 和 endpoint/runtime/kind/location 必须与既有 definition
完全一致，expected revision 必须命中，且 `current_leases == 0`。该更新不再要求已经 drain 的
listener 仍回报 accepting/healthy；registry 同步把 health 转为 `disabled`。首次 disabled、
active lease、stale revision 以及 owner/generation/token/transport identity mismatch 均 fail closed。

## 8. Workspace、cwd 与 attestation

启动时冻结 `workspace_roots[]` 与 `artifact_roots[]`。每次 dispatch：

1. root 必须存在、是目录、可写，并位于冻结 allow roots 中；
2. envelope root、lease root 和执行前/后的 attestation identity 必须一致；
3. cwd 只能由 envelope/lease 对应的 workspace root 派生，不能用启动 shell 的任意 cwd；
4. symlink/junction/重解析后的路径仍需留在 allow root；
5. artifact 只写 artifact root，并回传 artifact ref/receipt；
6. attestation 保存内部真实 root/cwd 供审计，但用户 projection、日志、frame 和 Web/CLI 状态只显示 opaque workspace/artifact identity。

root unavailable/corrupt、lease conflict/expired、turn timeout 和 execution failed 必须保持既有 `BackendFailureKind`，让 recovery policy 可区分 retry backend、change backend、rebuild workspace、change provider route 或 reconcile。

## 9. Capability URL 与脱敏

capability token 是 listener 的 bearer secret，只允许出现在内存中的 base URL 和受保护的 registry storage。要求：

- 128-bit 以上随机性、每个 process generation 唯一；
- 不接受 query token、Authorization 或 Cookie 替代；
- constant-time 比较；错误响应不暴露 token 是否接近匹配；
- 不写 argv、环境回显、stdout/stderr、event、artifact、receipt、Web projection；
- `_redact_url` 必须把整个 path token 替换为 opaque marker，而非只去掉 query/userinfo；
- registry list/detail 不能通过 `BackendDefinition.to_dict()` 原样回传 endpoint。

FE-G00R 保留内部 `to_dict()` 供 canonical persistence 使用，新增 `to_public_dict()`：
public list/health projection 清空 endpoint/health endpoint，只返回不可逆 endpoint identity，
并过滤敏感 metadata。`_redact_url()` 只保留 scheme/host/port 和 `/[OPAQUE]`；测试会在
API projection 与 transport metadata 中搜索 token。T-01 已解决。

## 10. Disable、health、zombie 与 failover

| 状态/事件 | 必须行为 |
|---|---|
| listener 未 ready | definition 不可被选；health unavailable/disabled |
| `drain` | 原子停止新 dispatch；active dispatch bounded completion/cancel/reconcile |
| CLI 正常退出 | drain -> 等待/取消 -> revision-fenced disable -> close |
| crash / zombie definition | health supervisor 探测失败；达到阈值 quarantine/unavailable；scheduler 选其他 backend |
| daemon 重启 | 新 generation/port/token；旧 generation health/receipt 不得被接受 |
| `resume` | 仅当前 generation、仍 enabled 且 roots 可用时恢复 accepting |
| dispatch 失败且未观察输出 | policy 可 retry/change backend |
| 已观察输出或外部效果不确定 | 不透明重试；进入 reconcile/reconcile_required |
| disable/unregister revision conflict | 不覆盖新 definition；把旧 listener 隔离并告警 |

FE-G00B 已让 `POST /backends` 表达上述 revision-fenced disable，但只提供服务端 mutation
authority；FE-S04 仍须实现 listener 的 drain、bounded settle/cancel、disable 调用和 close 时序。

当前真实 HTTP failover/control/attestation 相邻测试：

```text
.venv\Scripts\python.exe -m pytest tests\unit\test_backend_registry.py tests\integration\test_backend_failover_dispatch.py tests\integration\test_product_entry_sealed_terminal_exclusion.py -q
10 passed in 14.37s（含 sealed exclusion 行为测试）
```

此外 backend/task-graph/provider 相邻回归为 `21 passed`，sealed/recovery 长链为
最终 HEAD `12 passed`（128.20s）。这些结果证明 Python contract 主路径可用，不证明尚不存在的
TypeScript capability-prefix listener 已完成。

## 11. Sealed 排除

sealed/competition run 中，发起该 run 的本机 terminal backend 必须进入 `BackendSelectionRequest.excluded_backend_ids`，避免“参赛控制端同时被调度为执行 worker”破坏零人工和物理调度证据。

要求：

1. sealed admission 产生 terminal backend id exclusion；
2. exclusion 随 recovery/retry 保留；
3. scheduler receipt 显示候选被排除的 reason，但不泄露 endpoint/token；
4. 如果排除后无合法 backend，fail closed，不回退使用 terminal；
5. mutation test 移除 exclusion 时必须出现可观测差异并被安全 gate 拒绝。

FE-G00R 新增 registry-owned terminal id 枚举；task-graph 主 dispatch 在 sealed、
sealed_autonomous、formal_benchmark 或 sealed competition mode 下把这些 id 加入
`excluded_backend_ids`，recovery successor 同样保留排除。FE-G00B 又把所有不可序列化
`dispatch_callable(worker.run)` 的 terminal id 排除固定在 router boundary；interactive action
只能走 typed `dispatch_payload`，不能靠整个 worker callable 偶然失败后 failover。sealed
CodeWorker 不安装 action delegation port，port 自身的 execution-mode fence 也会拒绝强制调用。
lease reason 只记录安全 backend identity，不含 endpoint/token。T-03 已解决并由 action-lane
mutation 对照继续锁定。

## 12. 明确排除项

FE-S04 及本 contract 不包括：

- 把 CLI 建成第二套 Agent runtime；
- 让 terminal listener 成为 scheduler、permission、event 或 workspace owner；
- 支持非 loopback bind、LAN 公网暴露或远程登录；
- 接受 Authorization/Cookie secret；
- terminal 自注册 `local_process`、`docker`、`cloud_http`；
- sealed run 调度回当前 terminal；
- 复制 Claude Code runtime、Ink UI 或其 permission owner；
- 修改 Phase 2 冻结事实或用模拟 label 冒充真实端侧 receipt。

## 13. 差异清单与 stop 决定

| ID | 严重度 | 当前事实 | 要求 | 决定 |
|---|---|---|---|---|
| T-01 | Resolved / security | public definition/registry projection 使用 opaque identity；transport path 为 `/[OPAQUE]` | capability path 在 public projection/metadata 中不可见 | `test_public_backend_projection_and_transport_metadata_hide_capability_path` |
| T-02 | Resolved / authority | revision + terminal proof 必填；只允许 attested loopback `edge_http + local` owner | terminal 只能 revision-fenced 注册自身 generation | registration 正/负向测试通过 |
| T-03 | Resolved / sealed evidence | 主 dispatch 与 recovery 接入 terminal ids；retry 保留 request exclusions；lease 留安全 reason | sealed run 全路径带 `excluded_backend_ids` | sealed/interactive mutation 对照通过 |
| T-04 | Resolved / integrity | receiver 拒绝缺失/错误 digest，并按 expected sequence 拒绝 gap/duplicate/reorder | 发送方 digest 必填并验证 | frame mutation 测试通过 |
| T-05 | Latent hazard | `BackendTransportRequest` 空 digest fallback 与 canonical M0 digest 口径不同 | production 只走 router 显式 digest | FE-S04 测试锁死 canonical path；不在 Gate 改代码 |
| T-06 | Resolved / listener security | TypeScript listener 只接受每进程高熵 capability prefix 下的八端点；错误/缺失 token 统一 404 | 不复用 Python reference 的无 prefix 暴露方式 | TS endpoint/token/Host 负向测试 |
| T-07 | Resolved / product | `run/scenario/interactive/resume` 进程启用 listener，正常退出 drain+disable+close；真实 Python transport 可达 | 真实 HTTP/kill/attestation/lifecycle | TS behavior + cross-language integration tests |
| T-08 | Resolved / lifecycle | terminal owner 可在 drain 后 revision-fenced disable 自己的既有 definition | 首次 disabled、active lease、stale/identity mismatch 必须拒绝 | FE-G00B registration lifecycle tests |
| T-09 | Resolved / dispatch | product permission-approved typed action 经 BackendRegistry HTTP transport；`worker.run` 排除 terminal | lease/envelope/transport + permission/result receipt；移除 delegation 有差异 | FE-G00B real HTTP + mutation tests |

**当前决定：** FE-G00R、FE-G00B 与用户单独授权的 FE-S04 均已完成，T-01..T-09
全部收口。FE-S04 没有修改服务端 contract、没有新增第四套协议，也没有形成 edge claim。
FE-S05 已完成产品/证据分层；FE-S06 已获授权，只重验 terminal lifecycle、真实 dispatch、
failover、sealed exclusion 和 release offline，不改变本 contract 的 owner 或 claim。

## 14. 后续行为测试 contract

| 测试 | 真实要求 |
|---|---|
| registration authority | terminal 只能注册 edge_http/local；local_process/cloud_http/docker 全拒绝；revision conflict 不覆盖 |
| capability secrecy | API list/detail、transport metadata、logs、events、frames、crash text 均搜索不到 token；错误 token 统一 404/403 |
| real HTTP dispatch | canonical router -> POST dispatch -> 9 frame parser -> receipt；断开 listener 触发真实 failover |
| sequence/digest mutation | 缺失/错误 digest、缺口/重复/倒序 sequence 全 fail closed |
| idempotency | 同 key+digest replay 相同 receipt；同 key+不同 digest 409 |
| roots/attestation | root escape、symlink/junction、cwd 漂移、不可写 artifact root 全拒绝；前后 attestation 一致 |
| control | dispatch/envelope cancel、drain、resume、terminal state、reconcile_required |
| process lifecycle | random port、double start、kill -9/crash、zombie health、generation fence、bounded shutdown |
| sealed exclusion | 同 run 真实排除 terminal；无候选 fail closed；mutation 断开后测试失败 |
| release offline | 最终包不依赖工作区外源码、Claude repo 或在线下载；Windows Bun entry 可运行 |
