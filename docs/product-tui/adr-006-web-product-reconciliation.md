# ADR-006：Web 实时呈现与 canonical task 对账

状态：Accepted

## 背景

Web 已有带 cursor、generation、snapshot barrier、gap healing 和重连的事件入口，但默认产品会话只按固定间隔重新读取 task。后端已经在 durable frame 与 live-only delta 上提供 `zyra.product-presentation/v1`，浏览器校验器却丢弃 durable presentation，并对 live delta 调用 `trim()`。结果是 Web 无法消费正式展示协议，空格和换行还可能失真；页面只能显示“正在工作”直到下一次轮询。

## 决定

1. Web ingress 独立校验并白名单化 `zyra.product-presentation/v1`。未知 kind、schema、severity、超限字段、终端控制数据和越界集合 fail closed；未知 runtime 字段不会因后端新增而自动进入产品界面。
2. Durable frame 的 presentation 随已排序、去重、完成 gap/partial assembly 的 `IngressBatch` 交付。它不写入 canonical projection，也不成为第二套 task 状态。
3. Live channel 只接受 bounded assistant delta，并保持原始空格、换行和 Unicode。浏览器最多保留 512 KiB transient 文本；越界时仅保留尾部并明确标记“较早内容已折叠”。
4. Durable `started` 用于确认不是中途接入；delta-first 明示为 partial。durable `completed` 仅进入“等待对账”，不能自行宣布 task 完成。
5. 每个 durable batch 以 75ms 合并窗口触发一次 canonical task GET。状态、最终回答、artifact/diff 引用和 verification 最终由该 GET 覆盖 transient presentation；terminal task 到达后清除 transient assistant buffer。
6. Permission event 触发已绑定 permission console 的 canonical refresh。没有 custody/binding 时 refresh 为只读 no-op；任何读取或 custody 失败仍 fail closed。
7. 原 2.5 秒 task refresh 保留为 event transport 失效时的低频恢复兜底。事件链路负责低延迟，不改变 polling 的 canonical owner 边界。
8. Web 最终回答与实时回答均使用 inert React Markdown：拒绝 raw HTML 和不安全链接，不使用 `dangerouslySetInnerHTML`；未闭合代码块保持为有界、明确的生成中内容。

## 结果

- `TaskLiveSync` 同时接收 connection、durable batch 和 live frame，但只持有可重建的瞬时 UI 状态；`WorkbenchController` 仍是 canonical task projection owner。
- generation 增加会丢弃旧 stream，重复或倒退的 live sequence 不会重复文本；中途接入和截断不会伪装成完整回答。
- 真实 API/SSE 集成测试启动 Web observer，由物理 `CodeWorkerRuntimeEventIngress` 发出 started/delta/completed，并验证 Web 先显示精确实时文本，再与 CLI 收敛到相同 status、final answer、verification 和 diff manifest。
- 权限 cross-view 继续由真实 custody/decision/restart 测试覆盖，CLI 与 Web 读取同一 request 和 decision receipt。
- 主要门禁为 `tests/integration/test_product_web_live_cross_view.py`、`tests/integration/test_cli_control_permission_recovery.py::test_real_permission_deny_allow_restart_web_consistency_and_sealed_fail_closed` 与完整 `apps/web` 测试套件。
