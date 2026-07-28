# M3-02 四 slice 独立聚合审查（2026-07-28）

## 1. 结论

审查结论：`PASS_AFTER_FIXES`。

本次按数字阶段聚合门禁审查以下四个 slice：

- `M3-S02A-01`：默认路径、安全与回归硬化；
- `M3-S02A-02`：两域 live benchmark、消融、统计与 100 分证据；
- `M3-S02B-01`：device/edge/cloud 部署、doctor 与 semantic health；
- `M3-S02B-02`：确定性发布包、锁、SBOM、clean install 与发布 admission。

数字阶段基线为 `42f760229588aba917c7283b841587afc7142b0b`，最终发布与聚合审查
目标为 `e87eeb37a915b38f1aef85f420010c594c9bad6d`。此前 slice 的历史实现事实保持
不变；本报告记录跨 unit 聚合复验、B02 正式长跑发现及修复后的精确结果。

M3-02A、M3-02B 和 M3-02 数字阶段均完成。M3 里程碑退出尚未完成，下一入口是
`docs/milestones/M3-freeze-productization/slice-03-01-evidence-report-archive-generation.md`。

## 2. 跨 unit 主路径闭环

M3-02 的默认主路径已经形成单一可验证链：

```text
M3-02A exact-commit live benchmark
  -> two domains / seven variants / three repetitions
  -> 42 admitted cells + 1,554 raw samples
  -> 100-point evidence + protected provider/deployment facts
  -> M3-02B benchmark link
  -> deterministic archive + wheel + SBOM/NOTICE/config
  -> 13-gate release admission
  -> isolated clean install
  -> installed API/Web/device/edge/cloud lifecycle
  -> sealed semantic health
  -> stop + uninstall
```

M3-02A 的当前证据指针仍绑定
`95fcf7aeaed5b5ec80fb2f7178b97fbdf8adbeb6`：2 domains、7 variants、3 repetitions、
42/42 cells、6 fresh source runs、1,554 raw samples、每 cell 2,310–4,003 个有效
transitions、0 human/operator intervention、100/100。M3-02B 没有复制这些事实，而是
用 benchmark-link 将其 digest 和 source commit 绑定到新的 release commit。

## 3. 聚合审查发现与修复

正式长跑没有沿用聚焦测试的乐观结论，而是暴露并关闭五项跨模块问题：

1. browser-use 成功 teardown 可能无限等待；新增 exact-PID process-tree deadline。
2. source scan 遍历 runtime/generated 目录导致聚合审计失控；改为 exact Git 文件集，
   fallback 也按目录剪枝。
3. Python release gate 缺单测试上限；新增 300 秒 faulthandler fail-closed。
4. Windows `USERPROFILE` 的伪造或删除分别破坏 Chrome 与 `Path.home()`；最终保留真实
   账户 identity，但隔离 HOME、temp、XDG、npm/Bun cache 和 app-data。
5. deployment doctor 将 `tmp` 测试 fixture 当成 source package；排除 runtime tmp，
   同时 mutation 证明正式 `packages/**` 违规依赖仍会阻断。

这些修复均在最终目标提交前完成；最终发布 pipeline 没有依赖忽略失败、扩大 fallback
或放宽 mandatory gate。

## 4. 正式验证与 cleanroom

最终 release pipeline：

- release ID：`M3-S02B-02-final-e87eeb3`；
- 13/13 mandatory gates passed；
- failed 0，blocked 0，required failures 0；
- Python governed release suite：
  `886 passed, 22 deselected, 5 warnings, 68 subtests passed`；
- TypeScript typecheck 与 Web production build：PASS；
- Bun 全量：`1268 passed, 0 failed, 1489 expect() calls`；
- `scripts/verify_m3.py`：两层 PASS；
- source custody、submission boundary、checksums、SBOM/NOTICE、benchmark link：
  全部 PASS。

归档：

- `M3-S02B-02-final-e87eeb3.tar.gz`；
- 12,929,493 bytes；
- SHA-256：
  `c18ed2ff1853faf71c69c6f0f02b722cbd04d0a5d19c4042ea460ef9234714f6`；
- 重复构建 byte-identical，size delta 0；
- wheel SHA-256：
  `222a65dfa29f86b220f8b62a954c5485fb4e57e4113ccfedd214ddda4f9ec3aa`。

cleanroom 从归档解包，安装 113 个 hash-locked Python 包和 26 个 frozen Bun 包，
安装 wheel，完成 TypeScript/Web/Bun/Node 构建、migration、activate、真实产品启动、
semantic health、stop 和 uninstall。它明确报告：

- `workspace_isolated=true`；
- `parent_source_repositories_present=false`；
- `product_lifecycle_exercised=true`；
- clean-install admission ready；
- semantic health ready；
- remaining active process 为 0。

semantic runtime 因无 cloud credential 为 degraded，但没有 blocker。provider-required
路径仍 fail-closed；device/edge/cloud lifecycle、sealed short task 和 checkpoint/failure
recovery 为 ready。

## 5. 有效代码聚合

四个 slice 的保守有效 production：

- `M3-S02A-01`：8,561；
- `M3-S02A-02`：8,594；
- `M3-S02B-01`：9,579；
- `M3-S02B-02`：9,285。

父级结果：

- M3-02A：17,155 / minimum 12,000 / margin 5,155；
- M3-02B：18,864 / minimum 12,000 / margin 6,864；
- M3-02 合计：36,019 / minimum 24,000 / margin 12,019。

tests、docs、evidence、lock data、generated report/bundle、audit runner、vendor/source-pool
和 adapter-only 均未计入有效 production。B02 的 11 个大文件逐项做 cohesion 审查，
没有用单一薄 adapter 调用不可审计上游黑箱。

## 6. 来源、状态 owner 与前向边界

- source-custody release ready，blocking item 0；
- Python parse error 0，JavaScript lex error 0；
- broad LangGraph forbidden hit 0，opaque runtime hit 0；
- bundle secret file 0，undeclared native artifact 0；
- cleanroom 不存在父级来源仓库运行依赖；
- canonical owner transfer 0；
- OpenClaw 保持 `excluded_forward_only`；
- LangGraph 保持 checkpoint/exact-resume 窄域 conformance；
- permission、scheduler、recovery、compact、release admission 等关键裁决仍由确定性
  Zyra-owned 状态机执行，不由 LLM 代替。

## 7. 批判性剩余项

以下不是 M3-02 blocker，但不能被省略：

- Python release policy 有 22 个显式 deselect；本报告不声称未经策略筛选的
  raw `pytest tests` 全绿。
- 5 个 warning 包含 3 个 `TestEntry` collection warning 和 2 个 Windows Proactor
  closed-pipe warning。最终 lifecycle 已证明无残留进程，但测试卫生仍应在 M3-03/退出
  审查收束。
- clean install 是 online proof，`offline=false`；没有把它写成离线 wheelhouse。
- 无 cloud credential 时 health 为 ready/degraded，正式 provider/model 事实依赖已保护
  证据绑定，不伪造当前 provider 请求。
- M3-02 完成不等于 M3 milestone exit。证据报告归档、最终材料、性能/交付和退出审查
  仍需后续 slice 完成。

机器可读摘要：
`docs/reviews/evidence/M3-02-independent-review/aggregate-review-evidence.json`。
