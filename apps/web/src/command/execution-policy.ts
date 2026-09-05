import type {
  CommandAvailabilityResult,
  CommandContext,
  CommandDefinition,
} from "./catalog.ts"
import type { ParsedInput } from "./parser.ts"

export type ExecutionPolicyCode =
  | "allow"
  | "empty"
  | "unknown-command"
  | "disabled"
  | "transport-unavailable"
  | "task-required"
  | "active-task-required"
  | "terminal-task-required"
  | "remote-command-required"
  | "input-too-large"
  | "invalid-origin"

export interface ExecutionPolicyInput {
  parsed: ParsedInput
  context: CommandContext
  availability?: CommandAvailabilityResult
  origin: "keyboard" | "button" | "overlay" | "recovery"
  busy: boolean
  allowQueue: boolean
}

export interface ExecutionPolicyDecision {
  allowed: boolean
  code: ExecutionPolicyCode
  reason: string
  queue: boolean
  remote: boolean
  effect: CommandDefinition["execution"] | "task-create"
}

function denied(
  code: Exclude<ExecutionPolicyCode, "allow">,
  reason: string,
  effect: ExecutionPolicyDecision["effect"],
): ExecutionPolicyDecision {
  return {
    allowed: false,
    code,
    reason,
    queue: false,
    remote: false,
    effect,
  }
}

export class CommandExecutionPolicy {
  #enabled = true
  #disabledReason = "Command execution policy is disabled."
  #maximumBytes = 256 * 1024

  disable(reason = "Command execution policy is disabled."): void {
    this.#enabled = false
    this.#disabledReason = reason.trim() || "Command execution policy is disabled."
  }

  enable(): void {
    this.#enabled = true
  }

  setMaximumBytes(value: number): number {
    if (!Number.isFinite(value)) throw new TypeError("Maximum command size must be finite.")
    this.#maximumBytes = Math.max(1024, Math.min(1024 * 1024, Math.floor(value)))
    return this.#maximumBytes
  }

  decide(input: ExecutionPolicyInput): ExecutionPolicyDecision {
    const effect =
      input.parsed.kind === "command" && input.parsed.definition
        ? input.parsed.definition.execution
        : "task-create"
    if (!this.#enabled) return denied("disabled", this.#disabledReason, effect)
    if (input.parsed.kind === "empty") return denied("empty", "Command input is empty.", effect)
    if (new TextEncoder().encode(input.parsed.raw).byteLength > this.#maximumBytes) {
      return denied("input-too-large", `Command input exceeds ${this.#maximumBytes} bytes.`, effect)
    }
    if (input.parsed.kind === "command" && !input.parsed.definition) {
      return denied("unknown-command", `Unknown command: /${input.parsed.trigger}`, effect)
    }
    if (!["keyboard", "button", "overlay", "recovery"].includes(input.origin)) {
      return denied("invalid-origin", "Command origin is not trusted.", effect)
    }
    const definition = input.parsed.kind === "command" ? input.parsed.definition : undefined
    if (definition && input.availability && !input.availability.enabled) {
      const reason = input.availability.reason ?? "Command is disabled."
      if (definition.remoteSafe && !input.context.transportEnabled) {
        return denied("transport-unavailable", reason, effect)
      }
      if (definition.availability === "requires-active-task" && !input.context.taskActive) {
        return denied("active-task-required", reason, effect)
      }
      if (definition.availability === "requires-terminal-task" && !input.context.taskTerminal) {
        return denied("terminal-task-required", reason, effect)
      }
      if (definition.availability === "requires-task" && !input.context.taskId) {
        return denied("task-required", reason, effect)
      }
      return denied("disabled", reason, effect)
    }
    const remote =
      input.parsed.kind === "prompt" ||
      definition?.remoteSafe === true
    if (remote && !input.context.transportEnabled) {
      return denied("transport-unavailable", "Typed API transport is unavailable.", effect)
    }
    if (
      input.origin === "recovery" &&
      definition &&
      !definition.remoteSafe &&
      definition.execution !== "navigation"
    ) {
      return denied(
        "remote-command-required",
        "Recovery may replay only remote-safe or navigation commands.",
        effect,
      )
    }
    const queue =
      input.busy &&
      input.allowQueue &&
      (
        input.parsed.kind === "prompt" ||
        definition?.queueable === true
      )
    return {
      allowed: true,
      code: "allow",
      reason: queue ? "Eligible input will be queued behind active work." : "Execution allowed.",
      queue,
      remote,
      effect,
    }
  }

  assert(input: ExecutionPolicyInput): ExecutionPolicyDecision {
    const result = this.decide(input)
    if (!result.allowed) throw new TypeError(result.reason)
    return result
  }
}
