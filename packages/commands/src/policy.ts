import type {
  CommandDeliveryMode,
  CommandPolicyDecision,
  CommandPriority,
  CommandTaskContext,
  ParsedCommand,
} from "./contracts.ts"
import type { CommandRegistry } from "./registry.ts"
import { isReadOnlyCommandAction } from "./registry.ts"

export interface CommandPolicyInput {
  parsed: ParsedCommand
  context: CommandTaskContext
  mode: CommandDeliveryMode
  busy: boolean
}

const SEALED_READ_ACTIONS: Readonly<Record<string, ReadonlySet<string>>> =
  Object.freeze({
    "/mcp": new Set(["", "list", "show"]),
    "/skills": new Set(["", "list", "show"]),
    "/agents": new Set(["", "list", "show"]),
  })

export function sealedMutationReason(
  parsed: ParsedCommand,
  context: CommandTaskContext,
): string | undefined {
  if (!context.sealed || !parsed.descriptor) return undefined
  const allowedActions = SEALED_READ_ACTIONS[parsed.descriptor.name]
  if (!allowedActions) return undefined
  const action = String(parsed.arguments.values.action ?? "").trim().toLowerCase()
  if (allowedActions.has(action)) return undefined
  return `Sealed autonomous mode rejects ${parsed.descriptor.name} ${action || "mutation"}.`
}

export class CommandExecutionPolicy {
  readonly #registry: CommandRegistry
  #enabled = true
  #disabledReason = "Command execution policy is disabled."

  constructor(registry: CommandRegistry) {
    this.#registry = registry
  }

  disable(reason = "Command execution policy is disabled."): void {
    this.#enabled = false
    this.#disabledReason =
      reason.trim() || "Command execution policy is disabled."
  }

  enable(): void {
    this.#enabled = true
  }

  decide(input: CommandPolicyInput): CommandPolicyDecision {
    if (!this.#enabled) {
      return {
        allowed: false,
        queue: false,
        priority: "next",
        mode: input.mode,
        code: "disabled",
        reason: this.#disabledReason,
      }
    }
    if (input.parsed.errors.length || !input.parsed.descriptor) {
      return {
        allowed: false,
        queue: false,
        priority: "next",
        mode: input.mode,
        code: "invalid",
        reason:
          input.parsed.errors.map((entry) => entry.message).join(" ") ||
          "Command input is invalid.",
      }
    }
    const descriptor = input.parsed.descriptor
    const action = String(input.parsed.arguments.values.action ?? "")
    const sealedReason = sealedMutationReason(input.parsed, input.context)
    if (sealedReason) {
      return {
        allowed: false,
        queue: false,
        priority: priorityFor(input.mode),
        mode: input.mode,
        code: "unavailable",
        reason: sealedReason,
      }
    }
    const availability = this.#registry.availability(
      descriptor,
      input.context,
      action,
    )
    if (!availability.allowed) {
      return {
        allowed: false,
        queue: false,
        priority: "next",
        mode: input.mode,
        code: "unavailable",
        reason: availability.reason ?? "Command is unavailable.",
      }
    }
    if (
      input.mode === "interrupt" &&
      isReadOnlyCommandAction(descriptor, action) &&
      descriptor.name !== "/btw"
    ) {
      return {
        allowed: false,
        queue: false,
        priority: "now",
        mode: input.mode,
        code: "interrupt-unsafe",
        reason:
          "Read-only commands cannot interrupt an active session; use steer or enqueue.",
      }
    }
    if (
      input.busy &&
      !descriptor.queueable &&
      !descriptor.immediate
    ) {
      return {
        allowed: false,
        queue: false,
        priority: priorityFor(input.mode),
        mode: input.mode,
        code: "busy-conflict",
        reason: `${descriptor.name} cannot be queued while the session is busy.`,
      }
    }
    return {
      allowed: true,
      queue:
        input.busy &&
        descriptor.queueable &&
        !descriptor.immediate,
      priority: priorityFor(input.mode),
      mode: input.mode,
      code: "allow",
      reason:
        input.busy && descriptor.queueable && !descriptor.immediate ?
          "Command will be admitted to the backend queue."
        : "Command may be submitted.",
    }
  }

  assert(input: CommandPolicyInput): CommandPolicyDecision {
    const decision = this.decide(input)
    if (!decision.allowed) {
      throw new TypeError(decision.reason)
    }
    return decision
  }
}

export function priorityFor(mode: CommandDeliveryMode): CommandPriority {
  if (mode === "interrupt") return "now"
  if (mode === "steer") return "now"
  return "next"
}

export function modeForKeyboard(input: {
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
  shiftKey?: boolean
}): CommandDeliveryMode {
  if (input.altKey && (input.ctrlKey || input.metaKey)) {
    return "interrupt"
  }
  if (input.altKey) return "steer"
  return "enqueue"
}

export function policyLabel(mode: CommandDeliveryMode): string {
  if (mode === "interrupt") return "Interrupt and apply"
  if (mode === "steer") return "Steer next"
  return "Enqueue"
}

export function policyShortcut(mode: CommandDeliveryMode): string {
  if (mode === "interrupt") return "Ctrl+Alt+Enter"
  if (mode === "steer") return "Alt+Enter"
  return "Enter"
}
