# M3-02 / M3-03 追溯式聚合与里程碑退出审查

## 结论

审查结论为 **BLOCKED_AFTER_FIXES**。

M3-02 与 M3-03 的工程实现、有效代码下限、M3 定向行为、Bun 全量测试、
TypeScript 类型检查、Web 构建和可重复发布包均通过本轮验证；审查发现的
三个代码/测试缺陷已经前向修复。但现有正式 case run 没有在同一批 live
run 中执行真实 `device/edge/cloud` 与至少两个 provider/model。历史 M1
provider 回执只能证明兼容能力，不能替代 M3 正式 case 的同 run 动态证据。
因此既有 `100/100` 与第一阶段 freeze 结论不得继续作为当前可提交状态。

本审查保留 `084d0e17a091a61ce19e876061400d8f99761c13` 之前的历史实现和证据
事实，不追溯改写原提交；所有纠偏都通过新的 review-fix 提交完成。

## 冻结范围

- M3-02 数字阶段 baseline：
  `42f760229588aba917c7283b841587afc7142b0b`
- 历史 M3-03 / 第一阶段证据提交：
  `084d0e17a091a61ce19e876061400d8f99761c13`
- 本轮最终 review target：
  `78a347aa1c43a4730be5227c44bb1fc5c6c3b5f1`
- 审查范围：`42f7602..78a347a`
- 原正式 benchmark target：
  `95fcf7aeaed5b5ec80fb2f7178b97fbdf8adbeb6`
- 原 M3-S03-02 implementation target：
  `8825722359e2ca30998d42e9fccf4a35ee307f31`

原始 diff 为 311 个文件、235,661 行增加、153 行删除。其中 raw additions
包含 164,435 行 docs/evidence 和大量生成归档、JSON、报告及数据，不能计入
production。有效代码仍以五个 M3 审计器的逐文件分桶为准。

## 来源角色、语言与状态 owner

M3-02/M3-03 的迁移模式分别为 `test_eval_hardening_only`、
`packaging_and_release_integration_only` 和 `report_and_freeze_only`。
本阶段没有新增 primary/supplementary 上游源码迁移，也没有转移既有
canonical state owner，因此不适用“上游 primary/supplementary 原语言迁移
为零”的阻断条件。生产责任由 Zyra-owned Python 评测/部署/发布/冻结模块和
既有 TypeScript/Bun runtime 共同承担。

状态 owner 保持不变：

- benchmark campaign、run、sample、freeze admission：Zyra evaluation runtime；
- deployment profile、semantic health、placement：Zyra orchestration runtime；
- release transaction、migration、archive integrity：Zyra productization runtime；
- report、evidence index、archive、submission gate：Zyra evaluation final-freeze
  runtime；
- provider/session/permission/memory/checkpoint 等既有 owner 未在本轮转移。

发布包构建与 boundary scanner 没有发现 `../claude-code-best`、
`../browser-use`、`../OpenHands`、`../opencode`、OpenClaw、npm link、pip
editable 或外部 Docker context 运行依赖。

## 有效代码门禁

五个审计器在最终 review target 上均返回 PASS：

| 范围 | 有效 production | 最低线 |
| --- | ---: | ---: |
| M3-S02A-01 | 8,561 | 6,000 |
| M3-S02A-02 | 8,613 | 6,000 |
| M3-02A 父级 | 17,174 | 12,000 |
| M3-S02B-01 | 9,661 | 6,000 |
| M3-S02B-02 | 9,291 | 6,000 |
| M3-02B 父级 | 18,952 | 12,000 |
| M3-02 数字阶段 | 36,126 | 24,000 |
| M3-S03-01 | 6,770 | 4,500 |
| M3-S03-02（历史 target 审计） | 5,688 | 4,500 |
| M3-03 父级（历史 target 审计） | 12,458 | 9,000 |

tests、docs、generated report/JSON/archive、schema/DTO/data、fixture/mock、
adapter-only、普通脚本、vendor/source-pool 和 ledger/manifest 记录均不计入
上述 production 数字。

M3-S03-02 没有可参数化到当前 target 的独立行数脚本，因此本表保留其历史
target 的已核验数字；本轮 final-freeze 修复只增加生产校验逻辑，没有用其
回填或降低原最低线。

## 主路径、失败路径与断开即失败

本轮确认：

- live benchmark 仍从 product port、campaign store、semantic-step verifier、
  deterministic domain verifier、deployment/fault verifier 和 freeze gate 可达；
- device/edge/cloud 是独立进程、节点身份和 endpoint，不是同进程标签；
- release archive 经两次独立构建得到相同 SHA-256，并由独立 verifier 解包、
  校验 manifest、checksum、wheel RECORD、SBOM、NOTICE 和 runtime inventory；
- submission candidate verifier 对路径逃逸、重复/大小写碰撞、symlink、
  directory member、digest/size/count/total mismatch 和非 manifest 成员
  fail-closed；
- 历史 M3-S03 evidence 只对原 target 有效，后续 runtime 变更不能借用旧回执；
- 正式 case 与 provider/deployment 回执不在同 run 时，benchmark freeze gate
  和 final critical review 现在都会阻断。

## 已修复问题

### F1 发布包/提交包重复成员可被歧义解释

原 release `ArchiveInspector` 拒绝大小写碰撞，但没有拒绝完全相同的 ZIP/TAR
member；submission verifier 还会覆盖 manifest 中的重复路径，并可能在顶层
已出现 blocker 时返回嵌套 `archive.valid=true`。

修复提交：`3c32dddf4cb81f9a00cf84bcae8e7fc6150297ed`

修复后：

- ZIP/TAR exact duplicate 统一拒绝；
- manifest duplicate/case collision、member count/total、ZIP duplicate/case
  collision、directory/symlink 和展开预算统一 fail-closed；
- `archive.valid` 与该归档产生的 blocker 一致；
- 新增自一致篡改清单和 ZIP/TAR 重复 member 对抗测试。

### F2 历史 M3-S03 测试错误绑定当前 HEAD

原测试把历史 evidence 的 review target 写成 `head_commit()`。任何后续
review-fix 都会让“历史 evidence 应通过”的测试自相矛盾。

修复提交：`a22c883667c43305ace397caa12b70ce70d6a994`

修复后历史回执固定绑定
`8825722359e2ca30998d42e9fccf4a35ee307f31`，并新增已知后续 runtime
改动必须被旧回执拒绝的测试。

### F3 历史 provider 能力回执被误计为当前正式 case 覆盖

原 freeze gate 只检查汇总后的 `provider_count/model_count/tier_ids`。这些数值
可以完全来自 M1 protected-prior 回执；原 final critical review 甚至把
“case 与 provider 证据不在同 run”记为 observation，仍允许 100/100 freeze。

修复提交：`78a347aa1c43a4730be5227c44bb1fc5c6c3b5f1`

修复后 freeze admission 必须同时具备：

- `protected_prior_receipts_only=false`；
- 当前真实 model request；
- 当前 provider/model 数量各至少 2；
- 当前 `device/edge/cloud` 三层；
- deployment/provider evidence 与正式 case 同 run。

final critical review 也把 separated evidence 升级为 blocker。现有
M3-S02A-02 与 M3-S03 证据因此按预期被拒绝，不能继续生成新的可提交 freeze。

## 验证结果

最终 target `78a347a`：

- M3 相关 Python：`109 passed in 185.24s`；
- Bun 全量：`1268 passed, 0 failed`，55 files；
- workspace TypeScript typecheck：PASS；
- Web build：PASS，370 modules；
- 五项 M3 有效代码审计：PASS；
- release pipeline（`--skip-ci`）：
  - source commit：`78a347aa1c43a4730be5227c44bb1fc5c6c3b5f1`
  - 两次 archive SHA-256：
    `cbdd297b0d8e2943032a8c142425667d11dd61270a3173e1fa9325638abdbd2f`
  - reproducibility：PASS；
  - independent verification：PASS；
  - wheel SHA-256：
    `d6a8b28c23ab4b6768a89947a048b2feb5353184957e735ff5fc48735c4bd8ff`
- 三进程 deployment/fault/restart 路径包含在 M3 Python 收口中并通过。

全仓 `pytest tests` 使用仓库内独立 basetemp 运行到 1,801 秒后被 30 分钟
预算终止，没有形成完成结果，因此不得写成全仓 PASS。受本轮改动影响的
M3/release/final-freeze 路径已全部通过；重新冻结前仍应在最终 provider
evidence target 上采用分片或更长预算完成全仓 Python。

## 赛题证据复核

现有证据仍能证明：

- 两个领域、42 cells、7 variants、3 repetitions；
- 6 个 fresh source runs；
- 单 run 2,310 至 4,003 个有效 canonical transitions；
- sealed human/operator intervention 为 0；
- fault/change/node/provider/network/tool 矩阵、消融、P50/P95、算法与复杂性；
- 独立 device/edge/cloud profile runtime 和 fail-closed credential handling。

但 M3-S02A-02 自身明确记录：

- current provider/model：`none / none`；
- authenticated provider CLI：`false`；
- external model request：`false`；
- provider/model 2/2 与 device/edge/cloud 只来自 protected M1 facts；
- case-study 与 protected provider evidence：`same_run=false`。

这不满足 M3 正式 benchmark 和第一阶段退出要求中的“两个正式 case 共同覆盖
真实端边云、多 provider/model 与多模型协作”。因此现有 100/100 只能保留为
历史生成结果，当前可提交得分不得声称为 100。

## 剩余 blocker 与恢复条件

唯一竞争证据 blocker：

1. 配置至少两个真实 provider/model 凭据与三个隔离 tier；
2. 在同一正式 campaign 中重新执行两个跨领域 sealed case；
3. 每个 case 维持零人工、至少一个 run 大于等于 2,000 有效 transitions；
4. provider/model request、placement、model split、failover、cost/latency、
   request/response digest 与最终 artifact 必须绑定同一 run/campaign；
5. 重新生成 M3-S02A-02 report/index/manifest、M3-S03 report/archive 和 final
   critical review；
6. 在该最终 target 上完成全仓 Python、Bun、typecheck、Web build、release
   clean-install/CI 和 submission verification；
7. 新 critical review blocker 为 0 后，才可恢复 100/100 与 first-stage
   freeze。

本轮没有调用付费 provider，也没有伪造 request ID、latency、cost 或 response。
取得凭据并执行外部调用涉及费用和外部状态，不能由代码修复替代。
