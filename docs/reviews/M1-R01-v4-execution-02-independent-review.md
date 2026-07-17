# M1-R01 v4 Execution-02 独立复审报告

复审日期：2026-07-17  
复审任务书：`docs/remediations/M1-R01-Execution完成后通用独立审查任务书.md`  
被审执行文档：`docs/remediations/M1-R01-claude-source-custody/execution-02-permission-mcp-skill-cutover.md`

## 1. 最终裁决

**FAIL**。

被审 implementation candidate `f7ef18c080533e81ea4e6b412007424696fa7423` 与 evidence candidate `69eaa206338268ba782dc64655e00ba4df1e88ec` 未满足 schema-v3 五跳 custody、默认 built 主路径、MCP live 行为、lost-ACK 恢复和 candidate gate 可信度要求。发现计数为 `P0=4`、`P1=2`。任一 P0 已足以自动判定 FAIL；本报告不把同一审查窗口随后实施的修复自判为 PASS。

用户授权“发现问题直接修复”。因此本报告冻结原候选事实；修复必须形成新的 implementation/evidence candidate，并保持 `implementation_complete_review_pending`，等待新的独立复审。E03 继续 blocked。

## 2. Target identity

- verified Zyra head：`f07fd239dd768f399a36329314da82e90ddce6a4`
- implementation target：`f7ef18c080533e81ea4e6b412007424696fa7423`
- reviewed evidence candidate：`69eaa206338268ba782dc64655e00ba4df1e88ec`
- 复审开始时 worktree：clean
- reviewer nonce：`c3aea4140990b3806f99b80eb9c191b9e222fa1051627fbb1905b1cb398ea0eb`

候选、verified baseline 与 implementation/evidence split 均可定位。candidate verifier 对 implementation target 自报 PASS；这不能抵消下述由清单全量交叉核对、built 入口和真实进程探针发现的失败。

## 3. Findings

### P0-01：target custody map 是轮转生成的伪五跳映射

- `execution-02-target-custody-map.jsonl` 有 1,481 条记录、70 个 target symbol，但只有 3 个 unique success test、3 个 unique failure test 和 3 个 unique state/effect assertion。
- 1,481 条映射共引用 48 个 mutation；其中 479 条映射引用的 mutation `target_path/target_symbol` 与该映射自己的 target 不一致。这样的 mutation 即使被杀死，也不能证明当前 source → target 映射的断开即失败。
- 去除 `#n/N` 分片后共有 877 个 base source symbols，其中 157 个被分配到多个 target；`src/services/mcp/client.ts::connectToServer` 被分配到 30 个 target，表现为按序号轮转而非语义裁决。
- generator 中的 route 选择是 `routes[ordinal % routes.length]`，`source_behavior_claim` 也只是统一模板。它没有证明 source range 与 target behavior 的语义对应关系。

因此 schema-v3 source symbol → target symbol → default callsite → state/effect → behavior test 五跳链不能接受。当前 verifier 没有拒绝 mutation-target mismatch、低基数测试循环或非语义 route 分配，造成假 PASS。

### P0-02：真实 built 默认 MCP 路径在第一调用及重启重放上均失败

reviewer 通过 `node dist/code-worker-node/main.js --e02-api` 启动默认 built 入口，并接入真实 stdio MCP child。server 完成 initialize 和 tools/list 后，首次 projected tool 调用失败：

1. `McpClientRuntime` 把含 `interactive`、`sealedAutonomous` 等 boolean 的 policy identity 直接传给 durable request journal；journal 把这些字段当必需 string 验证，返回 `invalid_request_identity: interactive is required`。
2. 纠正身份边界后，journal 又把 coordinator placeholder `method="dynamic"` 持久化，而不是物理 JSON-RPC 方法 `tools/call`。
3. 纠正物理方法后，同 request/tool-call 在 graceful restart 后没有先重放 committed execution，而是再次 authorization，触发 `idempotency_payload_mismatch`。

这些不是测试夹具问题：真实 local stdio child 已被启动、发现并投影，失败位于 E02 默认 coordinator → MCP client → durable journal 主路径。候选不能完成 MCP live tool 与 exact replay。

### P0-03：lost-ACK 候选在外部副作用前没有 durable fence

reviewer 用真实 MCP tool 写入进程外计数文件，在外部 effect 已经变为 1、但 owner 尚未返回 receipt 时杀死默认 built E02 进程。原候选恢复出的 execution ledger 与 transition journal 为空，说明 permit/transition/effect-start 只在内存中；同一请求可被当成新调用再次执行。

这违反任务书要求的单 effect、lost-ACK、restore-before-bootstrap 与 deterministic recovery。静态 checkpoint JSON、同进程 replay 和两个内存 journal 对象不能替代 OS process kill/restart。

### P0-04：built health/cleanroom 用 E01 prerequisite 冒充 E02 完成

`node dist/code-worker-node/main.js --health` 返回：

- `defaultCapabilityEntrypoint="E02CapabilityCoordinator.execute"`；但
- `productizedRuntime.verificationStatus="independent_review_passed"`、implementation/evidence/review commits 全部来自 E01；
- `metadataSource="docs/reviews/evidence/M1-R01-v3/execution-02/e01-verified-prerequisite.json"`。

cleanroom 只检查通用 `productizedRuntime.complete=true` 和 `evidenceIntegrity=true`，没有验证 E02 candidate、E02 review status 或 E02 live probe。因此 E01 prerequisite 的 PASS 被错误提升为 E02 cleanroom PASS，构成 completion false positive。

### P1-01：reviewer probe 脚本没有执行它声称的默认 built 行为

- `runtime-origin` 直接 import source `E02CapabilityCoordinator`，再输出声称 built default entrypoint 的字符串。
- `resume` 虽 fork 子进程，但子进程仍直接 open source coordinator；没有走构建后的 code-worker API/CLI/worker。
- `lost-ack` 只创建两个内存 `McpRequestJournal`，没有真实外部 effect、kill、restart 或 ACK loss。
- `disable` 同样直接打开 source coordinator。

因此原 `probe_m1_r01_e02.ts` 产生的布尔字段不能作为任务书要求的 built default、same-session multi-PID、lost-ACK 或 fail-closed 证据。

### P1-02：candidate gate 缺少候选绑定与动态可达性拒绝条件

- verifier 没有验证 `candidate_head_at_g0` 与被审 implementation target 的精确关系，也没有对 implementation/evidence/control-plane commit 做允许集裁决。
- `restore_before_bootstrap` 等关键语义主要信任 evidence JSON；default callsite 只检查声明存在，不证明从 built entry 动态抵达；state effect 主要依赖测试标题。
- cleanroom 对 evidence HEAD 执行时只因 archive 内 target mismatch 失败，对 implementation target 才通过；执行命令没有强制显式 target，使结果依赖当前 HEAD。

gate 因而能接受 custody mismatch、伪 probe 与 E01 health 冒充 E02 完成。

## 4. Source-language 五跳抽样与扩展

reviewer nonce 抽样命中 mapping/mutation target 不一致后，按 bucket expansion 规则扩展到 1,481 条全量交叉核对。结果：

| 指标 | 结果 |
|---|---:|
| custody mappings | 1,481 |
| unique target symbols | 70 |
| mutation records | 48 |
| mutation-target mismatches | 479 |
| unique success/failure/state assertions | 3 / 3 / 3 |
| base source symbols | 877 |
| split across multiple targets | 157 |
| max targets from one base source | 30 |

因断开即失败链在 479 条记录上引用错误 target，且全量映射以三组 generic assertion 循环，不能给出独立可接受的 schema-v3 custody coverage。该结论不否认 target TypeScript 代码存在；它否认当前 manifest 对这些代码与 Claude source 的五跳归属证明。

## 5. Runtime origin、write path 与恢复探针

- permission built default nonce 测试可跨两个 PID 恢复 permit，且没有 Python fallback；该局部路径通过。
- live MCP 默认路径在身份/journal/replay上失败，故 MCP runtime origin 与 durable write path 不通过。
- lost-ACK kill/restart 恢复出空 execution/transition state，故 effect fence 与 recovery write path 不通过。
- built health 返回 E01 metadata，故不能证明 E02 自身 readiness。
- 原 probe 的 source import/内存对象不能补偿上述 built default 失败。

## 6. Scope isolation 与 cleanroom

本轮以 `f07fd239..69eaa206` 为候选范围，并将 implementation `f7ef18c` 与 evidence `69eaa206` 分开核对。Zyra worktree 在审查开始时 clean；根目录 manifests 不属于 Zyra Git，按 receipt hash 与磁盘内容单独核查。

source-free archive/build/test cleanroom 本身可以证明依赖与构建隔离，但它没有运行真实 E02 built probe，且 health predicate 绑定到 E01 prerequisite。因此 cleanroom 不能关闭 E02 completion gate。

## 7. 测试可信度与升级验证

候选已有 TypeScript suite、48/48 mutation 自报和 cleanroom receipt，但三者均没有杀死上述真实默认路径缺陷。由于命中默认 MCP、canonical journal、跨进程 restore/idempotency 与 candidate gate 高风险触发，本轮升级执行了：

- target map/mutation 全量交叉核对；
- 默认 built E02 API + 真实 stdio MCP server；
- 多 PID permission permit 恢复；
- 真实进程外 effect + kill/restart lost-ACK；
- built health provenance 与 cleanroom predicate 复核；
- implementation/evidence target binding 复核。

## 8. 状态迁移

- 原 implementation/evidence candidate `f7ef18c...` / `69eaa206...`：**failed candidate**
- verified head：保持 `f07fd239...`
- 用户已授权直接修复；修复完成后只能形成新的 `implementation_complete_review_pending` candidate
- E03：继续 `blocked`
- next entry：仍为 E02 独立复审，不得进入 E03

## 9. Review evidence commit

本文件只记录原候选 FAIL。后续生产、测试、manifest、probe、cleanroom 与 self-review 修复不得改写本裁决；它们属于新候选，必须由新的独立窗口审查。由于本窗口参与修复，本窗口不会对修复后候选签发独立 PASS。
