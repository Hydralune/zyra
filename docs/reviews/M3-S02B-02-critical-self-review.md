# M3-S02B-02 增量批判式自审

## 1. 结论

结论：`PASS`。

- baseline：`29fa59132c5c84f230e2ff3b94c75df510a4c20d`
- 实现前裁决：`1774cd21fa430f0d626987a80dabc62d51ff9048`
- 最终实现目标：`e87eeb37a915b38f1aef85f420010c594c9bad6d`
- release ID：`M3-S02B-02-final-e87eeb3`
- 下一入口：`M3-S03-01`

本 slice 完成确定性发布包、Python wheel、锁定依赖、SBOM/NOTICE、配置样例、
安装事务、迁移/回滚/卸载、跨平台 clean-install、发布 CI admission、证据绑定和
installed product lifecycle。它不转移 task、event、permission、memory、scheduler、
artifact、checkpoint、provider 或 deployment canonical owner。

## 2. 默认主路径与断开即失败

默认发布路径为：

```text
exact clean Git commit
  -> deterministic archive + wheel
  -> lock / boundary / source-custody / checksum / SBOM gates
  -> isolated clean install
  -> migration + activate
  -> installed API/Web/device/edge/cloud lifecycle
  -> sealed semantic health
  -> stop + uninstall
  -> exact 13-receipt release admission
```

以下任一条件都会使发布 admission fail-closed：

- Git target、manifest、payload、archive、wheel RECORD 或 checksum 不一致；
- Python/Bun lock 漂移、缺 hash、未声明依赖或构建边界出现根目录来源路径；
- secret、未声明 native artifact、opaque runtime 或来源仓库进程进入发布包；
- 安装事务、迁移、activate、rollback、uninstall 或幂等键不一致；
- cleanroom 不是隔离工作区、检测到父级来源仓库，或产品生命周期未执行；
- API/Web/三 profile、sealed short task、checkpoint/failure recovery 或 semantic
  health 出现 blocker；
- 13 个 mandatory gate 有缺失、重复、失败、阻断或证据文件缺失。

## 3. 执行中发现并关闭的缺陷

本 slice 没有把首次实现后的绿色聚焦测试当成最终完成。正式发布长跑暴露并关闭了以下问题：

1. 发布 CI 将 `USERPROFILE` 指向隔离目录时，Chrome 拒绝 remote debugging；完全删除
   `USERPROFILE` 又会使 Windows `Path.home()` 失败。最终保留真实 Windows 账户身份，
   同时隔离 `HOME`、`TEMP/TMP`、XDG、npm 和 Bun cache，并移除 APPDATA/LOCALAPPDATA。
2. browser-use 正常关闭可能在异步 teardown 中无限等待。新增 exact-PID process-tree
   deadline，成功路径仍先执行正常 close，超时路径才强制终止。
3. ledger source scan 曾递归遍历 `.tmp`、`tmp`、`dist` 和缓存，使聚合门禁耗时失控。
   最终改为受 exact repository root 约束的 `git ls-files`，并保留有界、剪枝的 fallback。
4. Python gate 缺少单测试失效保护。最终加入 300 秒 faulthandler timeout 和
   fail-closed exit，避免单个测试吞掉整个 5,400 秒 gate。
5. deployment doctor 只排除了 `.tmp`，没有排除 `tmp`，将测试故意生成的违规
   `package.json` fixture 误报为正式源码依赖。最终排除 runtime `tmp`，同时新增 mutation
   证明 `packages/**` 中同样的违规依赖仍被阻断。

修复后，正式顺序的三个浏览器 live 用例加部署产品用例为 `4 passed`，相邻 release、
deployment 单元/集成为 `53 passed`。

## 4. 正式发布证据

最终命令固定到 `e87eeb37a915b38f1aef85f420010c594c9bad6d`，工作树为 clean。

- mandatory gates：`13/13 passed`；
- failed / blocked：`0 / 0`；
- Python：`886 passed, 22 deselected, 5 warnings, 68 subtests passed`；
- TypeScript typecheck：PASS；
- Web production build：PASS，370 modules；
- source custody：release ready，0 blocker；
- submission boundary：PASS；
- clean install：PASS；
- semantic health：PASS；
- `scripts/verify_m3.py`：`M3 runtime verification passed` 与
  `M3 verification passed`；
- Bun 全量：`1268 passed, 0 failed, 1489 expect() calls`。

发布包结果：

- archive：`M3-S02B-02-final-e87eeb3.tar.gz`；
- size：`12,929,493` bytes；
- SHA-256：`c18ed2ff1853faf71c69c6f0f02b722cbd04d0a5d19c4042ea460ef9234714f6`；
- 两次独立构建的 archive byte-identical，size delta 为 0；
- wheel：`zyra-0.1.0-py3-none-any.whl`；
- wheel SHA-256：
  `222a65dfa29f86b220f8b62a954c5485fb4e57e4113ccfedd214ddda4f9ec3aa`。

cleanroom 从归档提取，安装 113 个 hash-locked Python 包、26 个 frozen Bun 包，
安装 wheel，执行 TypeScript typecheck、Bun/Node code-worker build、Web build、
submission boundary、release doctor、product start、semantic health、stop 和 uninstall。
报告明确 `workspace_isolated=true`、`parent_source_repositories_present=false`、
`product_lifecycle_exercised=true`。

## 5. 有效代码分桶

审计区间：

`1774cd21fa430f0d626987a80dabc62d51ff9048..e87eeb37a915b38f1aef85f420010c594c9bad6d`

保守 gate：

- raw additions：`17,582`；
- 计入 slice minimum 的有效 production：`9,285`；
- minimum：`6,000`；
- margin：`3,285`；
- tests/mock/fixture：`1,765`，不计；
- nonproduction audit/runner：`478`，不计；
- schema/DTO/data：`910`，不计；
- type declaration：`141`，不计；
- comments/docs/blank：`582`，不计；
- requirements lock、生成 bundle/SBOM/report、CI YAML、docs、tests 和 runners 不计。

11 个大文件均进入 cohesion 审查。有效代码分别承担 release boundary/integrity、
inventory/SBOM、bundle/wheel、transaction/migration、clean install/lifecycle、
admission/control surface 和 submission/evidence binding；不存在 vendor/source-pool、
generated 或 adapter-only 行数冒充。

## 6. 来源、状态与安全边界

- source-custody candidate scan：Python parse error 0、JavaScript lex error 0、
  broad LangGraph forbidden hit 0、opaque runtime hit 0、blocking work item 0；
- bundle boundary：blocker 0、secret file 0、undeclared native artifact 0；
- clean install 不依赖 `../claude-code-best`、`../browser-use`、`../OpenHands`、
  `../opencode` 或其它工作区来源仓库；
- OpenClaw 继续为 `excluded_forward_only`；
- LangGraph 继续只承担 checkpoint/exact-resume 窄域 conformance；
- 发布层只打包、安装、编排和验证既有 owner，不创建第二 canonical owner；
- semantic health 在无 cloud credential 时为 `ready=true/status=degraded`，
  provider-required dispatch 保持 fail-closed，没有伪造凭据或付费 provider 成功。

## 7. 剩余非阻断边界

- 正式 Python release policy 有 22 个显式 deselect；本报告不把它表述为未经策略筛选的
  原始 `pytest tests` 全绿。最终 governed release suite 为 886 PASS。
- 5 个 warning 包含 3 个 `TestEntry` dataclass collection warning 和 2 个 Windows
  Proactor closed-pipe warning。cleanroom 最终 stop receipt 显示 remaining active process
  为 0；这些 warning 仍应在 M3-03/退出冻结测试卫生中清理。
- clean install 使用网络获取锁定依赖，`offline=false`。它证明 online clean install，
  不冒充完整离线 wheelhouse。
- cloud credential 缺失是明确 degraded 状态；正式 provider/model 证据继续由已保护的
  M1/M3-02A 证据绑定承担。
- 本 slice 与 M3-02 数字阶段完成不等于 M3 里程碑退出。证据报告归档、最终提交材料和
  里程碑退出审查仍从 `M3-S03-01` 继续。
- 根目录 `docs/milestones/execution-state.yaml` 不属于 Zyra Git 仓库，只能在 evidence
  commit 冻结后单独更新。
