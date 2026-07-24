import {
  AnsiStreamParser,
  type AnsiControlToken,
  type AnsiCsiToken,
  type AnsiEscapeToken,
  type AnsiOscToken,
  type AnsiToken,
} from "./ansi.ts"

export interface TerminalColor {
  kind: "default" | "indexed" | "rgb"
  value?: number | readonly [number, number, number]
}

export interface TerminalCellStyle {
  foreground: TerminalColor
  background: TerminalColor
  underlineColor: TerminalColor
  bold: boolean
  faint: boolean
  italic: boolean
  underline: "none" | "single" | "double" | "curly" | "dotted" | "dashed"
  blink: boolean
  inverse: boolean
  invisible: boolean
  strike: boolean
  overline: boolean
  hyperlink?: string
}

export interface TerminalCell {
  text: string
  width: 0 | 1 | 2
  style: TerminalCellStyle
}

export interface TerminalLine {
  cells: readonly TerminalCell[]
  wrapped: boolean
  revision: number
}

export interface TerminalCursor {
  row: number
  col: number
  visible: boolean
  blinking: boolean
  shape: "block" | "underline" | "bar"
}

export interface TerminalScreenSnapshot {
  rows: number
  cols: number
  lines: readonly TerminalLine[]
  scrollback: readonly TerminalLine[]
  cursor: TerminalCursor
  title: string
  iconName: string
  alternate: boolean
  applicationCursorKeys: boolean
  bracketedPaste: boolean
  mouseTracking: "none" | "x10" | "button" | "any"
  revision: number
  ignoredSequences: number
  bellCount: number
}

interface MutableLine {
  cells: TerminalCell[]
  wrapped: boolean
  revision: number
}

interface SavedCursor {
  row: number
  col: number
  style: TerminalCellStyle
  originMode: boolean
  wrapPending: boolean
}

interface ScreenBuffer {
  lines: MutableLine[]
  cursorRow: number
  cursorCol: number
  saved: SavedCursor
  scrollTop: number
  scrollBottom: number
  wrapPending: boolean
}

export interface TerminalScreenOptions {
  rows?: number
  cols?: number
  maximumScrollback?: number
  tabWidth?: number
}

const DEFAULT_COLOR: TerminalColor = Object.freeze({ kind: "default" })

function copyColor(color: TerminalColor): TerminalColor {
  if (color.kind === "rgb" && Array.isArray(color.value)) {
    return { kind: "rgb", value: [color.value[0], color.value[1], color.value[2]] }
  }
  if (color.kind === "indexed") return { kind: "indexed", value: Number(color.value) }
  return { kind: "default" }
}

function defaultStyle(): TerminalCellStyle {
  return {
    foreground: DEFAULT_COLOR,
    background: DEFAULT_COLOR,
    underlineColor: DEFAULT_COLOR,
    bold: false,
    faint: false,
    italic: false,
    underline: "none",
    blink: false,
    inverse: false,
    invisible: false,
    strike: false,
    overline: false,
  }
}

function copyStyle(style: TerminalCellStyle): TerminalCellStyle {
  return {
    foreground: copyColor(style.foreground),
    background: copyColor(style.background),
    underlineColor: copyColor(style.underlineColor),
    bold: style.bold,
    faint: style.faint,
    italic: style.italic,
    underline: style.underline,
    blink: style.blink,
    inverse: style.inverse,
    invisible: style.invisible,
    strike: style.strike,
    overline: style.overline,
    hyperlink: style.hyperlink,
  }
}

function blankCell(style: TerminalCellStyle = defaultStyle()): TerminalCell {
  return {
    text: " ",
    width: 1,
    style: copyStyle(style),
  }
}

function blankLine(cols: number, revision: number, style?: TerminalCellStyle): MutableLine {
  return {
    cells: Array.from({ length: cols }, () => blankCell(style)),
    wrapped: false,
    revision,
  }
}

function codePointWidth(value: string): 0 | 1 | 2 {
  const code = value.codePointAt(0) ?? 0
  if (code === 0) return 0
  if (code >= 0x0300 && code <= 0x036f) return 0
  if (code >= 0x1ab0 && code <= 0x1aff) return 0
  if (code >= 0x1dc0 && code <= 0x1dff) return 0
  if (code >= 0x20d0 && code <= 0x20ff) return 0
  if (code >= 0xfe20 && code <= 0xfe2f) return 0
  if (code >= 0x1100 && (
    code <= 0x115f
    || code === 0x2329
    || code === 0x232a
    || (code >= 0x2e80 && code <= 0xa4cf && code !== 0x303f)
    || (code >= 0xac00 && code <= 0xd7a3)
    || (code >= 0xf900 && code <= 0xfaff)
    || (code >= 0xfe10 && code <= 0xfe19)
    || (code >= 0xfe30 && code <= 0xfe6f)
    || (code >= 0xff00 && code <= 0xff60)
    || (code >= 0xffe0 && code <= 0xffe6)
    || (code >= 0x1f300 && code <= 0x1faff)
    || (code >= 0x20000 && code <= 0x3fffd)
  )) return 2
  return 1
}

function firstParameter(token: AnsiCsiToken, fallback: number, zeroIsFallback = true): number {
  const selected = token.parameters[0]?.[0]
  if (selected === undefined) return fallback
  if (zeroIsFallback && selected === 0) return fallback
  return Math.min(1_000_000, Math.max(0, selected))
}

function parameterAt(
  token: AnsiCsiToken,
  index: number,
  fallback: number,
  zeroIsFallback = true,
): number {
  const selected = token.parameters[index]?.[0]
  if (selected === undefined) return fallback
  if (zeroIsFallback && selected === 0) return fallback
  return Math.min(1_000_000, Math.max(0, selected))
}

function freezeCell(cell: TerminalCell): TerminalCell {
  return Object.freeze({
    text: cell.text,
    width: cell.width,
    style: Object.freeze({
      ...cell.style,
      foreground: Object.freeze(copyColor(cell.style.foreground)),
      background: Object.freeze(copyColor(cell.style.background)),
      underlineColor: Object.freeze(copyColor(cell.style.underlineColor)),
    }),
  })
}

function freezeLine(line: MutableLine): TerminalLine {
  return Object.freeze({
    cells: Object.freeze(line.cells.map(freezeCell)),
    wrapped: line.wrapped,
    revision: line.revision,
  })
}

function savedCursor(): SavedCursor {
  return {
    row: 0,
    col: 0,
    style: defaultStyle(),
    originMode: false,
    wrapPending: false,
  }
}

function createBuffer(rows: number, cols: number, revision: number): ScreenBuffer {
  return {
    lines: Array.from({ length: rows }, () => blankLine(cols, revision)),
    cursorRow: 0,
    cursorCol: 0,
    saved: savedCursor(),
    scrollTop: 0,
    scrollBottom: rows - 1,
    wrapPending: false,
  }
}

export class TerminalScreen {
  readonly #parser: AnsiStreamParser
  readonly #maximumScrollback: number
  readonly #tabWidth: number
  #rows: number
  #cols: number
  #primary: ScreenBuffer
  #alternate: ScreenBuffer
  #useAlternate = false
  #scrollback: MutableLine[] = []
  #style = defaultStyle()
  #revision = 0
  #title = ""
  #iconName = ""
  #cursorVisible = true
  #cursorBlinking = true
  #cursorShape: TerminalCursor["shape"] = "block"
  #originMode = false
  #insertMode = false
  #autoWrap = true
  #applicationCursorKeys = false
  #bracketedPaste = false
  #mouseTracking: TerminalScreenSnapshot["mouseTracking"] = "none"
  #savedTitleStack: string[] = []
  #ignoredSequences = 0
  #bellCount = 0

  constructor(options: TerminalScreenOptions = {}) {
    this.#rows = Math.min(500, Math.max(2, Math.floor(options.rows ?? 24)))
    this.#cols = Math.min(1_000, Math.max(2, Math.floor(options.cols ?? 80)))
    this.#maximumScrollback = Math.min(
      100_000,
      Math.max(0, Math.floor(options.maximumScrollback ?? 10_000)),
    )
    this.#tabWidth = Math.min(32, Math.max(1, Math.floor(options.tabWidth ?? 8)))
    this.#primary = createBuffer(this.#rows, this.#cols, this.#revision)
    this.#alternate = createBuffer(this.#rows, this.#cols, this.#revision)
    this.#parser = new AnsiStreamParser()
  }

  write(input: string): TerminalScreenSnapshot {
    this.#apply(this.#parser.push(input))
    return this.snapshot()
  }

  finish(): TerminalScreenSnapshot {
    this.#apply(this.#parser.finish())
    return this.snapshot()
  }

  resize(rows: number, cols: number): TerminalScreenSnapshot {
    const nextRows = Math.min(500, Math.max(2, Math.floor(rows)))
    const nextCols = Math.min(1_000, Math.max(2, Math.floor(cols)))
    if (nextRows === this.#rows && nextCols === this.#cols) return this.snapshot()
    this.#revision += 1
    this.#resizeBuffer(this.#primary, nextRows, nextCols, true)
    this.#resizeBuffer(this.#alternate, nextRows, nextCols, false)
    this.#rows = nextRows
    this.#cols = nextCols
    return this.snapshot()
  }

  reset(): TerminalScreenSnapshot {
    this.#revision += 1
    this.#primary = createBuffer(this.#rows, this.#cols, this.#revision)
    this.#alternate = createBuffer(this.#rows, this.#cols, this.#revision)
    this.#useAlternate = false
    this.#scrollback = []
    this.#style = defaultStyle()
    this.#title = ""
    this.#iconName = ""
    this.#cursorVisible = true
    this.#cursorBlinking = true
    this.#cursorShape = "block"
    this.#originMode = false
    this.#insertMode = false
    this.#autoWrap = true
    this.#applicationCursorKeys = false
    this.#bracketedPaste = false
    this.#mouseTracking = "none"
    this.#savedTitleStack = []
    this.#ignoredSequences = 0
    this.#bellCount = 0
    this.#parser.reset()
    return this.snapshot()
  }

  snapshot(): TerminalScreenSnapshot {
    const buffer = this.#buffer()
    return Object.freeze({
      rows: this.#rows,
      cols: this.#cols,
      lines: Object.freeze(buffer.lines.map(freezeLine)),
      scrollback: Object.freeze(this.#scrollback.map(freezeLine)),
      cursor: Object.freeze({
        row: buffer.cursorRow,
        col: buffer.cursorCol,
        visible: this.#cursorVisible,
        blinking: this.#cursorBlinking,
        shape: this.#cursorShape,
      }),
      title: this.#title,
      iconName: this.#iconName,
      alternate: this.#useAlternate,
      applicationCursorKeys: this.#applicationCursorKeys,
      bracketedPaste: this.#bracketedPaste,
      mouseTracking: this.#mouseTracking,
      revision: this.#revision,
      ignoredSequences: this.#ignoredSequences,
      bellCount: this.#bellCount,
    })
  }

  text(options: { includeScrollback?: boolean; trimEnd?: boolean } = {}): string {
    const lines = options.includeScrollback
      ? [...this.#scrollback, ...this.#buffer().lines]
      : this.#buffer().lines
    return lines
      .map((line) => {
        let value = ""
        for (const cell of line.cells) {
          if (cell.width === 0) continue
          value += cell.text
        }
        return options.trimEnd === false ? value : value.trimEnd()
      })
      .join("\n")
  }

  #apply(tokens: readonly AnsiToken[]): void {
    for (const token of tokens) {
      if (token.kind === "text") {
        this.#writeText(token.value)
        continue
      }
      if (token.kind === "control") {
        this.#control(token)
        continue
      }
      if (token.kind === "escape") {
        this.#escape(token)
        continue
      }
      if (token.kind === "csi") {
        this.#csi(token)
        continue
      }
      if (token.kind === "osc") {
        this.#osc(token)
        continue
      }
      this.#ignoredSequences += 1
    }
  }

  #writeText(value: string): void {
    for (const character of value) this.#writeCharacter(character)
  }

  #writeCharacter(character: string): void {
    const width = codePointWidth(character)
    const buffer = this.#buffer()
    if (width === 0) {
      const col = Math.max(0, buffer.cursorCol - 1)
      const cell = buffer.lines[buffer.cursorRow]?.cells[col]
      if (cell && cell.width !== 0) {
        cell.text += character
        this.#touch(buffer.cursorRow)
      }
      return
    }
    if (buffer.wrapPending && this.#autoWrap) {
      buffer.lines[buffer.cursorRow]!.wrapped = true
      this.#lineFeed(true)
      buffer.cursorCol = 0
      buffer.wrapPending = false
    }
    if (width === 2 && buffer.cursorCol === this.#cols - 1) {
      if (!this.#autoWrap) return
      buffer.lines[buffer.cursorRow]!.wrapped = true
      this.#lineFeed(true)
      buffer.cursorCol = 0
    }
    const line = buffer.lines[buffer.cursorRow]!
    if (this.#insertMode) this.#insertCells(width)
    this.#clearWideAt(line, buffer.cursorCol)
    line.cells[buffer.cursorCol] = {
      text: character,
      width,
      style: copyStyle(this.#style),
    }
    if (width === 2 && buffer.cursorCol + 1 < this.#cols) {
      line.cells[buffer.cursorCol + 1] = {
        text: "",
        width: 0,
        style: copyStyle(this.#style),
      }
    }
    this.#touch(buffer.cursorRow)
    const next = buffer.cursorCol + width
    if (next >= this.#cols) {
      buffer.cursorCol = this.#cols - 1
      buffer.wrapPending = true
    } else {
      buffer.cursorCol = next
      buffer.wrapPending = false
    }
  }

  #control(token: AnsiControlToken): void {
    const buffer = this.#buffer()
    if (token.code === "bell") {
      this.#bellCount += 1
      return
    }
    if (token.code === "backspace") {
      buffer.cursorCol = Math.max(0, buffer.cursorCol - 1)
      buffer.wrapPending = false
      return
    }
    if (token.code === "tab") {
      const next = Math.min(
        this.#cols - 1,
        Math.floor(buffer.cursorCol / this.#tabWidth + 1) * this.#tabWidth,
      )
      buffer.cursorCol = next
      buffer.wrapPending = false
      return
    }
    if (token.code === "line-feed") {
      this.#lineFeed(false)
      return
    }
    if (token.code === "carriage-return") {
      buffer.cursorCol = 0
      buffer.wrapPending = false
    }
  }

  #escape(token: AnsiEscapeToken): void {
    const buffer = this.#buffer()
    if (token.final === "7") {
      this.#saveCursor()
      return
    }
    if (token.final === "8") {
      this.#restoreCursor()
      return
    }
    if (token.final === "D") {
      this.#lineFeed(false)
      return
    }
    if (token.final === "E") {
      this.#lineFeed(false)
      buffer.cursorCol = 0
      return
    }
    if (token.final === "M") {
      this.#reverseIndex()
      return
    }
    if (token.final === "c") {
      this.reset()
      return
    }
    if (token.final === "H") return
    this.#ignoredSequences += 1
  }

  #csi(token: AnsiCsiToken): void {
    if (token.privateMarker === "?") {
      if (token.final === "h" || token.final === "l") {
        this.#setPrivateModes(token, token.final === "h")
        return
      }
      this.#ignoredSequences += 1
      return
    }
    if (token.final === "A") {
      this.#moveCursor(-firstParameter(token, 1), 0)
      return
    }
    if (token.final === "B" || token.final === "e") {
      this.#moveCursor(firstParameter(token, 1), 0)
      return
    }
    if (token.final === "C" || token.final === "a") {
      this.#moveCursor(0, firstParameter(token, 1))
      return
    }
    if (token.final === "D") {
      this.#moveCursor(0, -firstParameter(token, 1))
      return
    }
    if (token.final === "E") {
      this.#moveCursor(firstParameter(token, 1), 0)
      this.#buffer().cursorCol = 0
      return
    }
    if (token.final === "F") {
      this.#moveCursor(-firstParameter(token, 1), 0)
      this.#buffer().cursorCol = 0
      return
    }
    if (token.final === "G" || token.final === "`") {
      this.#setCursor(undefined, firstParameter(token, 1) - 1)
      return
    }
    if (token.final === "d") {
      this.#setCursor(firstParameter(token, 1) - 1, undefined)
      return
    }
    if (token.final === "H" || token.final === "f") {
      this.#setCursor(
        parameterAt(token, 0, 1) - 1,
        parameterAt(token, 1, 1) - 1,
      )
      return
    }
    if (token.final === "J") {
      this.#eraseDisplay(firstParameter(token, 0, false))
      return
    }
    if (token.final === "K") {
      this.#eraseLine(firstParameter(token, 0, false))
      return
    }
    if (token.final === "L") {
      this.#insertLines(firstParameter(token, 1))
      return
    }
    if (token.final === "M") {
      this.#deleteLines(firstParameter(token, 1))
      return
    }
    if (token.final === "@") {
      this.#insertCells(firstParameter(token, 1))
      return
    }
    if (token.final === "P") {
      this.#deleteCells(firstParameter(token, 1))
      return
    }
    if (token.final === "X") {
      this.#eraseCells(firstParameter(token, 1))
      return
    }
    if (token.final === "S") {
      this.#scrollUp(firstParameter(token, 1))
      return
    }
    if (token.final === "T") {
      this.#scrollDown(firstParameter(token, 1))
      return
    }
    if (token.final === "r") {
      this.#setScrollRegion(token)
      return
    }
    if (token.final === "s") {
      this.#saveCursor()
      return
    }
    if (token.final === "u") {
      this.#restoreCursor()
      return
    }
    if (token.final === "m") {
      this.#setGraphics(token)
      return
    }
    if (token.final === "h" || token.final === "l") {
      this.#setModes(token, token.final === "h")
      return
    }
    if (token.final === "q" && token.intermediates === " ") {
      this.#setCursorStyle(firstParameter(token, 0, false))
      return
    }
    this.#ignoredSequences += 1
  }

  #osc(token: AnsiOscToken): void {
    if (token.command === 0) {
      this.#iconName = this.#safeTitle(token.data)
      this.#title = this.#iconName
      return
    }
    if (token.command === 1) {
      this.#iconName = this.#safeTitle(token.data)
      return
    }
    if (token.command === 2) {
      this.#title = this.#safeTitle(token.data)
      return
    }
    if (token.command === 8) {
      const separator = token.data.indexOf(";")
      const uri = separator < 0 ? "" : token.data.slice(separator + 1)
      this.#style.hyperlink = this.#safeHyperlink(uri)
      return
    }
    if (token.command === 22) {
      this.#savedTitleStack.push(this.#title)
      if (this.#savedTitleStack.length > 32) this.#savedTitleStack.shift()
      return
    }
    if (token.command === 23) {
      this.#title = this.#savedTitleStack.pop() ?? this.#title
      return
    }
    this.#ignoredSequences += 1
  }

  #setGraphics(token: AnsiCsiToken): void {
    const parameters = token.parameters.length ? token.parameters : [[0]]
    for (let index = 0; index < parameters.length; index += 1) {
      const group = parameters[index]!
      const code = group[0] ?? 0
      if (code === 0) {
        this.#style = defaultStyle()
        continue
      }
      if (code === 1) this.#style.bold = true
      else if (code === 2) this.#style.faint = true
      else if (code === 3) this.#style.italic = true
      else if (code === 4) this.#style.underline = this.#underlineStyle(group[1])
      else if (code === 5 || code === 6) this.#style.blink = true
      else if (code === 7) this.#style.inverse = true
      else if (code === 8) this.#style.invisible = true
      else if (code === 9) this.#style.strike = true
      else if (code === 21) this.#style.underline = "double"
      else if (code === 22) {
        this.#style.bold = false
        this.#style.faint = false
      } else if (code === 23) this.#style.italic = false
      else if (code === 24) this.#style.underline = "none"
      else if (code === 25) this.#style.blink = false
      else if (code === 27) this.#style.inverse = false
      else if (code === 28) this.#style.invisible = false
      else if (code === 29) this.#style.strike = false
      else if (code >= 30 && code <= 37) {
        this.#style.foreground = { kind: "indexed", value: code - 30 }
      } else if (code === 38) {
        const parsed = this.#extendedColor(parameters, index)
        if (parsed) {
          this.#style.foreground = parsed.color
          index = parsed.next
        }
      } else if (code === 39) this.#style.foreground = DEFAULT_COLOR
      else if (code >= 40 && code <= 47) {
        this.#style.background = { kind: "indexed", value: code - 40 }
      } else if (code === 48) {
        const parsed = this.#extendedColor(parameters, index)
        if (parsed) {
          this.#style.background = parsed.color
          index = parsed.next
        }
      } else if (code === 49) this.#style.background = DEFAULT_COLOR
      else if (code === 53) this.#style.overline = true
      else if (code === 55) this.#style.overline = false
      else if (code === 58) {
        const parsed = this.#extendedColor(parameters, index)
        if (parsed) {
          this.#style.underlineColor = parsed.color
          index = parsed.next
        }
      } else if (code === 59) this.#style.underlineColor = DEFAULT_COLOR
      else if (code >= 90 && code <= 97) {
        this.#style.foreground = { kind: "indexed", value: code - 90 + 8 }
      } else if (code >= 100 && code <= 107) {
        this.#style.background = { kind: "indexed", value: code - 100 + 8 }
      }
    }
  }

  #extendedColor(
    parameters: readonly (readonly (number | undefined)[])[],
    index: number,
  ): { color: TerminalColor; next: number } | undefined {
    const group = parameters[index]!
    if (group.length > 1) {
      if (group[1] === 5 && group[2] !== undefined) {
        return {
          color: { kind: "indexed", value: Math.min(255, group[2]) },
          next: index,
        }
      }
      if (group[1] === 2 && group[3] !== undefined && group[4] !== undefined && group[5] !== undefined) {
        return {
          color: {
            kind: "rgb",
            value: [
              Math.min(255, group[3]),
              Math.min(255, group[4]),
              Math.min(255, group[5]),
            ],
          },
          next: index,
        }
      }
    }
    const mode = parameters[index + 1]?.[0]
    if (mode === 5) {
      const color = parameters[index + 2]?.[0]
      if (color === undefined) return undefined
      return {
        color: { kind: "indexed", value: Math.min(255, color) },
        next: index + 2,
      }
    }
    if (mode === 2) {
      const red = parameters[index + 2]?.[0]
      const green = parameters[index + 3]?.[0]
      const blue = parameters[index + 4]?.[0]
      if (red === undefined || green === undefined || blue === undefined) return undefined
      return {
        color: {
          kind: "rgb",
          value: [Math.min(255, red), Math.min(255, green), Math.min(255, blue)],
        },
        next: index + 4,
      }
    }
    return undefined
  }

  #underlineStyle(value: number | undefined): TerminalCellStyle["underline"] {
    if (value === 2) return "double"
    if (value === 3) return "curly"
    if (value === 4) return "dotted"
    if (value === 5) return "dashed"
    return "single"
  }

  #setModes(token: AnsiCsiToken, enabled: boolean): void {
    for (const group of token.parameters) {
      const mode = group[0]
      if (mode === 4) this.#insertMode = enabled
    }
  }

  #setPrivateModes(token: AnsiCsiToken, enabled: boolean): void {
    for (const group of token.parameters) {
      const mode = group[0]
      if (mode === 1) this.#applicationCursorKeys = enabled
      else if (mode === 6) {
        this.#originMode = enabled
        this.#setCursor(0, 0)
      } else if (mode === 7) this.#autoWrap = enabled
      else if (mode === 12) this.#cursorBlinking = enabled
      else if (mode === 25) this.#cursorVisible = enabled
      else if (mode === 47 || mode === 1047 || mode === 1049) {
        if (enabled) this.#enterAlternate(mode === 1049)
        else this.#leaveAlternate(mode === 1049)
      } else if (mode === 1000) this.#mouseTracking = enabled ? "button" : "none"
      else if (mode === 1002) this.#mouseTracking = enabled ? "button" : "none"
      else if (mode === 1003) this.#mouseTracking = enabled ? "any" : "none"
      else if (mode === 2004) this.#bracketedPaste = enabled
    }
  }

  #setCursorStyle(value: number): void {
    if (value === 0 || value === 1 || value === 2) this.#cursorShape = "block"
    else if (value === 3 || value === 4) this.#cursorShape = "underline"
    else if (value === 5 || value === 6) this.#cursorShape = "bar"
    this.#cursorBlinking = value === 0 || value % 2 === 1
  }

  #setCursor(row: number | undefined, col: number | undefined): void {
    const buffer = this.#buffer()
    if (row !== undefined) {
      const base = this.#originMode ? buffer.scrollTop : 0
      const maximum = this.#originMode ? buffer.scrollBottom : this.#rows - 1
      buffer.cursorRow = Math.min(maximum, Math.max(base, base + row))
    }
    if (col !== undefined) buffer.cursorCol = Math.min(this.#cols - 1, Math.max(0, col))
    buffer.wrapPending = false
  }

  #moveCursor(rows: number, cols: number): void {
    const buffer = this.#buffer()
    const minimumRow = this.#originMode ? buffer.scrollTop : 0
    const maximumRow = this.#originMode ? buffer.scrollBottom : this.#rows - 1
    buffer.cursorRow = Math.min(maximumRow, Math.max(minimumRow, buffer.cursorRow + rows))
    buffer.cursorCol = Math.min(this.#cols - 1, Math.max(0, buffer.cursorCol + cols))
    buffer.wrapPending = false
  }

  #lineFeed(wrapped: boolean): void {
    const buffer = this.#buffer()
    buffer.wrapPending = false
    if (buffer.cursorRow === buffer.scrollBottom) {
      this.#scrollUp(1)
      if (wrapped) buffer.lines[buffer.cursorRow]!.wrapped = true
      return
    }
    buffer.cursorRow = Math.min(this.#rows - 1, buffer.cursorRow + 1)
  }

  #reverseIndex(): void {
    const buffer = this.#buffer()
    buffer.wrapPending = false
    if (buffer.cursorRow === buffer.scrollTop) {
      this.#scrollDown(1)
      return
    }
    buffer.cursorRow = Math.max(0, buffer.cursorRow - 1)
  }

  #scrollUp(amount: number): void {
    const buffer = this.#buffer()
    const count = Math.min(buffer.scrollBottom - buffer.scrollTop + 1, Math.max(1, amount))
    for (let index = 0; index < count; index += 1) {
      const removed = buffer.lines.splice(buffer.scrollTop, 1)[0]!
      buffer.lines.splice(buffer.scrollBottom, 0, blankLine(this.#cols, ++this.#revision, this.#style))
      if (
        !this.#useAlternate
        && buffer.scrollTop === 0
        && buffer.scrollBottom === this.#rows - 1
        && this.#maximumScrollback > 0
      ) {
        this.#scrollback.push(removed)
        while (this.#scrollback.length > this.#maximumScrollback) this.#scrollback.shift()
      }
    }
  }

  #scrollDown(amount: number): void {
    const buffer = this.#buffer()
    const count = Math.min(buffer.scrollBottom - buffer.scrollTop + 1, Math.max(1, amount))
    for (let index = 0; index < count; index += 1) {
      buffer.lines.splice(buffer.scrollBottom, 1)
      buffer.lines.splice(buffer.scrollTop, 0, blankLine(this.#cols, ++this.#revision, this.#style))
    }
  }

  #eraseDisplay(mode: number): void {
    const buffer = this.#buffer()
    if (mode === 0) {
      this.#eraseLine(0)
      for (let row = buffer.cursorRow + 1; row < this.#rows; row += 1) {
        buffer.lines[row] = blankLine(this.#cols, ++this.#revision, this.#style)
      }
      return
    }
    if (mode === 1) {
      this.#eraseLine(1)
      for (let row = 0; row < buffer.cursorRow; row += 1) {
        buffer.lines[row] = blankLine(this.#cols, ++this.#revision, this.#style)
      }
      return
    }
    if (mode === 2 || mode === 3) {
      for (let row = 0; row < this.#rows; row += 1) {
        buffer.lines[row] = blankLine(this.#cols, ++this.#revision, this.#style)
      }
      if (mode === 3 && !this.#useAlternate) this.#scrollback = []
    }
  }

  #eraseLine(mode: number): void {
    const buffer = this.#buffer()
    const line = buffer.lines[buffer.cursorRow]!
    const start = mode === 1 || mode === 2 ? 0 : buffer.cursorCol
    const end = mode === 0 || mode === 2 ? this.#cols : buffer.cursorCol + 1
    for (let col = start; col < end; col += 1) line.cells[col] = blankCell(this.#style)
    line.wrapped = false
    this.#touch(buffer.cursorRow)
  }

  #insertLines(amount: number): void {
    const buffer = this.#buffer()
    if (buffer.cursorRow < buffer.scrollTop || buffer.cursorRow > buffer.scrollBottom) return
    const count = Math.min(buffer.scrollBottom - buffer.cursorRow + 1, amount)
    for (let index = 0; index < count; index += 1) {
      buffer.lines.splice(buffer.cursorRow, 0, blankLine(this.#cols, ++this.#revision, this.#style))
      buffer.lines.splice(buffer.scrollBottom + 1, 1)
    }
  }

  #deleteLines(amount: number): void {
    const buffer = this.#buffer()
    if (buffer.cursorRow < buffer.scrollTop || buffer.cursorRow > buffer.scrollBottom) return
    const count = Math.min(buffer.scrollBottom - buffer.cursorRow + 1, amount)
    for (let index = 0; index < count; index += 1) {
      buffer.lines.splice(buffer.cursorRow, 1)
      buffer.lines.splice(buffer.scrollBottom, 0, blankLine(this.#cols, ++this.#revision, this.#style))
    }
  }

  #insertCells(amount: number): void {
    const buffer = this.#buffer()
    const line = buffer.lines[buffer.cursorRow]!
    const count = Math.min(this.#cols - buffer.cursorCol, Math.max(1, amount))
    line.cells.splice(
      buffer.cursorCol,
      0,
      ...Array.from({ length: count }, () => blankCell(this.#style)),
    )
    line.cells.length = this.#cols
    this.#repairWide(line)
    this.#touch(buffer.cursorRow)
  }

  #deleteCells(amount: number): void {
    const buffer = this.#buffer()
    const line = buffer.lines[buffer.cursorRow]!
    const count = Math.min(this.#cols - buffer.cursorCol, Math.max(1, amount))
    line.cells.splice(buffer.cursorCol, count)
    while (line.cells.length < this.#cols) line.cells.push(blankCell(this.#style))
    this.#repairWide(line)
    this.#touch(buffer.cursorRow)
  }

  #eraseCells(amount: number): void {
    const buffer = this.#buffer()
    const line = buffer.lines[buffer.cursorRow]!
    const count = Math.min(this.#cols - buffer.cursorCol, Math.max(1, amount))
    for (let offset = 0; offset < count; offset += 1) {
      line.cells[buffer.cursorCol + offset] = blankCell(this.#style)
    }
    this.#repairWide(line)
    this.#touch(buffer.cursorRow)
  }

  #setScrollRegion(token: AnsiCsiToken): void {
    const buffer = this.#buffer()
    const top = parameterAt(token, 0, 1) - 1
    const bottom = parameterAt(token, 1, this.#rows) - 1
    if (top < 0 || bottom >= this.#rows || top >= bottom) return
    buffer.scrollTop = top
    buffer.scrollBottom = bottom
    this.#setCursor(0, 0)
  }

  #saveCursor(): void {
    const buffer = this.#buffer()
    buffer.saved = {
      row: buffer.cursorRow,
      col: buffer.cursorCol,
      style: copyStyle(this.#style),
      originMode: this.#originMode,
      wrapPending: buffer.wrapPending,
    }
  }

  #restoreCursor(): void {
    const buffer = this.#buffer()
    buffer.cursorRow = Math.min(this.#rows - 1, Math.max(0, buffer.saved.row))
    buffer.cursorCol = Math.min(this.#cols - 1, Math.max(0, buffer.saved.col))
    buffer.wrapPending = buffer.saved.wrapPending
    this.#style = copyStyle(buffer.saved.style)
    this.#originMode = buffer.saved.originMode
  }

  #enterAlternate(save: boolean): void {
    if (this.#useAlternate) return
    if (save) this.#saveCursor()
    this.#alternate = createBuffer(this.#rows, this.#cols, ++this.#revision)
    this.#useAlternate = true
  }

  #leaveAlternate(restore: boolean): void {
    if (!this.#useAlternate) return
    this.#useAlternate = false
    if (restore) this.#restoreCursor()
    this.#revision += 1
  }

  #resizeBuffer(
    buffer: ScreenBuffer,
    rows: number,
    cols: number,
    primary: boolean,
  ): void {
    for (const line of buffer.lines) {
      if (cols < line.cells.length) {
        line.cells.length = cols
      } else {
        while (line.cells.length < cols) line.cells.push(blankCell(this.#style))
      }
      this.#repairWide(line)
      line.revision = this.#revision
    }
    if (rows < buffer.lines.length) {
      const remove = buffer.lines.length - rows
      const removed = buffer.lines.splice(0, remove)
      if (primary && this.#maximumScrollback > 0) {
        this.#scrollback.push(...removed)
        while (this.#scrollback.length > this.#maximumScrollback) this.#scrollback.shift()
      }
      buffer.cursorRow = Math.max(0, buffer.cursorRow - remove)
    } else {
      while (buffer.lines.length < rows) buffer.lines.push(blankLine(cols, this.#revision))
    }
    buffer.cursorRow = Math.min(rows - 1, buffer.cursorRow)
    buffer.cursorCol = Math.min(cols - 1, buffer.cursorCol)
    buffer.scrollTop = 0
    buffer.scrollBottom = rows - 1
    buffer.wrapPending = false
  }

  #clearWideAt(line: MutableLine, col: number): void {
    const current = line.cells[col]
    if (current?.width === 0 && col > 0) line.cells[col - 1] = blankCell(this.#style)
    if (current?.width === 2 && col + 1 < line.cells.length) line.cells[col + 1] = blankCell(this.#style)
  }

  #repairWide(line: MutableLine): void {
    for (let col = 0; col < line.cells.length; col += 1) {
      const cell = line.cells[col]!
      if (cell.width === 2) {
        if (col + 1 >= line.cells.length) {
          line.cells[col] = blankCell(this.#style)
          continue
        }
        line.cells[col + 1] = { text: "", width: 0, style: copyStyle(cell.style) }
        col += 1
        continue
      }
      if (cell.width === 0) {
        const previous = col > 0 ? line.cells[col - 1] : undefined
        if (!previous || previous.width !== 2) line.cells[col] = blankCell(this.#style)
      }
    }
  }

  #touch(row: number): void {
    const line = this.#buffer().lines[row]
    if (!line) return
    line.revision = ++this.#revision
  }

  #safeTitle(value: string): string {
    return value
      .replace(/[\u0000-\u001f\u007f-\u009f]/gu, "")
      .replace(/\s+/gu, " ")
      .trim()
      .slice(0, 256)
  }

  #safeHyperlink(value: string): string | undefined {
    if (!value) return undefined
    if (value.length > 2_048 || /[\u0000-\u0020\u007f-\u009f]/u.test(value)) return undefined
    try {
      const url = new URL(value)
      if (!["http:", "https:", "mailto:"].includes(url.protocol)) return undefined
      url.username = ""
      url.password = ""
      return url.toString()
    } catch {
      return undefined
    }
  }

  #buffer(): ScreenBuffer {
    return this.#useAlternate ? this.#alternate : this.#primary
  }
}

export function terminalLineText(line: TerminalLine, trimEnd = true): string {
  let value = ""
  for (const cell of line.cells) {
    if (cell.width === 0) continue
    value += cell.text
  }
  return trimEnd ? value.trimEnd() : value
}

export function terminalVisibleRows(
  snapshot: TerminalScreenSnapshot,
  start: number,
  count: number,
): readonly TerminalLine[] {
  const rows = [...snapshot.scrollback, ...snapshot.lines]
  const offset = Math.min(rows.length, Math.max(0, Math.floor(start)))
  const limit = Math.min(10_000, Math.max(0, Math.floor(count)))
  return Object.freeze(rows.slice(offset, offset + limit))
}
