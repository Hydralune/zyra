import type {
  CommandDeliveryMode,
  CommandPolicyDecision,
  CommandPriority,
  CommandTaskContext,
  ParsedCommand,
} from "./contracts.ts"
import type { CommandRegistry } from "./registry.ts"

export interface CommandPolicyInput {
  parsed: ParsedCommand
  context: CommandTaskContext
  mode: CommandDeliveryMode
  busy: boolean
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
    const availability = this.#registry.availability(
      descriptor,
      input.context,
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
      descriptor.mutation === "read-only" &&
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
