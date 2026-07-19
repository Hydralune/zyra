# M1-R01 v4 Execution 04 修复报告

## 1. 结论与候选身份

- 修复候选 commit：`2340b792d184bc7625ba15fe7e9d958c3daa4c66`
- 修复候选 tree：`27b5f17e87ee204acdc51240f1d5731fddab616d`
- 受保护基线：`299b708d3559da7a5da1f9d6d55d2d1f1b155249`
- G0 tooling commit：`c62af85aec158bc5e0e4887fa69389b46fd8082c`
- 候选门禁：`10/10` 全部通过
- 当前状态：`implementation_complete_review_pending`

本报告记录对首次独立审查四项发现的修复，不覆盖或改写历史 FAIL。修复候选仍需新的独立审查才能把 E04 判为独立 PASS。

## 2. 独立审查发现与修复

### 2.1 Python owner census schema 不完整

G0 已按正式 refreeze 流程重建并归档旧版本。最终 census 共 `1,619` 个 symbol；每条记录都包含 schema v4 要求的 `tests`、`callsites`、`callsite_scan_complete` 和 `allowed_physical_durable_responsibility`。引用采用真实 `path:line`，verifier 对路径、行号范围、baseline blob 与 retained symbol 逐条校验。

最终候选结果：schema error `0`，Python logical owner `0`，unexpected candidate owner addition `0`，missing retained symbol `0`。

### 2.2 终端 crash matrix 没有真实 TypeScript 重启

Python durable host 现使用 host checkpoint revision、parent revision、writer identity、commit digest 和 compare-and-swap 拒绝 stale writer；损坏 checkpoint fail closed。每次真实 TypeScript `Popen` 都推进 process epoch。

五个真实故障点均通过：

1. checkpoint 已持久化、ACK 前杀死 TypeScript；
2. final checkpoint 已 ACK、terminal 前杀死 TypeScript；
3. terminal receipt 已持久化、ACK 丢失；
4. Python host 在 terminal transition 断开 stdin；
5. TypeScript 在发出 terminal result 后被杀死。

除 terminal receipt 已提交、无需重启的 lost-ACK 外，其余场景均跨至少两个真实进程 epoch。每个场景物理工具执行恰好一次、terminal receipt 恰好一份，提交 terminal 后再次恢复不会重新 `Popen`。协议帧按 epoch、PID、方向、sequence、kind、correlation ID 与 payload digest 完整持久化并验证连续性。

### 2.3 cleanroom 复用了原 `.venv` / `node_modules`

cleanroom 现从 detached candidate worktree 开始，删除既有 `node_modules`、`dist`、缓存及禁止 source-pool/vendor 路径；使用系统 Python 新建 candidate-local `.venv`，重新安装 `pytest==9.1.1`，并用 candidate-local npm cache 获取 Bun 1.2.15、执行 frozen Node 安装。

最终 cleanroom 精确绑定完整候选 commit/tree，`independent_tooling=true`、`dirty_paths=0`、`forbidden_exists=[]`。类型检查、Bun/Node 双构建、`1,257` 项 TypeScript、`34` 项 Python 和全部 E04 探针通过。

### 2.4 审计工具误计为 production

line bucket 将 `scripts/remediation/**` 单独归入 `audit-tooling`。最终分桶为：production 新增 `2,432`，test 新增 `2,913`，adapter-only 新增 `790`，audit-tooling 新增 `3,268`；docs/data/generated/vendor-like 均独立列示。审计脚本不再计入 production。

## 3. 额外修复

- tool effect receipt 新增稳定 effect key，真实进程重启后即使 TypeScript 重新生成 tool call ID，也不会重复物理副作用。
- E02/E03 source 与 Node built stdio ports 都执行真实 initialize/request 或 control envelope，并验证 TypeScript canonical owner。
- mutation corpus 从 `13` 扩展到 `15`，新增 Python host disconnect 与 TypeScript disconnect；最终 `15/15` 编译存活、被 exact killer 杀死、恢复哈希一致，残留 backup 为 `0`。
- cleanroom 自身发现并修复两项证据缺陷：source-port probe 的 Bun 路径硬编码，以及短 SHA 与完整 SHA 的候选绑定不一致。

## 4. 最终证据

- `docs/reviews/evidence/M1-R01-v4/execution-04/candidate-gate-result.json`
- `docs/reviews/evidence/M1-R01-v4/execution-04/terminal-protocol-crash-matrix.json`
- `docs/reviews/evidence/M1-R01-v4/execution-04/cleanroom-result.json`
- `docs/reviews/evidence/M1-R01-v4/execution-04/mutation-results.json`
- `docs/reviews/evidence/M1-R01-v4/execution-04/python-owner-result.json`
- `docs/reviews/evidence/M1-R01-v4/execution-04/line-buckets.json`
- `docs/reviews/evidence/M1-R01-v4/execution-04/default-path-result.json`
- `docs/reviews/evidence/M1-R01-v4/execution-04/build-and-test-result.json`

候选总门禁阈值：8/8 语义域、5/5 terminal fault points、minimum restart epochs `2`、mutation kill rate `1.0`、Python logical owner `0`、forbidden dependency `0`、dirty cleanroom path `0`。

## 5. 第二次独立审查后的修复候选

本节记录对候选 `2340b792d184bc7625ba15fe7e9d958c3daa4c66` / evidence `2c74e21ec35ab29fe67a11b24a5f4d90de64cbfc` 的独立审查及后续修复，不改写前述候选的历史。该独立审查以 `FAIL` 结束，审查证据和四项 finding 保存在 `M1-R01-v4-execution-04-independent-review.md` 及其 evidence 目录。

- 新实现候选 commit/tree：`3d4a00d62e47264cc4ac8678de41af497be7aec8` / `af2f174ae9607fbcaebd33a691f1e1c5451fcadb`
- 受保护 baseline commit/tree：`299b708d3559da7a5da1f9d6d55d2d1f1b155249` / `897924b1d7b47fe5dcfdf6f0ea91d8b0a717fb00`
- 当前 G0 tooling commit/tree：`3d4a00d62e47264cc4ac8678de41af497be7aec8` / `af2f174ae9607fbcaebd33a691f1e1c5451fcadb`
- 当前状态：`implementation_complete_review_pending`

修复内容如下：

1. G0 receipt 明确分离受保护 `verified_zyra_head/tree` 与可演进的 `g0_tooling_head/tree`，不再把冻结工具提交冒充受保护 baseline；refreeze 会校验两组身份并归档旧 manifest。
2. 18 个 credited source ranges 全部扩展到可执行成熟控制流；G0 verifier 现在拒绝只有参数/类型声明、空 body 或缺少真实控制流的 source range。
3. 18 项 provenance 改为 18 个不同的 qualified candidate symbols，并在精确 G0 tooling commit 的精确文件中解析真实 method/function body、计算候选区间和 fingerprint；generic whole-class anchor、stale range 和 symbol-anywhere 通过路径均被关闭。
4. `ToolExecutionRuntime.partitionToolCalls` 成为真实运行分区 owner；skill discovery 由 `TypeScriptSkillRuntime.loadSkillsFromSkillsDir` 承担 discover/validate/register；forked skill 由 `executeForkedSkill` 构造受限 child input；plugin hook 条目绑定真实 `PluginCoordinator.replaceActiveHooks`。
5. mutation 工具在 Windows 上按 LF 逻辑匹配、按原 newline 写回，并以 base64 保存原始 bytes 和 SHA，保证 15 个 operator 的修改/恢复完全可逆。

新候选复验结果：candidate gate `10/10`，E04 专项 `37/37`，mutation `15/15` killed，cleanroom 通过且 `independent_tooling=true`、`dirty_paths=0`、`forbidden_exists=[]`，8 个语义域、18 个 source ranges 和 18 个 qualified candidate targets 全部关闭。当前分桶为 production `2,570/487`、test `2,961/112`、adapter-only `790/39`、audit-tooling `3,644/0`（新增/删除）；审计工具、adapter 与文档不计入 production。

本轮审查者同时实施了上述修复，因此不能对新候选签发新的独立 PASS。新候选及随本报告提交的 evidence 必须由不同的独立审查运行重新验收；在此之前不得推进 M1-S05C-01。

