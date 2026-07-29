# P2-S00-01 第一阶段冻结接收增量自审

日期：2026-07-29
结论：`PASS`
切片：`P2-S00-01`

## 1. 冻结身份

- slice 入口提交：`57c0def91a69c851c941e7d4df80519cf881a8c7`
- reconciliation implementation 提交：
  `4bd1b3f4a58589287c0f1490c55dced40e56ca8c`
- 规范化修正提交：
  `e207b46ca690171139a718b8b85d808cb5a79c1e`
- 唯一 `P2_BASE_COMMIT`：
  `e207b46ca690171139a718b8b85d808cb5a79c1e`
- P2 base tree：
  `f5f0b018cd98fa65516b56d92cecf5593be3f702`
- baseline manifest digest：
  `85fa3a4a05d3ce7906e7af8507b3e413b26a5c53042bf0073d8f3039b9e5db37`
- manifest verification digest：
  `bf6e279208b7bd1a3f1d48d4da42d9ce99af8d081af97a95d01fd8470de31ec8`

第一阶段 benchmark implementation、benchmark evidence、report
implementation/evidence、final-freeze evidence 和最终报告均经 Git ancestor
约束绑定到上述 P2 base；没有回写或重跑第一阶段冻结结果。

## 2. 实现审查

本切片新增 `Phase2BaselineVerifier` 和 release CLI 入口。验证器重算：

- manifest 自身摘要；
- P2 commit、tree 和 lineage ancestor；
- 24 个只读引用的路径、文件大小和 SHA-256；
- 正式 benchmark、cleanroom、release 和第一阶段 evidence index 的
  JSON pointer commit 绑定；
- evidence inventory 完整性、只读语义和
  `training_sample_count=0` 禁训练边界；
- symlink、目录逃逸、脏工作树和缺失引用的 fail-closed 门禁。

release evidence linker 改为读取 `formal-current.json` 权威指针，并核对
report implementation commit；当前 release link 因此绑定
`09e99cdc...` campaign 和 DeepSeek、Kimi、GLM 三个当前 provider，而不再
回落到旧 protected provider 样例。clean-install 现在强制使用一次性 Pip、
uv、Bun cache，并禁用用户 site package。

未发现第二套状态 owner、外部 source runtime 依赖或把外部机制提升为
canonical writer 的变更。`phase2_strongest_v1` 只冻结为
`baseline_frozen_not_activated`，不声称后续机制 readiness 已通过。

## 3. 真实验证

### 3.1 基线对账与 mutation

```text
python scripts/release/verify_phase2_baseline.py \
  --manifest docs/release/phase2-baseline-manifest.json
valid=true, reference_count=24, finding_count=0
```

`tests/integration/test_evidence_index_reconciliation.py` 在真实临时 Git
repository 中重算引用和 commit lineage。聚焦测试覆盖：

- 正常 manifest 与全部摘要/commit 绑定通过；
- 故意改变被引用文件但保留旧 digest，验证失败；
- 故意提供不匹配 evidence commit，验证失败。

### 3.2 release 与 clean install

从干净 P2 base 构建两份 byte-identical 发布包，结果：

- archive SHA-256：
  `dde7e0994059617e160fcba795e722ba6bd126055ce30808a1f8eec937eab0a3`
- release manifest digest：
  `e3b341979cc0fb0b7e8a22af872f912ef6519f3cdd2645e837cfe551a4b16d37`
- payload digest：
  `b595692ccb7511520bbabe8ebd07d3bd15de7d186dbe8ef047f6247843c0f35e`
- wheel、checksums、SBOM、NOTICE、runtime inventory 和 configuration
  独立验证通过。

clean-install 在系统临时目录和空缓存中完成 113 个 hash-locked Python
包、26 个 frozen-lock Bun 包安装，执行 wheel import、全量 TypeScript
typecheck、Bun/Node code-worker build、Web build、install、短任务、
stop 和 uninstall。父级 source repositories 不在 cleanroom 内；运行结束后
删除一次性缓存，再次验证同一 archive/manifest/payload digest 仍通过。

第一次 sandbox 尝试因禁止网络 socket 而失败，没有把该次尝试记录为成功；
获准的网络安装完成后才形成 `ready=true` 回执。

### 3.3 测试与账本

```text
pytest test_evidence_index_reconciliation.py
       test_release_bundle_clean_install.py
       test_release_productization.py
49 passed in 8.11s

verify_internalization_ledger.py --base 57c0def91a69 --json
ok=true, blockers=0, errors=0, effective_added=1034
```

ledger 的 734 个既有 warning 分别为 432 个未 materialize 的 planned
target、299 个 license notice 提醒、2 个 target owner 冲突提醒和 1 个
execution-unit coverage 提醒；本切片不改写这批第一阶段 ledger 债务，
也没有 blocker 或 error。

## 4. 路径、缓存和 credential 审计

- submission boundary verifier：通过；
- dependency configuration 的 absolute/parent source path 命中：`0`；
- editable Pip / npm link：`0`；
- external Docker context：`0`；
- runtime parent-source dependency：`0`；
- credential material 命中：`0`；
- non-test 的 18 个 path 命中均为边界/发布检测器字面量；
- non-test 的 3 个 credential 命中均为随机 lease token 生成器或
  private-key 检测器字面量；
- cleanroom cache roots 在运行后均已删除。

## 5. 当前 diff 分桶

相对 slice 入口 `57c0def...`：

| 分桶 | 路径/规模 | 结论 |
| --- | --- | --- |
| production | release verifier/linker/cache isolation，`748` added、`6` deleted | 计实现 |
| scripts | baseline CLI，`64` added | 计实现 |
| test | 两个 integration 文件，`222` added | 计测试 |
| docs | decision 与本自审 | 不计 production |
| data | baseline manifest 与结构化 evidence receipts | 不计 production |
| generated | release archive 和原始临时回执仅留在 ignored `dist/.tmp` | 不提交、不计实现 |
| fixture | `0` | 无固定成功轨迹 |
| adapter-only | `0` | 无新增 adapter-only 体量 |
| vendor-like/runtime-assets | `0` | 无迁移外部实现 |

internalization line accounting 对 source/test/scripts 计得
`effective_added=1034`；manifest、报告、release archive、SBOM 和日志不计为
生产实现。

## 6. 双仓边界与残余

根目录 `docs/milestones/execution-state.yaml` 和
`docs/比赛要求追踪矩阵.md` 不属于 Zyra Git。其切片执行中快照已保存为
`root-planning-snapshot.json`；本 evidence commit 完成后才在根目录回写
最终 evidence commit 和完成状态，根文件的新摘要不会伪装成 Zyra commit
内容。

本切片未重复受保护的 13-gate release CI；它继承第一阶段冻结证据，同时
当前执行了直接相关的双构建、独立 bundle verification、完整 clean install、
生命周期和 49 项相邻回归。clean install 仍需要访问锁定依赖源，在完全离线
环境下需使用第一阶段已经记录的 offline-wheelhouse hardening residual；
这不改变当前 hash-locked、空缓存、无来源仓库依赖的通过结论。

未发现阻止 `P2-S00-02` 的差异。后继必须消费同一
`P2_BASE_COMMIT` 与 baseline manifest，不得改写本切片冻结身份。
