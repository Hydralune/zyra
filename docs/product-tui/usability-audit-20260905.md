# Web / CLI 体验修复记录（2026-09-05）

本轮已启动真实 Web、独立 daemon 和产品 CLI，修复下列问题。**完整浏览器逐功能验收尚未完成**：浏览器控制工具返回 `apps: [], browsers: []`，已请求用户允许改用 Playwright，尚未收到答复。本文不把组件测试记作浏览器实测。

## 修复

| 问题 | 当前行为 | 证据 |
|---|---|---|
| CLI Esc 清空补全草稿，并提示不存在的 `/restore` | Esc 先收起补全，保留内容；运行中无菜单时才中断 | 实际 TTY 复现、composer 回归、真实 ConPTY 屏幕 |
| 多行上下键不移动；查看历史后草稿被清空 | 上下键在行间移动，历史返回恢复原草稿；修复首行 Home | 输入回归 |
| Alt+Enter / 编码的 Shift+Enter 不换行 | 支持这些换行序列，Ctrl+J 继续保留 | 输入回归 |
| 超过八项的补全选中项不可见 | 列表跟随选择滚动 | 真实 ConPTY 连续下移十四次 |
| 每次按键重复擦写整屏；空白把输入框推至物理底部 | 相同帧不输出、光标移动不重写文字、从首个变更行重绘；高度作为上限 | 真实 80×24 / 120×40；重绘和缩放回归 |
| `/artifact` 必须知道内部编号 | 运行中和结束后都可无参数选择；显示产物标题，支持预览与返回 | 真实任务产物选择、读取、End 查看末尾 |
| 详情按原始行分页，并静默截断两千字符以后的内容 | 按显示宽度分页，缩放重排，超总量上限明确提示 | 单行 JSON 实机复现和修复后末尾屏幕；长中文行回归 |
| 同会话追问没有上一轮内容 | 从保存的同会话终态任务投影历史问答，至多 24 轮 / 64,000 字符，截断明确标记 | 真实模型两轮问答；模型请求绑定、跨会话隔离回归 |
| 多轮会话全部结束后无法恢复；列表只有内部编号 | 全部结束时恢复最近一轮；默认用首轮问题作标题；多个运行任务仍要求明确选择 | 真实恢复菜单；会话 API 回归 |
| 文件读写已执行，但 `/tools` 显示没有调用 | 将物理 worker 已保存的工具事件投影到产品界面；完成/失败由同一调用的结果凭据决定 | 原真实任务的事件流复验；开始、成功、失败、缺失凭据及跨任务回归 |
| 模型不可用只给英文错误和重复恢复建议 | 给出 `/doctor`、`/model` 和重新提交的说明，保留失败终态 | 实际任务失败、任务观察器回归 |
| Web 最近会话固定十二项 | 搜索、显示更多、加载更早记录 | 构建、组件/路由回归；浏览器待验 |
| Web “交付物”只跳回对话 | 会话交付物独立视图、展开预览、空状态和返回对话；链接可刷新 | 组件/路由回归；浏览器待验 |
| 已结束任务仍可能显示“进行中” | 同时检查任务是否仍在执行 | 源码检查；浏览器待验 |
| MCP/技能/代理结果按钮依赖面板已经挂载 | 跳转到对应任务的证据页并定位面板 | 类型检查、路由回归；浏览器待验 |
| Web 弹层 Esc 可冒泡并触发第二个动作 | 当前弹层处理后停止传播；详情抽屉尊重上层弹窗 | 构建；嵌套弹层浏览器待验 |
| 设置清除历史后数字不更新，无反馈 | 立即更新数量、反馈结果，并禁用空历史清除 | 构建；浏览器待验 |
| Web 终端中文覆盖旧双宽字符后夹杂空格 | 同时清除被覆盖的两格所属旧字符，组合音标附着到字符头 | 真实 ConPTY 输出重放、独立 ANSI 屏幕回归 |

系统与确认弹窗的常用文案、快捷键说明也作了整理。高级证据面板仍保留技术信息。
计划详情将“等待前置步骤”改为“依赖前置步骤”，避免完成的步骤仍显示等待。

## 真实入口

Web 由以下正式入口启动，测试状态位于 `.tmp/ux-audit-20260905/`：

```powershell
$env:ZYRA_CLI_STATE_DIR='G:\agent-zoo\zyra\.tmp\ux-audit-20260905\cli'
$env:ZYRA_STATE_ROOT='G:\agent-zoo\zyra\.tmp\ux-audit-20260905\runtime'
node apps/cli/dist/zyra.js ui --base-url http://127.0.0.1:18740 --web-port 18741 --open=false
```

CLI 实际操作包括启动、补全/筛选/滚动/取消、模型和推理强度选择、帮助返回、状态、空交付物和退出。另用真实 ConPTY 在 80×24 与 120×40 下分别采集十帧；经终端状态机还原后十二条屏幕断言通过，包括输入光标第八行、Esc 后草稿仍在、模型选择后菜单消失。原始记录和文本屏幕保留于 `.tmp/ux-audit-20260905/cli-*`，该目录不提交。

最初两个真实提交均失败，不能将它们记作真实 provider 成功验证：

- `task_cb5c51f26176`：只要求两行文字，不调用工具。
- `task_e82303dfdb90`：只要求回答“交互测试完成”，不调用工具。

终态错误均为 `no provider/model route satisfies the request constraints`。进一步运行 `node --env-file=.env.deepseek.local --experimental-strip-types scripts/smoke_deepseek_provider.ts`，底层错误为 `connect EACCES ...:443`：本次执行沙箱限制了模型网络访问。取得限于现有 DeepSeek 测试的网络权限后，同一 smoke 一次请求返回 HTTP 200、113 tokens、固定标记匹配。未修改用户 provider 配置。仅为本轮独立 daemon 申请网络权限继续复验，随后 `task_a4fc16b632ca` 成功返回“交互测试完成”。

真实两轮复验发现会话缺陷：`task_f3b6bf8337d0` 已回答“青松七号”，但同会话的 `task_100739aad8e0` 声称没有上一轮上下文。两轮虽然都是 completed，内容验证失败，不能以终态代替体验验收。

修复并重启测试 daemon 后，以下真实两轮通过；第二轮提示词没有包含标记本身：

- 第一轮 `task_4aa732440e7e`：要求记住“青松七号”，最终回答“青松七号”。
- 第二轮 `task_2dbb812954aa`：问上一轮的标记，最终回答仍为“青松七号”。
- 同一会话 `session_01a06fe41b6d000_8bba1ccfe7d9ada30595`，两个任务都 completed。
- 在同一真实 CLI 中继续打开计划、验证、工具空状态、产物列表与预览，End 到达完整 JSON 末尾，再检查会话列表和恢复选择器，退出码 0。十帧记录在 `cli-canonical-task-capture.json`，对应文本屏幕在 `cli-canonical-task-*.txt`。

测试先等待任务真实终态和 CLI 回到输入状态，再发送追问。早期仅按流式回答匹配的脚本曾把当前任务的修订误记作第二轮，已废弃该结论。历史上下文作为用户级输入进入原有模型请求，原有 prompt 摘要绑定仍校验完整输入；不复用旧权限、工具执行状态或其他会话内容。

真实文件任务 `task_78b4eb4ed242` 也已 completed：在隔离目录使用 `file_write` 创建 `ux-smoke.txt`，使用 `file_read` 校验内容为 `ZYRA_UX_FILE_OK`；进程外再次读取文件确认一致。`/diff` 显示新文件和正确内容。这次操作直接被现有策略放行，没有出现权限审批弹窗，因此不算审批交互实测。原始记录为 `cli-file-task-capture.json`。

该次连续自动输入曾有一帧出现 `^[/tools` 回显，未打开预期页面。随后单独恢复已结束任务，重复两次 diff → 返回 → tools，并检查 plan，详情均能退出，未复现同一异常；不能据此宣称已定位或修复偶发回显，保留为待复查项。这次复查另外确认并修复了工具历史为空的问题。加载修复后再次恢复原任务，实际工具选择器显示两条已完成的 `file_write` / `file_read`，退出详情恢复输入，十一帧保留于 `cli-detail-return-capture.json`。

## 验证与限制

- `bun run test:cli`：210 pass / 0 fail，997 expect；覆盖新的输入/分页回归、原任务/会话/权限/产物测试。最终计划文案调整后运行 `bun test ./apps/cli/test/product-presentation.test.ts ./apps/cli/test/product-task-observer.test.ts`：41 pass / 0 fail，172 expect。
- `bun run test:web`：323 pass / 0 fail，1928 expect，包含 typed API client 和 Web 测试。
- `.venv/Scripts/python.exe -m pytest tests/integration/test_product_conversation_context.py tests/integration/test_product_entry_session_api.py tests/integration/test_physical_code_worker_reasoning_loop.py -q --basetemp=.tmp/ux-audit-20260905/pytest-context-final -o cache_dir=.tmp/ux-audit-20260905/cache-context`：23 passed。使用工作区临时目录；模型链路集成测试使用本地协议服务，不能记作真实外部模型。
- `.venv/Scripts/python.exe -m pytest tests/unit/test_product_presentation_contract.py -q --basetemp=.tmp/ux-audit-20260905/pytest-tool-presentation -o cache_dir=.tmp/ux-audit-20260905/cache-context`：8 passed；pytest 缓存写入出现访问拒绝警告，测试本身通过。
- `bun run typecheck:cli`、`bun run build:cli`、`bun run build:web`：通过，Web 构建包含其 TypeScript 检查。
- `.venv/Scripts/python.exe -m pytest tests/integration/test_product_tui_async_redraw_conpty.py -q --basetemp=.tmp/ux-audit-20260905/pytest-conpty-compact -o cache_dir=.tmp/ux-audit-20260905/cache-compact`：3 passed。包含中文输入与 1000 次 resize、异常退出、100 次强制结束。
- 初次 pytest 使用默认临时目录时遇到 Windows 访问拒绝；改用本工作区独立临时目录后通过。
- 测试中暴露的旧断言（固定物理底行、要求英文原始错误、按尾部字节判断菜单消失）按新的用户行为更新，未改变任务终态断言。
- 历史上下文首次实现触发了原有模型输入摘要绑定校验；已将历史与当前请求共同纳入原有用户消息及摘要，保留校验，并通过集成和真实模型复验。

未运行：浏览器所有页面的真实点击/输入/截图、移动端实测、真实等待权限/提问的完整流程、人工 IME 候选窗、长程 soak、发布 clean-install、其他操作系统。CLI 实测覆盖上文列出的旅程，不声称所有有状态命令都已逐项实测。另需复查上文的一次偶发输入回显。

下一步为浏览器用户旅程检查，并继续修复新发现的问题。本轮尚不能声明用户要求的 Web 全功能审查完成。

根目录 `G:\agent-zoo\PRODUCT_TUI_TASK.zh-CN.md` 已重写为本轮可执行的用户旅程和验收标准。它不属于 `zyra` Git 仓库，因此不会包含在本轮提交中。
