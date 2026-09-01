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
  { name: "new", description: "开始一个新的产品会话", availability: "idle" },
  { name: "resume", description: "恢复 task 或 session", usage: "/resume <task|session>", availability: "idle" },
  { name: "sessions", aliases: ["ls"], description: "显示最近会话", availability: "idle" },
  { name: "status", description: "显示当前会话和任务状态", availability: "always" },
  { name: "pwd", aliases: ["cwd"], description: "显示当前工作目录", availability: "always" },
  { name: "diff", description: "查看当前任务文件变更", availability: "always" },
  { name: "agents", aliases: ["subagents"], description: "查看协作代理摘要", availability: "always" },
  { name: "permissions", description: "查看当前权限请求", availability: "always" },
  { name: "ui", description: "在 Web 看板打开当前任务", availability: "always" },
  { name: "clear", description: "清除本地 transcript，不删除任务", availability: "idle" },
  { name: "detach", description: "退出 TUI，远端任务继续", availability: "always" },
  { name: "exit", aliases: ["quit"], description: "退出 TUI，远端任务继续", availability: "always" },
])

const CONTROL_DEFINITIONS: readonly ProductCommandDefinition[] = ACTIVE_CONTROL_COMMANDS.map((command) => ({
  name: command.slice(1).split(" ")[0]!,
  description: "控制当前任务",
  usage: command,
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
  const [head = "", ...tail] = (raw === "?" ? "/help" : raw).slice(1).split(/\s+/u)
  const name = head.toLowerCase()
  const definition = PRODUCT_COMMAND_REGISTRY.find((item) => item.name === name || item.aliases?.includes(name))
  return definition ? { definition, args: tail.join(" ").trim(), raw } : undefined
}

export function productCommandCandidates(): string[] {
  return PRODUCT_COMMAND_REGISTRY.map((command) => `/${command.name}`)
}

export function productCommandHelp(running: boolean): string {
  const available = PRODUCT_COMMAND_REGISTRY.filter((command) => command.availability === "always" || command.availability === (running ? "running" : "idle"))
  return available.map((command) => `${command.usage ?? `/${command.name}`} — ${command.description}`).join("\n")
}
