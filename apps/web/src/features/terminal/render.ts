import type {
  TerminalCell,
  TerminalCellStyle,
  TerminalColor,
  TerminalLine,
  TerminalScreenSnapshot,
} from "./screen.ts"
import { terminalLineText } from "./screen.ts"

export interface TerminalRenderRun {
  key: string
  text: string
  className: string
  style: Readonly<Record<string, string>>
  hyperlink?: string
  startColumn: number
  endColumn: number
}

export interface TerminalRenderLine {
  key: string
  row: number
  logicalRow: number
  wrapped: boolean
  text: string
  runs: readonly TerminalRenderRun[]
  cursorColumn?: number
  cursorShape?: "block" | "underline" | "bar"
  cursorBlinking?: boolean
}

export interface TerminalViewport {
  firstRow: number
  lastRow: number
  totalRows: number
  rows: readonly TerminalRenderLine[]
  beforeRows: number
  afterRows: number
  followOutput: boolean
  revision: number
}

export interface TerminalRenderOptions {
  firstRow?: number
  visibleRows?: number
  overscan?: number
  includeScrollback?: boolean
  followOutput?: boolean
  trimTrailingWhitespace?: boolean
}

const ANSI_16 = [
  "#000000", "#cd3131", "#0dbc79", "#e5e510",
  "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
  "#666666", "#f14c4c", "#23d18b", "#f5f543",
  "#3b8eea", "#d670d6", "#29b8db", "#ffffff",
] as const

function byte(value: number): number {
  return Math.min(255, Math.max(0, Math.round(value)))
}

function cube(value: number): number {
  return value === 0 ? 0 : 55 + value * 40
}

export function indexedTerminalColor(index: number): string {
  const selected = Math.min(255, Math.max(0, Math.floor(index)))
  if (selected < 16) return ANSI_16[selected]!
  if (selected < 232) {
    const offset = selected - 16
    const red = Math.floor(offset / 36)
    const green = Math.floor((offset % 36) / 6)
    const blue = offset % 6
    return `rgb(${cube(red)} ${cube(green)} ${cube(blue)})`
  }
  const gray = 8 + (selected - 232) * 10
  return `rgb(${gray} ${gray} ${gray})`
}

export function terminalColorCss(
  color: TerminalColor,
  fallback: string,
): string {
  if (color.kind === "default") return fallback
  if (color.kind === "indexed") return indexedTerminalColor(Number(color.value ?? 0))
  const value = Array.isArray(color.value) ? color.value : [255, 255, 255]
  return `rgb(${byte(value[0] ?? 0)} ${byte(value[1] ?? 0)} ${byte(value[2] ?? 0)})`
}

function styleSignature(style: TerminalCellStyle): string {
  return JSON.stringify([
    style.foreground.kind,
    style.foreground.value,
    style.background.kind,
    style.background.value,
    style.underlineColor.kind,
    style.underlineColor.value,
    style.bold,
    style.faint,
    style.italic,
    style.underline,
    style.blink,
    style.inverse,
    style.invisible,
    style.strike,
    style.overline,
    style.hyperlink ?? "",
  ])
}

function styleClass(style: TerminalCellStyle): string {
  const classes = ["terminal-run"]
  if (style.bold) classes.push("is-bold")
  if (style.faint) classes.push("is-faint")
  if (style.italic) classes.push("is-italic")
  if (style.blink) classes.push("is-blinking")
  if (style.invisible) classes.push("is-invisible")
  if (style.strike) classes.push("is-struck")
  if (style.overline) classes.push("is-overline")
  if (style.underline !== "none") classes.push(`is-underline-${style.underline}`)
  if (style.hyperlink) classes.push("is-link")
  return classes.join(" ")
}

function styleProperties(style: TerminalCellStyle): Readonly<Record<string, string>> {
  let foreground = terminalColorCss(style.foreground, "var(--terminal-fg)")
  let background = terminalColorCss(style.background, "var(--terminal-bg)")
  if (style.inverse) [foreground, background] = [background, foreground]
  return Object.freeze({
    color: foreground,
    backgroundColor: background,
    textDecorationColor: terminalColorCss(style.underlineColor, foreground),
    fontWeight: style.bold ? "700" : "400",
    opacity: style.faint ? "0.64" : "1",
    fontStyle: style.italic ? "italic" : "normal",
    visibility: style.invisible ? "hidden" : "visible",
  })
}

function cellText(cell: TerminalCell): string {
  return cell.width === 0 ? "" : cell.text || " "
}

export function terminalRenderRuns(
  line: TerminalLine,
  row: number,
  options: { trimTrailingWhitespace?: boolean } = {},
): readonly TerminalRenderRun[] {
  const cells = line.cells
  let finalColumn = cells.length
  if (options.trimTrailingWhitespace !== false) {
    while (finalColumn > 0) {
      const cell = cells[finalColumn - 1]
      if (!cell || cell.width === 0 || cell.text === " ") {
        finalColumn -= 1
        continue
      }
      break
    }
  }
  const runs: TerminalRenderRun[] = []
  let currentSignature = ""
  let currentStyle: TerminalCellStyle | undefined
  let currentText = ""
  let currentStart = 0
  const flush = (endColumn: number) => {
    if (!currentStyle || !currentText) return
    runs.push(Object.freeze({
      key: `${row}:${currentStart}:${endColumn}:${runs.length}`,
      text: currentText,
      className: styleClass(currentStyle),
      style: styleProperties(currentStyle),
      hyperlink: safeTerminalHyperlink(currentStyle.hyperlink),
      startColumn: currentStart,
      endColumn,
    }))
    currentText = ""
  }
  for (let column = 0; column < finalColumn; column += 1) {
    const cell = cells[column]
    if (!cell || cell.width === 0) continue
    const signature = styleSignature(cell.style)
    if (currentStyle && signature !== currentSignature) {
      flush(column)
      currentStart = column
    }
    if (!currentStyle || signature !== currentSignature) {
      currentStyle = cell.style
      currentSignature = signature
    }
    currentText += cellText(cell)
  }
  flush(finalColumn)
  if (!runs.length) {
    return Object.freeze([
      Object.freeze({
        key: `${row}:empty`,
        text: "\u00a0",
        className: "terminal-run",
        style: Object.freeze({}),
        startColumn: 0,
        endColumn: 0,
      }),
    ])
  }
  return Object.freeze(runs)
}

export function safeTerminalHyperlink(value: string | undefined): string | undefined {
  if (!value || value.length > 2_048) return undefined
  try {
    const url = new URL(value)
    if (!["http:", "https:"].includes(url.protocol)) return undefined
    if (url.username || url.password) return undefined
    return url.toString()
  } catch {
    return undefined
  }
}

function screenLines(
  screen: TerminalScreenSnapshot,
  includeScrollback: boolean,
): readonly TerminalLine[] {
  return includeScrollback
    ? Object.freeze([...screen.scrollback, ...screen.lines])
    : screen.lines
}

export function renderTerminalViewport(
  screen: TerminalScreenSnapshot,
  options: TerminalRenderOptions = {},
): TerminalViewport {
  const includeScrollback = options.includeScrollback !== false
  const lines = screenLines(screen, includeScrollback)
  const visibleRows = Math.min(
    2_000,
    Math.max(1, Math.floor(options.visibleRows ?? screen.rows)),
  )
  const overscan = Math.min(200, Math.max(0, Math.floor(options.overscan ?? 8)))
  const followOutput = options.followOutput !== false
  const requested = followOutput
    ? Math.max(0, lines.length - visibleRows)
    : Math.max(0, Math.floor(options.firstRow ?? 0))
  const firstRow = Math.max(0, requested - overscan)
  const lastRow = Math.min(lines.length, requested + visibleRows + overscan)
  const rows: TerminalRenderLine[] = []
  const scrollbackOffset = includeScrollback ? screen.scrollback.length : 0
  for (let row = firstRow; row < lastRow; row += 1) {
    const line = lines[row]
    if (!line) continue
    const screenRow = row - scrollbackOffset
    const cursor = (
      screen.cursor.visible
      && screenRow === screen.cursor.row
      && screenRow >= 0
    )
      ? screen.cursor
      : undefined
    rows.push(Object.freeze({
      key: `${row}:${line.revision}`,
      row,
      logicalRow: screenRow,
      wrapped: line.wrapped,
      text: terminalLineText(line, options.trimTrailingWhitespace !== false),
      runs: terminalRenderRuns(line, row, options),
      cursorColumn: cursor?.col,
      cursorShape: cursor?.shape,
      cursorBlinking: cursor?.blinking,
    }))
  }
  return Object.freeze({
    firstRow,
    lastRow,
    totalRows: lines.length,
    rows: Object.freeze(rows),
    beforeRows: firstRow,
    afterRows: Math.max(0, lines.length - lastRow),
    followOutput,
    revision: screen.revision,
  })
}

export interface TerminalSearchMatch {
  row: number
  startColumn: number
  endColumn: number
  text: string
}

export function searchTerminalScreen(
  screen: TerminalScreenSnapshot,
  query: string,
  options: {
    caseSensitive?: boolean
    wholeWord?: boolean
    maximum?: number
    includeScrollback?: boolean
  } = {},
): readonly TerminalSearchMatch[] {
  const needle = query.slice(0, 1_024)
  if (!needle) return Object.freeze([])
  const flags = options.caseSensitive ? "gu" : "giu"
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&")
  const source = options.wholeWord ? `\\b${escaped}\\b` : escaped
  const pattern = new RegExp(source, flags)
  const maximum = Math.min(10_000, Math.max(1, options.maximum ?? 1_000))
  const lines = screenLines(screen, options.includeScrollback !== false)
  const result: TerminalSearchMatch[] = []
  for (let row = 0; row < lines.length && result.length < maximum; row += 1) {
    const text = terminalLineText(lines[row]!, false)
    pattern.lastIndex = 0
    for (const match of text.matchAll(pattern)) {
      const start = match.index ?? 0
      result.push(Object.freeze({
        row,
        startColumn: start,
        endColumn: start + match[0].length,
        text: match[0],
      }))
      if (result.length >= maximum) break
    }
  }
  return Object.freeze(result)
}
