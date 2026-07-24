import type { TerminalLine, TerminalScreenSnapshot } from "./screen.ts"
import { terminalLineText } from "./screen.ts"

export interface TerminalPoint {
  row: number
  column: number
}

export interface TerminalSelectionRange {
  anchor: TerminalPoint
  focus: TerminalPoint
  rectangular: boolean
}

export interface NormalizedTerminalSelection {
  start: TerminalPoint
  end: TerminalPoint
  rectangular: boolean
  reversed: boolean
}

export interface TerminalSelectionSnapshot {
  range?: NormalizedTerminalSelection
  selecting: boolean
  text: string
  revision: number
}

export interface TerminalSearchOptions {
  caseSensitive?: boolean
  regularExpression?: boolean
  wholeWord?: boolean
  includeScrollback?: boolean
  maximumMatches?: number
}

export interface TerminalSearchResult {
  query: string
  matches: readonly TerminalSelectionRange[]
  activeIndex: number
  truncated: boolean
  revision: number
}

function integer(value: number, minimum = 0): number {
  if (!Number.isFinite(value)) return minimum
  return Math.max(minimum, Math.floor(value))
}

function compare(left: TerminalPoint, right: TerminalPoint): number {
  return left.row - right.row || left.column - right.column
}

function point(value: TerminalPoint): TerminalPoint {
  return Object.freeze({
    row: integer(value.row),
    column: integer(value.column),
  })
}

function normalized(range: TerminalSelectionRange): NormalizedTerminalSelection {
  const anchor = point(range.anchor)
  const focus = point(range.focus)
  const reversed = compare(anchor, focus) > 0
  return Object.freeze({
    start: reversed ? focus : anchor,
    end: reversed ? anchor : focus,
    rectangular: range.rectangular,
    reversed,
  })
}

function allLines(
  screen: TerminalScreenSnapshot,
  includeScrollback = true,
): readonly TerminalLine[] {
  return includeScrollback
    ? Object.freeze([...screen.scrollback, ...screen.lines])
    : screen.lines
}

function columns(line: TerminalLine): number {
  return line.cells.reduce((total, cell) => total + (cell.width === 0 ? 0 : 1), 0)
}

function boundedPoint(
  value: TerminalPoint,
  lines: readonly TerminalLine[],
): TerminalPoint {
  if (!lines.length) return Object.freeze({ row: 0, column: 0 })
  const row = Math.min(lines.length - 1, integer(value.row))
  return Object.freeze({
    row,
    column: Math.min(columns(lines[row]!), integer(value.column)),
  })
}

function sliceColumns(line: TerminalLine, start: number, end: number): string {
  let column = 0
  let value = ""
  for (const cell of line.cells) {
    if (cell.width === 0) continue
    if (column >= end) break
    if (column >= start) value += cell.text || " "
    column += 1
  }
  return value
}

export function extractTerminalSelection(
  screen: TerminalScreenSnapshot,
  range: TerminalSelectionRange | NormalizedTerminalSelection,
  options: {
    includeScrollback?: boolean
    trimTrailingWhitespace?: boolean
    maximumBytes?: number
  } = {},
): string {
  const lines = allLines(screen, options.includeScrollback !== false)
  if (!lines.length) return ""
  const source = "start" in range ? range : normalized(range)
  const start = boundedPoint(source.start, lines)
  const end = boundedPoint(source.end, lines)
  const maximumBytes = Math.min(
    64 * 1_024 * 1_024,
    Math.max(1, Math.floor(options.maximumBytes ?? 8 * 1_024 * 1_024)),
  )
  const output: string[] = []
  let used = 0
  const append = (value: string) => {
    const candidate = options.trimTrailingWhitespace === false
      ? value
      : value.replace(/\s+$/u, "")
    const encoded = new TextEncoder().encode(candidate)
    if (used + encoded.byteLength > maximumBytes) {
      const remaining = Math.max(0, maximumBytes - used)
      const selected = new TextDecoder().decode(encoded.slice(0, remaining))
      output.push(selected)
      used = maximumBytes
      return false
    }
    output.push(candidate)
    used += encoded.byteLength
    return true
  }
  if (source.rectangular) {
    const left = Math.min(start.column, end.column)
    const right = Math.max(start.column, end.column)
    for (let row = start.row; row <= end.row; row += 1) {
      if (!append(sliceColumns(lines[row]!, left, right))) break
    }
    return output.join("\n")
  }
  for (let row = start.row; row <= end.row; row += 1) {
    const line = lines[row]!
    const from = row === start.row ? start.column : 0
    const to = row === end.row ? end.column : columns(line)
    if (!append(sliceColumns(line, from, to))) break
  }
  return output.join("\n")
}

function wordBoundary(text: string, column: number): [number, number] {
  const selected = Math.min(text.length, integer(column))
  const word = /[\p{L}\p{N}_$./:@~+-]/u
  let start = selected
  let end = selected
  while (start > 0 && word.test(text[start - 1]!)) start -= 1
  while (end < text.length && word.test(text[end]!)) end += 1
  if (start === end && end < text.length) end += 1
  return [start, end]
}

export function terminalWordSelection(
  screen: TerminalScreenSnapshot,
  value: TerminalPoint,
  includeScrollback = true,
): TerminalSelectionRange {
  const lines = allLines(screen, includeScrollback)
  const selected = boundedPoint(value, lines)
  const text = terminalLineText(lines[selected.row]!, false)
  const [start, end] = wordBoundary(text, selected.column)
  return Object.freeze({
    anchor: Object.freeze({ row: selected.row, column: start }),
    focus: Object.freeze({ row: selected.row, column: end }),
    rectangular: false,
  })
}

export function terminalLineSelection(
  screen: TerminalScreenSnapshot,
  row: number,
  includeScrollback = true,
): TerminalSelectionRange {
  const lines = allLines(screen, includeScrollback)
  const selected = Math.min(Math.max(0, integer(row)), Math.max(0, lines.length - 1))
  return Object.freeze({
    anchor: Object.freeze({ row: selected, column: 0 }),
    focus: Object.freeze({ row: selected, column: columns(lines[selected]!) }),
    rectangular: false,
  })
}

function searchPattern(query: string, options: TerminalSearchOptions): RegExp {
  const flags = options.caseSensitive ? "gu" : "giu"
  let source = query
  if (!options.regularExpression) {
    source = query.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&")
  }
  if (options.wholeWord) source = `\\b(?:${source})\\b`
  try {
    return new RegExp(source, flags)
  } catch (error) {
    throw new TypeError(
      `Terminal search expression is invalid: ${
        error instanceof Error ? error.message : String(error)
      }`,
    )
  }
}

export function searchTerminalSelections(
  screen: TerminalScreenSnapshot,
  query: string,
  options: TerminalSearchOptions = {},
): Omit<TerminalSearchResult, "activeIndex" | "revision"> {
  const selected = query.slice(0, 1_024)
  if (!selected) return { query: "", matches: Object.freeze([]), truncated: false }
  const maximum = Math.min(
    100_000,
    Math.max(1, Math.floor(options.maximumMatches ?? 10_000)),
  )
  const lines = allLines(screen, options.includeScrollback !== false)
  const pattern = searchPattern(selected, options)
  const matches: TerminalSelectionRange[] = []
  let truncated = false
  for (let row = 0; row < lines.length; row += 1) {
    const text = terminalLineText(lines[row]!, false)
    pattern.lastIndex = 0
    for (const match of text.matchAll(pattern)) {
      const start = match.index ?? 0
      const length = match[0].length
      matches.push(Object.freeze({
        anchor: Object.freeze({ row, column: start }),
        focus: Object.freeze({ row, column: start + Math.max(1, length) }),
        rectangular: false,
      }))
      if (matches.length >= maximum) {
        truncated = true
        break
      }
      if (length === 0) pattern.lastIndex += 1
    }
    if (truncated) break
  }
  return {
    query: selected,
    matches: Object.freeze(matches),
    truncated,
  }
}

export class TerminalSelectionModel {
  #screen: TerminalScreenSnapshot
  #range?: TerminalSelectionRange
  #selecting = false
  #revision = 0
  #search: TerminalSearchResult = Object.freeze({
    query: "",
    matches: Object.freeze([]),
    activeIndex: -1,
    truncated: false,
    revision: 0,
  })

  constructor(screen: TerminalScreenSnapshot) {
    this.#screen = screen
  }

  update(screen: TerminalScreenSnapshot): TerminalSelectionSnapshot {
    this.#screen = screen
    if (this.#range) {
      const lines = allLines(screen)
      this.#range = {
        ...this.#range,
        anchor: boundedPoint(this.#range.anchor, lines),
        focus: boundedPoint(this.#range.focus, lines),
      }
    }
    this.#revision += 1
    return this.snapshot()
  }

  begin(value: TerminalPoint, rectangular = false): TerminalSelectionSnapshot {
    const selected = boundedPoint(value, allLines(this.#screen))
    this.#range = {
      anchor: selected,
      focus: selected,
      rectangular,
    }
    this.#selecting = true
    this.#revision += 1
    return this.snapshot()
  }

  extend(value: TerminalPoint): TerminalSelectionSnapshot {
    if (!this.#range || !this.#selecting) return this.snapshot()
    this.#range = {
      ...this.#range,
      focus: boundedPoint(value, allLines(this.#screen)),
    }
    this.#revision += 1
    return this.snapshot()
  }

  end(value?: TerminalPoint): TerminalSelectionSnapshot {
    if (value) this.extend(value)
    this.#selecting = false
    this.#revision += 1
    return this.snapshot()
  }

  word(value: TerminalPoint): TerminalSelectionSnapshot {
    this.#range = terminalWordSelection(this.#screen, value)
    this.#selecting = false
    this.#revision += 1
    return this.snapshot()
  }

  line(row: number): TerminalSelectionSnapshot {
    this.#range = terminalLineSelection(this.#screen, row)
    this.#selecting = false
    this.#revision += 1
    return this.snapshot()
  }

  all(): TerminalSelectionSnapshot {
    const lines = allLines(this.#screen)
    this.#range = {
      anchor: { row: 0, column: 0 },
      focus: {
        row: Math.max(0, lines.length - 1),
        column: lines.length ? columns(lines[lines.length - 1]!) : 0,
      },
      rectangular: false,
    }
    this.#selecting = false
    this.#revision += 1
    return this.snapshot()
  }

  clear(): TerminalSelectionSnapshot {
    this.#range = undefined
    this.#selecting = false
    this.#revision += 1
    return this.snapshot()
  }

  search(
    query: string,
    options: TerminalSearchOptions = {},
  ): TerminalSearchResult {
    const result = searchTerminalSelections(this.#screen, query, options)
    const previous = this.#search
    const sameQuery = previous.query === result.query
    const activeIndex = result.matches.length
      ? sameQuery
        ? Math.min(previous.activeIndex, result.matches.length - 1)
        : 0
      : -1
    this.#search = Object.freeze({
      ...result,
      activeIndex,
      revision: previous.revision + 1,
    })
    if (activeIndex >= 0) this.#range = result.matches[activeIndex]
    this.#revision += 1
    return this.#search
  }

  next(direction: 1 | -1 = 1): TerminalSearchResult {
    if (!this.#search.matches.length) return this.#search
    const length = this.#search.matches.length
    const activeIndex = (
      this.#search.activeIndex + direction + length
    ) % length
    this.#search = Object.freeze({
      ...this.#search,
      activeIndex,
      revision: this.#search.revision + 1,
    })
    this.#range = this.#search.matches[activeIndex]
    this.#revision += 1
    return this.#search
  }

  searchSnapshot(): TerminalSearchResult {
    return this.#search
  }

  snapshot(): TerminalSelectionSnapshot {
    const range = this.#range ? normalized(this.#range) : undefined
    return Object.freeze({
      range,
      selecting: this.#selecting,
      text: range ? extractTerminalSelection(this.#screen, range) : "",
      revision: this.#revision,
    })
  }
}
