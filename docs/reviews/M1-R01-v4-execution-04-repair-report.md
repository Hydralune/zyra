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

