export type CommandKeyAction =
  | "submit"
  | "newline"
  | "cancel"
  | "close-suggestions"
  | "suggestion-next"
  | "suggestion-previous"
  | "history-next"
  | "history-previous"
  | "restore-queue"
  | "none"

export interface CommandKeyContext {
  key: string
  shift: boolean
  alt: boolean
  ctrl: boolean
  meta: boolean
  composing: boolean
  suggestionsOpen: boolean
  suggestionCount: number
  value: string
  cursor: number
  busy: boolean
  overlayOpen: boolean
  editableQueuedCount: number
  inHistory: boolean
}

export interface CommandKeyDecision {
  action: CommandKeyAction
  preventDefault: boolean
  stopPropagation: boolean
  reason: string
}

function decision(
  action: CommandKeyAction,
  reason: string,
  options: { preventDefault?: boolean; stopPropagation?: boolean } = {},
): CommandKeyDecision {
  return {
    action,
    preventDefault: options.preventDefault ?? action !== "none",
    stopPropagation: options.stopPropagation ?? false,
    reason,
  }
}

function cursorOnFirstLine(value: string, cursor: number): boolean {
  return !value.slice(0, Math.max(0, cursor)).includes("\n")
}

function cursorOnLastLine(value: string, cursor: number): boolean {
  return !value.slice(Math.max(0, cursor)).includes("\n")
}

export function decideCommandKey(context: CommandKeyContext): CommandKeyDecision {
  if (context.composing) return decision("none", "IME composition owns the key.")
  if (context.key === "Enter") {
    if (context.shift || context.alt) {
      return decision("newline", "Modified Enter inserts a newline.", { preventDefault: false })
    }
    if (context.ctrl || context.meta) {
      return decision("submit", "Primary modifier plus Enter submits.")
    }
    return decision("submit", context.suggestionsOpen
      ? "Enter accepts the active suggestion."
      : "Enter submits the current draft.")
  }
  if (context.key === "Escape") {
    if (context.suggestionsOpen) {
      return decision("close-suggestions", "Escape closes command suggestions.")
    }
    if (context.overlayOpen || context.busy) {
      return decision("cancel", "Escape closes the overlay or cancels active work.")
    }
    if (context.editableQueuedCount > 0) {
      return decision("restore-queue", "Escape restores editable queued input.")
    }
    return decision("none", "There is no active escape target.")
  }
  if (context.key === "ArrowDown") {
    if (context.suggestionsOpen && context.suggestionCount > 0) {
      return decision("suggestion-next", "Arrow Down advances command suggestions.")
    }
    if (cursorOnLastLine(context.value, context.cursor)) {
      return decision("history-next", "Arrow Down advances command history.")
    }
    return decision("none", "The cursor can move inside the multiline draft.")
  }
  if (context.key === "ArrowUp") {
    if (context.suggestionsOpen && context.suggestionCount > 0) {
      return decision("suggestion-previous", "Arrow Up reverses command suggestions.")
    }
    if (cursorOnFirstLine(context.value, context.cursor)) {
      return decision("history-previous", "Arrow Up reverses command history.")
    }
    return decision("none", "The cursor can move inside the multiline draft.")
  }
  return decision("none", "Key is handled by the text editor.")
}

export function selectedSuggestionIndex(
  current: number,
  count: number,
  direction: "next" | "previous",
): number {
  if (count <= 0) return 0
  const normalized = ((current % count) + count) % count
  return direction === "next"
    ? (normalized + 1) % count
    : (normalized - 1 + count) % count
}

export function keyContextFromEvent(
  event: Pick<KeyboardEvent, "key" | "shiftKey" | "altKey" | "ctrlKey" | "metaKey" | "isComposing">,
  input: Omit<CommandKeyContext, "key" | "shift" | "alt" | "ctrl" | "meta" | "composing">,
): CommandKeyContext {
  return {
    ...input,
    key: event.key,
    shift: event.shiftKey,
    alt: event.altKey,
    ctrl: event.ctrlKey,
    meta: event.metaKey,
    composing: event.isComposing,
  }
}
