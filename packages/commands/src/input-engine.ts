import type {
  CommandDeliveryMode,
  CommandParseError,
  CommandReceipt,
  CommandTaskContext,
  ParsedCommand,
} from "./contracts.ts"
import { commandBytes, commandHash } from "./identity.ts"
import type { CommandHistoryStore } from "./history.ts"
import type { CommandPalette, PaletteEntry } from "./palette.ts"
import { parseCommand } from "./parser.ts"
import type { CommandRegistry } from "./registry.ts"

export type CommandInputPhase =
  | "idle"
  | "editing"
  | "composing"
  | "submitting"
  | "disabled"
  | "closed"

export type CommandInputAction =
  | "none"
  | "newline"
  | "submit"
  | "apply-suggestion"
  | "move-suggestion"
  | "close-suggestions"
  | "cancel-active"
  | "history-previous"
  | "history-next"

export interface CommandInputSelection {
  start: number
  end: number
  direction: "forward" | "backward" | "none"
}

export interface CommandInputCapture {
  id: string
  text: string
  cursor: number
  selection: CommandInputSelection
  mode: CommandDeliveryMode
  taskId?: string
  runId?: string
  sessionId?: string
  createdAt: number
  fingerprint: string
}

export interface CommandInputSnapshot {
  phase: CommandInputPhase
  enabled: boolean
  revision: number
  value: string
  cursor: number
  selection: CommandInputSelection
  composing: boolean
  bytes: number
  maximumBytes: number
  withinLimit: boolean
  command: boolean
  knownCommand: boolean
  parsed: ParsedCommand
  errors: readonly CommandParseError[]
  canSubmit: boolean
  suggestedMode: CommandDeliveryMode
  paletteOpen: boolean
  selectedSuggestion?: PaletteEntry
  activeCapture?: CommandInputCapture
  failedCapture?: CommandInputCapture
  lastReceipt?: CommandReceipt
  lastError?: string
}

export interface CommandInputKey {
  key: string
  shiftKey?: boolean
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
  composing?: boolean
  repeat?: boolean
}

export interface CommandInputDecision {
  handled: boolean
  preventDefault: boolean
  stopPropagation: boolean
  action: CommandInputAction
  mode?: CommandDeliveryMode
  value?: string
  cursor?: number
  selected?: PaletteEntry
  reason?: string
}

function clamp(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum
  return Math.max(minimum, Math.min(maximum, Math.floor(value)))
}

function selection(
  value: string,
  input?: Partial<CommandInputSelection>,
  cursor?: number,
): CommandInputSelection {
  const fallback = clamp(cursor ?? value.length, 0, value.length)
  const start = clamp(input?.start ?? fallback, 0, value.length)
  const end = clamp(input?.end ?? start, start, value.length)
  const direction =
    input?.direction === "forward" || input?.direction === "backward"
      ? input.direction
      : "none"
  return Object.freeze({ start, end, direction })
}

function copyCapture(value: CommandInputCapture): CommandInputCapture {
  return {
    ...value,
    selection: { ...value.selection },
  }
}

function emptyParsed(registry: CommandRegistry): ParsedCommand {
  return parseCommand("", registry)
}

function commandTrigger(value: string): string {
  return value.trimStart().split(/\s+/, 1)[0]?.toLowerCase() ?? ""
}

function inputMode(input: CommandInputKey): CommandDeliveryMode {
  if (input.altKey && (input.ctrlKey || input.metaKey)) return "interrupt"
  if (input.altKey) return "steer"
  return "enqueue"
}

function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : String(error || "Command submission failed.")
}

export class CommandInputEngine {
  readonly #registry: CommandRegistry
  readonly #palette: CommandPalette
  readonly #history: CommandHistoryStore
  readonly #context: () => CommandTaskContext
  readonly #now: () => number
  readonly #listeners = new Set<() => void>()
  readonly #captures = new Map<string, CommandInputCapture>()
  #maximumBytes = 256 * 1024
  #snapshot: CommandInputSnapshot
  #closed = false
  #disabledReason = "Command input is disabled."

  constructor(options: {
    registry: CommandRegistry
    palette: CommandPalette
    history: CommandHistoryStore
    context: () => CommandTaskContext
    now?: () => number
    maximumBytes?: number
  }) {
    this.#registry = options.registry
    this.#palette = options.palette
    this.#history = options.history
    this.#context = options.context
    this.#now = options.now ?? Date.now
    if (options.maximumBytes !== undefined) {
      this.#maximumBytes = Math.max(
        1024,
        Math.min(1024 * 1024, Math.floor(options.maximumBytes)),
      )
    }
    const parsed = emptyParsed(this.#registry)
    this.#snapshot = Object.freeze({
      phase: "idle",
      enabled: true,
      revision: 0,
      value: "",
      cursor: 0,
      selection: selection(""),
      composing: false,
      bytes: 0,
      maximumBytes: this.#maximumBytes,
      withinLimit: true,
      command: false,
      knownCommand: false,
      parsed,
      errors: Object.freeze([]),
      canSubmit: false,
      suggestedMode: "enqueue",
      paletteOpen: false,
    })
  }

  getSnapshot = (): CommandInputSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  update(input: {
    value: string
    cursor?: number
    selection?: Partial<CommandInputSelection>
    composing?: boolean
    mode?: CommandDeliveryMode
  }): CommandInputSnapshot {
    this.#assertAvailable()
    const value = String(input.value)
    const selected = selection(value, input.selection, input.cursor)
    const cursor = clamp(input.cursor ?? selected.end, 0, value.length)
    const composing = input.composing === true
    const bytes = commandBytes(value)
    const trimmed = value.trim()
    const command = trimmed.startsWith("/")
    const trigger = commandTrigger(value)
    const knownCommand = command && Boolean(this.#registry.resolve(trigger))
    const parsed = command ? parseCommand(value, this.#registry) : emptyParsed(this.#registry)
    const errors = command ? parsed.errors : []
    const palette = command
      ? this.#palette.update(value, cursor)
      : (this.#palette.close(), this.#palette.getSnapshot())
    const withinLimit = bytes <= this.#maximumBytes
    const context = this.#context()
    const canSubmit =
      Boolean(trimmed) &&
      withinLimit &&
      !composing &&
      context.transportEnabled &&
      (
        !command ||
        (
          knownCommand &&
          errors.length === 0 &&
          Boolean(context.taskId) &&
          Boolean(context.runId)
        )
      )
    this.#replace({
      phase: composing ? "composing" : trimmed ? "editing" : "idle",
      value,
      cursor,
      selection: selected,
      composing,
      bytes,
      maximumBytes: this.#maximumBytes,
      withinLimit,
      command,
      knownCommand,
      parsed,
      errors: Object.freeze([...errors]),
      canSubmit,
      suggestedMode: input.mode ?? this.#snapshot.suggestedMode,
      paletteOpen: palette.open,
      selectedSuggestion: palette.entries[palette.selectedIndex],
      lastError:
        withinLimit
          ? undefined
          : `Command input exceeds ${this.#maximumBytes} bytes.`,
    })
    return this.#snapshot
  }

  compositionStart(): void {
    this.#assertAvailable()
    this.#replace({
      phase: "composing",
      composing: true,
      canSubmit: false,
    })
  }

  compositionEnd(value = this.#snapshot.value, cursor = this.#snapshot.cursor): void {
    this.#assertAvailable()
    this.update({ value, cursor, composing: false })
  }

  key(input: CommandInputKey): CommandInputDecision {
    this.#assertAvailable()
    if (input.composing || this.#snapshot.composing || input.key === "Process") {
      return this.#decision("none", false)
    }
    const palette = this.#palette.getSnapshot()
    if (palette.open) {
      if (input.key === "ArrowDown" || input.key === "ArrowUp") {
        const selected = this.#palette.move(
          input.key === "ArrowDown" ? "next" : "previous",
        )
        this.#replace({
          paletteOpen: true,
          selectedSuggestion: selected,
        })
        return this.#decision("move-suggestion", true, { selected })
      }
      if (
        input.key === "Home" &&
        (input.ctrlKey || input.metaKey)
      ) {
        const selected = this.#palette.move("first")
        this.#replace({ selectedSuggestion: selected })
        return this.#decision("move-suggestion", true, { selected })
      }
      if (
        input.key === "End" &&
        (input.ctrlKey || input.metaKey)
      ) {
        const selected = this.#palette.move("last")
        this.#replace({ selectedSuggestion: selected })
        return this.#decision("move-suggestion", true, { selected })
      }
      if (input.key === "Tab" && !input.shiftKey) {
        const applied = this.#palette.apply(this.#snapshot.value)
        if (!applied) {
          return this.#decision("none", false, {
            reason: "No enabled palette suggestion is selected.",
          })
        }
        this.update({
          value: applied.value,
          cursor: applied.cursor,
          mode: this.#snapshot.suggestedMode,
        })
        return this.#decision("apply-suggestion", true, {
          value: applied.value,
          cursor: applied.cursor,
          selected: applied.entry,
        })
      }
      if (input.key === "Escape") {
        this.#palette.close()
        this.#replace({
          paletteOpen: false,
          selectedSuggestion: undefined,
        })
        return this.#decision("close-suggestions", true)
      }
    }
    if (input.key === "Enter") {
      if (input.shiftKey && !input.altKey) {
        return this.#decision("newline", false)
      }
      if (!this.#snapshot.canSubmit) {
        return this.#decision("none", false, {
          reason: this.#snapshot.lastError ||
            this.#snapshot.errors[0]?.message ||
            "Command input is not ready to submit.",
        })
      }
      const mode = inputMode(input)
      this.#replace({ suggestedMode: mode })
      return this.#decision("submit", true, { mode })
    }
    if (input.key === "Escape") {
      return this.#decision("cancel-active", true)
    }
    if (
      input.key === "ArrowUp" &&
      this.#snapshot.selection.start === 0 &&
      this.#snapshot.selection.end === 0
    ) {
      return this.#decision("history-previous", true)
    }
    if (
      input.key === "ArrowDown" &&
      this.#snapshot.selection.start === this.#snapshot.value.length &&
      this.#snapshot.selection.end === this.#snapshot.value.length
    ) {
      return this.#decision("history-next", true)
    }
    return this.#decision("none", false)
  }

  history(direction: "previous" | "next"): CommandInputSnapshot {
    this.#assertAvailable()
    const context = this.#context()
    const result = this.#history.navigate({
      scope: context.taskId,
      direction,
      draft: this.#snapshot.value,
      query: this.#snapshot.command ? this.#snapshot.parsed.trigger : undefined,
    })
    if (!result.handled) return this.#snapshot
    return this.update({
      value: result.value,
      cursor: result.cursor,
      mode: this.#snapshot.suggestedMode,
    })
  }

  beginSubmit(
    value = this.#snapshot.value,
    mode = this.#snapshot.suggestedMode,
  ): CommandInputCapture {
    this.#assertAvailable()
    if (
      value !== this.#snapshot.value ||
      !this.#snapshot.canSubmit
    ) {
      this.update({
        value,
        cursor: value.length,
        mode,
      })
    }
    if (!this.#snapshot.canSubmit) {
      throw new TypeError(
        this.#snapshot.lastError ||
        this.#snapshot.errors[0]?.message ||
        "Command input cannot be submitted.",
      )
    }
    const context = this.#context()
    const now = this.#now()
    const fingerprint = commandHash([
      context.taskId ?? "",
      context.runId ?? "",
      context.sessionId ?? "",
      mode,
      value.trim(),
    ].join("\u0000"))
    const capture: CommandInputCapture = {
      id: `command-input:${fingerprint}:${now.toString(36)}`,
      text: value,
      cursor: this.#snapshot.cursor,
      selection: { ...this.#snapshot.selection },
      mode,
      taskId: context.taskId,
      runId: context.runId,
      sessionId: context.sessionId,
      createdAt: now,
      fingerprint,
    }
    const duplicate = [...this.#captures.values()]
      .find((candidate) => candidate.fingerprint === fingerprint)
    if (duplicate) return copyCapture(duplicate)
    this.#captures.set(capture.id, capture)
    this.#replace({
      phase: "submitting",
      activeCapture: copyCapture(capture),
      failedCapture: undefined,
      lastError: undefined,
      canSubmit: false,
    })
    return copyCapture(capture)
  }

  settle(captureId: string, receipt: CommandReceipt): void {
    this.#assertAvailable()
    const capture = this.#captures.get(captureId)
    if (!capture) throw new TypeError("Unknown command input capture.")
    if (
      capture.taskId !== receipt.taskId ||
      capture.runId !== receipt.runId
    ) {
      throw new TypeError(
        "Command input receipt does not match the captured task/run binding.",
      )
    }
    this.#captures.delete(captureId)
    this.#palette.remember(capture.text)
    this.#history.record({
      text: capture.text,
      scope: capture.taskId,
      taskId: capture.taskId,
      runId: capture.runId,
      sessionId: capture.sessionId,
      mode: capture.mode,
      receipt,
    })
    this.#replace({
      phase: this.#snapshot.value.trim() ? "editing" : "idle",
      activeCapture: undefined,
      failedCapture: undefined,
      lastReceipt: receipt,
      lastError: receipt.error?.message,
      canSubmit:
        Boolean(this.#snapshot.value.trim()) &&
        this.#snapshot.withinLimit &&
        !this.#snapshot.composing,
    })
  }

  fail(captureId: string, error: unknown): CommandInputCapture | undefined {
    this.#assertAvailable()
    const capture = this.#captures.get(captureId)
    if (!capture) return undefined
    this.#captures.delete(captureId)
    this.update({
      value: capture.text,
      cursor: capture.cursor,
      selection: capture.selection,
      mode: capture.mode,
    })
    this.#replace({
      phase: "editing",
      activeCapture: undefined,
      failedCapture: copyCapture(capture),
      lastError: errorMessage(error),
    })
    return copyCapture(capture)
  }

  clear(): void {
    this.#assertAvailable()
    this.#palette.close()
    this.update({ value: "", cursor: 0, mode: "enqueue" })
    this.#replace({
      failedCapture: undefined,
      lastError: undefined,
    })
  }

  disable(reason = "Command input is disabled."): void {
    if (this.#closed || !this.#snapshot.enabled) return
    this.#disabledReason = reason.trim() || "Command input is disabled."
    this.#palette.close()
    this.#replace({
      phase: "disabled",
      enabled: false,
      canSubmit: false,
      paletteOpen: false,
      selectedSuggestion: undefined,
      lastError: this.#disabledReason,
    })
  }

  enable(): void {
    this.#assertOpen()
    if (this.#snapshot.enabled) return
    this.#replace({
      phase: this.#snapshot.value.trim() ? "editing" : "idle",
      enabled: true,
      lastError: undefined,
    })
    this.update({
      value: this.#snapshot.value,
      cursor: this.#snapshot.cursor,
      selection: this.#snapshot.selection,
      composing: false,
      mode: this.#snapshot.suggestedMode,
    })
  }

  close(reason = "Command input closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#captures.clear()
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      phase: "closed",
      enabled: false,
      canSubmit: false,
      activeCapture: undefined,
      lastError: reason,
      revision: this.#snapshot.revision + 1,
    })
    this.#listeners.clear()
  }

  #decision(
    action: CommandInputAction,
    handled: boolean,
    patch: Partial<CommandInputDecision> = {},
  ): CommandInputDecision {
    return Object.freeze({
      handled,
      preventDefault: handled,
      stopPropagation: handled,
      action,
      ...patch,
    })
  }

  #replace(patch: Partial<CommandInputSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of [...this.#listeners]) {
      try {
        listener()
      } catch {
        // Input observers cannot alter parsed or captured submission state.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command input is closed.")
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) throw new TypeError(this.#disabledReason)
  }
}
