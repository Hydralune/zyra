# P2-S06-02 封闭对抗长程运行增量自审

## 结论

P2-S06-02 已达到 slice 的代码、真实运行、独立验证和证据闭环要求。
权威成功批次为 `P2-S06-02-attempt-08`：两个跨领域 run 均通过，
失败 run 为 0，人工干预为 0。独立验证器从原始 canonical events
重新计算得到：

| run | domain | 有效迁移 | 无效迁移 | blocker | final verifier |
| --- | --- | ---: | ---: | ---: | --- |
| `software-sdk` | `software_delivery` | 2,295 | 0 | 0 | passed |
| `technical-intelligence` | `cross_source_research` | 7,168 | 0 | 0 | passed |

独立验证摘要为
`051b3e6de622a17c15e3d08573363f13b4f4c79ffc88d447a17b5c55c3cce084`。
验证器不信任 runner 声明的迁移数，排除 heartbeat、log、replay、
UI repaint 和 no-op，并重建事件因果、语义唯一性及 before/after 状态哈希链。

## 冻结边界

- slice：`P2-S06-02`
- base commit：`3d766628caee0fcff9bffb5789b27a756d037593`
- candidate commit：`28c0a1376c6d4226975e8fd60a1f088b75be3a42`
- manifest commit：`3c55f7895b4f0b8d6f570a04f5e97aaa1a8857f3`
- sealed manifest digest：
  `14441f1136a7e26f19c831031204d928999202f4fa31e23610c75e51aced4e31`
- runner index digest：
  `626d805a44f45da862cd0466e8b3fb3ec891afbb953279d2c86acbca9a1fdede`
- evidence index 文件摘要：
  `5022b4ac064024b78f7e70d17bdfae0f6fc838062aa5c1b34fbf039082879772`

manifest 冻结了输入、seed、预算、失败计划、策略、provider/model、
硬件/网络配置、独立验证器以及所有实际运行源码摘要。候选 commit 是
manifest commit 的祖先；正式运行前后未发现候选源码或配置变更。

## 真实运行与硬门

两个 run 均通过以下硬门：

- `human_intervention_count=0`，`early_exit_false_positive=0`。
- critical fact recall 与 obligation retention 均为 `1.0`。
- superseded requirement、无 provenance 检索、重复完成/提交/claim/
  spend/lease/side effect、隐私权限违规和 unsafe commit 均为 `0`。
- 每个 run 的 local、edge、cloud 三条 lane 均
  `real_gate_closed=true`、`simulated=false`、`semantic_only=false`。
- 两次 DeepSeek cloud dispatch 均为 HTTP 200，model 为
  `deepseek-v4-pro`；软件 run 成本 `0.000033495 USD`、延迟
  `1204 ms`，研究 run 成本 `0.0000348 USD`、延迟 `1590 ms`。
- edge 进程在 receipt commit 后被真实停止，并由 fenced local lane
  安全降级；artifact continuity 保持。
- compact/restore、process restart、handoff、requirement revision
  均有连续性 receipt；poisoned、stale、conflicting memory 均被拒绝。
- 同一 run 中真实完成 role/operator 的新增和删除，最终 mutation
  由 `GraphStateCustody` 提交。
- 三个 permission/privacy/pending-side-effect 对抗 proposal 全部被
  reject/project，unsafe commit 为 0。
- LoopX restart recovery、claim conflict、quota exhaustion fail-closed
  均通过，worker lease 与 execution budget owner 未被替代。
- memory continuity、symbolic projector、LoopX、dynamic topology、
  operator selection、physical dispatch 六项 disable evidence 均改变结果。

## 公开来源证据

研究 run 只访问冻结 allowlist 中的三个 HTTPS 主机，并在当前环境中使用
显式有界的 `198.18.0.0/15` egress proxy 映射。跨主机 redirect 会被拒绝，
literal proxy address 仍被禁止。三份新鲜来源均为 HTTP 200：

| 来源 | wire bytes | source digest |
| --- | ---: | --- |
| RFC 9110 | 139,493 | `21c1cdce6ab0e5509b04d84a28000836c7a087cf786efe6f04877ebfff47232a` |
| IANA HTTP Status Codes | 3,269 | `4a9550d4b4ae49cf41cf9050cf9b56b0d6082ad1edfc0e2b09b07f251a36d7a4` |
| MDN HTTP Status | 31,481 | `93450a73fbc03f06e6ac2b46d09915e4da599f52c60eae61aaa02413d27bbf92` |

正式证据保留 source metadata、规范化文本、原始 wire bytes、request
identity、acquisition time、checksum、citation 和最终 report。

## 失败批次保留

attempt-01 至 attempt-07 均未被覆盖或改写。每次失败都生成独立
evidence index 和 failure receipt；修复后使用新 candidate、新 manifest
和新 attempt ID：

1. collapsed evidence parent/source mutation guard；
2. sandboxed cloud provider failure；
3. physical receipt contract envelope 解包；
4. Windows path length 与有界 proxy CIDR；
5. non-canonical fault kind；
6. mechanism bundle `FrozenDict` JSON boundary；
7. hard-gate digest normalization 与 published report kind；
8. accepted。

统一索引位于
`docs/evidence/phase2/sealed/P2-S06-02-campaign-index.json`。

## 增量实现分类

### Production

- `long_run_validator.py`：独立迁移重算、因果/状态链/硬门/最终产物校验。
- `sealed_physical.py`：真实 ResourceScheduler、worker lease/fence、
  deployment process、local/edge/cloud dispatch 和 safe degradation。
- `sealed_mechanisms.py`：真实记忆连续性、动态图、神经符号、MaAS 和
  LoopX 对抗/disable evidence。
- `sealed_long_run.py`：冻结检查、隔离运行、失败保留、证据打包与
  candidate/manifest 绑定。
- `research_delivery.py`：冻结来源主机 allowlist 与 redirect 约束。
- `live_models.py`：只有在真实物理 boundary validation 关闭硬门时，
  edge/cloud loopback control endpoint 才可被接受。
- `run_phase2_sealed_scenarios.py`：正式双领域 CLI 入口。

### Test

- `test_phase2_sealed_long_runs.py` 覆盖独立 2,000-step 重算、伪造计数、
  contract envelope、bounded proxy/host allowlist、fault preflight、
  canonical JSON/digest、published artifact kind、disable/attack 路径。
- 目标与相邻回归最终为 `35 passed`。

### Runtime assets

无新增 vendor runtime、模型 checkpoint、训练数据或外部源码运行依赖。
既有 LoopX 完整 runtime 仅通过 Zyra-owned bridge 使用。

### Generated / evidence

正式提交只把原始 canonical event trail、transition index、hard gates、
mechanism/physical bundles、final artifacts/verifiers、独立验证报告、
物理 receipt/verifier 以及精确来源 bytes 计为证据。SQLite/WAL、进程
scratch、`__pycache__` 和重复发布副本属于 generated runtime state，
不计入 Zyra 实现体量。

### Data

只有 attempt-08 实时获取的三份公开来源 bytes；无训练 dataset、
benchmark fixture 或缓存回放。

### Docs

本自审与 campaign index 是 P2-S06-02 的增量说明和证据导航。

### Adapter-only

无新增第二套 agent runtime 或 canonical owner。sealed owner 只委托既有
`CanonicalLiveScenarioOwners`、`GraphStateCustody`、
`ResourceScheduler`、permission 和 lease owner。

### Mock / fixture

正式结果未使用 mock、固定成功 fixture、预录轨迹或残留缓存。
测试中的最小构造对象只验证 fail-closed contract，不作为正式证据。

## 验证命令

```text
python -m pytest tests/scenarios/test_phase2_sealed_long_runs.py
  tests/scenarios/test_m2_s05_02_live_scenarios.py
  tests/integration/test_memory_continuity_policy_runtime.py
  tests/integration/test_neuro_symbolic_projector.py
  tests/integration/test_loopx_control_commands.py
  tests/integration/test_physical_dispatch_receipts.py
  -q -p no:cacheprovider

python -m zyra_evaluation.policy_benchmark.long_run_validator
  --evidence docs/evidence/phase2/sealed/P2-S06-02-attempt-08/sealed-evidence-index.json
  --output docs/evidence/phase2/sealed/P2-S06-02-attempt-08/independent-validation.json
```

## 自审裁决与交接

当前 diff、正式证据和独立验证中无开放 P0/P1 blocker。
P2-S06-02 可以标记完成。下一候选为 `P2-S06-03`，但本记录不授权执行它。
