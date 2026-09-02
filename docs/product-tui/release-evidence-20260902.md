# 产品 CLI / TUI Windows 发布证据（2026-09-02）

## 判定

提交 `fedd24e0e7f9d54730cc950a92c0433155b4171c` 的 Windows 发布候选已完成全量 CLI/Web 回归、可重复归档和真实隔离 clean-install。build/install、state migration、产品生命周期、卸载和端口释放发布门均关闭。

这不等价于整份产品任务完成：Windows Terminal 人工 IME 候选窗仍未签字，因此 COMP-02、Phase F 和 Phase H 保持未完成。Linux/macOS 与官方 Codex 认证后参考序列也继续明确标为未运行。

## 产品语法重构后复验（`c0bde95b`）

Phase I/J 产品语法与结构化用户提问完成后，已在干净提交 `c0bde95b005d656e0fe5f1c8d21eb317368ca243` 上重新执行发布门，不沿用本页下方旧候选结果：

- 产品入口双构建一致：`zyra.js` 988,282 bytes，SHA-256 `ce630b5402b22b50e7e17b3da1d6e8acdd47de677fc0fc0032ca38aa48ab6365`；Node shebang、命令面、JSONL、退出码、依赖闭包和 Windows entry probe 全部通过。
- release id：`product-tui-candidate-20260902-c0bde95`；Windows zip 两次独立构建字节一致，47,303,323 bytes / 4,904 files，SHA-256 `d075a5a6814dbc918c386b7519ac23bfe9018f707540743892897f525ceb8425`，pipeline digest `3f964282d753ba346c077cca3d9e1ce90e058e89308f6023d1de9e46fb6ba862`。
- 隔离 clean-install：`ready=true`，source commit 与 archive hash 精确匹配；安装 113 个哈希锁定 Python 依赖和 frozen Bun lockfile，完成全仓 typecheck、CodeWorker/CLI/Web build、v0→v1 migration、产品 lifecycle/restart、uninstall 与 5 个端口释放；workspace isolated，editable/link 与隐式用户 cache/state 依赖均为 0。
- clean-install receipt：`.tmp/product-tui-clean-install-20260902-c0bde95.json`，duration 976,504.166 ms，文件 SHA-256 `dfc8cb12c495e790c5ee771a855f41c28bffb620846a838a7212297103657b98`。

首次 clean-install 尝试被受限网络阻止访问 PyPI；获准联网重跑后，Bun 发现仓库本地 release cache 中两个 `csstype` 生成缓存项损坏。仅删除这两个已验证位于 `.tmp/bun-release-cache` 的可再生成目录后，第三次使用同一 commit 和同一 archive 成功。没有修改源码、lockfile 或用户全局缓存。

此复验关闭了“重构后 release/clean-install”门，但不会替代真实 provider continuation、人工 Windows Terminal IME 或两名外部用户盲测。

## 当前源码回归

| 门 | 结果 |
|---|---|
| CLI test | 187 pass，0 fail，902 expect，17.55 s |
| Web test | 320 pass，0 fail，1916 expect，45.92 s |
| 全仓 typecheck | pass；runtime、memory、typed client、commands、CLI、Web 全部通过 |
| code-worker build | Bun/Node 各 275 modules，主产物约 5.73 MB |
| CLI build | pass，101 modules，约 0.94 MB |
| Web build | pass，385 modules，主 JS 约 5.56 MB |
| Phase G provider task | `task_141d76cf35ad` / `run_9aee0f813e80`，canonical `completed` |
| Phase G 产品标记门 | 两次恢复附着、500 resize；changed files、diff、`node --test / exit 0`、真实 `/exit` 全部通过 |
| 8 小时 soak | 已绑定 `2aed010b` 通过；详见 `phase-f-hardening-evidence-20260901.md`，未用短跑冒充重跑 |

CLI 全量回归已包含 daemon downtime/generation recovery、100 次 stream disconnect、100k event、10k transcript、权限 custody/race、path escape、ANSI/OSC、JSONL/退出码和历史 workspace payload 降级。Web 全量回归覆盖 canonical projection、CLI/Web 对账、permission、diff、artifact、topology、terminal 和 long-horizon workbench。

## 可重复归档

- release id：`product-tui-candidate-20260902-final`
- archive：`.tmp/product-tui-release-20260902-final/product-tui-candidate-20260902-final.zip`
- SHA-256：`ab3cb09b44eb0432fef1cff660d23e8ee4a83d320344a2798d56c7f57ff2f05e`
- size：47,214,053 bytes
- file count：4,874
- source commit：`fedd24e0e7f9d54730cc950a92c0433155b4171c`
- pipeline digest：`73759bc6117e53ca3a9359d1448e72d28c9f2b520eb60288ea232ff08b6da0f3`
- 两次独立 build 的归档字节完全一致，size delta 为 0
- Python wheel：`zyra-0.1.0-py3-none-any.whl`，2,243 entries，SHA-256 `c9d18bbd2072a45ed1837ff089aa5d916133e58cd14cf5b6d9b7ddd4ab197c6a`
- LoopX pinned embedded source、SBOM、checksums、runtime inventory、NOTICE 和 product entry verification 全部通过

构建命令：

```powershell
.\.venv\Scripts\python.exe .\scripts\run_release_pipeline.py `
  --release-id product-tui-candidate-20260902-final `
  --expected-commit fedd24e0e7f9d54730cc950a92c0433155b4171c `
  --benchmark-commit 09e99cdc5ed9cf3a935ccc327f7261110e6c7d1b `
  --output-root .tmp\product-tui-release-20260902-final `
  --format zip `
  --skip-ci
```

`--skip-ci` 只跳过 release pipeline 内部重复 CI；本节开头列出的全量 CLI/Web test、全仓 typecheck 和三类 build 已在同一干净提交上独立通过。机器可读报告位于 `.tmp/product-tui-release-20260902-final/pipeline-report.json`。

## 隔离安装与生命周期

精确归档在自动创建的系统临时目录与全新 Python venv 中执行：

```powershell
.\.venv\Scripts\python.exe -m zyra_productization.release.cli `
  --project-root G:\agent-zoo\zyra `
  clean-install .tmp\product-tui-release-20260902-final\product-tui-candidate-20260902-final.zip `
  --expected-commit fedd24e0e7f9d54730cc950a92c0433155b4171c `
  --output .tmp\product-tui-clean-install-20260902-final.json
```

最终 receipt：

- schema：`zyra.clean-install-receipt/v1`
- ready：`true`
- duration：452,346.924 ms
- workspace isolated：`true`
- parent source repositories present：`false`
- product lifecycle exercised：`true`
- dependency install：113 个带 hash 约束的 Python 包；Bun 1.2.15 使用 frozen lockfile
- install transaction：`committed`
- schema migration：v0 → v1，可逆
- lifecycle：submission boundary、doctor、start、semantic health、restart、post-restart health、stop、post-stop status 全部符合预期
- uninstall transaction：`uninstalled`
- 声明的 5 个生命周期端口在结束后全部释放；未声明端口/进程计数为 0
- editable/link install、外部 build context、用户 site 和隐式用户 cache/state 依赖均为 0
- receipt digest：`bdcc24be847c7c6c97d61f439bd22f60766b7204cd55232adde9ead6b52065e9`

机器可读 receipt 位于 `.tmp/product-tui-clean-install-20260902-final.json`。它包含临时机路径和逐命令输出，不复制进源码；发布判定使用 source commit、archive SHA-256 和 receipt digest 三重绑定。

## 已知限制和未运行项

- Windows Terminal 人工 IME 候选窗尚未执行；自动 Unicode、组合字符、emoji、paste burst 和 ConPTY 门不能替代实机输入法候选选择。
- 官方 Codex standalone `0.142.0` 当前 `codex login status` 为 `Not logged in`；登录页/终端生命周期已探测，认证后的 `/status`/`exit` 参考序列未执行。该项是参考证据限制，不阻塞 Zyra 运行。
- Linux/macOS 没有实机结果；当前发布证据只判定 Windows amd64。
- 本候选没有代码签名、系统 installer 或 npm publish；交付物是经 digest 验证的 Windows zip 与文档化生命周期命令。

## 最后人工关闭步骤

在真实 Windows Terminal 中运行：

```powershell
Set-Location G:\agent-zoo\zyra
.\scripts\product-tui\windows_ime_manual_gate.ps1
```

按脚本要求完成中文候选窗非首选词、组合态、emoji 和 exact echo 签字。该签字完成前，不得把 Phase F/H 或整份 `PRODUCT_TUI_TASK.zh-CN.md` 标记为完成。
