# FE-S04 同机终端节点与真实 HTTP Dispatch：增量自审

## 结论

FE-S04 在 `1169797e42a705c74e4201ba0372019db31bbbf1` 基线上完成，验收结论为
**PASS**。CLI 的 `run`、`scenario`、`interactive`、`resume` 进程现在会创建一个随进程
结束的 TypeScript loopback listener，以 `edge_http + local + CodeWorkerRuntime` 身份经
`POST /backends` 自注册，并只承接已通过 canonical permission 的 typed file/search/shell/artifact
动作。正常退出执行 drain、bounded settle、revision-fenced disable 和 listener close。

本片没有修改 Python 服务端 contract，没有创建第四套协议，没有把完整 `worker.run` 发送到
terminal，也没有修改 `real_edge_dispatch_claimed`。本片只在全部安全、transport、真实 dispatch、
failover 和 sealed exclusion 测试通过后设置 `real_terminal_dispatch_claimed=true`。

## 实现与 owner 自审

| 检查项 | 结果 | 证据 |
|---|---|---|
| listener lifecycle | PASS | OS 随机端口、`127.0.0.1` bind、每进程 32-byte random capability token；CLI finally 中 drain/disable/close |
| endpoint contract | PASS | capability prefix 下严格暴露 health、dispatch/list/status、两级 cancel、drain、resume 八端点 |
| frame contract | PASS | accepted/progress/stdout/stderr/artifact/heartbeat/result/error/cancelled 九类；严格 sequence 与 sender digest |
| registry owner | PASS | 只经现有 `POST /backends`、expected revision、owner/generation/token proof 修改自身 definition；state owner 仍为 Python store |
| placement/lease | PASS | `BackendRegistryActionDispatchPort -> WorkerDispatchRouter -> HttpBackendTransport`；terminal 不自选任务、工具、模型或 placement |
| permission | PASS | action 必须带 allowed、非空 receipt id 且 receipt tool-call identity 一致；listener 不提供本地批准入口 |
| envelope integrity | PASS | 原始 canonical JSON 上验证 envelope checksum 与 input digest，并核对 route/M0/lease HTTP headers |
| workspace custody | PASS | 启动时冻结 real startup root；workspace/artifact root 必须是其下真实可写目录；relative path、symlink/junction 和 cwd 再校验 |
| action parity | PASS | file read/write/edit/delete、workspace/network-bounded search、shell、artifact write 均返回 `zyra.terminal-action-result/v1` |
| cancellation/control | PASS | dispatch/envelope cancel 真实 kill 子进程；drain 拒绝新 dispatch并取消 active；resume 恢复接受 |
| idempotency | PASS | 同 key+digest 重放原帧；同 key+不同 digest 409，副作用不重复 |
| secrecy | PASS | wrong/missing token 与 external Host 统一 404；status/frame/public definition 不含 token/root；shell stdout/stderr/result 做 root/token/credential redaction |
| failover | PASS | 强制终止高优先级 TypeScript terminal，真实连接失败后签发第二 lease 并转到另一 TypeScript terminal |
| sealed | PASS | execution-mode fence 使真实 listener dispatch count 保持 0；既有 `excluded_backend_ids`/callable exclusion 回归通过 |
| CLI projection | PASS | TTY bottom status 显示 safe generation/ready/draining/active 摘要；不显示 endpoint/token/root |

## 真实行为与失败证据

1. TypeScript listener 通过真实 Python `ProviderBackendApi` 注册，registration authority 在写入前主动
   probe listener health identity、generation 和 capability 集。
2. Python production `BackendRegistryActionDispatchPort` 取得真实 lease/envelope，经
   `HttpBackendTransport` 调用 TypeScript listener，写入真实 workspace 文件并返回 transport receipt。
3. 高优先级 listener 被 OS 终止后，下一 dispatch 首个 attempt 产生
   `backend_unavailable`，registry health 变为 unavailable；router 写入
   `backend.dispatch.failed` 与 `backend.failover.committed`，第二 attempt 在存活 listener 完成。
4. 同一真实 listener 上，sealed action port 的 available actions 为空，强制调用 fail closed，
   health 的 terminal dispatch count 前后不变。
5. capability token 缺失/错误、外部 Host、`..`、symlink/junction escape、非目录 artifact root、
   unknown action、digest mutation、超预算 request 全部拒绝。
6. shell 测试真实产生 stdout、stderr、heartbeat，输出真实 cwd 时 frame 中只保留 `[redacted]`；
   两个 cancel endpoint 均能结束真实长时间子进程。
7. 真实 HTTP 生命周期测试观察 enabled registration、safe local status 和同 owner/generation 的
   disabled definition；public health 中 endpoint/health endpoint 均已脱敏。

## 验证记录

- CLI typecheck：通过。
- CLI unit/behavior：`35 passed`，其中 FE-S04 6 tests / 145 assertions，九类 frame 均有实际覆盖。
- terminal contract/action/sealed/router/failover 跨语言相邻回归：`14 passed`。
- FE-S01～FE-S03 真实 API/CLI 相邻回归：`8 passed`。
- TypeScript listener + Python registration/transport/failover 定向集成：`2 passed`。
- CLI Bun build 与构建产物 `--version` smoke：通过。
- `git diff --check`：通过。

Pytest 在默认用户临时目录曾因 Windows ACL 返回 `WinError 5`；使用仓库内独立
`--basetemp .tmp/...` 后同一组 14 项全部通过。该问题发生在 test setup 创建目录前，不是产品
行为失败；证据命令固定显式 basetemp，缓存和临时产物不入 commit。

## 分桶

| bucket | 增量 | 说明 |
|---|---:|---|
| production | 5 new + 3 modified files | terminal contracts/actions/server/registration/lifecycle 与 CLI/renderer 接线 |
| test | 4 new files | TS 行为测试、两项 test fixture、Python 跨语言真实集成 |
| docs | 4 markdown + 1 JSON evidence | contract/status/self-review/机器证据；不改赛题冻结事实 |
| runtime-assets | 0 | 无 vendor/source pool/外部运行依赖 |
| generated | 0 tracked | dist、pytest temp/cache 不提交 |
| data | 0 | 无 dataset、label、checkpoint |
| adapter-only | 0 | 有真实注册、HTTP action、副作用、kill/failover 与 disable 证据 |
| mock/fixture | fake fetch + process fixture | fake fetch 只验证请求形状；完成条件由真实 API、SQLite、HTTP、lease、进程和文件副作用验证 |

## 赛题与后继边界

- 本片增强 `REQ-FAULT-01` 的本机节点失效/恢复证据和 `SCORE-UX` 的可理解 terminal 状态，
  但不重写根目录赛题矩阵已经冻结的第一阶段状态。
- terminal 是 local placement，不是隔离 edge runtime；本片不能单独兑现 `REQ-EDGE-01`，也不改变
  既有 device/edge/cloud 证据。
- FE-S05 可消费本片的 safe terminal status、placement、lease、failure 和 failover receipt；
  FE-S06 仍负责最终 cleanroom、发布包和比赛材料。两者均未由本片自动授权。

机器可读证据见 `docs/product-entry/evidence/FE-S04-terminal-node-dispatch.json`。
