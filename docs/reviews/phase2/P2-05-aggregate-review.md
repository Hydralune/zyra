# P2-05 audit_and_fix 聚合审查

## 裁决

`PASS_AFTER_FIX`（代码与定向证据）；最终长程证据门保留到全阶段唯一一次
target-bound sealed/preflight。

- 数字阶段 base：`6b81f660822b1f8d5c30206d38f7d4d9d998f0fc`。
- 数字阶段原始代码 target：`4eb7db194721ce8be9d72e12cf3ba46f68dccf6d`。
- 数字阶段原始 evidence head：`ff460ab7c3c138ad021b64edc303fc282effb995`。
- 聚合审查开始时仓库 head：`23e39222cb1b649e6f1e59e13e79fcc9dcea7d70`。
- 修复后实现 target：`197f722653972f01e22df3a760ce350444387701`。
- 两名独立复审员结论：PASS；P0 open：0；P1 open：0。

本轮没有运行全量回归、cleanroom、sealed 或 preflight；这些重型流程保留到
P2-06 审查修复完成后的最终 target 上各执行一次。

## Slice 聚合结论

| Slice | 结果 | 聚合判断 |
| --- | --- | --- |
| P2-S05-01 | `PASS_AFTER_FIX` | 57 个指标规格由同一 production run 的 canonical event/artifact/state receipt 计算；报告由 canonical evaluation admission 精确绑定，调用方 scalar、自签报告、错误 schema、伪物理 receipt 和断连均 fail closed。 |
| P2-S05-02 | `PASS_AFTER_FIX` | API 回查 event/artifact/report owner；Web 对真实 API 页重算 report、filter、snapshot 和 page digest，verified causal refs 均有可导航 route。 |

## 审计发现与修复

1. 原正式生成器导入测试 helper 并自行拼装 receipt。现改为隔离状态中的真实
   `phase2_strongest_v1` production task，由 `CanonicalRuntimeReceiptResolver` 从
   runtime owner 解析输入；缺失 family 只会 unavailable/degraded/failed。
2. 原静态、自签 JSON 可显示为 verified。现由 canonical evaluation event 精确绑定
   report digest、文件 SHA、source admission、event-ref digest 和 transition count；
   即使篡改指标并重签全部 JSON，也因无匹配 owner admission 返回 409。
3. 十类 receipt 使用固定 schema。physical 必须通过 production v2 contract 与
   `PhysicalDispatchReceiptValidator` 的 `real_gate_closed=true`，simulated 或 endpoint、
   worker、backend 篡改不能进入 real numerator。
4. production composition root 现在发布真实 physical、continuity 和
   `NeuroSymbolicEvidenceBundle`。symbolic bundle 不再由评估 resolver 合成。
5. 正式 run 先由 `MemoryFabric` 写入 critical fact，再由 production continuity 路径
   实际消费；31 个 usage/downstream ref 均解析到同 run canonical owner，recall 与
   provenance coverage 均为 1.0。
6. `effective_transition_count` 绑定 event spine 的精确 event ID 集合，不接受调用方
   大数。runtime contract digest 必须出现于 canonical policy artifact admission。
7. API 为每个 verified causal ref 提供内部 route；expired cursor 返回 409，permission
   或 adapter disconnect 显示 degraded，不产生空成功。
8. Web 不再只检查 64 位十六进制格式；它通过 WebCrypto 重算并核对 report、filter、
   snapshot 和整个 evidence page 的 canonical digest payload。保持 64hex 的内容篡改
   也会被拒绝。

## target-bound 正式证据

`197f722` 上只运行一次正式 production capture：

- run：`run_a3b4a637913d`；task：`task_264ea99edaad`；
- canonical source event：61；metric-report admission event：1；
- physical receipt：1；symbolic bundle：1；failed run：0；
- metric specs：57；fixture-free：true；production runtime capture：true；
- continuity critical-fact recall：1.0；provenance coverage：1.0；
- report digest：`e1327fe0dbf09d7fb612abcb06e108c3940cfaaf157a49e94eea305ea8f8c8e6`；
- lineage digest：`73d73bf4796228ee3902aea01d5eff9acbe8d44a3cdc8d737ef4c7c758bdc0d9`；
- manifest digest：`8cc874f07d14a989ff43bc1147028d1bef55cec1a8d881a4713198300f2f6453`；
- API→真实 TypeScript admission：12 transitions、48 causal refs、0 empty routes，
  evidence digest `5e0ae96e192afab66236feb8dd8b40f09fb38402b84e862e0cc5efa376f3d8a6`。

`metric-report.json` 是语义 canonical JSON 归档；其 JSON report digest 与 owner
report 完全一致。Windows 换行序列使归档文件字节 SHA 与 owner admission 中的原始
文件 SHA 不同，因此归档只以 report digest 验证，原始 owner 文件 SHA 由 canonical
admission event 验证，不混用两个身份。

## 定向复验

- 指标 unit/integration/anti-gaming：14 passed。
- evidence API 非规模负向门：4 passed / 1 deselected。
- production runtime→metric→API E2E：1 passed，覆盖真实 memory actual use、
  symbolic owner、physical、report admission、断连和重签篡改拒绝。
- Web 定向 admission：5 passed；`typecheck:web` passed。
- 正式 API page 经真实 TypeScript verifier：passed。
- Python compile 与 `git diff --check`：passed。

较早 target 的 2,105 ACK 容量测试与 281 项 Web 回归不冒充最终 target 的长程证据。
最终只在全阶段唯一 sealed/preflight 中验收至少 2,000 个非 heartbeat、log、replay、
UI repaint 或 no-op 的真实有效 transition，并同时验证 API cursor/snapshot 与真实 Web
async admission；该门通过前 Phase 2 不得标记 completed。

## 独立复审

两名独立复审员只读复核 `197f722` 和正式 evidence，均给出 PASS：P0=0、P1=0。
复审确认 canonical report admission、非空 causal route、Web digest 重算、production
symbolic owner 与 MemoryFabric actual use 已闭合。两人均明确 2,000+ 真实有效长程是
最终 evidence gate，而不是当前 P2-05 代码 blocker。

## 代码量边界

报告使用两个不重叠范围：

1. `6b81f660..ff460ab7`：原 P2-05 数字阶段；
2. `23e39222..197f722`：P2-05 聚合审查修复。

两段合计 60 个 changed-file observations、14,376 additions、326 deletions。其中
production 32 files / 5,476 additions / 56 deletions，tests 11 / 2,156 / 91，
adapter scripts 3 / 517 / 179，docs/evidence 14 / 6,227 / 0。runtime-assets、
generated/data、mock/fixture 和 OpenClaw 变更均为 0。测试、脚本、文档、formal
runtime-state 与证据不冒充 Zyra production 实现。

## 证据

- `docs/reviews/evidence/phase2/P2-05/197f722/aggregate-review.json`
- `docs/reviews/evidence/phase2/P2-05/197f722/gate-matrix.json`
- `docs/reviews/evidence/phase2/P2-05/197f722/bucket-summary.json`
- `docs/reviews/evidence/phase2/P2-05/197f722/commands.json`
- `docs/reviews/evidence/phase2/P2-05/197f722/evidence-manifest.json`
