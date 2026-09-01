# ADR-013：专用工作流必须落到 task 或 canonical control

状态：已接受
日期：2026-09-01

## 背景

Codex 的 `/review`、`/init` 与 `/compact` 不是普通帮助文本：它们分别建立代码审查、仓库初始化和上下文压缩工作流。Zyra 不应仅增加同名菜单项，也不应绕过已有 task、control command 与 MemoryFabric owner。

## 决策

- idle `/review [focus]` 创建同一产品 session 中的新 canonical task。固定工作流要求 findings-first、按严重度排序、精确文件/行引用、剩余风险与验证缺口；默认只读，除非用户随后明确要求修改。
- running `/review [focus]` 不创建并行本地工作流，而以 revisioned `/change` steer mutation 将同一审查契约发送给当前 task。
- idle `/init [focus]` 创建 canonical task，先检查真实仓库、既有 `AGENTS.md`、构建/测试与安全约束，再创建或改进根指引；必须保留用户规则并报告未验证命令。
- `/compact [focus]` 只在 task 运行期间可用，提交现有 canonical control command。后端 `MemoryFabric` 持久化 summary artifact 与 compaction metadata，CLI 只显示 command receipt。
- idle `/compact` 明确说明没有可压缩的活动 task，不通过清空 transcript 或本地摘要伪造成功。

## 结果

三个命令复用同一 task/session/controller 体系。审查和初始化能被 resume、Web 与 canonical history 观察；压缩仍由后端 memory owner 执行并受 revision/idempotency 约束。
