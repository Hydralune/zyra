# M1-R01 v2 Execution-02 独立复审报告

复审日期：2026-07-17  
复审任务书：`docs/remediations/M1-R01-Execution完成后通用独立审查任务书.md`  
被审执行文档：`docs/remediations/M1-R01-claude-source-custody/execution-02-permission-mcp-skill-cutover.md`

## 1. 最终裁决

**FAIL**。

被审 evidence candidate `8f31f6a38af71bac3eddf94fd35956499bda21c9` 没有满足 schema-v3 custody、cleanroom、default write path、跨进程恢复和受保护历史边界。发现计数为 `P0=4`、`P1=3`、`P2=1`。任一 P0 已足以自动判定 FAIL；本报告不把同一窗口后续修复自判为 PASS。

用户同时授权“发现问题直接修改”。因此原候选裁决在本报告中冻结为 FAIL；修复必须形成新的 implementation/evidence candidate，状态只能回到 `implementation_complete_review_pending`，等待另一轮独立复审。E03 继续 blocked。

## 2. Findings

### P0-01：1,480/1,480 条 target custody records 均不满足 schema-v3

- `execution-02-target-custody-map.jsonl` 的 1,480 条记录全部为 `target_sha256=null`。
- 全部记录同时缺少 `canonical_owner_id`、`default_entry_id`、`default_callsite_path`、`default_callsite_symbol`、`state_effect_assertion` 和 `disable_test_ids`。
- 1,480 条映射只循环引用三组 generic 测试标签，不能证明 source symbol → target symbol → default callsite → state/effect → behavior test 五跳闭环。
- 原 verifier 没有按 schema-v3 校验这些字段，故其 PASS 输出无效。按任务书的 mandatory manifest 规则，独立可接受 target custody coverage 为 `0 / 1,480`。

### P0-02：候选门禁把冻结阈值当实测 credit，且没有执行关键动态门禁

- 原 source counter 以 AST 连续区间的物理行数直接申报 20,047 行；使用与生产计量一致的空行/注释排除后，实际只有 17,260 行，其中 Claude-primary 为 15,752，分别低于 20,047 和 18,447。
- 原 clone normalizer 只归一化 literal，不归一化 identifier，无法识别批量改名的同构实现。
- adapter ratio 的分母口径与执行合同不一致；原 verifier 还信任 frozen threshold 汇总而没有逐 blob、range、symbol、test ID 复算。
- runtime-origin、write-path、disable、same-session cross-process resume、lost-ACK 和 built semantics 未由 candidate gate 执行或验证，只检查了静态文件/预录 JSON 的存在。

### P0-03：cleanroom 把明确不完整的 built health 当作成功

- 原 cleanroom 只检查 `runtime:built:health` 命令退出码。
- 被记录的 built health payload 明确包含 `productizedRuntime.complete=false` 和 `evidenceIntegrity=false`，但 cleanroom 仍写入 `ok=true`。
- `apps/code-worker/src/main.ts` 的 health command 在上述字段为 false 时仍返回退出码 0，形成一致的假阳性链。
- 因此原 source-free cleanroom receipt 不能证明 built Bun/Node 默认入口完整，也不能作为 candidate PASS 证据。

### P0-04：E02 候选越界改写了受保护的 E01 evidence

- verified head `f07fd239...` 之后，E02 提交修改了 `docs/reviews/evidence/M1-R01-v14/execution-01-independent-review/candidate-metadata.json`，并新增 `health-verification.json`。
- 这些文件属于已完成 E01 的保护证据，不是 E02 可回写范围；E02 需要的 prerequisite 应写入 E02 自有 receipt，而不是改写历史 candidate metadata。
- 该越界同时破坏 verified baseline 的审计可重复性，命中工作区/提交/状态边界高风险触发。

### P1-01：permission default host 把 session revision 固定为 0

- `PermissionedCapabilityHost` 虽接收 session metadata/context，却在 authorization 与 execution binding 中硬编码 `sessionRevision: 0` / `e02SessionRevision: 0`。
- E02 coordinator 的 skill reload 和 direct execution 默认路径也使用 revision 0。
- 非零 revision session 会得到错误 permit subject，破坏 approval binding、restart continuity 与 stale-revision 拒绝语义。

### P1-02：没有真实 MCP transport/server 与磁盘 skill reload 行为证据

- 原 E02 tests 没有启动真实 stdio child、loopback HTTP/SSE server 或 OS process boundary；transport 证据主要停留在内存对象。
- skill/plugin 测试没有在真实临时目录执行 add/change/delete 后触发 registry revision/tombstone reload。
- 因此 MCP live transport、auth/session/protocol correlation 与 Markdown skill disk lifecycle 的动态可达性没有被原候选证明。

### P1-03：缺少同 session 跨进程 resume 与 lost-ACK 单 effect 探针

- 原 evidence 没有 reviewer-owned 的多 PID、多 epoch、restore-before-bootstrap 运行结果。
- 没有证明 lost ACK 后同 request/effect ID 只重传 receipt、外部 effect count 保持 1。
- 静态 checkpoint JSON、同进程 replay 或实现方预录输出不能替代任务书要求的 OS-process-boundary probe。

### P2-01：Python/source 有效行计量把物理区间误称为 executable SLOC

- G0 的 Python 三桶同样按连续区间物理行数切分，未排除空行、注释和 docstring 边界。
- 使用同一可执行行分类重算后，既有 delete pool 至少短缺 1,282 行；即使纳入原候选已经物理删除但旧清单漏标的两个 skill 文件，仍短缺 564 行。
- 这不允许降低 31,070 阈值；缺口必须由真实退休的 Python logical owner 补足并由 absence test 约束。

## 3. Target identity

- verified Zyra head：`f07fd239dd768f399a36329314da82e90ddce6a4`
- implementation target：`8db12e16edaa72b810eacefe3ede3f1ead59d321`
- reviewed evidence candidate：`8f31f6a38af71bac3eddf94fd35956499bda21c9`
- 复审开始时 worktree：clean
- reviewer nonce：`93624cb48ad0758a9ff5f35538be7123070953222c1ce17b1bdedb69855c97c5`

candidate 可定位且 verified baseline 正确；但 evidence candidate 包含受保护 E01 改写，scope isolation 失败。

## 4. Scope isolation

审查以 `f07fd239..8f31f6a` 为净 diff，并区分 implementation `8db12e1` 与 evidence `8f31f6a`。E02 production/test 主体可定位，但后验证范围包含 E01 candidate metadata 与新增 health evidence，违反“已完成保护边界不可回写”。根目录 manifests 不在 Zyra Git 中，另以 baseline receipt hash 和磁盘内容核对。

## 5. 独立有效行数下界

| 指标 | 原申报/门槛 | 独立复审可接受值 | 结论 |
|---|---:|---:|---|
| accepted source executable SLOC | 20,047 | 17,260 | FAIL |
| Claude-primary executable SLOC | 18,447 | 15,752 | FAIL |
| target custody mappings | 1,480 | 0 个 schema-v3 闭环 | FAIL |
| Python delete executable SLOC | 31,070 | 原冻结算法不可接受；至少短缺 1,282 | FAIL |
| effective changed TS | 41,795 | 原 clone/gate 口径不足以独立接受 | FAIL |
| effective behavior tests | 7,043 | live MCP/disk skill/cross-process 恢复硬行为缺失 | FAIL |

生产与测试文件的存在不能抵扣 mandatory custody、cleanroom 和动态行为失败。本轮不以一个已被证明不完整的 verifier 输出替代独立保守下界。

## 6. Source-language 五跳抽样

用 reviewer nonce 对 source rows 做确定性抽样后，抽样立即命中 target record 的全量结构性缺陷；按任务书的 bucket expansion 规则扩展到 1,480 条全量检查，全部缺关键 custody 字段或 target hash。source blob/range 的进一步复算又发现 executable credit 只有 17,260。故无需通过反复 churn 样本获得结论：整个 target bucket 为 FAIL。

## 7. Runtime origin

静态默认入口指向 TypeScript `CodeWorkerApplication.runTaskRuntime` / `E02CapabilityCoordinator.execute`，但原候选没有以 reviewer-owned disable probe 证明 permission evaluator、MCP client、SkillTool 断开后均 fail closed；built health 还明确报告 runtime/evidence 不完整。因此只能确认声明方向，不能确认有效 built runtime origin。

## 8. Write-path census

- permission host 的真实 authorization/execution binding 写入了错误的 revision 0。
- MCP 没有真实 child/server transport write path 证据。
- skill registry 没有真实 disk add/change/delete write path 证据。
- target map 没有 default callsite 和 state/effect assertion，无法从 1,480 条来源记录反查唯一 canonical write path。

因此原候选不能证明 permission、MCP、skill/plugin 三域各只有一个 TypeScript canonical owner。

## 9. Reviewer-owned probes

对原候选不能接受实现方静态证据替代以下 probe：

1. 非零 session revision 的 permission authorize → permit → execute binding：原代码静态确定失败。
2. MCP stdio child 与 HTTP/SSE server：原测试不存在。
3. disk skill add/change/delete/reload：原测试不存在。
4. same-session 三进程、两次 restart、restore-before-bootstrap：原 runner/evidence 不存在。
5. lost-ACK 单 effect：原 runner/evidence 不存在。
6. built health：现有 payload 明确 incomplete，cleanroom 却误判成功。

缺失 runner 本身不是环境阻塞，而是候选证据与实现缺口，因此 verdict 为 FAIL，不是 INCONCLUSIVE。

## 10. 测试可信度抽样

原 TypeScript suite 的大量 case 可以证明内存状态机局部行为，但无法覆盖 live OS transport、disk lifecycle 和跨进程恢复。原 candidate gate 没有把 mutation、probe、built payload 的语义字段纳入拒绝条件。测试数量 `774` 与 mutation `48/48` 的自报结论，不能抵消上述断层。

## 11. 升级触发与执行

候选命中 canonical permission/runtime custody、Python owner 大规模删除、默认 built health、跨进程 restore/idempotency、受保护 E01 evidence 边界等高风险触发。本轮执行了：

- verified/evidence/implementation 三点身份与净 diff；
- 全量 1,480 target schema 检查；
- source/Python executable SLOC 纠偏复算；
- built health/cleanroom 因果复核；
- default permission revision 静态 write-path 审计；
- MCP/skill/live process/disk test census；
- E01 保护范围 diff 审计。

## 12. 状态迁移

- 原 E02 candidate：`8f31f6a...` → **failed candidate**
- verified head：保持 `f07fd239...`
- 原候选状态：`implementation_complete_review_pending` → `ready_for_fix`
- 用户已授权本轮直接修复；修复必须产生新的 implementation/evidence candidate 后再回到 `implementation_complete_review_pending`
- E03：继续 `blocked`
- next entry：仍为本 E02 执行文档/其独立复审入口，不得进入 E03

## 13. Review evidence commit

本文件应作为只记录原候选 FAIL 的独立 review evidence commit 提交。后续生产、测试、manifest、cleanroom 和 self-review 修复不得改写本裁决；它们属于新候选，必须由新的独立窗口复审。
