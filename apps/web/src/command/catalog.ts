export type CommandExecution = "local-overlay" | "task-create" | "task-cancel" | "task-resume" | "navigation"
export type CommandAvailability = "enabled" | "disabled" | "requires-task" | "requires-active-task" | "requires-terminal-task"

export interface CommandArgument {
  name: string
  label: string
  required: boolean
  variadic?: boolean
  choices?: readonly string[]
  description: string
}

export interface CommandDefinition {
  id: string
  trigger: string
  aliases: readonly string[]
  title: string
  description: string
  category: "task" | "navigation" | "help" | "runtime"
  execution: CommandExecution
  remoteSafe: boolean
  queueable: boolean
  availability: CommandAvailability
  arguments: readonly CommandArgument[]
  keywords: readonly string[]
}

export interface CommandContext {
  taskId?: string
  runId?: string
  taskStatus?: string
  taskActive?: boolean
  taskTerminal?: boolean
  transportEnabled: boolean
}

export interface CommandAvailabilityResult {
  enabled: boolean
  reason?: string
}

const DEFINITIONS: readonly CommandDefinition[] = Object.freeze([
  {
    id: "command.task.new",
    trigger: "new",
    aliases: ["create", "task"],
    title: "Create task",
    description: "Create a task and optionally start its first run.",
    category: "task",
    execution: "task-create",
    remoteSafe: true,
    queueable: false,
    availability: "enabled",
    arguments: [
      {
        name: "goal",
        label: "goal",
        required: true,
        variadic: true,
        description: "The task objective sent to the Zyra runtime.",
      },
      {
        name: "run",
        label: "--run",
        required: false,
        choices: ["--run", "--no-run"],
        description: "Start immediately; defaults to --run.",
      },
    ],
    keywords: ["create", "start", "goal", "task"],
  },
  {
    id: "command.task.cancel",
    trigger: "cancel",
    aliases: ["stop", "abort"],
    title: "Cancel active task",
    description: "Cancel the selected task through the typed lifecycle endpoint.",
    category: "task",
    execution: "task-cancel",
    remoteSafe: true,
    queueable: false,
    availability: "requires-active-task",
    arguments: [
      {
        name: "reason",
        label: "reason",
        required: false,
        variadic: true,
        description: "A bounded cancellation reason stored by the backend.",
      },
    ],
    keywords: ["cancel", "stop", "abort", "task", "run"],
  },
  {
    id: "command.task.resume",
    trigger: "resume",
    aliases: ["continue", "retry"],
    title: "Resume task",
    description: "Resume the selected task from its durable backend state.",
    category: "task",
    execution: "task-resume",
    remoteSafe: true,
    queueable: false,
    availability: "requires-terminal-task",
    arguments: [],
    keywords: ["resume", "continue", "retry", "task", "run"],
  },
  {
    id: "command.navigation.tasks",
    trigger: "tasks",
    aliases: ["list", "home"],
    title: "Open task list",
    description: "Navigate to the canonical task list.",
    category: "navigation",
    execution: "navigation",
    remoteSafe: false,
    queueable: false,
    availability: "enabled",
    arguments: [
      {
        name: "status",
        label: "status",
        required: false,
        choices: ["all", "active", "completed", "failed", "cancelled"],
        description: "Optional task status filter.",
      },
    ],
    keywords: ["tasks", "list", "home", "filter"],
  },
  {
    id: "command.navigation.task",
    trigger: "open",
    aliases: ["goto", "show"],
    title: "Open task",
    description: "Navigate to a task identity returned by the backend.",
    category: "navigation",
    execution: "navigation",
    remoteSafe: false,
    queueable: false,
    availability: "enabled",
    arguments: [
      {
        name: "task-id",
        label: "task_id",
        required: true,
        description: "Canonical task identity.",
      },
    ],
    keywords: ["open", "goto", "show", "task", "detail"],
  },
  {
    id: "command.navigation.settings",
    trigger: "settings",
    aliases: ["config"],
    title: "Open settings",
    description: "Open local transport and workbench settings.",
    category: "navigation",
    execution: "navigation",
    remoteSafe: false,
    queueable: false,
    availability: "enabled",
    arguments: [],
    keywords: ["settings", "config", "transport"],
  },
  {
    id: "command.help.commands",
    trigger: "help",
    aliases: ["commands", "?"],
    title: "Command reference",
    description: "Open the local command reference overlay.",
    category: "help",
    execution: "local-overlay",
    remoteSafe: false,
    queueable: false,
    availability: "enabled",
    arguments: [],
    keywords: ["help", "commands", "reference"],
  },
  {
    id: "command.help.keyboard",
    trigger: "keys",
    aliases: ["keyboard", "shortcuts"],
    title: "Keyboard shortcuts",
    description: "Open the local keyboard shortcut overlay.",
    category: "help",
    execution: "local-overlay",
    remoteSafe: false,
    queueable: false,
    availability: "enabled",
    arguments: [],
    keywords: ["keys", "keyboard", "shortcut", "accessibility"],
  },
  {
    id: "command.runtime.status",
    trigger: "status",
    aliases: ["health", "runtime"],
    title: "Runtime status",
    description: "Refresh health and readiness, then open the status overlay.",
    category: "runtime",
    execution: "local-overlay",
    remoteSafe: true,
    queueable: false,
    availability: "enabled",
    arguments: [],
    keywords: ["status", "health", "readiness", "runtime", "transport"],
  },
])

function cloneDefinition(definition: CommandDefinition): CommandDefinition {
  return {
    ...definition,
    aliases: [...definition.aliases],
    arguments: definition.arguments.map((argument) => ({
      ...argument,
      choices: argument.choices ? [...argument.choices] : undefined,
    })),
    keywords: [...definition.keywords],
  }
}

export class CommandCatalog {
  readonly #definitions = new Map<string, CommandDefinition>()
  readonly #triggers = new Map<string, string>()
  #enabled = true
  #disabledReason = "Command runtime is disabled."

  constructor(definitions: readonly CommandDefinition[] = DEFINITIONS) {
    for (const definition of definitions) this.register(definition)
  }

  register(definition: CommandDefinition): void {
    if (!/^command\.[a-z0-9.-]{3,120}$/.test(definition.id)) {
      throw new TypeError(`Invalid command identity: ${definition.id}`)
    }
    if (!/^[a-z?][a-z0-9-]{0,40}$/.test(definition.trigger)) {
      throw new TypeError(`Invalid command trigger: ${definition.trigger}`)
    }
    if (this.#definitions.has(definition.id)) {
      throw new TypeError(`Duplicate command identity: ${definition.id}`)
    }
    const triggers = [definition.trigger, ...definition.aliases]
    for (const trigger of triggers) {
      const normalized = trigger.toLowerCase()
      if (this.#triggers.has(normalized)) {
        throw new TypeError(`Duplicate command trigger: ${normalized}`)
      }
      this.#triggers.set(normalized, definition.id)
    }
    this.#definitions.set(definition.id, cloneDefinition(definition))
  }

  disable(reason = "Command runtime is disabled."): void {
    this.#enabled = false
    this.#disabledReason = reason.trim() || "Command runtime is disabled."
  }

  enable(): void {
    this.#enabled = true
  }

  get enabled(): boolean {
    return this.#enabled
  }

  resolve(trigger: string): CommandDefinition | undefined {
    const id = this.#triggers.get(trigger.trim().replace(/^\//, "").toLowerCase())
    const definition = id ? this.#definitions.get(id) : undefined
    return definition ? cloneDefinition(definition) : undefined
  }

  get(id: string): CommandDefinition | undefined {
    const definition = this.#definitions.get(id)
    return definition ? cloneDefinition(definition) : undefined
  }

  list(): CommandDefinition[] {
    return [...this.#definitions.values()]
      .map(cloneDefinition)
      .sort((left, right) => {
        const category = left.category.localeCompare(right.category)
        return category || left.trigger.localeCompare(right.trigger)
      })
  }

  availability(definition: CommandDefinition, context: CommandContext): CommandAvailabilityResult {
    if (!this.#enabled) return { enabled: false, reason: this.#disabledReason }
    if (definition.remoteSafe && !context.transportEnabled) {
      return { enabled: false, reason: "Typed API transport is unavailable." }
    }
    if (definition.availability === "disabled") {
      return { enabled: false, reason: "This command is disabled." }
    }
    if (definition.availability === "requires-task" && !context.taskId) {
      return { enabled: false, reason: "Select a task first." }
    }
    if (definition.availability === "requires-active-task" && !context.taskActive) {
      return { enabled: false, reason: "Select an active task first." }
    }
    if (definition.availability === "requires-terminal-task" && !context.taskTerminal) {
      return { enabled: false, reason: "Select a terminal or interrupted task first." }
    }
    return { enabled: true }
  }

  search(query: string, context: CommandContext, limit = 12): CommandSuggestion[] {
    const normalized = query.trim().replace(/^\//, "").toLowerCase()
    const terms = normalized.split(/\s+/g).filter(Boolean)
    const suggestions = this.list().map((definition) => {
      const availability = this.availability(definition, context)
      return {
        definition,
        availability,
        score: scoreDefinition(definition, normalized, terms),
      }
    })
    return suggestions
      .filter((entry) => entry.score > Number.NEGATIVE_INFINITY)
      .sort((left, right) => {
        if (left.availability.enabled !== right.availability.enabled) {
          return left.availability.enabled ? -1 : 1
        }
        const score = right.score - left.score
        return score || left.definition.trigger.localeCompare(right.definition.trigger)
      })
      .slice(0, Math.max(1, Math.min(50, Math.floor(limit))))
  }
}

export interface CommandSuggestion {
  definition: CommandDefinition
  availability: CommandAvailabilityResult
  score: number
}

function scoreDefinition(
  definition: CommandDefinition,
  normalized: string,
  terms: readonly string[],
): number {
  if (!normalized) return 1
  let score = 0
  const trigger = definition.trigger.toLowerCase()
  const aliases = definition.aliases.map((alias) => alias.toLowerCase())
  if (trigger === normalized) score += 1000
  else if (trigger.startsWith(normalized)) score += 500 - trigger.length
  else if (trigger.includes(normalized)) score += 250 - trigger.indexOf(normalized)
  for (const alias of aliases) {
    if (alias === normalized) score += 700
    else if (alias.startsWith(normalized)) score += 300 - alias.length
  }
  const haystack = [
    definition.title,
    definition.description,
    ...definition.keywords,
    ...definition.aliases,
  ].join(" ").toLowerCase()
  for (const term of terms) {
    if (!haystack.includes(term)) return Number.NEGATIVE_INFINITY
    score += definition.keywords.includes(term) ? 80 : 20
  }
  return score || Number.NEGATIVE_INFINITY
}

export function defaultCommandCatalog(): CommandCatalog {
  return new CommandCatalog()
}

export function commandUsage(definition: CommandDefinition): string {
  const argumentsText = definition.arguments.map((argument) => {
    const label = argument.choices?.join("|") ?? argument.label
    const value = argument.variadic ? `${label}…` : label
    return argument.required ? `<${value}>` : `[${value}]`
  })
  return [`/${definition.trigger}`, ...argumentsText].join(" ")
}
