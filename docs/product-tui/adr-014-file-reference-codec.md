# ADR-014：文件搜索 sigil 与提交路径分离

状态：已接受

日期：2026-09-01

## 背景

Codex 的 composer 使用 `@` 启动 workspace file search，但选中后以路径替换活动 token；含空白的路径会被引用。Zyra 旧实现把 `@` 和原始路径直接写入 draft，因而 `@设计 文档/方案.md` 会在空白处产生歧义，也无法可靠处理引号、控制字符和嵌套文件名搜索。

## 决策

- `@` 只属于 TUI 搜索语法，不进入 canonical task goal。
- workspace candidate 必须是相对路径、长度不超过 4096、没有 `..` segment、绝对路径前缀或 C0/C1 控制字符。
- 选中普通路径后直接插入相对路径；包含空白或引号时使用 JSON string 规则引用和转义，并自动补一个参数分隔空格。
- 文件匹配按前缀、basename 前缀和有边界加权的 subsequence 排序，只保留前 50 个结果；候选正规化索引按 immutable candidate set 缓存。
- 本地索引仍拒绝 symlink/junction、credential 名称、依赖/构建目录，并保持 64 层、20,000 条硬上限。

## 验证

- codec/completion 测试覆盖 Unicode、空白、引号、嵌套 basename、路径逃逸和 ANSI 控制字符。
- `file_reference_gate.ts` 在 Windows 上创建并索引 20,000 个真实文件，执行 1,000 次宽查询；门禁要求索引不超过 30 秒、查询 P95 不超过 50ms、RSS 增量不超过 256MiB。
