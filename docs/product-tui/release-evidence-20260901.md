# 产品 CLI / TUI Windows 发布证据（2026-09-01）

## 结论

提交 `3e66b97709f3d8e1f00a6c87c52de6405412d9ab` 的 Windows 发布候选已经完成可重复构建和真实隔离安装闭环。该证据关闭产品 TUI 的 build/install、state migration、lifecycle 和 uninstall 发布门；它不替代后续提交的回归，也不证明 Agent 的比赛任务成功率。

## 可重复归档

- release id：`product-tui-candidate-20260901-v2`
- archive：`.tmp/product-tui-release-v3/product-tui-candidate-20260901-v2.zip`
- SHA-256：`507ba2b356d5927f54f1aeb566bcd53e867359004f7b48c80bfc50f81fc92a54`
- size：47,090,499 bytes
- file count：4,841
- source commit：`3e66b97709f3d8e1f00a6c87c52de6405412d9ab`
- 两次独立 build 的归档字节完全一致。
- `scripts/verify_product_entry_release.py` 同时验证 Node 和 Bun 产品入口、完整命令清单和两次 CLI build 一致性。

构建入口：

```powershell
.\.venv\Scripts\python.exe .\scripts\run_release_pipeline.py `
  --release-id product-tui-candidate-20260901-v2 `
  --expected-commit 3e66b97709f3d8e1f00a6c87c52de6405412d9ab `
  --benchmark-commit 09e99cdc5ed9cf3a935ccc327f7261110e6c7d1b `
  --output-root .tmp\product-tui-release-v3 `
  --format zip `
  --skip-ci
```

机器可读报告位于 `.tmp/product-tui-release-v3/pipeline-report.json`。`.tmp` 是本地证据目录，不进入 Git；可由上述命令重建。

## 隔离安装与生命周期

精确归档在新建的系统临时目录和全新 Python venv 中执行：

```powershell
.\.venv\Scripts\python.exe -m zyra_productization.release.cli `
  --project-root G:\agent-zoo\zyra `
  clean-install .tmp\product-tui-release-v3\product-tui-candidate-20260901-v2.zip `
  --expected-commit 3e66b97709f3d8e1f00a6c87c52de6405412d9ab `
  --output .tmp\product-tui-clean-install-v3.json
```

最终 receipt：

- schema：`zyra.clean-install-receipt/v1`
- ready：`true`
- duration：1,825,783ms
- workspace isolated：`true`
- parent source repositories present：`false`
- product lifecycle exercised：`true`
- dependency install：113 个带 hash 约束的 Python 包；Bun 使用 frozen lockfile
- install transaction：`committed`
- schema migration：v0 → v1，reversible
- lifecycle：start、semantic health、stop 均通过
- uninstall transaction：`uninstalled`
- lifecycle 后端口全部释放
- receipt digest：`e9f9ef270e7bec4c774b0bdad3480af855efb5a41237b34f08c777871df3a645`

机器可读 receipt 位于 `.tmp/product-tui-clean-install-v3.json`。由于它包含临时机路径和约 680KiB 的逐命令输出，不将其复制进源码；发布判定使用 archive hash、source commit 和 receipt digest 三重绑定。

## 迁移与回退边界

- 发布安装使用 transaction id、idempotency key、manifest digest 和 migration version 绑定。
- migration registry 支持显式 `migrate` 和 `rollback`，clean-install 已实际执行 v0 → v1。
- 产品 TUI 本地草稿另有 `zyra.product-draft/v0` → `v1` 的延迟迁移；未知、超限或损坏 schema 不覆盖原文件，降级为空草稿并显示警告。
- 这份证据验证 Windows amd64。Linux/macOS 仍按任务书标记为未实机验证，不得由本结果外推。
