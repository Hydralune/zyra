# M1-S04C-01 Action Registry / Permission Foundation 增量批判式自审

## 1. 结论

- Slice：`M1-S04C-01`
- 实现提交：`fbc6faf9c8c2482704de8bb822bc6bcb193fea12`
- 基线提交：`dfc740804323f60c8d3af1998a74475e4ae6ae84`
- 自审结论：**当前 foundation slice 完成，不阻断进入 `M1-S04C-02`。**
- 父级状态：`M1-04C` 仍在进行中；本记录不把 foundation 冒充为完整 BrowserWorker action gateway 集成。
- 高风险升级：未触发。当前改动在既有 04A session/CDP、04B selector、03A permission 和既有 event/artifact owner 上新增 04C action/security owner；没有转移 canonical state owner、改变全局默认 permission 策略、引入外部运行时依赖或修改提交/打包边界。

## 2. 当前 slice 目标覆盖矩阵

| 当前 slice 目标 | 状态 | 生产证据 | 行为证据 | 阻断 |
| --- | --- | --- | --- | --- |
| Zyra-owned action registry、严格参数 schema、不可变 ToolSpec | 完成 | `browser_action/catalog.py`、`registry.py`、`schema.py`；旧 `browser_actions.py` 默认 registry 改为本地 catalog | unknown/alias/schema/immutable projection 与根来源仓库独立测试 | 否 |
| 敏感 action 分类在 03A permission 之前执行 | 完成 | `sensitive_policy.py`、`gateway.py`、`permission_bridge.py` | sealed `evaluate_js` 在零 CDP 下拒绝；interactive ASK 可审批后只执行一次 | 否 |
| 复用 03A decision/request/grant owner，不建立第二套 permission store | 完成 | `permission_bridge.py` 只适配 `BrowserActionPermissionGate` | exact grant、参数/selector/network/file/secret/form receipt 变化、replay 均 fail closed | 否 |
| 复用 04B selector identity，覆盖 stale/OOPIF/特殊控件 | 完成 | `selector_guard.py`、`cdp_probe.py`、`geometry_guard.py` | OOPIF session/frame 绑定、stale resume、遮挡、滚动重探测、file input/select 验证 | 否 |
| URL/domain/DNS/IP/redirect 安全 | 完成 | `network_policy.py`、`redirect_guard.py` | IDNA/端口/边界、literal/private/loopback、DNS rebinding、request-paused redirect 均有失败测试 | 否 |
| 上传/下载文件边界 | 完成 | `file_policy.py`、`download_guard.py` | 根目录 containment、traversal/symlink/identity、ADS/保留名、owned write/quarantine 行为测试 | 否 |
| Secret/clipboard/form 边界 | 完成 | `secret_policy.py`、`clipboard_guard.py`、`form_policy.py` | secret grant 后一次物化并递归脱敏；clipboard origin one-use/revoke；form DOM destination/method/effect binding | 否 |
| 唯一 typed side-effect fence、无 blind JS click fallback | 完成 | `executor.py`、`geometry_guard.py`、`keyboard_codec.py` | consumed grant + fresh receipts 才可 dispatch；occlusion/stale/deny/disabled 均为零 mutating CDP | 否 |
| action result/event/artifact 因果接入 | 完成 | `event_port.py`、`gateway.py` | gateway success/failure 生成 receipt-bound result/event/artifact；真实 BrowserWorker smoke 产生 4 action events | 否 |
| source-to-target 账本与去重裁决 | 完成 | `sync_browser_action_permission_source_ledger.py`、ledger seed | `--check`：10 decisions、7 productized、owner `M1-04C` | 否 |
| 父级 `M1-04C` 默认 BrowserWorker 全 gateway 接入/API resume/control | 当前 slice 不承担 | foundation factory 与 default registry 已生产可达 | 当前测试分别证明 BrowserWorker registry 真实路径和 gateway 完整安全链；二者的默认全链路合并留给 04C-02 | 否；是下一 slice 明确任务 |

## 3. 来源角色与目标落位

| 来源角色 | 来源机制 | Zyra 目标 | 裁剪/改造结果 |
| --- | --- | --- | --- |
| primary：browser-use | tools registry/service/views、actor page/element/mouse、security/download watchdog、filesystem | `browser_action/{catalog,registry,schema,gateway,executor,cdp_probe,geometry_guard,network_policy,redirect_guard,file_policy,download_guard}.py` | 拆成 Zyra contracts、receipt、policy 和 typed CDP primitives；删除 runtime reflection、vendor AST discovery、public actor bypass、blind JS click、上游 filesystem owner |
| supplementary：claude-code-best | permission runtime、QueryEngine/tool binding 模式 | `permission_bridge.py` + 既有 `zyra_runtime.permission.action_gate` | 只迁移 exact binding 思路；03A 仍是唯一 decision/request/grant owner |
| supplementary：oh-my-pi | typed tools/hooks、hashline、all-sections preflight | `hook_preflight.py`、`sequence.py` | 采用 deny-first typed hook、argument digest、全序列预检；拒绝 dynamic extension、yolo/bypass |
| conformance-only：opencode | permission question 行为 | `sensitive_policy.py` 的 ASK 对照 | 不迁移第二套 permission runtime/store |
| reference-only：openclaw / agentscope | policy/safe-bin/permission engine 反例 | source audit 与 ledger 记录 | 不获得生产迁移配额，不成为 canonical owner |

去重结果满足每个状态域一个 primary implementation source、最多两个 supplementary sources。来源仓库只在迁移期提供代码级机制分析；最终运行不依赖 `../browser-use`、`../claude-code-best`、`../opencode`、vendor/source-pool 或外部 sidecar。

## 4. 主路径与动态可达性

| 模块 | 生产入口 | owner 接入 | 动态证据 |
| --- | --- | --- | --- |
| registry/schema/catalog | `zyra_workers.browser_actions.default_browser_action_registry()` | BrowserWorker action plan validation | BrowserWorker integration 与真实 Chrome smoke 报告 `action_registry_source=zyra-browser-action-foundation` |
| full foundation gateway | `BrowserActionFoundationFactory.create()` → `BrowserActionGateway.run/prepare/authorize/execute` | 04A transport、04B selector store、03A gate、event/artifact ports | `test_real_03a_permission_gateway...`、ASK approval/replay、stale OOPIF、form preflight 测试 |
| network/redirect | gateway preflight + `BrowserRedirectGuard.on_request_paused` | DNS resolver 与 Fetch interception port | rebinding/redirect 测试在 target request 前拒绝 |
| file/download | gateway/executor + download event guard | workspace/artifact/download roots | 上传 identity/containment、owned destination、quarantine 测试 |
| secret/clipboard/form | gateway security receipts + executor | scoped provider/clipboard port/04B DOM semantics | 一次物化、一次 clipboard grant、DOM-bound form 测试 |
| typed side effects | `BrowserSideEffectFence.dispatch` | consumed 03A grant + fresh receipts + CDP transport | read action执行、navigation once、replay/stale/deny 零 mutating CDP |
| event/artifact | `BrowserActionEventPort`、`BrowserActionResultProjector` | 既有 canonical event/artifact ports | result/action/permission/event IDs 因果绑定；live smoke 4 events |

断开即失败证据：gateway、event port、risk classifier、permission bridge、selector store、form policy 或 executor 被禁用/失效时，针对性测试会在 CDP/network/file side effect 前失败；真实执行不会回退到 static fixture、vendor runtime 或无授权 browser-use actor。

## 5. 零副作用失败矩阵

| 失败条件 | 期望 | 证据 |
| --- | --- | --- |
| sealed arbitrary JavaScript | deterministic deny，human ASK 不可依赖 | `evaluate_js` 测试，`Runtime.evaluate` 次数 0 |
| DNS 地址集合变化 | rebind failure | network receipt revalidate 测试，target request 0 |
| redirect 到被拒绝 origin/IP | Fetch request-paused deny | redirect guard 测试，continue 次数 0 |
| path traversal/symlink/identity change/ADS | file containment failure | file policy 测试，write/upload 0 |
| stale selector/OOPIF generation change | selector receipt failure | ASK resume stale 测试，mutating CDP 0 |
| element occluded | no nearest-element/blind JS fallback | geometry 测试，仅 probe，无 click/scroll fallback |
| form target/method 与 DOM 不一致 | pre-permission form failure | form test，permission/CDP side effect 0 |
| permission replay/argument receipt change | exact grant consumption failure | gateway ASK/replay 测试，navigation 仅 1 次 |
| secret/clipboard receipt replay | one-use failure + revoke/redaction | secret/clipboard 测试，无 secret 出现在 public event/result |

## 6. 状态 custody map

| 状态 | canonical owner | 04C 行为 |
| --- | --- | --- |
| task/run/session | 既有 TaskState/API persistence | 只消费 identity，不建立 session store |
| browser process/session/target/CDP generation | M1-04A | 通过 transport 与 selector binding 消费 |
| DOM capture/selector revision | M1-04B `BrowserSelectorMapStore` | 只生成短期 selector receipt 并在 grant 后重验证 |
| permission mode/rules/request/decision/grant | M1-03A `PermissionStateStore`/gate | 只提供 browser metadata 和 exact receipt binding |
| canonical event/artifact | 既有 event log / artifact port | 写入 action/result/permission 因果投影，不建立第二份 log |
| registry/policy | 04C immutable in-process definitions | 无运行期 vendor scan；digest 绑定每个 receipt |
| security receipts | 04C 短期 immutable receipts | 不作为 canonical history；过期、owner 变化或 identity 变化即失效 |
| workspace/artifact/download paths | deployment-owned roots + existing artifact owner | 04C 做 containment/identity/quarantine，不接管 task state |

## 7. 有效行数分桶

基线到实现提交的 `git diff --numstat`：

| 桶 | 新增 | 删除 | 是否计入 9,000 行 production 下限 |
| --- | ---: | ---: | --- |
| `packages/workers/zyra_workers/browser_action/**` + `browser_actions.py` | 10,510 | 0 | 是 |
| 其中保守排除 package export `__init__.py` 与 audit-only `source_audit.py` | 566 | 0 | 否 |
| 保守有效 production | **9,944** | 0 | **是，超过 9,000** |
| tests | 783 | 3 | 否 |
| product verification / ledger scripts | 486 | 0 | 否 |
| ledger JSON data | 1,386 | 18 | 否 |
| generated/data-as-code/mock-only/fixture-only | 0 | 0 | 否 |
| vendor/vendor-runtimes/source-pool | 0 | 0 | 否 |

父级 `M1-04C` 的 15,000 行门禁尚未关闭。按上述保守口径，04C-02 至少还需交付 5,056 行有效 production，并完成默认 BrowserWorker 全链路/API/control resume 集成；本 slice 不提前计父级完成。

## 8. 验证记录

最终实现提交上的直接验证：

1. `.venv\Scripts\python.exe -m unittest tests.unit.test_browser_action_registry_permission_foundation -v`
   - 14 passed；覆盖 registry/schema、DNS/redirect、file/secret/clipboard、selector/OOPIF、geometry、special controls、form、03A ASK/replay/deny/disable。
2. `.venv\Scripts\python.exe -m unittest tests.integration.test_browser_worker tests.integration.test_browser_worker_permission_gate -v`
   - 27 passed in 77.919s；包含真实 Chrome 输入/点击/导航、上传/下载以及 03A 相邻回归。
3. `.venv\Scripts\python.exe scripts\smoke_browser_action_permission_foundation_live.py`
   - passed in 9.3s；`browser_backend=browser-use-live`、`action_event_count=4`、`permission_grants_consumed=4`、`source_runtime_dependency=false`。
4. `.venv\Scripts\python.exe scripts\sync_browser_action_permission_source_ledger.py --check`
   - aligned；10 decisions、7 productized、owner `M1-04C`。
5. `.venv\Scripts\python.exe -m compileall -q ...`、`git diff --check`、增量 runtime source path audit
   - passed；无新增 dependency/lock、subprocess/local port/Docker/plugin/MCP/dynamic import/root-source runtime path。

实现期间的相邻验证：

- 03A permission + 04B foundation/integration：28 passed in 80.3s。
- 04A browser session integration：9 passed in 58.56s。
- internalization ledger unit suites：24 passed；最初发现 `OpenClaw`/`AgentScope` repo key 大小写不符合 ledger canonical key，修正为 `openclaw`/`agentscope` 后全过。
- 一次把所有套件放进 120s wrapper 的组合命令发生 orchestration timeout；拆分后每个直接相关套件均通过，不是语义失败。

未运行项：普通 slice 未重复全仓 unittest 与完整 cleanroom。原因是没有命中高风险触发器，当前真实行为、失败路径、相邻回归、实际 Chrome、dependency/path 和 ledger 已覆盖；完整全仓/cleanroom 按规则在 `M1-04A` 到 `M1-04D` 数字阶段聚合审查执行。

## 9. 批判式发现与剩余风险

1. 默认 BrowserWorker 已使用新的 Zyra-owned registry，完整 `BrowserActionGateway` 也已经通过 production factory 和真实 owner 测试可达；但 BrowserWorker 的所有 action 分支尚未默认统一穿过完整 04C gateway。这是 04C-02 的明确 integration 工作，不能在本 foundation slice 中虚报关闭。
2. 本 slice 的真实 Chrome smoke 证明 registry、permission 与 action event 的运行可达性；gateway 的 DNS/redirect/stale/OOPIF/form 等安全效果由真实 owner + recording CDP/Fetch ports 验证。04C-02 应把这些 receipts 绑定到默认 live BrowserWorker 每个 action 分支并追加跨 target/redirect live 场景。
3. `form_policy` 只信任 04B DOM selector semantics；模型的 form target/method 只是断言。当前 04B selector hint 能解析直接元素的 `formaction/formmethod`，表单祖先的完整 action 语义采集应在 04C-02/04D live integration 中扩展，而不能回退为信任模型参数。
4. Browser-use 在 Windows 上仍可能产生 page-readiness warning；最终 final-target live smoke 9.3s 正常完成且资源清理成功。聚合审查仍应保留 timing-sensitive live 样本。
5. `source_audit.py` 是生产期边界审计辅助而非 action 决策 owner，已从保守有效行数中排除；ledger、scripts、tests 和 data 同样未抵扣 production 下限。

## 10. 完成判定

- 当前 slice 目标：完成。
- 当前 slice 最低有效 production：完成（保守 9,944 / 要求 9,000）。
- 动态可达性、语义效果、失败路径、断开即失败：完成。
- 根来源仓库/runtime/vendor 依赖：未引入。
- 高风险升级：不适用。
- 下一入口：`docs/milestones/M1-runtime-memory-scheduler-fault/slice-04c-02-action-registry-permission-integration.md`。
