# CLI 与 Web 看板联动验收（2026-09-06）

本次按“CLI 执行 agent 任务，Web 查看过程与交付物”的用法，同时启动 Windows ConPTY 中的真实 CLI 和 Chrome 中的 Web。调用用户已授权、已配置的 DeepSeek V4 Flash，没有用模拟模型代替实际执行。

## 修复

- Web 首页和已结束的会话也会发现 CLI 新建的任务；正在查看最新一轮时，自动跟随同会话后续任务。后台同步不替换为加载骨架，并保留已加载的较早分页。
- CLI 重命名自动反映到 Web；执行器的旧检查点不能覆盖并发保存的新名称。
- 规划、路由和执行阶段及时写入共享状态；工具开始、成功、失败事件通过部署节点实时转发。工具记录保留名称，历史浏览器投影会从后端重建。
- 先处理敏感信息再计算事件摘要，避免 HTTP 输出时再次修改嵌套产物字段而导致整页事件校验失败。
- `artifact_write` 进入共享产物存储。实时工具结果和最终物理执行结果都登记正式引用，校验任务归属、摘要、版本与实际字节。CLI 和 Web 都能读取，内部快照和运行跟踪仍归入证据。
- 空的工具回合不再清空上一条进度文字；中间消息不再被标成“正在确认最终回答”。修复 Web Markdown 将 `SYNC_FOLLOWUP_OK` 等标识符中的下划线吞掉的问题。

## 实际验收

隔离 API `18846`、ConPTY 桥 `18847`、Web `18848`，状态与示例文件位于 `.tmp/cli-audit-20260906/`。未修改用户原服务的启动配置。

| 场景 | 真实结果 |
| --- | --- |
| 初次对照 `task_115e3e668e2c` | 任务完成且测试通过，但发现新会话不自动出现、阶段状态滞后、Web 交付物缺失；没有把模型声称“同步通过”当作验收结论。 |
| 事件链定位 `task_ac3d3a2dc06d` | 发现工具产物事件在传输后摘要不一致；从 Web 确认停止后，CLI 同步显示取消。 |
| 失败与取消 `task_72c46fd14e5b` | 直接执行 Python 脚本被现有权限策略拒绝，失败事件同步到两端；CLI `/cancel` 后 Web 显示已停止。 |
| 文件与交付物 `task_35947d952a7e` | CLI 创建 `test_dashboard_d.py`，实际执行 `python -m unittest test_dashboard_d -v`，3 个独立测试全部通过，退出码 0，任务最终 completed。 |
| 运行中交付物 | 上述任务尚在执行时，Web 已显示并能读取“实时同步D”“测试结果D”两份 Markdown，包含实际 unittest 输出。 |
| 同会话下一轮 `task_0a4200c325ee` | CLI 提交只回复 `SYNC_FOLLOWUP_OK` 的任务，Web 自动切换到新任务，保留上一轮；两端最终回复相同。 |
| 退出与恢复 | CLI 正常退出，刷新 Web 仍能恢复两轮完整回答和交付物；重启隔离 API 与最终编译 CLI 后，`/resume task_35947d952a7e`、`/artifact` 能列出两份交付物并读取对应内容。Web 也能重新加载并搜索历史 shell 事件。 |

完整本地诊断与截图保留在 `.tmp/cli-audit-20260906/`，不提交运行数据库和凭据。

## 回归检查

- Web typecheck/build、CLI typecheck/build 通过。
- Web：`bun test ./apps/web/test/product-preferences.test.ts ./apps/web/test/artifact-custody-catalog-viewers.test.ts ./apps/web/test/task-live-sync.test.ts ./apps/web/test/workbench-shell.test.tsx`：74 项通过。
- CLI：产物预览、结果展示、权限托管恢复 3 个测试文件：9 项通过。
- Python：共享进度、部署事件传输、节点 API 生命周期、任务图、SQLite、终端动作、产物校验、运行时消息总线 8 个测试文件：60 项通过。
- 任务执行 owner 和终态收敛专项：6 项通过（其中 2 项与共享进度检查重复）。

Windows 默认 pytest 临时目录无访问权限，已改用工作区内独立 `--basetemp` 后完成检查。未运行整个仓库全量测试；本次没有验收跨机器网络分区、多浏览器同时审批、所有 MCP／浏览器／子代理组合或长时间压力场景。

## 边界

两端必须连接同一个 Zyra API；CLI 的 `/ui` 使用当前 API 地址启动对应看板。CLI 执行与 Web 展示共享后端记录，不要求 Web 具备 CLI 的本地执行能力。

直接运行任意 Python 脚本仍受现有权限策略约束，本次没有扩大权限或开启绕过。真实运行中出现过权限状态刷新暂时失败提示，之后 `/permissions status` 可正常读取模式；本次不据此宣称所有权限审批并发场景均已验证。

这些结果证明上述主同步链路可用，不代表所有功能组合零缺陷。已有后端进程需要重启才能加载 Python 修复，Web 需刷新以加载新构建。
