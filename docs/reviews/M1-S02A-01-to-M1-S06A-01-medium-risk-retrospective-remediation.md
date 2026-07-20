# M1-S02A-01 至 M1-S06A-01 中风险追溯审查修复记录

## 1. 结论

本轮已完成原审查报告三个 finding 的生产修复和针对性行为验证：

- `MR-P1-001`：CodeWorker 公开 ASK 队列、批次 suspension、精确 approval resume、拒绝、过期、部分副作用 fence 与 terminal ACK 丢失恢复均已修复。
- `MR-P1-002`：Browser disclosure 已进入当前 TypeScript provider request envelope，隔离 Browser → 02D 测试通过。
- `MR-P2-001`：根 `pyproject.toml` 已成为可编辑安装和依赖声明边界，全新 Python venv 可直接执行 `pip install -e ".[test]"`，不再需要 `vendor/browser-use` editable install。

实现提交为：

- `23e9f700396411d8c06ab5e5d6cbef1b8997ca94`：三个原始 finding 的主体修复。
- `b77a6444717827f5de2d10e99666dc787eb83160`：ASK 专项 terminal ACK 丢失恢复与公开 API route contract 修复。

本记录状态为 `IMPLEMENTATION_AND_TARGETED_VERIFICATION_COMPLETE`，不是独立复审 PASS。根据用户本轮收口范围，没有执行最终提交 `b77a644` 的 exact-target cleanroom，也没有执行独立复审；因此未修改 `docs/milestones/execution-state.yaml`，原追溯审查不能仅凭本记录改写为 PASS。

## 2. ASK 修复链

公开主路径现在为：

1. TypeScript E02 作出 `ask`，E01 保留同一 scheduled batch/tool-call graph，查询会话进入 durable suspended 状态。
2. Python 只把 exact approval binding 投影到认证后的公开 transport queue，不取得 permission policy owner。
3. 同一批次只要存在 ASK，所有 sibling physical effects 都保持未执行；逐项审批后以原 logical worker request 恢复。
4. allow/deny 都绑定 request、continuation、tool call、decision 与一次性 permit；过期请求继续 fail closed。
5. 若工具副作用已经提交而 terminal ACK 丢失，Python host 先持久化 terminal receipt。下一次公开 API 调用不重启 TypeScript 工具执行，而是恢复该 receipt。
6. terminal replay 的 API route contract 只复用相同 `task_id + session_id + logical_worker_request_id` 的既有持久相位；普通请求不会借用历史相位。

新增专项测试验证：首次 ASK 返回 `409 permission_suspended`；审批后注入 `terminal_result_ack_lost`；最终请求从 `durable-terminal-receipt` 返回 `201`，`route_contract.ok=true`，append 型副作用内容仍严格为一次 `once`。

## 3. Browser provider envelope

当前 TypeScript model stream 会把输入消息规范化为 provider messages，并保留 Browser disclosure/source/capture/selector/action/artifact provenance。外部 Browser context 被明确标为 `external_untrusted`、provider role `user`，并携带 secret redaction state。

每个真实 model stream report 现在包含 request、session、logical worker、turn、实际 model、完整 provider messages 与 context 计数。fallback attempt 使用实际被选中的 model，而不是默认 model。原隔离测试 `test_api_browser_checkpoint_is_delivered_to_real_02d_provider_once` 已通过。

## 4. Python 安装边界

根 `pyproject.toml` 明确声明 setuptools build backend、全部 Zyra Python package roots、integration package data、`browser-use[core]==0.13.3` 和 test extra。根 README 只要求：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

在 detached `23e9f70` worktree 的全新 venv 中，上述安装成功；`browser-use`、`cdp-use`、`psutil` 与全部 `zyra_*` packages 均来自声明依赖/根 editable project，不依赖来源仓库相对路径或现有主工作区 `.venv`。

## 5. 验证结果

| 验证 | 结果 |
|---|---|
| TypeScript 全量 runtime suite | `1258/1258` PASS |
| TypeScript typecheck | PASS |
| G1 Python E01 cutover | `23` PASS |
| G2 Python E02/continuation | `11` tests + `14` subtests PASS |
| G4 workspace/gateway | `71` tests + `9` subtests PASS |
| Browser → 02D isolated | PASS |
| ASK 单审批、双 ASK/拒绝、过期、terminal ACK loss | `4/4` PASS |
| 相邻 generic terminal/batch ACK loss | `2/2` PASS |
| 根 packaging unit | `2/2` PASS |
| `pip check` 与 Zyra/Browser imports | PASS |
| `git diff --check` | PASS（仅 Windows LF→CRLF 提示） |

ASK terminal ACK lost red probe 在修复前稳定得到：durable WorkerResult 已恢复，但 route contract 缺少 `model_stream_report`、`compact_restore_report`、`codeworker_restore_integration`，HTTP 仍为 `409`。修复后的同一测试返回 `201`，这证明新增测试真实杀死了原缺陷，而不是只做静态存在性检查。

## 6. State custody、可达性与断开即失败

| 状态域 | canonical owner | 本轮接入 |
|---|---|---|
| query/session/turn/tool/batch/compact/provider | TypeScript E01/E04 | ASK 是 suspension，不是失败 settlement；exact batch 可恢复 |
| permission policy/decision/permit/journal | TypeScript E02 | Python queue 只作认证 transport projection |
| permission request transport/custody | Zyra Python `PermissionStateStore`/control plane | 公共 GET/resolve/cancel/abort/expire 可达 |
| physical tool effect/terminal receipt | Zyra Python host + TypeScript binding | effect receipt 与 terminal receipt 防重复执行 |
| POST CodeWorker API projection/contract | Zyra API + 02D projection runtime | terminal replay 读取 exact logical binding 的持久相位 |
| Browser context | Zyra Browser runtime；TypeScript provider consumption | provenance 进入当前 provider envelope |
| Python installation manifest | root `pyproject.toml` | fresh editable install 可复现 |

动态断开效果已经由测试证明：

- 断开 transport merge 时，公开 pending ASK 为空，原单 ASK 测试失败。
- 把 ASK 当成 terminal settlement 时，恢复会丢失 scheduled batch，双 ASK 测试失败。
- 取消 batch ASK fence 时，未完成审批的 sibling 会提前产生物理副作用。
- 取消 terminal replay 的 exact historical phase selection 时，专项红测恢复 WorkerResult 但 API 错误返回 `409`。
- 取消 provider message provenance 时，Browser → 02D isolated 测试报 `provider_context_missing`。
- 取消根 package discovery/dependency 声明时，根 editable install 和 packaging unit 失败。

## 7. 有效行数与依赖边界

相对 review commit `b581c17`，截至实现 target `b77a644` 的增量分桶：

| 桶 | 新增 | 删除 | 计入有效生产实现 |
|---|---:|---:|---|
| production | 587 | 76 | 是 |
| behavior tests | 524 | 4 | 否，单列测试证据 |
| build/config (`pyproject.toml`) | 38 | 1 | 配置责任，单列 |
| docs (`README.md`) | 13 | 0 | 否 |
| generated/data/vendor-like/source-pool/adapter-only/mock-only | 0 | 0 | 否 |

本轮没有新增运行期 `../claude-code-best`、`../browser-use`、`../OpenHands`、`../opencode` 或 `../openclaw` 路径依赖，没有 npm link、pip editable vendor path、新 MCP server、新端口或 Docker 依赖。`browser-use[core]` 是根项目显式锁定的发布依赖；其物理代码不计为 Zyra 深度内化有效行数。

## 8. 未在本轮关闭的门禁

- 最终 target `b77a644` exact-target cleanroom：未执行；用户明确停止重复 Bun clean install。本轮只保留 `23e9f70` fresh Python install 成功事实。
- 独立复审：未执行；本记录由修复实施者生成。
- `execution-state.yaml`：未修改。
- 临时 detached cleanroom worktree：仍位于 `G:\agent-zoo\.tmp\m1-medium-risk-23e9f70-cleanroom`，不属于 Git 提交或运行依赖。

结构化明细见 `docs/reviews/evidence/M1-S02A-01-to-M1-S06A-01-medium-risk-retrospective/remediation-verification.json`。
