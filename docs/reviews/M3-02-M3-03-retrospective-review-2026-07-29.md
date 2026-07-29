# M3-02 / M3-03 追溯式聚合与里程碑退出审查

## 结论

审查结论为 **PASS_WITH_NONBLOCKING_RESIDUALS**。

本轮按照 `docs/执行单元完成后通用审查任务书.md` 对 M3-02 与 M3-03
实施、真实行为、正式赛题证据、发布归档和第一阶段冻结进行了追溯式聚合
审查。最初发现的归档歧义、历史回执错误绑定当前 HEAD、历史 provider 回执
冒充当前正式 case 覆盖等问题已经修复；随后使用当前配置的 DeepSeek、Kimi
和 GLM 完成同一正式 campaign 的真实请求、重新生成 M3-S02A-02 与 M3-S03-01
证据，并生成新的 M3-S03-02 final-freeze 和提交候选。

当前不存在第一阶段 blocker。保留两个非阻断 residual：

1. 当前提交生成并独立验证了字节一致的双构建发布包，但没有重复运行已在
   M3-S02B-02 完成的约一小时 13-gate 全量 release CI；
2. offline 缺包、哈希错误与 fail-closed 策略已通过，但完整离线 wheelhouse
   尚未形成实物证明。

二者均已进入 `second-stage-handoff.json` 的 `ci-hardening`，不会被误写成
第一阶段完成项，也不影响当前冻结正确性、正式 case 证据或提交归档完整性。

## 身份与提交边界

- M3-02 数字阶段历史 baseline：
  `42f760229588aba917c7283b841587afc7142b0b`
- 当前 provider/formal benchmark 实现 target：
  `09e99cdc5ed9cf3a935ccc327f7261110e6c7d1b`
- 当前正式 benchmark evidence commit：
  `6b928d96f9181acf94eb9e84cb662df39feb9be3`
- 当前 M3-S03-01 report 实现 target：
  `88b88e05aad14e1091f4536bcead02037622408f`
- 当前 M3-S03-01 evidence commit：
  `faf78d1ec6aa1bfe197dbb6120baedabbb723eb7`
- 当前 M3-S03-02 final-freeze evidence commit：
  `725e8699555f8c8b5201ed9e6bbd724d31f85eab`
- 历史 M3-S03-02 implementation target：
  `8825722359e2ca30998d42e9fccf4a35ee307f31`

历史 evidence 继续保留原 target 事实，不追溯覆盖；当前结果写入独立目录：

- `docs/reviews/evidence/M3-S02A-02/formal-current-09e99cdc`
- `docs/reviews/evidence/M3-S03-01/generated-88b88e05`
- `docs/reviews/evidence/M3-S03-02/current-faf78d1`

根目录 `docs/**` 不属于 `zyra` Git 仓库，权威执行状态的回写不包含在上述
commit 中。

## 来源角色、语言与状态 owner

M3-02/M3-03 的迁移模式仍为 `test_eval_hardening_only`、
`packaging_and_release_integration_only` 和 `report_and_freeze_only`。
本轮没有引入新的上游 primary/supplementary runtime，也没有转移既有
canonical state owner。

状态责任保持为：

- benchmark campaign、source case、raw sample、provider receipt、freeze
  admission：Zyra evaluation runtime；
- deployment profile、tier placement、semantic health、fault/recovery：
  Zyra orchestration runtime；
- release transaction、archive、checksum、SBOM、NOTICE 和 lifecycle：
  Zyra productization runtime；
- report、100-point index、submission candidate、dual automated gate、
  final-freeze：Zyra evaluation final-freeze runtime。

提交边界扫描未发现 `../claude-code-best`、`../browser-use`、
`../OpenHands`、`../opencode`、OpenClaw、npm link、pip editable、外部
Docker context 或根目录来源仓库运行依赖。OpenClaw 继续遵守
`excluded_forward_only`。

## 有效代码门禁

历史五个审计器的有效 production 结果继续成立：

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
| M3-S03-02（历史 target） | 5,688 | 4,500 |
| M3-03 父级（历史 target） | 12,458 | 9,000 |

tests、docs、generated JSON/archive、fixture/mock、schema/DTO/data、
adapter-only、ledger/manifest、source-pool/vendor-like 内容均未计入
production。当前补丁主要是 provider profile、evidence binding、恢复式生成
和验证器修复，不使用生成证据回填有效代码。

## 已修复问题

### F1 发布包和提交包允许重复成员造成歧义

修复提交：`3c32dddf4cb81f9a00cf84bcae8e7fc6150297ed`

- ZIP/TAR exact duplicate 与大小写碰撞统一拒绝；
- manifest duplicate、member count/total、directory/symlink、展开预算均
  fail-closed；
- 嵌套 `archive.valid` 与顶层 blocker 保持一致；
- 增加自一致篡改与重复成员对抗测试。

### F2 历史 M3-S03 evidence 错误绑定当前 HEAD

修复提交：`a22c883667c43305ace397caa12b70ce70d6a994`

历史回执固定绑定原 implementation target；后续 runtime 变更不能借用旧
evidence。当前 critical review 的 evidence commit 由权威路径 Git 历史动态
解析，不再硬编码过期提交。

### F3 历史 provider 回执被误计为当前正式 case 覆盖

修复提交：`78a347aa1c43a4730be5227c44bb1fc5c6c3b5f1`

freeze admission 现在必须具备：

- `protected_prior_receipts_only=false`；
- 当前真实 model request；
- 当前 provider/model 数量各至少 2；
- 当前 `device/edge/cloud` 三层；
- provider/deployment receipt 与正式 source case 同 campaign；
- provider receipt、request digest、binding token、tool result 与 case identity
  可重算。

case/provider 分离不再降级为 observation，而是 blocker。

### F4 当前 provider 覆盖不足

修复提交：

- Kimi K2.7 Code：`a1e4934`
- GLM-5.2：`37b1d15`
- 三 provider 绑定正式 case：`b27d51f`
- 验证 baseline 收窄：`09e99cd`

Kimi 在 thinking 模式下不再强制 `tool_choice`，避免兼容 API 拒绝请求。
DeepSeek、Kimi、GLM 均通过真实 HTTP 请求和正式 tool-call 绑定。

### F5 reporting 失败可能重复消耗 provider 额度

修复提交：`4a04347d1f6e428418087cb42eaf8a97d4c02481`

正式 provider receipt 成功后可以离线恢复 reporting/freeze。验证器不再错误
要求确定性相同的 raw tool response digest 全局唯一，而是要求 request ID、
request digest、binding-token digest 和 tool-result digest 唯一且可重算。
本轮 reporting 修复后从既有 12 个成功 receipt 恢复，新增 API 调用为 0。

### F6 M3-S03 report 仍使用历史 provider 占位结论

修复提交：

- current provider evidence 进入 report input：`88b88e0`
- report/archive 重生成：`faf78d1`

compatibility 与 case study 现在以当前 DeepSeek、Kimi、GLM 的 per-case
evidence 为主，M1 Anthropic/OpenAI 只保留 supplementary/history，不再制造
第三 provider 占位或声称 case/provider 分离。

## 正式 API campaign

正式 campaign 只执行 12 次外部 model request，没有自动重试扩张：

| Provider | Model | 请求数 | Tokens |
| --- | --- | ---: | ---: |
| DeepSeek | `deepseek-v4-pro` | 4 | 1,610 |
| Kimi | `kimi-k2.7-code` | 2 | 355 |
| GLM | `glm-5.2` | 6 | 1,665 |
| 合计 | 3 models | 12 | 3,630 |

三个 provider 均返回成功响应；凭据仅从本地环境读取，没有写入 receipt、
report、archive 或提交候选。正式 campaign 结果：

- 42 cells，1,554 raw samples；
- 6 个 source cases，两个跨领域任务各 3 次独立重复；
- 单 source run 有效 canonical transitions：2,394–4,003；
- 6 个 source runs 合计 19,191 transitions；
- device、edge、cloud 三层均有当前 dispatch evidence；
- human/operator intervention 均为 0；
- 12 个 provider request 与正式 source cases 为同一 campaign；
- score 100，19 个 requirement 全部有可导航 evidence。

正式 3,630 tokens 之外只发生少量 smoke/pilot 请求，用于确认三个端点与 Kimi
thinking/tool-call 兼容性；没有运行大额度生成任务。

## M3-S03 report 与最终冻结

当前 M3-S03-01 输出：

- score：100；
- requirement：19；
- internalization ledger：60 rows / 13 sources；
- evidence archive：3,493,055 bytes / 42 members；
- archive SHA-256：
  `4dc68ae420c5b455b57a75b2f46bb5bea7bb47f3b1ffc21489bf2b72564f6a3e`；
- manifest digest：
  `581b74ddfe38873b14b4c37d39fb166b2fcc1b901d3ad359e2d73663eab6925e`；
- output digest：
  `6d05243443e0b41a4fc599cd0367c809bd58df32ce27539c258406bd27376f2f`。

当前 M3-S03-02：

- critical review：PASS，0 blocker；
- rehearsal：7/7 required drills PASS；
- final-freeze：7 components，独立复验 PASS；
- final-freeze digest：
  `6e70ac8627294d39a24c9df754ca0b072a095e6650866a37d4f3baab05aa783d`；
- current release archive：13,219,866 bytes；
- release archive SHA-256：
  `8e1974420b1a3674b97b734159548365acb0f1766f25c26dec18033ed2e6e3f7`；
- 两次 release build 字节一致，独立 archive verification PASS；
- submission candidate：16,707,951 bytes / 7 members；
- submission archive SHA-256：
  `9f2dddd6808c8ff81873554f94a8c156c089e9d22976bdb427f779b9dc4ab8a2`；
- submission manifest digest：
  `29f029459f2b7ca1cf6078f372326959533ece448fb1b6e44d85dd213bbe027c`。

自动 technical/submission gate 使用两个不同 identity 和 role 审查同一 manifest。
这不是未来的人类双签或实际发送确认；人类双签仍按 2026-09-12 gate 执行，
正式提交截止日期仍为 2026-09-15。

## 验证结果

- current provider TypeScript tests：18 passed；
- workspace TypeScript typecheck：PASS；
- M3 benchmark/report/final-freeze Python：
  `52 passed in 29.22s`；
- current Web package build：PASS，370 modules；
- M3-S03-01 independent report verifier：PASS；
- M3-S03-02 independent final-freeze verifier：PASS；
- submission archive/checksum/manifest/email verifier：PASS；
- submission boundary scanner：PASS；
- current evidence credential-pattern scan：0 hits；
- `git diff --check`：PASS。

历史 M3 全仓收口中的 Python、Bun、typecheck、Web 和 13-gate release receipt
仍作为 protected evidence 复验；本轮没有把未重复的小时级 release CI 写成
当前执行结果。

## 最终裁决

M3-02 与 M3-03 满足数字阶段聚合审查和第一阶段退出审查要求。原
`BLOCKED_AFTER_FIXES` 的唯一竞争证据 blocker 已由当前三 provider、同
campaign 正式 case evidence 关闭，100/100 与 first-stage freeze 恢复为当前
可验证状态。

后续不得把两个 `ci-hardening` residual 写成已完成，也不得把自动双角色 gate
写成人类双签或实际提交成功。除此之外，本轮审查无需继续调用付费 API。
