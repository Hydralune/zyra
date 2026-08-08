# CLAUDE.md

## 项目与工作区

- `G:\agent-zoo\zyra` 是项目本体和 Git 仓库；`G:\agent-zoo` 根目录不是同一个 Git 仓库。
- 当前工作以 Claude Code 执行真实长程任务、自动化测试和必要修复为主。
- 以用户当前明确指令为最高优先级，不从旧阶段、milestone、unit 或 slice 文档推断当前任务或执行授权。
- 赛题文档：`G:\agent-zoo\XH-202631荣耀终端股份有限公司-面向超长程复杂任务的动态异构群体智能架构与深度协同推理技术比赛方案(3).pdf`。

## 工作约定

- 修改前运行 `git status --short`，保护用户已有改动，不回滚、覆盖或混入无关内容。
- 搜索文件和源码优先使用 `rg`。若不在 `PATH`，使用 `C:\Users\libin\AppData\Local\Microsoft\WinGet\Packages\BurntSushi.ripgrep.MSVC_Microsoft.Winget.Source_8wekyb3d8bbwe\ripgrep-15.2.0-x86_64-pc-windows-msvc\rg.exe`。
- 测试优先覆盖用户指定的真实入口、长程场景和失败路径；不得把 mock、fixture、预录结果或未执行步骤表述为真实验证。
- 如实报告执行命令、结果、失败与未运行项。
- 只修改完成当前任务所必需的内容，不顺带重构或清理无关文件。

## Git

- 由代理自动完成 Git 操作：任务完成并完成必要验证后，无需用户提醒，主动创建范围清晰的 commit。
- 提交前检查 `git status` 和相关 diff，只暂存并提交当前任务范围内的文件，不混入用户已有或其他无关改动。
- 最终答复报告 commit hash、提交主题、验证结果、未运行项和剩余工作区状态。
- 默认不自动 push；只有用户明确要求时才 push。
- 除非用户明确要求，不改写历史、不 reset、不清理未跟踪文件、不切换分支。
- 根目录的 `AGENTS.md` 和 `docs/**` 不会随 `zyra` 的 commit 自动提交；修改后应在最终说明中注明，不能伪称已经提交。
