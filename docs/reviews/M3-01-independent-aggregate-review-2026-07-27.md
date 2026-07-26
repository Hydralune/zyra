# M3-01 四 slice 独立聚合审查（2026-07-27）

## 1. 结论

审查结论：`PASS_AFTER_FIXES`。

本次按 `docs/执行单元完成后通用审查任务书.md` 对以下四个 slice 进行了独立聚合审查：

- `M3-S01A-01`：source role / dependency / process / custody 审计；
- `M3-S01A-02`：state owner / default reachability / causality / effective-code 审计；
- `M3-S01B-01`：runtime owner / fallback absorption；
- `M3-S01B-02`：config migration / clean default runtime。

聚合基线为 `1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4`，原四 slice 证据终点为
`a853e4748e5590fd1f292416d4c3202024ad7b5b`，本次修复后的精确实现目标为
`f40eb720138e6e0e37ba45050765a82b53b048a3`。

原四个 slice 的历史实现和自审事实保持不变。本报告只记录独立聚合审查发现、前向修复和精确目标复验。

## 2. 独立审查发现与修复

### 2.1 P0：M3-01B 未消费 A01 的受保护 source-custody 原始队列

A01 已冻结
`packages/integrations/zyra_integrations/data/m3_01b_source_custody_work_queue.json`，
其中有 251 个工作项、143 个 blocking 项、246 个 M3-01B 项。原 B01/B02 verifier
只消费 A02 派生的 state-owner 队列，没有证明这份原始 source/dependency/process/custody
队列已经闭环，因此原 B02 将“数字阶段聚合已关闭”写入 slice 自审并不构成独立聚合证据。

修复：

- 新增 `scripts/verify_m3_01_source_custody_closure.py`；
- verifier 校验受保护输入的 digest、项目数、blocking fingerprints 和当前精确 revision；
- 当前扫描中 M3-01B blocking 项必须为 0，受保护 blocking fingerprint 不得残留；
- 未来阶段 blocker 必须带明确 owner，不能被 M3-01 的 PASS 吞掉。

精确目标结果：

- 受保护输入：251 items / 143 blocking / 142 个 M3-01B blocking；
- 当前扫描：164 items / 1 blocking；
- 当前 M3-01B blocking：0；
- 唯一 blocker：`python_lockfile_missing`，owner 为 `M3-02B`；
- M3-01 source-custody closure：`release_ready_for_m3_01=true`。

### 2.2 P1：聚合 verifier 与审计入口存在失真

发现：

- B01/B02 verifier 依赖调用方预先设置 `PYTHONPATH`，从普通仓库入口运行会导入失败；
- `scripts/verify_m3.py` 只检查一个已经生成的 TypeScript artifact，没有执行真实 skill verifier；
- M0-M3 internalization audit 仍引用已删除/改名的测试路径；
- 因果断言仍使用旧数量，不能约束当前 11 个 canonical state domains；
- 四个 slice 缺少可机器复验的 source-language custody 证据。

修复：

- verifier 自行建立仓库内 package bootstrap；
- `verify_m3.py` 改为执行 `scripts/verify_m3_skills.ts`；
- internalization audit 更新到当前正式 TypeScript runtime/test 路径；
- 因果门禁改为当前 11/11 domain、0 invalid link；
- 为四个 slice 增加独立 source-language custody 证据，并在各自冻结
  baseline/implementation revision 上复验。

### 2.3 P1：source-custody 审计把测试、审计和事件监听误报为发布运行时风险

发现：

- JavaScript 任意 `.listen(...)` 被当成端口监听，包括事件 emitter；
- Python builtin、测试 fixture import 和测试命令可能被提升为 runtime dependency/process blocker；
- provenance/audit 路径中的 source-specific 字符串会被误当成默认运行时；
- catalog、process profiles、vendor map、NOTICE 和 custody digest 已与当前路径漂移；
- `psutil` 是正式直接依赖，但未在 `pyproject.toml` 声明；
- API 顶层存在开发期 import fallback；
- shell argv/profile 没有完整表达 `shell=False` 和平台差异；
- `claude-code-best` 的项目复用授权被不准确地写成 upstream open-source license。

修复：

- 使用结构化 listener 识别，只把真实 server listener 计入端口审计；
- 区分 runtime、test、build、audit、provenance scope，只有运行时路径进入发布 blocker；
- 补充 builtin/fixture dependency 识别与 mutation tests；
- 修正 catalog、process profiles、vendor map、NOTICE 和 digest；
- 声明 `psutil>=7,<8`；
- 删除 API 顶层开发 fallback，规范平台 shell argv 且保持 `shell=False`；
- 将 `claude-code-best` 记录为 `USER-AUTHORIZED-PROJECT-REUSE`，明确它不是上游开源许可证声明。

修复后 source scan：

- Python parse errors：0；
- JavaScript lex errors：0；
- broad LangGraph forbidden hits：0；
- opaque runtime hits：0；
- source-risk bridge blocking records：0；
- M3-01B source-custody blocking items：0。

## 3. 状态 owner、动态可达性和断开即失败

可复用的静态 state-owner inventory 在精确目标上仍保持 `release_ready=false`：

- 11 个 catalog state domains；
- 11/11 event-mutation-causality links 有效，0 invalid；
- 静态 default-entry reachability 只能解析 3/11；
- work queue 有 91 items，其中 39 blocking；20 个 blocking work items 仍标为 M3-01B。

这些 M3-01B 静态项集中在
`state_owner_disable_probe_missing`、`undeclared_canonical_owner_candidate`、
`state_domain_no_reachable_default_entry` 和 `catalog_runtime_reference_dead_code`。
它们是通用静态图不能解析动态 composition、runtime registry 和 verifier-bound owner-loss probe 的结果，
不能单独作为动态 runtime 未闭环的证据。

对应的 slice-specific 动态 verifier 在同一精确 revision 上给出：

- 11/11 runtime domains 进入 productized composition；
- 11/11 owner-loss probes 在 owner disabled 时拒绝请求；
- 0 fallback success；
- runtime absorption receipt `release_ready=true`；
- config migration / clean default receipt `release_ready=true`；
- clean bootstrap、crash rollback restart、revision binding 和 inventory binding 均有效。

因此本次不通过修改 catalog 来“消灭”静态队列，也不把 projection/cache/fallback 提升为第二 canonical owner。
通用静态审计器的动态 composition 分析增强保留为后续审计工具改进；M3-01 的真实行为门禁由精确
runtime verifier 和 owner-loss mutation 覆盖。

## 4. 有效代码与 source-language custody

`1d19ea39..f40eb72` 的保守有效行数审计结果：

- raw added：374,485；
- raw deleted：2,841；
- production raw：37,724；
- effective production：30,435；
- Python effective：30,380；
- TypeScript effective：55；
- docs/data/tests/adapter-only/generated 不计入有效 production；
- M3-01 parent minimum：18,000；
- margin：12,435；
- 单文件迁移占比超过 20%：0。

四个 slice 的语言守恒结果均为 PASS：

- A01 Python：9,276 added production lines；
- A02 Python：10,603；
- B01 Python：7,380，TypeScript：21；
- B02 Python：6,975，TypeScript：1,022；
- 无 mixed/unknown production language，无跨语言例外。

## 5. 回归与 cleanroom

### 5.1 精确目标工作树

- M3-01 source/state/runtime/config/adjacent Python：105 passed，7 subtests passed；
- source-custody 修复聚焦测试：64 passed，7 subtests passed；
- Bun 全量：1268 passed，0 failed，1489 assertions；
- TypeScript typecheck：9 projects passed；
- web production build：passed；
- 四 slice source-language custody：全部 passed；
- `verify_m3.py`：passed；
- source-custody closure：passed。

扩展 Python 全仓命令在排除两个已知历史遗留文件后运行满 30 分钟预算仍未完成，
被终止且不记为 PASS：

```text
pytest tests
  --ignore=tests/integration/test_api_control_commands.py
  --ignore=tests/integration/test_code_worker_tool_loop_budget.py
  -q
```

M3-01 相关及相邻行为测试已经完成；剩余全仓长时套件按任务书后移到 M3 退出审查。

### 5.2 历史遗留失败隔离

- `test_code_worker_tool_loop_budget.py`：当前 4 failed / 3 passed；在 M3-01 基线
  `1d19ea39` 上同样为 4 failed / 3 passed，失败内容相同；
- `test_api_control_commands.py`：当前 15 failed / 10 passed；基线代表性失败可复现。

这些测试仍断言旧 Python permission/session/side-effect owner 语义，而 canonical TypeScript
permission/skill/command/runtime 测试为绿色。它们不是 M3-01 引入的回归，保留为 M3-03
测试迁移/退休债务。

### 5.3 Detached cleanroom

cleanroom 固定到 `f40eb720138e6e0e37ba45050765a82b53b048a3`：

- detached worktree，源目录无未跟踪文件；
- Bun 1.2.15 `--frozen-lockfile` 安装 26 packages；
- 本地 wheel 构建成功：
  `zyra-0.1.0-py3-none-any.whl`，
  SHA-256 `c440fbd95eb7ff96494f8f69722a3a344738ea7d7936b37b308403ed45881439`；
- wheel 安装后 `zyra_integrations` 和 `zyra_evaluation` 均从 cleanroom venv
  `site-packages` 加载；
- venv、wheel、pip cache 和审计输出均放在仓库 worktree 外，避免第三方源码污染 source scan；
- cleanroom source scan：257 findings / 1 blocker / M3-01B blocking 0；
- cleanroom source closure：PASS，唯一 blocker 仍为 M3-02B 的 Python lockfile；
- cleanroom Python focused tests：40 passed；
- cleanroom Bun：1268 passed；
- cleanroom TypeScript typecheck：9 projects passed；
- cleanroom web build：passed。

当前没有 Python release lockfile；这是审计器明确输出并归属 M3-02B 的发布门禁。
本次只证明 M3-01 精确提交可构建/安装 wheel 且审计主路径从隔离环境执行，不把仓库错误声明为整体 freeze-ready。

## 6. 风险与后续边界

- M3-01A、M3-01B 和 M3-01 数字阶段在修复后完成；
- 仓库整体仍不是 freeze-ready；
- `python_lockfile_missing` 必须由 M3-02B 关闭；
- M3-02A/M3-02B/M3-03 的 requirement runtime evidence 和发布冻结工作仍保留；
- 旧 Python cutover 测试迁移/退休由 M3-03 处理；
- OpenClaw 继续保持 `excluded_forward_only`，本次未恢复源码、路径或运行依赖；
- LangGraph 仅保留 checkpoint/exact-resume 窄域 conformance，未取得 broad production owner；
- 唯一下一执行入口仍为
  `docs/milestones/M3-freeze-productization/slice-02a-01-default-path-security-regression-hardening.md`。

机器可读摘要：
`docs/reviews/evidence/M3-01-independent-review/aggregate-review-evidence.json`。
