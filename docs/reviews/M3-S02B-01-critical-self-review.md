# M3-S02B-01 增量批判式自审

## 1. 结论与精确边界

结论：`PASS`。

- baseline：`f241d33c011e7f0adabb2f390395034ac064c9d6`
- 实现前裁决：`e1d828964f9b909d34c053c90780a9817f184997`
- implementation：`81b08adaabc00cf18917a89418ff73dea3f6c67f`
- evidence：由后续独立证据提交冻结
- 下一入口：`M3-S02B-02`

本 slice 增加 Zyra-owned 部署编排、三类节点、签名协议、状态存储、placement、dispatch、checkpoint handoff、recovery、doctor、semantic-health DAG、短 canonical task 与 API/CLI 控制面。它没有转移 M1/M2 canonical runtime owner；部署层只读取 readiness、编排进程和验证跨 owner 行为。

## 2. 真实主路径与断开即失败

默认产品路径为：

`zyra-deploy / DeploymentOrchestrator -> API + Web + device/edge/cloud 独立进程 -> semantic probes -> canonical short task -> placement/failure/checkpoint verdict`

断开效果由测试覆盖：

- profile 身份、HMAC、nonce/timestamp、generation 或进程隔离失效时，节点 health/dispatch 拒绝；
- sensitivity/capability/resource/latency policy 不满足时 placement fail-closed；
- dispatch receipt、artifact、checkpoint 或 idempotency 不一致时验收失败；
- edge 网络故障必须迁移到另一个 profile，并验证 checkpoint export/import 与 predecessor attempt；
- semantic probe、profile isolation、canonical owner readiness 或 short task 任一 blocker 都使总 health 不 ready；
- sandbox gateway 在干净状态下若不建立 session custody，首个真实 workspace 写入失败；本 slice 对该真实回归补了 7 行窄修复，并由 34 个既有 sandbox 测试及新增产品测试覆盖。

## 3. 状态 owner、失败关闭与秘密处理

- deployment SQLite store 只拥有 process generation、placement、dispatch attempt/idempotency、handoff、probe report 与部署事件；event/reducer、permission、memory、scheduler、artifact、checkpoint 等 canonical state 仍由既有 owner 持有。
- 节点身份与 generation 持久化，不从 component 名称猜测认证身份；自审删除了未使用的猜测式 client helper。
- supervisor secret 不进入 argv、event、health 或 evidence；节点 secret 由 supervisor secret、node ID 和 generation 派生，只经子进程环境传入。health 仅暴露 credential presence。
- API shutdown 只允许 loopback caller；节点请求/响应均有 HMAC、时间窗、nonce replay fence 和固定 profile/generation 校验。
- cloud credential 缺失不伪造成功：非 provider 工作负载可做本地 profile 验证并报告 degraded warning；provider-required workload 继续 fail-closed。
- 未声明来源仓库、来源进程、外部 Docker/context、npm link、editable source path 和 opaque native bundle均未进入运行主路径。

## 4. 有效代码分桶

实现提交相对裁决提交 raw additions 为 12,112 行。保守门禁结果：

- 计入有效 production：9,579 行；
- 最低要求：6,000 行；
- 余量：3,579 行；
- test/mock/fixture：758 行，不计；
- audit/runner：246 行，不计；
- schema/DTO/type/data、注释与空行按 AST/token 规则扣除；
- adapter-only、generated、vendor/source-pool、docs/report：0 行计入；
- 7 行 sandbox 修复经扣除后仅 2 行计入 production，不以相邻修复放大本 slice 规模。

大文件逐项审查见 `docs/reviews/evidence/M3-S02B-01/effective-code-audit.json`。这些文件分别持有单一的 process、state、node、semantic-health 或 product lifecycle 不变量；没有把测试、manifest、预造 finding 或数据伪装成实现。

## 5. 验证结果

- 三 profile 独立进程、真实 dispatch、故障迁移、restart 与完整产品启动：`2 passed in 130.79s`。
- profile/runtime 单元与 sandbox 相邻回归：`41 passed, 4 subtests passed in 19.10s`。
- 实现期间合计聚焦回归：`42 passed, 4 subtests passed in 27.48s`；最终精确实现提交使用上述两组命令复验。
- semantic evidence：3 个不同 PID；device/edge/cloud placement 全覆盖；edge 网络故障迁移到 device；checkpoint handoff verified；短任务 85 events、4 artifacts、0 failed assertion、0 human intervention。
- `scripts/verify_submission_boundary.py`：PASS。
- `scripts/verify_m3.py`（既有 M3-01 runtime 与 M3-02A admission）：PASS；证据提交再将本 slice admission 接入该入口。
- TypeScript 全项目 typecheck 与 Web production build：PASS，370 modules bundled。
- `compileall` 与 `git diff --check`：PASS。

未执行无差别 Python 全仓长套件：当前 slice 的进程/端口/状态风险已由精确 implementation 上的产品集成、相邻 sandbox 回归、M3 freeze gate 和 submission boundary 覆盖；全仓长套件按既定分层留给 M3 退出审查。本 slice 命中新增进程、端口和默认启动主路径高风险，已升级执行三进程真实生命周期、完整产品启动/停止、M3 gate、边界审计和前端正式构建。

## 6. 批判性发现与剩余边界

- Python release lockfile 仍缺失，doctor 明确报 `python_lockfile_missing` degraded warning；这是 `M3-S02B-02` 的已分配发布职责，本 slice 不伪造关闭。
- cloud provider credential 在本机不存在，证据 health 因此为 `ready=true/status=degraded`。缺失值没有泄漏或被模拟，provider-required 路径保持拒绝；正式凭据/多 provider 安装验证属于后续 release clean-install 矩阵。
- 完整 detached cleanroom、wheel/安装、锁文件、SBOM、release archive 与 CI matrix 属于紧随其后的 `M3-S02B-02`。本 slice 已在工作区内从全新 deployment/product state 启动真实 API/Web/节点并清理所有进程，但不冒充下一 slice 的 clean-install 结论。
- semantic probe thread 的外部 I/O 均有 HTTP/子进程超时；线程层结果会记录 probe failure。进一步的强制线程取消没有安全通用语义，不能用它掩盖尚在进行的有状态动作。
- 根目录里程碑状态文件不属于 `zyra` Git 仓库；它将在 evidence commit 后单独更新并在交付说明中明确提交边界。
