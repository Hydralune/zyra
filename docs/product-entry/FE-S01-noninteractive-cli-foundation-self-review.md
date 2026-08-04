# FE-S01 非交互 CLI 基础：增量自审

## 结论

FE-S01 在 `026e5149db85781ffe6b8bb9c0cee419b4275560` 基线上完成，
implementation commit 为 `3de2eb8c0e9f5dc4679d3cb8ab7809da3cfab9fe`。
验收结论为 **PASS**：`apps/cli` 已形成 TypeScript + Bun walking skeleton，普通
run、sealed cancel、sealed scenario 与 daemon 生命周期均走现有真实 API/runtime
owner；非 TTY stdout 只输出 JSONL，stderr 只输出人类诊断，退出码稳定为 0..5。

这不是完整交互 CLI 完成声明。REPL、viewport、输入队列、permission 交互、完整
cursor 恢复与 D5 压力向量仍属于后继 slice。

## 实现与 owner 自审

| 检查项 | 结果 | 证据 |
|---|---|---|
| 唯一 typed transport | PASS | `ZyraTypedApiClient` 下沉到既有 `@zyra/typed-api-client`；CLI 无 Web import 或 fallback |
| task canonical owner | PASS | CLI 只调用 create/run/get/cancel/events；不持久化 task/session/result |
| event canonical owner | PASS | capabilities -> snapshot -> delta，并以 legacy replay 只读补齐 verifier；CLI projection 不写 canonical event |
| scenario canonical owner | PASS | registry/create/start/cancel/verify/evidence 全部调用现有 HTTP contract；不调用本地 evidence 脚本 |
| daemon owner 边界 | PASS | 本地只保存 pid、generation、base URL、时间和 project id；CLI 退出不停止 daemon |
| ordinary/sealed 分流 | PASS | `run` 默认 ordinary；显式 `--sealed=true` 才携带 sealed competition policy |
| process failure cleanup | PASS | 自动启动失败或 health 超时会终止刚拉起的子进程并清除本地 state |
| stdout/stderr | PASS | stdout 每行完整 JSON；ANSI、absolute path、token/secret 类字段脱敏；diagnostic 只进 stderr |
| terminal 边界 | PASS | 未启用 terminal node，未声明 `real_terminal_dispatch_claimed` |
| 服务端边界 | PASS | 未修改 `apps/api`、runtime/permission owner 或 public persistence schema |

## 真实行为与负向证据

1. 普通 `zyra run` 启动真实 API/runtime，得到 task/run identity、event-ingress
   事件、final verifier 与 completion gate，并以 0 结束。
2. sealed `zyra run --cancel-after=1s` 产生真实 `task.cancel` committed receipt，
   task canonical 状态为 cancelled，退出码为 4。
3. scenario 默认 artifact preflight 脏时保留 `scenario_preflight_dirty` 的 fail-closed
   结果；显式 `--preflight` 仅透传后端已存在且由服务端验证的 clean targets，随后
   sealed create/start/verify/evidence 全链通过。
4. daemon 不可达时自动启动真实 `scripts/dev_api.py`，启动 CLI 结束后另一 CLI
   仍观测到 reachable + managed + 相同 pid/generation；测试结束后显式停止。
5. 断开 event ingress 且没有 verifier 时不会从已打印内容推断成功；结果为 exit 5。
6. unknown schema、incompatible API version 和 disabled transport 均 fail closed，
   没有 Web fallback。EPIPE 安静关闭 writer，不输出 partial JSON。

## 分桶

| bucket | 规模 | 说明 |
|---|---:|---|
| production | 11 files / 2357 lines | `apps/cli/src/**` 与共享 typed client composition |
| test | 3 files / 598 lines | 纯 parser/writer/contract 测试 + 真实 API/runtime/daemon 集成 |
| config/generated | 4 metadata files | package/workspace/tsconfig/bun.lock；不计入实现体量 |
| runtime-assets | 0 | 无 vendor runtime 或论文资源 |
| data | 0 | 无 dataset/checkpoint |
| adapter-only | 0 | 未用空壳 adapter 冒充行为实现 |
| mock/fixture | test-only | fetch fake 只覆盖负向 contract；所有完成条件依赖真实集成测试 |

## 验证记录

- `bun test ./apps/cli/test ./packages/core/typed-api-client/test`：28 passed。
- `.venv\Scripts\python.exe -m pytest tests\integration\test_cli_noninteractive_foundation.py -q`：4 passed in 94.03s。
- `bun run typecheck`：通过，包含 CLI、typed client、commands、Web 与相邻 TS 包。
- `bun run build:cli`：通过，产物为 `apps/cli/dist/zyra.js`。
- `node apps/cli/dist/zyra.js --version`：exit 0，stdout 两行均为合法 JSON。
- `git diff --check`：通过。

机器可读证据见 `docs/product-entry/evidence/FE-S01-noninteractive-cli-foundation.json`。

## 未越界项与后继风险

- 没有实现 FE-S02 的交互 REPL、输入编辑、scroll/search/status line。
- 没有实现 FE-S03 的 command queue 与 permission 交互。
- SSE、generation change、cursor gap 的完整恢复与长期 cursor journal 仍需后继切片；
  FE-S01 当前以 snapshot + bounded delta + canonical GET/replay 完成 walking skeleton。
- daemon double-start/stale-pid/crash/zombie 的完整压力矩阵仍归 FE-S06；本片完成了
  auto-start、health timeout、generation state、active-task stop guard、存活与显式停止。
- FE-S02 只是下一候选，未获得执行授权。
