import type { TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"
import type { CommandCatalog } from "./catalog.ts"
import type { CompletionContext, ParsedInput } from "./parser.ts"

export interface ArgumentSuggestion {
  id: string
  value: string
  label: string
  description: string
  kind: "choice" | "task" | "status" | "flag"
  disabled?: boolean
}

function match(value: string, query: string): boolean {
  const normalized = query.trim().toLowerCase()
  if (!normalized) return true
  return value.toLowerCase().includes(normalized)
}

function unique(values: readonly ArgumentSuggestion[]): ArgumentSuggestion[] {
  const seen = new Set<string>()
  const result: ArgumentSuggestion[] = []
  for (const value of values) {
    const key = `${value.kind}:${value.value}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push(value)
  }
  return result
}

export function argumentSuggestions(input: {
  parsed: ParsedInput
  completion: CompletionContext
  catalog: CommandCatalog
  tasks: readonly TaskProjection[]
  limit?: number
}): ArgumentSuggestion[] {
  if (
    input.parsed.kind !== "command" ||
    !input.parsed.definition ||
    input.completion.kind !== "argument"
  ) {
    return []
  }
  const definition = input.parsed.definition
  const argumentIndex = Math.max(0, input.completion.argumentIndex ?? 0)
  const argument = definition.arguments[Math.min(argumentIndex, definition.arguments.length - 1)]
  const query = input.completion.query
  const suggestions: ArgumentSuggestion[] = []

  if (argument?.choices) {
    for (const choice of argument.choices) {
      if (!match(choice, query)) continue
      suggestions.push({
        id: `${definition.id}:choice:${choice}`,
        value: choice,
        label: choice,
        description: argument.description,
        kind: choice.startsWith("--") ? "flag" : "choice",
      })
    }
  }

  if (definition.id === "command.navigation.task") {
    for (const task of input.tasks) {
      if (!match(`${task.taskId} ${task.userGoal} ${task.status}`, query)) continue
      suggestions.push({
        id: `${definition.id}:task:${task.taskId}`,
        value: task.taskId,
        label: task.userGoal || task.taskId,
        description: `${task.status} · ${task.taskId}`,
        kind: "task",
      })
    }
  }

  if (definition.id === "command.navigation.tasks") {
    const statuses = new Set<string>(["all", "active", "completed", "failed", "cancelled"])
    for (const task of input.tasks) statuses.add(task.status)
    for (const status of [...statuses].sort()) {
      if (!match(status, query)) continue
      suggestions.push({
        id: `${definition.id}:status:${status}`,
        value: status,
        label: status,
        description: status === "all" ? "Show every task" : `Filter task list to ${status}`,
        kind: "status",
      })
    }
  }

  if (definition.id === "command.task.new") {
    for (const flag of ["--run", "--no-run"]) {
      if (!match(flag, query)) continue
      suggestions.push({
        id: `${definition.id}:flag:${flag}`,
        value: flag,
        label: flag,
        description: flag === "--run" ? "Start the task immediately" : "Create without starting",
        kind: "flag",
      })
    }
  }

  const limit = Math.max(1, Math.min(30, Math.floor(input.limit ?? 10)))
  return unique(suggestions).slice(0, limit)
}

export function applyArgumentSuggestion(
  value: string,
  completion: CompletionContext,
  suggestion: ArgumentSuggestion,
): { value: string; cursor: number } {
  if (completion.kind !== "argument") return { value, cursor: value.length }
  const before = value.slice(0, completion.replaceStart)
  const after = value.slice(completion.replaceEnd)
  const needsLeadingSpace = before.length > 0 && !/\s$/.test(before)
  const needsTrailingSpace = after.length === 0 || !/^\s/.test(after)
  const insertion = `${needsLeadingSpace ? " " : ""}${suggestion.value}${needsTrailingSpace ? " " : ""}`
  return {
    value: `${before}${insertion}${after}`,
    cursor: before.length + insertion.length,
  }
}
