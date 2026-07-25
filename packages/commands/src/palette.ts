import type {
  ArgumentSuggestion,
  CommandCompletion,
  CommandSuggestion,
  CommandTaskContext,
  ParsedCommand,
} from "./contracts.ts"
import {
  applyCommandCompletion,
  argumentSuggestions,
  completionFor,
  parseCommand,
} from "./parser.ts"
import type { CommandRegistry } from "./registry.ts"

export interface PaletteEntry {
  id: string
  kind: "command" | "argument"
  label: string
  description: string
  category: string
  value: string
  disabled: boolean
  disabledReason?: string
  score: number
  matched: readonly [number, number][]
}

export interface PaletteGroup {
  id: string
  label: string
  entries: readonly PaletteEntry[]
}

export interface PaletteSnapshot {
  open: boolean
  query: string
  parsed: ParsedCommand
  completion: CommandCompletion
  entries: readonly PaletteEntry[]
  groups: readonly PaletteGroup[]
  selectedId?: string
  selectedIndex: number
  revision: number
}

function commandEntry(value: CommandSuggestion): PaletteEntry {
  return {
    id: value.id,
    kind: "command",
    label: value.label,
    description: value.description,
    category: value.category,
    value: value.descriptor.name,
    disabled: value.disabled,
    disabledReason: value.disabledReason,
    score: value.score,
    matched: value.matched,
  }
}

function argumentEntry(
  value: ArgumentSuggestion,
  category: string,
): PaletteEntry {
  return {
    id: value.id,
    kind: "argument",
    label: value.label,
    description: value.description,
    category,
    value: value.value,
    disabled: value.disabled ?? false,
    score:
      value.matched.length === 1
        ? 1000 - value.matched[0]![0]
        : 500 - value.matched.length,
    matched: value.matched,
  }
}

function groups(entries: readonly PaletteEntry[]): PaletteGroup[] {
  const values = new Map<string, PaletteEntry[]>()
  for (const entry of entries) {
    const group = values.get(entry.category) ?? []
    group.push(entry)
    values.set(entry.category, group)
  }
  return [...values]
    .map(([id, selected]) => ({
      id,
      label: id
        .split("-")
        .map((part) => `${part[0]?.toUpperCase() ?? ""}${part.slice(1)}`)
        .join(" "),
      entries: Object.freeze(
        [...selected].sort((left, right) => {
          if (left.disabled !== right.disabled) return left.disabled ? 1 : -1
          const difference = right.score - left.score
          if (difference) return difference
          return left.label.localeCompare(right.label)
        }),
      ),
    }))
    .sort((left, right) => {
      const leftEnabled = left.entries.some((entry) => !entry.disabled)
      const rightEnabled = right.entries.some((entry) => !entry.disabled)
      if (leftEnabled !== rightEnabled) return leftEnabled ? -1 : 1
      return left.label.localeCompare(right.label)
    })
}

function unique(entries: readonly PaletteEntry[]): PaletteEntry[] {
  const selected = new Map<string, PaletteEntry>()
  for (const entry of entries) {
    const key = `${entry.kind}:${entry.value}`
    const current = selected.get(key)
    if (!current || entry.score > current.score) {
      selected.set(key, entry)
    }
  }
  return [...selected.values()]
}

function selectedIndex(
  entries: readonly PaletteEntry[],
  selectedId?: string,
): number {
  if (!entries.length) return -1
  const exact = selectedId
    ? entries.findIndex((entry) => entry.id === selectedId)
    : -1
  if (exact >= 0 && !entries[exact]!.disabled) return exact
  const enabled = entries.findIndex((entry) => !entry.disabled)
  return enabled >= 0 ? enabled : 0
}

export class CommandPalette {
  readonly #registry: CommandRegistry
  readonly #context: () => CommandTaskContext
  readonly #listeners = new Set<() => void>()
  readonly #history: string[] = []
  readonly #identitySources = new Map<
    string,
    readonly { id: string; label?: string; description?: string }[]
  >()
  #snapshot: PaletteSnapshot
  #closed = false
  #limit = 16

  constructor(options: {
    registry: CommandRegistry
    context: () => CommandTaskContext
  }) {
    this.#registry = options.registry
    this.#context = options.context
    const parsed = parseCommand("/", this.#registry)
    this.#snapshot = Object.freeze({
      open: false,
      query: "/",
      parsed,
      completion: completionFor("/", 1, this.#registry),
      entries: Object.freeze([]),
      groups: Object.freeze([]),
      selectedIndex: -1,
      revision: 0,
    })
  }

  getSnapshot = (): PaletteSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  setLimit(value: number): number {
    if (!Number.isFinite(value)) {
      throw new TypeError("Palette limit must be finite.")
    }
    this.#limit = Math.max(1, Math.min(100, Math.floor(value)))
    this.update(
      this.#snapshot.query,
      this.#snapshot.completion.replaceEnd,
    )
    return this.#limit
  }

  setIdentities(
    argumentName: string,
    values: readonly { id: string; label?: string; description?: string }[],
  ): void {
    const normalized = argumentName.trim()
    if (!normalized) throw new TypeError("Argument identity source is required.")
    const seen = new Set<string>()
    const selected = values
      .filter((value) => {
        if (!value.id.trim() || seen.has(value.id)) return false
        seen.add(value.id)
        return true
      })
      .slice(0, 5000)
      .map((value) => ({ ...value }))
    this.#identitySources.set(normalized, Object.freeze(selected))
    this.update(
      this.#snapshot.query,
      this.#snapshot.completion.replaceEnd,
    )
  }

  remember(value: string): void {
    const normalized = value.trim()
    if (!normalized) return
    const existing = this.#history.indexOf(normalized)
    if (existing >= 0) this.#history.splice(existing, 1)
    this.#history.unshift(normalized)
    if (this.#history.length > 100) this.#history.length = 100
  }

  open(value = "/", cursor = value.length): PaletteSnapshot {
    this.#assertOpen()
    this.update(value, cursor)
    this.#replace({ open: true })
    return this.#snapshot
  }

  close(): void {
    if (this.#closed || !this.#snapshot.open) return
    this.#replace({
      open: false,
      selectedId: undefined,
      selectedIndex: -1,
    })
  }

  update(value: string, cursor = value.length): PaletteSnapshot {
    this.#assertOpen()
    const parsed = parseCommand(value, this.#registry)
    const completion = completionFor(value, cursor, this.#registry)
    const context = this.#context()
    const commandEntries =
      completion.kind === "command"
        ? this.#registry
            .search(completion.query, context, this.#limit)
            .map(commandEntry)
        : []
    const identityValues = completion.argumentName
      ? this.#identitySources.get(completion.argumentName)
      : undefined
    const argumentEntries =
      completion.kind === "command"
        ? []
        : argumentSuggestions({
            parsed,
            completion,
            identities: identityValues,
            history: this.#history,
            limit: this.#limit,
          }).map((entry) =>
            argumentEntry(
              entry,
              parsed.descriptor?.category ?? "argument",
            ))
    const entries = unique([...commandEntries, ...argumentEntries])
      .sort((left, right) => {
        if (left.disabled !== right.disabled) return left.disabled ? 1 : -1
        const difference = right.score - left.score
        if (difference) return difference
        return left.label.localeCompare(right.label)
      })
      .slice(0, this.#limit)
    const index = selectedIndex(entries, this.#snapshot.selectedId)
    this.#replace({
      open: value.trimStart().startsWith("/"),
      query: value,
      parsed,
      completion,
      entries: Object.freeze(entries),
      groups: Object.freeze(groups(entries)),
      selectedIndex: index,
      selectedId: entries[index]?.id,
    })
    return this.#snapshot
  }

  move(direction: "next" | "previous" | "first" | "last"): PaletteEntry | undefined {
    this.#assertOpen()
    const entries = this.#snapshot.entries
    if (!entries.length) return undefined
    const enabled = entries
      .map((entry, index) => ({ entry, index }))
      .filter(({ entry }) => !entry.disabled)
    if (!enabled.length) return entries[this.#snapshot.selectedIndex]
    let position = enabled.findIndex(
      ({ index }) => index === this.#snapshot.selectedIndex,
    )
    if (direction === "first") position = 0
    else if (direction === "last") position = enabled.length - 1
    else if (direction === "next") position = (position + 1 + enabled.length) % enabled.length
    else position = (position - 1 + enabled.length) % enabled.length
    const selected = enabled[position]!
    this.#replace({
      selectedIndex: selected.index,
      selectedId: selected.entry.id,
    })
    return selected.entry
  }

  select(id: string): PaletteEntry | undefined {
    this.#assertOpen()
    const index = this.#snapshot.entries.findIndex((entry) => entry.id === id)
    const selected = this.#snapshot.entries[index]
    if (!selected || selected.disabled) return undefined
    this.#replace({
      selectedId: selected.id,
      selectedIndex: index,
    })
    return selected
  }

  apply(
    value = this.#snapshot.query,
  ): { value: string; cursor: number; entry: PaletteEntry } | undefined {
    this.#assertOpen()
    const entry = this.#snapshot.entries[this.#snapshot.selectedIndex]
    if (!entry || entry.disabled) return undefined
    const applied = applyCommandCompletion(
      value,
      this.#snapshot.completion,
      entry.value,
    )
    this.remember(entry.value)
    this.close()
    return {
      ...applied,
      entry,
    }
  }

  handleKey(input: {
    key: string
    altKey?: boolean
    ctrlKey?: boolean
    metaKey?: boolean
    shiftKey?: boolean
  }): {
    handled: boolean
    action?: "move" | "apply" | "close"
    result?: ReturnType<CommandPalette["apply"]>
  } {
    if (!this.#snapshot.open) return { handled: false }
    if (input.key === "ArrowDown") {
      this.move("next")
      return { handled: true, action: "move" }
    }
    if (input.key === "ArrowUp") {
      this.move("previous")
      return { handled: true, action: "move" }
    }
    if (input.key === "Home" && (input.ctrlKey || input.metaKey)) {
      this.move("first")
      return { handled: true, action: "move" }
    }
    if (input.key === "End" && (input.ctrlKey || input.metaKey)) {
      this.move("last")
      return { handled: true, action: "move" }
    }
    if (
      input.key === "Enter" &&
      !input.shiftKey &&
      !input.altKey &&
      !input.ctrlKey &&
      !input.metaKey
    ) {
      return {
        handled: true,
        action: "apply",
        result: this.apply(),
      }
    }
    if (input.key === "Tab" && !input.shiftKey) {
      return {
        handled: true,
        action: "apply",
        result: this.apply(),
      }
    }
    if (input.key === "Escape") {
      this.close()
      return { handled: true, action: "close" }
    }
    return { handled: false }
  }

  destroy(): void {
    if (this.#closed) return
    this.#closed = true
    this.#listeners.clear()
    this.#identitySources.clear()
    this.#history.length = 0
  }

  #replace(patch: Partial<PaletteSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Palette subscribers cannot alter registry or submission policy.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new TypeError("Command palette is closed.")
    }
  }
}
