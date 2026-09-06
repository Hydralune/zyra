import { ACTIVE_CONTROL_COMMANDS } from "../../control/commands.ts"

export type ProductCommandAvailability = "always" | "idle" | "running"

export interface ProductCommandDefinition {
  name: string
  aliases?: readonly string[]
  description: string
  usage?: string
  availability: ProductCommandAvailability
}

const LOCAL_COMMANDS: readonly ProductCommandDefinition[] = Object.freeze([
  { name: "help", aliases: ["?"], description: "显示命令和快捷键", availability: "always" },
  { name: "new", description: "开始新会话", availability: "idle" },
  { name: "resume", description: "选择并恢复历史会话", usage: "/resume [会话]", availability: "idle" },
  { name: "rename", description: "给当前会话设置一个易识别的标题", usage: "/rename <标题>", availability: "always" },
  { name: "sessions", aliases: ["ls"], description: "显示最近会话", availability: "idle" },
  { name: "status", description: "显示当前配置和运行状态", availability: "always" },
  { name: "pwd", aliases: ["cwd"], description: "显示当前工作目录", availability: "always" },
  { name: "doctor", description: "检查服务连接、模型配置和就绪状态", availability: "always" },
  { name: "model", description: "查看或选择后续任务使用的模型", usage: "/model [status]", availability: "always" },
  { name: "mode", description: "查看或选择后续任务的执行模式", usage: "/mode [status]", availability: "always" },
  { name: "diff", description: "查看当前任务文件变更", availability: "always" },
  { name: "plan", description: "查看完整计划和步骤状态", availability: "always" },
  { name: "verification", aliases: ["verify"], description: "查看验证命令与结果", usage: "/verification [details]", availability: "always" },
  { name: "review", description: "审查当前代码变更", usage: "/review [关注点]", availability: "always" },
  { name: "init", description: "检查仓库并生成 AGENTS.md 指引", usage: "/init [关注点]", availability: "idle" },
  { name: "compact", description: "压缩当前任务的上下文", usage: "/compact [说明]", availability: "running" },
  { name: "context", aliases: ["tokens", "usage"], description: "查看上下文记录与压缩状态", usage: "/context [范围]", availability: "always" },
  { name: "btw", description: "在不改变主任务的情况下询问一个问题", usage: "/btw <question>", availability: "running" },
  { name: "memory", description: "查看当前任务的记忆与压缩状态", usage: "/memory [query]", availability: "always" },
  { name: "skills", description: "查看当前任务可用的技能及调用状态", availability: "always" },
  { name: "mcp", description: "查看当前任务连接的 MCP 服务与工具", usage: "/mcp [servers|tools]", availability: "always" },
  { name: "tools", description: "查看工具调用、耗时和输出", availability: "always" },
  { name: "artifact", description: "选择并查看任务交付物", usage: "/artifact [产物编号]", availability: "always" },
  { name: "agents", aliases: ["subagents"], description: "查看协作代理及其进度", usage: "/agents [代理]", availability: "always" },
  { name: "permissions", description: "处理权限请求或管理当前权限模式", usage: "/permissions [mode|status]", availability: "always" },
  { name: "questions", description: "重新打开 Zyra 正在等待的用户问题", availability: "running" },
  { name: "copy", description: "复制最近一条助手回答", availability: "always" },
  { name: "export", description: "将当前对话导出为 Markdown", usage: "/export [文件名.md]", availability: "always" },
  { name: "raw", description: "打开适合复制的纯文本对话", availability: "always" },
  { name: "ui", description: "在 Web 看板打开当前任务", availability: "always" },
  { name: "clear", description: "清除本地对话显示，不删除任务", availability: "idle" },
  { name: "detach", description: "退出 TUI，远端任务继续", availability: "always" },
  { name: "exit", aliases: ["quit"], description: "退出 TUI，远端任务继续", availability: "always" },
])

const CONTROL_HELP: Readonly<Record<string, { description: string; usage: string }>> = {
  "/queue": { description: "查看并管理排队中的指令", usage: "/queue" },
  "/now": { description: "中断当前步骤并立即执行指令", usage: "/now <斜杠命令>" },
  "/next": { description: "在当前步骤结束后执行指令", usage: "/next <斜杠命令>" },
  "/later": { description: "将指令放到队列后面", usage: "/later <斜杠命令>" },
  "/cancel": { description: "停止当前任务", usage: "/cancel [原因]" },
  "/cancel-command": { description: "取消一条排队指令", usage: "/cancel-command <请求编号>" },
  "/continue": { description: "继续执行等待中的任务", usage: "/continue" },
  "/redirect": { description: "调整当前任务的方向", usage: "/redirect <说明>" },
  "/interrupt": { description: "中断当前步骤并调整方向", usage: "/interrupt <说明>" },
  "/retry": { description: "重试一条失败的指令", usage: "/retry <请求编号>" },
  "/approve": { description: "允许指定的权限请求", usage: "/approve <请求编号> [反馈]" },
  "/deny": { description: "拒绝指定的权限请求", usage: "/deny <请求编号> [反馈]" },
}

const CONTROL_DEFINITIONS: readonly ProductCommandDefinition[] = ACTIVE_CONTROL_COMMANDS.map((command) => ({
  name: command.slice(1).split(" ")[0]!,
  description: CONTROL_HELP[command]?.description ?? "控制当前任务",
  usage: CONTROL_HELP[command]?.usage ?? command,
  availability: "running" as const,
}))

export const PRODUCT_COMMAND_REGISTRY: readonly ProductCommandDefinition[] = Object.freeze([
  ...LOCAL_COMMANDS,
  ...CONTROL_DEFINITIONS.filter((candidate, index, values) => values.findIndex((item) => item.name === candidate.name) === index),
])

export interface ParsedProductCommand {
  definition: ProductCommandDefinition
  args: string
  raw: string
}

export function parseProductCommand(value: string): ParsedProductCommand | undefined {
  const raw = value.trim()
  if (!raw.startsWith("/") && raw !== "?") return undefined
  const [, head = "", tail = ""] = /^(\S+)(?:\s+([\s\S]*))?$/u.exec((raw === "?" ? "/help" : raw).slice(1)) ?? []
  const name = head.toLowerCase()
  const definition = PRODUCT_COMMAND_REGISTRY.find((item) => item.name === name || item.aliases?.includes(name))
  return definition ? { definition, args: tail.trim(), raw } : undefined
}

export function productCommandCandidates(): string[] {
  return PRODUCT_COMMAND_REGISTRY.map((command) => `/${command.name}`)
}

export function productCommandHelp(running: boolean): string {
  const available = PRODUCT_COMMAND_REGISTRY.filter((command) => command.availability === "always" || command.availability === (running ? "running" : "idle"))
  return available.map((command) => `${command.usage ?? `/${command.name}`} — ${command.description}`).join("\n")
}
