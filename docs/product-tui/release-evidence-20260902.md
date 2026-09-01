# 产品 CLI / TUI 当前发布回归证据（2026-09-02）

## 判定

当前源码的自动化、构建、Windows ConPTY、性能和真实 daemon restart 回归已通过；完整比赛级前端发布仍被真实 provider Phase G 覆盖和人工 Windows IME 验收阻塞。2026-09-01 的可重复归档与 clean-install 证据继续有效，但它绑定旧提交 `3e66b97709f3d8e1f00a6c87c52de6405412d9ab`，不能替代当前源码的最终发布归档。

## 当前回归

| 门 | 结果 |
|---|---|
| CLI test | 183 pass，0 fail，889 expect，17.44 s |
| Web test | 320 pass，0 fail，1916 expect，45.92 s |
| CLI typecheck | pass |
| Web typecheck | pass |
| CLI build | pass，101 modules，约 0.94 MB |
| Web build | pass，385 modules，主 JS 约 5.56 MB |
| Python 产品/集成集 | 31 pass；真实 daemon restart 单项复验 1 pass / 38.76 s |
| Windows ConPTY package gate | pass；1000 resize，startup 414.939 ms，exit 274.499 ms，无 alternate screen，paste disable 已恢复 |
| 性能回归 | pass；100k event replay 87.721 ms，input P95 0.007 ms，repaint P95 11.575 ms，stable/max RSS 159.715 MiB |
| 8 小时 soak | 已绑定 `2aed010b` 通过；详见 `phase-f-hardening-evidence-20260901.md`，未用短跑替代重跑 |

`product-tui:conpty` 的 Windows package script 已改用跨 Bun 可解析的 `.venv/Scripts/python.exe` 路径。真实 daemon restart 门禁也已按 Windows paste-burst 产品契约模拟逐键输入，验证停机重连、新 PID、新 daemon generation、同一 task 恢复、控制提交和终端清理。

## 已知失败和未运行项

- `product-tui:smoke` 到达真实 API 和任务执行后失败：physical provider route 不满足请求约束，任务没有 final answer。该结果不是通过项。
- Phase G 三次真实负载均在副作用前收到相同的 `route_policy_rejected`；详见 `phase-g-long-run-evidence-20260902.md`。
- Windows Terminal 人工 IME 候选窗尚未执行；自动化 Unicode/组合字符/emoji 门不等价于实机 IME。
- 官方 Codex standalone `0.142.0` 当前 `codex login status` 为 `Not logged in`；登录页/终端生命周期已探测，认证后的 `/status`/`/exit` 参考序列未执行。
- Linux/macOS 没有实机结果。
- 当前源码尚未重新生成并 clean-install 一个绑定最新提交的最终归档。

## 发布关闭顺序

1. 配置可路由的真实 provider，补齐 Phase G 文件、diff、verification/permission 状态。
2. 在 Windows Terminal 执行 `scripts/product-tui/windows_ime_manual_gate.ps1` 并人工签字。
3. 对最终提交重新执行可重复 release pipeline 和隔离 clean-install，记录 archive hash 与 receipt digest。
4. 可选但属于 Codex 实机参考闭环：人工登录官方 Codex 后补录 `/status`/`exit` 序列。
