# FE-S02 交互会话、输入与行式流式呈现：增量自审

## 结论

FE-S02 在 `87fe4901bff7451073dce5177ed24fc32f38bf48` 基线上完成，
implementation commit 为 `fab56a03b9839594832f78ec327ee6472be81d62`。验收结论为
**PASS**：`zyra`、`zyra "目标"`、`zyra resume`、`zyra ls` 四个入口已落位；
交互观察走同一 typed transport 的 capabilities → paged snapshot → SSE，task/session/
cursor/revision 均来自服务端；普通终端 scrollback 逐行追加，只有 TTY 的单行状态区可重绘。

这不是完整前端收口声明。permission 与 command queue 属于 FE-S03，terminal node 属于
FE-S04，`zyra ui` 与 Web 产品入口属于 FE-S05，最终 cleanroom/封闭验收属于 FE-S06。

## 实现与 owner 自审

| 检查项 | 结果 | 证据 |
|---|---|---|
| 四个入口 | PASS | args/main 分流 interactive、goal、resume、ls；`ui` 继续 fail closed 到 FE-S05 |
| task/session owner | PASS | create/get/resume/list 和 task-backed session resolver 全走 API；resume 真实测试前后 task total 不变 |
| event owner | PASS | CLI 只读 `typescript.RuntimeEventSpine` 的 generation/cursor/frame；无 canonical write 或本地 replay owner |
| snapshot 内存边界 | PASS | snapshot 按 500 条分页边读边投影；交互路径不一次聚合全部历史；recent search budget 默认 512 |
| SSE 失败边界 | PASS | SSE unavailable、缺 cursor、unknown schema、cross-binding、generation mismatch、gap/reorder/conflicting duplicate 均停止；无 polling fallback |
| 输入本地状态 | PASS | draft/cursor/history/paste ref/completion/stash 仅存进程内；Bracketed Paste 提交时才展开；Ctrl+J 与 Enter 分离 |
| 外部编辑 | PASS | Ctrl+E 通过 `VISUAL`/`EDITOR`、无 shell 子进程和 0600 临时文件；失败不会提交半条 command |
| 行式呈现 | PASS | transcript append-only；tool progress 只更新 footer；tool settled 只显示摘要/digest/artifact ref |
| terminal 兼容 | PASS | 80/120 列稳定换行；pipe 不发 footer；未引入 alternate screen、Ink、blessed、React reconciler |
| CLI/Web 连续性 | PASS | 真实 daemon 中 Web `TaskApi` 读取 CLI task 的相同 taskId/runId/updatedAt |
| terminal node 边界 | PASS | footer 仅显示 `terminal n/a(FE-S04)`；无 terminal dispatch claim |
| 服务端边界 | PASS | 未修改 `apps/api`、permission owner、runtime owner或 public persistence schema |

## Claude Code 参考转化自审

1. `PromptInput` 的多行、首末行 history、大 paste ref、slash/ref completion、草稿恢复和
   external editor 被 `ADAPT` 为 Zyra 自有 `PromptDraft`/`TerminalPrompt`，未复制源码或 wire schema。
2. REPL 的 ephemeral tool progress 被 `ADAPT` 为 footer-only 状态；settled event 仍以稳定
   transcript record 追加。
3. bounded transcript、search、sticky follow、unread 和 resize 被 `ADAPT` 为非权威
   `SessionProjection` 状态；禁用 search 只产生体验退化，server revision 仍推进。
4. Ink/React 根布局、alternate screen 和进程内 transcript owner 保持 `REJECT`。
5. 详细裁决和测试落位已补入 `claude-cli-behavior-matrix.md` 6.1 节，完成含义不是“读完”。

## 真实行为与负向证据

1. 真实 daemon 上 `zyra "目标"` 创建并完成任务，行式输出含 model、artifact 和终态；
   真实 runtime-event-spine tool called/succeeded 经 `resume` 同一 snapshot 显示。
2. `zyra ls` 返回 task store projection；Web `TaskApi` 读取同一 task/run/updatedAt；通过
   session identity resume 后 task 总数不变。
3. 2,101 transition 等价压力中 projection 只保留 128 条测试预算，1,973 条明确标为
   recent-only eviction；server `1:2101` revision、顺序和搜索体验退化均可观察。
4. 80/120 列、长摘要、digest/artifact ref 顺序稳定；非 TTY 无 alternate-screen 控制；
   continuous tool progress 不覆盖已经打印的 transcript。
5. 实际 TTY byte stream 验证 Ctrl+J multiline、Bracketed Paste、Esc stash/Ctrl+R restore、
   history arrow 和原子 Enter submit，不产生半条 command。
6. SSE disabled 时显示 disconnected 并停止，stream/poll 均未调用；缺失 cursor 在访问
   transport 前拒绝。
7. exact duplicate 只去重一次；冲突 duplicate、reorder 和 gap fail closed；unknown API/event
   schema 继续沿用 FE-S01 contract 测试证明无 fallback。

## 分桶

| bucket | 规模 | 说明 |
|---|---:|---|
| production | 13 files / 2452 lines | CLI command/input/render/session/API 与共享 SSE decoder；含既有文件完整行数 |
| test | 2 files / 670 lines | 21 个 CLI 行为/contract 测试及 5 个真实 API 集成场景所在文件 |
| docs | 4 files | CLI README、Claude 行为矩阵增量、本自审、机器可读证据 |
| runtime-assets | 0 | 无 vendor runtime 或参考仓库复制 |
| generated | 0 tracked | build 产物不纳入本次 commit |
| data | 0 | 无 dataset/checkpoint |
| adapter-only | 0 | 无空壳 adapter 冒充交互行为 |
| mock/fixture | test-only | fake API 只测 SSE disable/contract failure；完成条件含真实 daemon/Web/event spine 证据 |

## 验证记录

- `bun run --cwd apps/cli typecheck && test && build`：21 passed，bundle 0.53 MB。
- `bun run --cwd packages/core/typed-api-client typecheck && test`：18 passed。
- `bun run --cwd apps/web typecheck`：通过。
- `.venv\Scripts\python.exe -m pytest tests\integration\test_cli_noninteractive_foundation.py -q`：
  5 passed in 94.65s。
- `git diff --check`：通过。

机器可读证据见 `docs/product-entry/evidence/FE-S02-interactive-session-streaming.json`。

## 未越界项与后继风险

- FE-S03 的运行中 command admission、priority/FIFO、permission 裁决和冲突恢复未实现。
- FE-S04 terminal node 未实现；状态区插槽明确显示未启用。
- FE-S05 `zyra ui` 未实现；args 仍确定性拒绝。
- FE-S06 仍需真实 PTY 矩阵、daemon crash/restart、slow reader、跨 generation cursor 恢复和
  最终 cleanroom；本片只承诺当前 slice 的真实行为与负向 contract。
- FE-S03 只是下一候选，未获得执行授权。
