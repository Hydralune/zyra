# P2-06 audit_and_fix 聚合审查

## 裁决

`PASS_AFTER_FIX`。P2-06 的代码、定向复验、最终一次全量回归复用、
target-bound preflight、sealed 双场景、cleanroom/release、source custody、
final freeze 与独立复审均已闭环；P0、P1、P2 开放项均为 0。

- 数字阶段 base：`ff460ab7c3c138ad021b64edc303fc282effb995`。
- 原用户授权收尾 target：`b63093e41b5b54be97eb6f60152a5bf2fe0af605`。
- 原数字阶段 evidence head：`8c1a49464870a399b3cf0e3828ce43e5517bd231`。
- 聚合审查修复起点：`e536c366141305d54bbf5434fd7d4171e12826aa` 之后的 P2-06 专属审查修复链。
- 最终受验 target：`0485261c8ba3e506c32eb2000ed50617b9b0b44d`。
- 最终 tree：`b61f75a2fe00c27bdd3d5d2ce13e08a601344ce7`。
- 独立只读复审：PASS；P0=0、P1=0、P2=0。

旧的 `P2-S06-03-user-authorized-closure.md` 仍按历史事实保留为
`completed_after_user_authorized_override`，没有被改写成技术 PASS。本报告以新
target 和新证据补齐其当时明确缺失的技术硬门。

## 顺序聚合结论

| Slice | 结果 | 最终判断 |
| --- | --- | --- |
| P2-S06-01 | `PASS_AFTER_FIX` | final target 上重新执行 strongest preflight，16/16 硬门通过，必需机制均为 `activation_ready + deterministic_ready`。 |
| P2-S06-02 | `PASS_AFTER_FIX` | final target 上执行两个真实 API sealed 场景，独立重算 3,144 / 7,437 个有效迁移，失败 run 和人工干预均为 0。 |
| P2-S06-03 | `PASS_AFTER_FIX` | 唯一最终回归补充链、cleanroom/release、source custody 和 13/13 final-freeze 全部通过，技术结论取代“仅人工授权”的未闭环状态，但不修改历史记录。 |

P2-04 的 `afab278` 和 P2-05 的 `197f722` 聚合审查继续按各自精确 target
复用。P2-05 遗留的 final 2,000+ effective-transition gate 已由本轮两个 sealed
run 解除，没有把较早的容量测试冒充最终证据。

## 审计发现与修复

1. 补齐 production custody、permission/lease compensation、terminal recovery、
   canonical outcome cutoff 与恢复 receipt 因果，失败路径不再悬挂 owner 状态。
2. 将 final regression、release、preflight、sealed 和 freeze 全部绑定最终 source
   tree、精确 commit 链、路径 allowlist、对象 mode/blob/rename 与外部 receipt SHA。
3. 修复 cleanroom 生命周期、临时端口复用/轮换、API 环境隔离、worker fault
   recovery 与 release registry closure；A/B 构建最终 byte-identical。
4. 修复 sealed route projection、provider 证据、重启后 semantic health、checkpoint
   lineage 以及 canonical head 的严格复用和 malformed lineage fail-closed。
5. 修复 final-freeze 外部源码扫描器对自身审计字面量的误报；真实 parent/workspace
   路径 mutation 仍会被拒绝。
6. 修复 preflight 只读 evidence index 的嵌套 digest 绑定，并增加 index digest、
   source count 和 decoy mutation；不再产生四个级联假 blocker。

## 最终一次重型验证

### 全量回归与严格复用

唯一一次完整重型回归在 origin target
`f5a5036e9e2d05dc4e7b84b023b31d9ad18ef923` 执行，Python 为
1,298 passed / 15 deselected、74 subtests passed，并包含 TypeScript、Web、Phase 1
和 release contract 门；原始 receipt 为 13/14，唯一未通过项是 LoopX
cross-version。随后在 direct resume target
`7995b64e8fd48028f1e12d4c60a23f6df4784a2e` 继承 11 个成功 gate，只重跑
3 个 logical gate 并另跑 33 个定向测试，形成 14/14 ready receipt。最终 target
没有重复运行同一重型流程；
`final-regression-supplement.json` 通过 12 段线性 commit 链、累计 allowlist、
对象与 blob 校验、最终定向回归和 policy/internalization 复验继承该结果。

- supplement：14/14 logical gates；failed=0；receipt digest
  `edbdd5bc7c62915827eedb16288c8044303dd074279e14830306653ada5279cc`。
- origin full-regression receipt SHA-256：
  `20d46575cc0d1e819654e5d2c4cf57e31a7e1aacac9d82810e557e954e972c75`。
- direct resume receipt SHA-256：
  `b8d9ff131cd1206044160a7cea590de1ea6bdd668e8e428fceb70fef143c4df4`。
- supplement 文件 SHA-256：
  `2764a4c39186e5cce23e99da9047a8ce4fe2482ee153681cc97a702e523c1353`。

### Preflight、sealed、release 与 freeze

- 正式 preflight attempt-02：16/16；report digest
  `036d48d9d4106691b0dc93608f1374351d2447c81b54b18a549059e60f254d36`。
- preflight attempt-01 因 Windows pytest basetemp ACL `PermissionError/WinError 5`
  失败，目录和四个失败 receipt 原样保留，未覆盖为成功。
- sealed：2 个真实 run、6 次已授权模型 API 调用、0 failed run、0 人工干预；
  独立验证 digest
  `6ef8f56cc5551c5e1f572c64da708f97717696cf2021f3203ec861810cc9cdc1`。
- release：14/14 admission，cleanroom ready，A/B 构建 byte-identical；归档
  SHA-256 `f476a506e7dd60307844d33fee969cdb27336a089ce21c707adb76320ffabefd`。
- final freeze：13/13 checks、`ready=true`、`verdict=PASS`、`blockers=[]`；
  report digest
  `185e5b227370aa3fb5b0163add38657a0300c5c1d072c99ab0d027171057b918`。

cleanroom 的 editable/link、external build context、implicit cache/user state、
undeclared port/process 均为 0。source-language custody 绑定
`e207b46ca690171139a718b8b85d808cb5a79c1e..0485261`，violations=0，
OpenClaw changed-file count=0。

## 定向复验与独立复审

- final-freeze / final-regression targeted unit：52 passed。
- final-target supplement 定向验证：250 passed + 2 subtests。
- `py_compile` 与 `git diff --check`：passed。
- 独立复审重新核对 target/tree、所有关键 SHA、失败证据保留、严格复用链、
  preflight、sealed、release、custody 和 freeze，结论 PASS，未修改文件，也未重跑
  重型流程。

## 代码量边界

使用两个不重叠范围：原 P2-06 为 `ff460ab7..8c1a494`，聚合审查修复为
`e536c366141305d54bbf5434fd7d4171e12826aa..0485261`。两段合计 306 个 changed-file observations、213,435 additions、
1,648 deletions。其中 production 85 / 17,355 / 1,045，tests 67 / 8,389 / 491，
validation scripts 21 / 3,015 / 43，config 8 / 648 / 65，docs/evidence
122 / 184,002 / 0。大量 sealed event trail 明确归为 evidence/data，不计入 Zyra
production；runtime-assets/vendor-like、mock/fixture、OpenClaw 均为 0。

## 最终结论与证据

P2-04、P2-05、P2-06 已按顺序完成 audit_and_fix；所有技术硬门和独立复审通过，
Phase 2 满足标记 `completed` 的条件。

- `docs/reviews/evidence/phase2/P2-06/0485261/aggregate-review.json`
- `docs/reviews/evidence/phase2/P2-06/0485261/gate-matrix.json`
- `docs/reviews/evidence/phase2/P2-06/0485261/bucket-summary.json`
- `docs/reviews/evidence/phase2/P2-06/0485261/commands.json`
- `docs/reviews/evidence/phase2/P2-06/0485261/evidence-manifest.json`
- `docs/reviews/phase2/P2-final-independent-review.md`
- `docs/reviews/phase2/P2-exit-report.md`
