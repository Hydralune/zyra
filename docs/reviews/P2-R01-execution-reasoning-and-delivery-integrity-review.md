# P2-R01 执行推理与交付完整性修复审查

日期：2026-08-07  
结论：`PASS_AFTER_FIX`  
基线：`a5d6f4deb833fe439e00277a1119358ac42e3ef8`  
最终实现提交：`bad492e4f928dc2edc43f81d8439f7e4fc74950f`

## 1. 最终结论

ClaudeCode 报告的核心问题属实：原物理执行路径会生成固定代码模板，目标完成状态与真实模型推理、工具执行和工作区交付物脱节。修复后，通用目标的主路径已经改为真实的 provider 模型回合、结构化工具调用、工具观察回注和最终模型回答；没有 provider 凭据、模型回合失败、工具交付失败或交付物不满足目标时均 fail closed，不再用模板、正则或常量 `implemented` 冒充成功。

这项建设不是单纯“新盖一栋楼”，也不是只接一根线。主要工作是把已经存在但未进入物理派发主路径的 TypeScript CodeWorker/provider 能力接通，同时补齐承重层：执行适配器、凭据边界、工具协议、交付契约、真实用量收据、租约身份、验收门、进程监督和发布净室验证。

## 2. 已修复范围

### 2.1 执行层真实推理与工具闭环

- 新增 Zyra-owned `CodeWorkerExecutionAdapter`，把物理 cloud worker 接到 TypeScript CodeWorker runtime 和 provider control plane。
- 每次执行遵循 `model -> tool call -> tool observation -> model` 回路；文件读写、目录检查和最终回答均进入结构化收据。
- 默认 provider 顺序保持 `zhipu/glm-5.2 -> deepseek/deepseek-v4-flash -> kimi-platform/kimi-k2.7-code`。
- 单次派发只中继所选 route 的作用域凭据；重试固定在拥有该凭据的 route，禁止切换到未中继凭据的 provider。
- 删除通用目标的固定 Python 模板、正则回复和按单词数伪造 token 用量路径。`provider_called` 与 token/cost 仅来自真实 provider 响应或明确的未调用状态。

### 2.2 真实交付与验收

- 目标在进入执行前被编译为可审计的 delivery contract，而不是完成状态提示词。
- 文件目标必须由 workspace 工具真实写入，并由文件 API 重新读取验证内容；最终任务元数据记录 changed paths 和 delivery evidence。
- verifier 同时检查推理收据、工具效果、累计交付证据和目标契约。`failed_conditions` 非空时不能再与 completion gate 成功并存。
- 直接回答目标必须由最终模型回答满足 exact-answer contract，不能由正则提前截取。

### 2.3 物理派发、身份与收据完整性

- cloud 推理 worker 与 local deterministic worker 分离为不同 worker identity，避免活动租约互相污染或替换注册。
- physical dispatch binding 绑定 task/run/session、worker、node、lease、route 和 provider receipt；收据摘要在脱敏后重新计算，避免内容与 digest 不一致。
- `runtime_worker` 清单不再只写一个未实际执行的名称；运行时类型、gateway、placement 和 provider/tool 证据可互相追溯。
- MaAS depth、低熵事件和累计 delivery evidence 进入同一 run 的因果链。

### 2.4 生命周期、错误与 CLI

- 部署节点默认受持久 supervisor 的父进程死亡围栏保护；一次性 lifecycle CLI 成功退出后不会误杀刚启动或 restart 后的节点。
- stop、异常退出和测试 teardown 会释放子进程、端口、lease 与注册；正式净室验证确认 stop 后活动进程为零，五个声明端口全部释放。
- API 未捕获执行异常被转换为结构化 HTTP 错误，保留 request/task/run/receipt 诊断，不再直接 reset socket。
- 客户端不再把所有 HTTP 409 翻译为 API 版本不匹配；版本错误只由真正的版本契约判定。
- CLI 当前构建与源码契约一致，权威命令面为 8 个；源码入口和构建后 Node 入口均通过。

### 2.5 发布验证分层

- 无凭据净室只运行 `lifecycle health --no-short-task`，验证结构健康、owner、节点、restart、stop、端口与进程隔离，不伪装成真实模型验证。
- CI 语义门会检查命令确实带有 `--no-short-task`，并检查报告的 `short_task_included` 为 `false`；否则 fail closed。
- 真实模型推理另由用户授权的两条智谱产品入口测试证明，避免把“可安装”和“真实付费推理”混成一个不可复现门禁。

## 3. 外部模型验证

仅以下两条用户授权的智谱目标被作为最终 live acceptance：

1. `测试，收到请回复 ok`：CLI 产品入口返回精确最终回答 `ok`，verifier 与 completion gate 均通过。
2. `建一个 smoke.txt 文件，内容是一行 ZYRA_SMOKE_OK`：CLI 产品入口完成，workspace delivery 记录 `smoke.txt`，文件 API 读回内容满足单行契约。

结果：`2 passed in 225.20s`，执行提交为 `41531f6dbcce293738ca5c33f29b49c1d2d16216`。其后的 `11abab8` 只修复部署 lifecycle owner，`bad492e` 只修复无凭据 release health 契约；两者没有改动 provider 推理、工具派发或交付主路径，因此不重复产生外部调用。

审计披露：在最终授权范围收紧前，一次较宽的回归门误启用了 FE-S02 interactive provider case，产生了一次额外 provider 请求。随后 live gate 被拆成 `ZYRA_RUN_LIVE_PROVIDER_TESTS=1` 与独立的 `ZYRA_RUN_LIVE_INTERACTIVE_PROVIDER_TESTS=1`；最终验证只开启前者并精确选择上述两个测试。未读取、输出或写入任何凭据值。

## 4. 验证结果

- 最终两条智谱产品入口：`2 passed in 225.20s`。
- 修复后的 Python 相关矩阵：`96 passed, 17 skipped in 787.21s`。
- TypeScript runtime 全套：`1270 passed, 0 failed`，55 个文件。
- provider + typed client + CLI：`83 passed, 0 failed`；provider Node 专项：`34 passed, 0 failed`。
- 四个 TypeScript project typecheck：全部通过。
- 最终 release 单元套件：`53 passed in 5.62s`。
- 最终 deployment process 套件：`9 passed in 215.71s`。
- structural/no-provider 集成验证：`1 passed in 138.43s`；契约单元验证：`2 passed`。
- Python `compileall` 与 `git diff --check`：通过。
- 产品入口 release verifier：通过；两个构建字节一致，artifact SHA-256 为 `f4ada12d49a654fe9dc0f3fbf30263ea07020ff8bfe99a7875095ea953a2a84d`，命令数为 8。
- 最终发布 ZIP：`P2-R01-bad492e.zip`，SHA-256 为 `a834879bbcd123fb8ae98bdd6668807c437d47e5d373f425c923a918e7668e0b`，严格绑定最终实现提交。
- Windows amd64 净室安装：`ready: true`，`workspace_isolated: true`，完整 lifecycle 与 isolation audit 均通过；五个声明端口全部释放，undeclared process/port、external build context、editable/link install 和隐式用户缓存依赖计数均为 0。
- Linux/macOS 没有可用宿主，产品入口 verifier 明确记为 `unavailable`，没有伪报跨平台通过。

## 5. 修复期间发现并关闭的问题

增量验证依次暴露并关闭了 scheduler 误分类、脱敏收据 digest 漂移、不可变 binding 富化、MaAS depth/低熵事件缺口、累计 delivery evidence、worker identity 与活动 lease 冲突、provider fallback 切换到未中继凭据、一次性 CLI 父进程围栏误杀 restart 节点，以及无凭据净室误执行真实模型短任务。每个问题都以生产修复和对应行为测试关闭，没有通过跳过主路径或硬编码成功结果绕过。

净室尝试中的非代码环境问题也被保留为事实：主工作区的受保护旧 `tmp-dirty-20260806-0815/` 无法安全遍历，因此改用独立 Git worktree；首次安装受 sandbox 网络策略阻断；本地 13KB Bun relocation launcher 不能在隔离工作区重定位，因此使用仓库锁定的真实 Bun 1.2.15 二进制。它们没有被记为产品能力成功或失败。

## 6. 增量自审分类

从基线到最终实现提交：

- production：37 个文件，3342 additions / 341 deletions。
- test：19 个文件，1919 additions / 74 deletions。
- adapter/bridge 子集：2 个 production 文件，939 additions / 0 deletions；已包含在 production 总量中，不重复计数。
- runtime-assets、generated、data、docs：实现提交中均为 0。
- production mock/fixture：0；测试中的 fake/stub 只用于失败路径与协议边界，不作为 live acceptance。

## 7. 提交链

- `f7d10c8f62a31e34e7283a2585e285af572ffc3d` — real execution reasoning、工具交付、验收、结构化错误和 CLI 契约。
- `41531f6dbcce293738ca5c33f29b49c1d2d16216` — live route、收据完整性、worker identity、累计交付证据和 provider route pinning。
- `11abab8b756184b9f0913b867c2083213d1bf5ab` — 部署生命周期 restart/supervisor ownership。
- `bad492e4f928dc2edc43f81d8439f7e4fc74950f` — 无凭据净室结构健康验证契约。

机器可读证据见 `docs/reviews/evidence/P2-R01-execution-reasoning-and-delivery-integrity.json`。
