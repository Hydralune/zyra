import type {
  CommandAvailabilityDecision,
  CommandDescriptor,
  CommandName,
  CommandSuggestion,
  CommandTaskContext,
} from "./contracts.ts"

const COMMANDS: readonly CommandDescriptor[] = Object.freeze([
  {
    id: "zyra.command.status",
    name: "/status",
    aliases: ["/health", "/state"],
    title: "Runtime status",
    description: "Inspect task, run, worker, scheduler, and runtime health.",
    category: "observability",
    mutation: "read-only",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: true,
    sealedAllowed: true,
    overlay: "status",
    arguments: [
      {
        name: "scope",
        kind: "enum",
        required: false,
        flag: "--scope",
        choices: ["task", "run", "workers", "scheduler", "all"],
        description: "Bound the status projection.",
      },
      {
        name: "verbose",
        kind: "boolean",
        required: false,
        flag: "--verbose",
        aliases: ["-v"],
        description: "Include detailed owner diagnostics.",
      },
    ],
    keywords: ["health", "state", "runtime", "workers", "scheduler"],
  },
  {
    id: "zyra.command.graph",
    name: "/graph",
    aliases: ["/topology", "/dag"],
    title: "Task graph",
    description: "Inspect the canonical dynamic task graph and current routes.",
    category: "observability",
    mutation: "read-only",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: true,
    sealedAllowed: true,
    overlay: "graph",
    arguments: [
      {
        name: "focus",
        kind: "identity",
        required: false,
        flag: "--focus",
        description: "Focus a node, edge, worker, or route identity.",
      },
      {
        name: "depth",
        kind: "integer",
        required: false,
        flag: "--depth",
        minimum: 1,
        maximum: 20,
        description: "Limit graph traversal depth.",
      },
    ],
    keywords: ["topology", "nodes", "edges", "routes", "workers"],
  },
  {
    id: "zyra.command.trace",
    name: "/trace",
    aliases: ["/events", "/timeline"],
    title: "Causal trace",
    description: "Inspect canonical causal events and reverse-linked effects.",
    category: "observability",
    mutation: "read-only",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: true,
    sealedAllowed: true,
    overlay: "trace",
    arguments: [
      {
        name: "query",
        kind: "string",
        required: false,
        variadic: true,
        maximumBytes: 8192,
        description: "Search event summaries and causal identities.",
      },
      {
        name: "limit",
        kind: "integer",
        required: false,
        flag: "--limit",
        minimum: 1,
        maximum: 5000,
        description: "Maximum trace rows.",
      },
    ],
    keywords: ["events", "timeline", "causal", "span", "mutation"],
  },
  {
    id: "zyra.command.artifacts",
    name: "/artifacts",
    aliases: ["/files", "/outputs"],
    title: "Artifacts",
    description: "Inspect canonical task artifacts and revision lineage.",
    category: "artifact",
    mutation: "read-only",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: true,
    sealedAllowed: true,
    overlay: "artifacts",
    arguments: [
      {
        name: "query",
        kind: "string",
        required: false,
        variadic: true,
        maximumBytes: 8192,
        description: "Filter artifacts by title, kind, or identity.",
      },
      {
        name: "kind",
        kind: "string",
        required: false,
        flag: "--kind",
        maximumBytes: 256,
        description: "Filter by canonical artifact kind.",
      },
    ],
    keywords: ["files", "outputs", "revisions", "download", "preview"],
  },
  {
    id: "zyra.command.permissions",
    name: "/permissions",
    aliases: ["/permission", "/policy"],
    title: "Permissions",
    description: "Inspect permission policy, requests, decisions, and receipts.",
    category: "permission",
    mutation: "permission",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: false,
    sealedAllowed: false,
    overlay: "permissions",
    arguments: [
      {
        name: "action",
        kind: "enum",
        required: false,
        choices: ["list", "show", "allow", "deny"],
        description: "Inspect or submit one explicit permission decision.",
      },
      {
        name: "request",
        kind: "identity",
        required: false,
        description: "Permission request identity for show/allow/deny.",
      },
      {
        name: "reason",
        kind: "string",
        required: false,
        flag: "--reason",
        maximumBytes: 8192,
        description: "Auditable decision reason.",
      },
    ],
    keywords: ["permission", "approval", "allow", "deny", "policy"],
  },
  {
    id: "zyra.command.btw",
    name: "/btw",
    aliases: ["/side", "/aside"],
    title: "Side question",
    description: "Ask one independent, tool-disabled side question.",
    category: "side-question",
    mutation: "read-only",
    availability: "active-task",
    queueable: false,
    remoteSafe: true,
    immediate: true,
    sealedAllowed: true,
    overlay: "btw",
    arguments: [
      {
        name: "question",
        kind: "string",
        required: true,
        variadic: true,
        maximumBytes: 65536,
        description: "Single-turn question that cannot use tools.",
      },
    ],
    keywords: ["side question", "independent", "tool-free", "usage"],
  },
  {
    id: "zyra.command.inject",
    name: "/inject",
    aliases: ["/fault", "/fail"],
    title: "Inject fault",
    description: "Inject a typed failure through the task graph owner.",
    category: "intervention",
    mutation: "task-graph",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: false,
    sealedAllowed: true,
    overlay: "receipt",
    arguments: [
      {
        name: "kind",
        kind: "enum",
        required: true,
        choices: [
          "worker_failure",
          "node_failure",
          "provider_failure",
          "timeout",
          "disconnect",
          "artifact_failure",
        ],
        description: "Canonical failure kind.",
      },
      {
        name: "target",
        kind: "identity",
        required: false,
        flag: "--target",
        description: "Target node, worker, provider, or artifact identity.",
      },
      {
        name: "reason",
        kind: "string",
        required: false,
        flag: "--reason",
        maximumBytes: 8192,
        description: "Fault reason stored in canonical evidence.",
      },
    ],
    keywords: ["fault", "failure", "worker", "node", "recovery"],
  },
  {
    id: "zyra.command.change",
    name: "/change",
    aliases: ["/requirement", "/需求变更"],
    title: "Requirement change",
    description: "Apply a requirement change through the task graph owner.",
    category: "intervention",
    mutation: "task-graph",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: false,
    sealedAllowed: true,
    overlay: "receipt",
    arguments: [
      {
        name: "instruction",
        kind: "string",
        required: true,
        variadic: true,
        maximumBytes: 65536,
        description: "New or changed requirement.",
      },
      {
        name: "scope",
        kind: "identity",
        required: false,
        flag: "--scope",
        description: "Optional graph node or task scope.",
      },
    ],
    keywords: ["requirement", "change", "replan", "scope"],
  },
  {
    id: "zyra.command.verify",
    name: "/verify",
    aliases: ["/check", "/validate"],
    title: "Verify task",
    description: "Run task verification through the canonical verifier.",
    category: "verification",
    mutation: "task-graph",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: false,
    sealedAllowed: true,
    overlay: "receipt",
    arguments: [
      {
        name: "scope",
        kind: "enum",
        required: false,
        flag: "--scope",
        choices: ["changed", "task", "artifacts", "trace", "all"],
        description: "Verification scope.",
      },
      {
        name: "strict",
        kind: "boolean",
        required: false,
        flag: "--strict",
        description: "Fail on every unresolved verifier finding.",
      },
    ],
    keywords: ["verify", "check", "tests", "evidence", "artifacts"],
  },
  {
    id: "zyra.command.eval",
    name: "/eval",
    aliases: ["/evaluate", "/score"],
    title: "Evaluate trace",
    description: "Evaluate the current task trace and competition evidence.",
    category: "verification",
    mutation: "task-graph",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: false,
    sealedAllowed: true,
    overlay: "receipt",
    arguments: [
      {
        name: "profile",
        kind: "string",
        required: false,
        flag: "--profile",
        maximumBytes: 256,
        description: "Evaluation profile identity.",
      },
      {
        name: "sealed",
        kind: "boolean",
        required: false,
        flag: "--sealed",
        description: "Require sealed-autonomous evaluation rules.",
      },
    ],
    keywords: ["evaluation", "score", "competition", "trace", "evidence"],
  },
  {
    id: "zyra.command.doctor",
    name: "/doctor",
    aliases: ["/diagnose", "/diagnostics"],
    title: "Runtime doctor",
    description: "Run real runtime, owner, dependency, and path diagnostics.",
    category: "health",
    mutation: "read-only",
    availability: "active-task",
    queueable: true,
    remoteSafe: true,
    immediate: true,
    sealedAllowed: true,
    overlay: "doctor",
    arguments: [
      {
        name: "scope",
        kind: "enum",
        required: false,
        flag: "--scope",
        choices: ["runtime", "owners", "dependencies", "paths", "all"],
        description: "Diagnostic scope.",
      },
      {
        name: "repair",
        kind: "boolean",
        required: false,
        flag: "--repair",
        description: "Request bounded owner-approved repair.",
      },
    ],
    keywords: ["doctor", "diagnose", "health", "dependency", "owner"],
  },
])

function cloneDescriptor(value: CommandDescriptor): CommandDescriptor {
  return {
    ...value,
    aliases: [...value.aliases],
    arguments: value.arguments.map((argument) => ({
      ...argument,
      aliases: argument.aliases ? [...argument.aliases] : undefined,
      choices: argument.choices ? [...argument.choices] : undefined,
    })),
    keywords: [...value.keywords],
  }
}

function normalizeTrigger(value: string): string {
  const normalized = value.trim().toLowerCase()
  if (!normalized) return ""
  return normalized.startsWith("/") ? normalized : `/${normalized}`
}

function availability(
  descriptor: CommandDescriptor,
  context: CommandTaskContext,
): CommandAvailabilityDecision {
  if (!context.transportEnabled) {
    return {
      allowed: false,
      code: "transport-unavailable",
      reason: "Typed command transport is unavailable.",
    }
  }
  if (!context.taskId || !context.runId) {
    return {
      allowed: false,
      code: "task-required",
      reason: "Select a task and run first.",
    }
  }
  if (descriptor.availability === "disabled") {
    return {
      allowed: false,
      code: "disabled",
      reason: "This command is disabled.",
    }
  }
  if (descriptor.availability === "active-task" && !context.active) {
    return {
      allowed: false,
      code: "active-task-required",
      reason: "Select an active task first.",
    }
  }
  if (descriptor.availability === "terminal-task" && !context.terminal) {
    return {
      allowed: false,
      code: "terminal-task-required",
      reason: "Select a terminal task first.",
    }
  }
  if (context.remote && !descriptor.remoteSafe) {
    return {
      allowed: false,
      code: "remote-unsafe",
      reason: "This command is unavailable through a remote console.",
    }
  }
  if (context.sealed && !descriptor.sealedAllowed) {
    return {
      allowed: false,
      code: "sealed-forbidden",
      reason: "Sealed autonomous mode rejects this interactive command.",
    }
  }
  return {
    allowed: true,
    code: "allow",
  }
}

function textPositions(text: string, query: string): [number, number][] {
  const source = text.toLocaleLowerCase()
  const target = query.toLocaleLowerCase()
  if (!target) return []
  const direct = source.indexOf(target)
  if (direct >= 0) return [[direct, direct + target.length]]
  const ranges: [number, number][] = []
  let cursor = 0
  let range: [number, number] | undefined
  for (const character of target) {
    const found = source.indexOf(character, cursor)
    if (found < 0) return []
    if (range && found === range[1]) {
      range[1] += 1
    } else {
      range = [found, found + 1]
      ranges.push(range)
    }
    cursor = found + 1
  }
  return ranges
}

function score(
  descriptor: CommandDescriptor,
  query: string,
): { score: number; matched: [number, number][] } | undefined {
  const normalized = normalizeTrigger(query)
  const bare = normalized.replace(/^\//, "")
  if (!bare) return { score: 1, matched: [] }
  const name = descriptor.name.replace(/^\//, "")
  if (name === bare) {
    return {
      score: 10_000,
      matched: [[1, descriptor.name.length]],
    }
  }
  if (name.startsWith(bare)) {
    return {
      score: 8_000 - name.length,
      matched: [[1, 1 + bare.length]],
    }
  }
  for (const alias of descriptor.aliases) {
    const aliasBare = alias.replace(/^\//, "").toLowerCase()
    if (aliasBare === bare) {
      return {
        score: 7_000,
        matched: [[0, descriptor.name.length]],
      }
    }
    if (aliasBare.startsWith(bare)) {
      return {
        score: 6_000 - aliasBare.length,
        matched: [[0, descriptor.name.length]],
      }
    }
  }
  const fields = [
    descriptor.title,
    descriptor.description,
    descriptor.category,
    ...descriptor.keywords,
    ...descriptor.aliases,
  ]
  let total = 0
  let best: [number, number][] = []
  for (const field of fields) {
    const positions = textPositions(field, bare)
    if (!positions.length) continue
    const contiguous = positions.length === 1
    const candidate =
      (contiguous ? 1200 : 600) -
      positions[0]![0] -
      positions.length * 8
    if (candidate > total) {
      total = candidate
      best = positions
    }
  }
  if (!total) return undefined
  return {
    score: total,
    matched: best,
  }
}

export class CommandRegistry {
  readonly #byName = new Map<string, CommandDescriptor>()
  readonly #byId = new Map<string, CommandDescriptor>()
  #enabled = true
  #disabledReason = "Command registry is disabled."

  constructor(descriptors: readonly CommandDescriptor[] = COMMANDS) {
    for (const descriptor of descriptors) {
      this.register(descriptor)
    }
  }

  register(input: CommandDescriptor): void {
    const descriptor = cloneDescriptor(input)
    if (!/^zyra\.command\.[a-z0-9-]{2,64}$/.test(descriptor.id)) {
      throw new TypeError(`Invalid command descriptor identity: ${descriptor.id}`)
    }
    if (!/^\/[a-z][a-z0-9-]{1,40}$/.test(descriptor.name)) {
      throw new TypeError(`Invalid command name: ${descriptor.name}`)
    }
    if (this.#byId.has(descriptor.id)) {
      throw new TypeError(`Duplicate command descriptor: ${descriptor.id}`)
    }
    const triggers = [descriptor.name, ...descriptor.aliases]
    for (const trigger of triggers) {
      const normalized = normalizeTrigger(trigger)
      if (this.#byName.has(normalized)) {
        throw new TypeError(`Duplicate command trigger: ${normalized}`)
      }
      this.#byName.set(normalized, descriptor)
    }
    this.#byId.set(descriptor.id, descriptor)
  }

  disable(reason = "Command registry is disabled."): void {
    this.#enabled = false
    this.#disabledReason = reason.trim() || "Command registry is disabled."
  }

  enable(): void {
    this.#enabled = true
  }

  get enabled(): boolean {
    return this.#enabled
  }

  resolve(value: string): CommandDescriptor | undefined {
    const found = this.#byName.get(normalizeTrigger(value))
    return found ? cloneDescriptor(found) : undefined
  }

  require(value: string): CommandDescriptor {
    const found = this.resolve(value)
    if (!found) {
      throw new TypeError(`Unknown Zyra control command: ${normalizeTrigger(value)}`)
    }
    return found
  }

  get(id: string): CommandDescriptor | undefined {
    const found = this.#byId.get(id)
    return found ? cloneDescriptor(found) : undefined
  }

  list(): CommandDescriptor[] {
    return [...this.#byId.values()]
      .sort((left, right) => {
        const category = left.category.localeCompare(right.category)
        if (category) return category
        return left.name.localeCompare(right.name)
      })
      .map(cloneDescriptor)
  }

  names(): CommandName[] {
    return this.list().map((descriptor) => descriptor.name)
  }

  availability(
    descriptor: CommandDescriptor,
    context: CommandTaskContext,
  ): CommandAvailabilityDecision {
    if (!this.#enabled) {
      return {
        allowed: false,
        code: "disabled",
        reason: this.#disabledReason,
      }
    }
    return availability(descriptor, context)
  }

  search(
    query: string,
    context: CommandTaskContext,
    limit = 12,
  ): CommandSuggestion[] {
    const bounded = Math.max(1, Math.min(50, Math.floor(limit)))
    return this.list()
      .map((descriptor): CommandSuggestion | undefined => {
        const result = score(descriptor, query)
        if (!result) return undefined
        const decision = this.availability(descriptor, context)
        return {
          id: descriptor.id,
          descriptor,
          label: descriptor.name,
          description: descriptor.description,
          category: descriptor.category,
          matched: result.matched,
          score: result.score + (decision.allowed ? 100 : -100),
          disabled: !decision.allowed,
          disabledReason: decision.reason,
        }
      })
      .filter((value): value is CommandSuggestion => value !== undefined)
      .sort((left, right) => {
        if (left.disabled !== right.disabled) return left.disabled ? 1 : -1
        const scoreDifference = right.score - left.score
        if (scoreDifference) return scoreDifference
        return left.label.localeCompare(right.label)
      })
      .slice(0, bounded)
  }

  audit(): {
    valid: boolean
    commandCount: number
    triggerCount: number
    errors: string[]
  } {
    const errors: string[] = []
    const required = new Set<CommandName>([
      "/status",
      "/graph",
      "/trace",
      "/artifacts",
      "/permissions",
      "/btw",
      "/inject",
      "/change",
      "/verify",
      "/eval",
      "/doctor",
    ])
    for (const descriptor of this.#byId.values()) {
      required.delete(descriptor.name)
      if (!descriptor.arguments.every((argument) => argument.description.trim())) {
        errors.push(`${descriptor.name} has an undocumented argument.`)
      }
      if (
        descriptor.mutation !== "read-only" &&
        descriptor.immediate
      ) {
        errors.push(`${descriptor.name} is mutating but marked immediate.`)
      }
      if (
        descriptor.name === "/btw" &&
        (
          descriptor.mutation !== "read-only" ||
          !descriptor.immediate ||
          descriptor.queueable
        )
      ) {
        errors.push("/btw violates the independent side-question contract.")
      }
    }
    for (const missing of required) {
      errors.push(`Required command is missing: ${missing}`)
    }
    return {
      valid: errors.length === 0,
      commandCount: this.#byId.size,
      triggerCount: this.#byName.size,
      errors,
    }
  }
}

export function createCommandRegistry(): CommandRegistry {
  return new CommandRegistry()
}

export function commandUsage(descriptor: CommandDescriptor): string {
  const segments = descriptor.arguments.map((argument) => {
    const label =
      argument.flag ??
      argument.choices?.join("|") ??
      argument.placeholder ??
      argument.name
    const value = argument.variadic ? `${label}…` : label
    return argument.required ? `<${value}>` : `[${value}]`
  })
  return [descriptor.name, ...segments].join(" ")
}
