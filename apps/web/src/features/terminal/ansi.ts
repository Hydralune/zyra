export type AnsiParserState =
  | "ground"
  | "escape"
  | "csi-entry"
  | "csi-parameter"
  | "csi-intermediate"
  | "osc-string"
  | "osc-escape"
  | "dcs-string"
  | "dcs-escape"
  | "ignore-until-st"

export interface AnsiTextToken {
  kind: "text"
  value: string
}

export interface AnsiControlToken {
  kind: "control"
  code: "bell" | "backspace" | "tab" | "line-feed" | "carriage-return" | "shift-out" | "shift-in"
}

export interface AnsiEscapeToken {
  kind: "escape"
  final: string
  intermediates: string
}

export interface AnsiCsiToken {
  kind: "csi"
  final: string
  privateMarker: string
  intermediates: string
  parameters: readonly (readonly (number | undefined)[])[]
}

export interface AnsiOscToken {
  kind: "osc"
  command: number | undefined
  data: string
  truncated: boolean
}

export interface AnsiIgnoredToken {
  kind: "ignored"
  family: "dcs" | "sos" | "pm" | "apc" | "invalid"
  byteLength: number
}

export type AnsiToken =
  | AnsiTextToken
  | AnsiControlToken
  | AnsiEscapeToken
  | AnsiCsiToken
  | AnsiOscToken
  | AnsiIgnoredToken

export interface AnsiParserSnapshot {
  state: AnsiParserState
  pendingText: string
  parameters: string
  intermediates: string
  privateMarker: string
  osc: string
  ignoredBytes: number
}

export interface AnsiParserOptions {
  maximumOscBytes?: number
  maximumSequenceBytes?: number
  maximumTextChunk?: number
}

const ESC = "\u001b"
const BEL = "\u0007"
const C1_CSI = "\u009b"
const C1_OSC = "\u009d"
const C1_DCS = "\u0090"
const C1_ST = "\u009c"

function codePointByteLength(value: string): number {
  const code = value.codePointAt(0) ?? 0
  if (code <= 0x7f) return 1
  if (code <= 0x7ff) return 2
  if (code <= 0xffff) return 3
  return 4
}

function controlCode(value: string): AnsiControlToken["code"] | undefined {
  if (value === "\u0007") return "bell"
  if (value === "\u0008") return "backspace"
  if (value === "\u0009") return "tab"
  if (value === "\u000a" || value === "\u000b" || value === "\u000c") return "line-feed"
  if (value === "\u000d") return "carriage-return"
  if (value === "\u000e") return "shift-out"
  if (value === "\u000f") return "shift-in"
  return undefined
}

function isC0(value: string): boolean {
  const code = value.charCodeAt(0)
  return code <= 0x1f || code === 0x7f
}

function isIntermediate(value: string): boolean {
  const code = value.charCodeAt(0)
  return code >= 0x20 && code <= 0x2f
}

function isParameter(value: string): boolean {
  const code = value.charCodeAt(0)
  return code >= 0x30 && code <= 0x3f
}

function isFinal(value: string): boolean {
  const code = value.charCodeAt(0)
  return code >= 0x40 && code <= 0x7e
}

function parseParameters(raw: string): readonly (readonly (number | undefined)[])[] {
  if (!raw) return Object.freeze([])
  const groups = raw.split(";")
  if (groups.length > 128) return Object.freeze([])
  return Object.freeze(
    groups.map((group) =>
      Object.freeze(
        group.split(":").slice(0, 16).map((value) => {
          if (!value) return undefined
          const parsed = Number.parseInt(value, 10)
          if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > 1_000_000) return undefined
          return parsed
        }),
      ),
    ),
  )
}

function parseOsc(raw: string, truncated: boolean): AnsiOscToken {
  const separator = raw.indexOf(";")
  if (separator < 0) {
    const command = Number.parseInt(raw, 10)
    return {
      kind: "osc",
      command: Number.isSafeInteger(command) && command >= 0 ? command : undefined,
      data: "",
      truncated,
    }
  }
  const commandValue = raw.slice(0, separator)
  const command = Number.parseInt(commandValue, 10)
  return {
    kind: "osc",
    command: Number.isSafeInteger(command) && command >= 0 ? command : undefined,
    data: raw.slice(separator + 1),
    truncated,
  }
}

export class AnsiStreamParser {
  readonly #maximumOscBytes: number
  readonly #maximumSequenceBytes: number
  readonly #maximumTextChunk: number
  #state: AnsiParserState = "ground"
  #text = ""
  #parameters = ""
  #intermediates = ""
  #privateMarker = ""
  #osc = ""
  #oscBytes = 0
  #oscTruncated = false
  #ignoredBytes = 0
  #sequenceBytes = 0
  #family: AnsiIgnoredToken["family"] = "invalid"

  constructor(options: AnsiParserOptions = {}) {
    this.#maximumOscBytes = Math.min(
      64 * 1_024,
      Math.max(256, Math.floor(options.maximumOscBytes ?? 8 * 1_024)),
    )
    this.#maximumSequenceBytes = Math.min(
      1_024 * 1_024,
      Math.max(64, Math.floor(options.maximumSequenceBytes ?? 64 * 1_024)),
    )
    this.#maximumTextChunk = Math.min(
      1_024 * 1_024,
      Math.max(64, Math.floor(options.maximumTextChunk ?? 16 * 1_024)),
    )
  }

  push(input: string): readonly AnsiToken[] {
    const tokens: AnsiToken[] = []
    for (const value of input) {
      this.#consume(value, tokens)
    }
    this.#flushText(tokens)
    return Object.freeze(tokens)
  }

  finish(): readonly AnsiToken[] {
    const tokens: AnsiToken[] = []
    this.#flushText(tokens)
    if (this.#state === "osc-string" || this.#state === "osc-escape") {
      tokens.push(parseOsc(this.#osc, true))
    } else if (this.#state !== "ground") {
      tokens.push({
        kind: "ignored",
        family: this.#family,
        byteLength: Math.max(1, this.#ignoredBytes + this.#sequenceBytes),
      })
    }
    this.reset()
    return Object.freeze(tokens)
  }

  reset(): void {
    this.#state = "ground"
    this.#text = ""
    this.#parameters = ""
    this.#intermediates = ""
    this.#privateMarker = ""
    this.#osc = ""
    this.#oscBytes = 0
    this.#oscTruncated = false
    this.#ignoredBytes = 0
    this.#sequenceBytes = 0
    this.#family = "invalid"
  }

  snapshot(): AnsiParserSnapshot {
    return Object.freeze({
      state: this.#state,
      pendingText: this.#text,
      parameters: this.#parameters,
      intermediates: this.#intermediates,
      privateMarker: this.#privateMarker,
      osc: this.#osc,
      ignoredBytes: this.#ignoredBytes,
    })
  }

  #consume(value: string, tokens: AnsiToken[]): void {
    if (this.#state === "ground") {
      this.#consumeGround(value, tokens)
      return
    }
    if (this.#state === "escape") {
      this.#consumeEscape(value, tokens)
      return
    }
    if (this.#state === "csi-entry" || this.#state === "csi-parameter") {
      this.#consumeCsiParameter(value, tokens)
      return
    }
    if (this.#state === "csi-intermediate") {
      this.#consumeCsiIntermediate(value, tokens)
      return
    }
    if (this.#state === "osc-string") {
      this.#consumeOsc(value, tokens)
      return
    }
    if (this.#state === "osc-escape") {
      this.#consumeOscEscape(value, tokens)
      return
    }
    if (this.#state === "dcs-string") {
      this.#consumeIgnored(value, tokens)
      return
    }
    if (this.#state === "dcs-escape") {
      this.#consumeIgnoredEscape(value, tokens)
      return
    }
    this.#consumeIgnored(value, tokens)
  }

  #consumeGround(value: string, tokens: AnsiToken[]): void {
    if (value === ESC) {
      this.#flushText(tokens)
      this.#begin("escape", "invalid")
      return
    }
    if (value === C1_CSI) {
      this.#flushText(tokens)
      this.#begin("csi-entry", "invalid")
      return
    }
    if (value === C1_OSC) {
      this.#flushText(tokens)
      this.#beginOsc()
      return
    }
    if (value === C1_DCS) {
      this.#flushText(tokens)
      this.#beginIgnored("dcs")
      return
    }
    if (isC0(value)) {
      this.#flushText(tokens)
      const code = controlCode(value)
      if (code) tokens.push({ kind: "control", code })
      return
    }
    this.#text += value
    if (this.#text.length >= this.#maximumTextChunk) this.#flushText(tokens)
  }

  #consumeEscape(value: string, tokens: AnsiToken[]): void {
    this.#sequenceBytes += codePointByteLength(value)
    if (value === ESC) {
      this.#intermediates = ""
      this.#sequenceBytes = 1
      return
    }
    if (isC0(value)) {
      const code = controlCode(value)
      if (code) tokens.push({ kind: "control", code })
      return
    }
    if (value === "[") {
      this.#state = "csi-entry"
      this.#parameters = ""
      this.#intermediates = ""
      this.#privateMarker = ""
      return
    }
    if (value === "]") {
      this.#beginOsc()
      return
    }
    if (value === "P") {
      this.#beginIgnored("dcs")
      return
    }
    if (value === "X") {
      this.#beginIgnored("sos")
      return
    }
    if (value === "^") {
      this.#beginIgnored("pm")
      return
    }
    if (value === "_") {
      this.#beginIgnored("apc")
      return
    }
    if (isIntermediate(value)) {
      if (this.#intermediates.length < 8) this.#intermediates += value
      this.#state = "csi-intermediate"
      return
    }
    if (isFinal(value)) {
      tokens.push({
        kind: "escape",
        final: value,
        intermediates: this.#intermediates,
      })
      this.#finishSequence()
      return
    }
    tokens.push({ kind: "ignored", family: "invalid", byteLength: this.#sequenceBytes })
    this.#finishSequence()
  }

  #consumeCsiParameter(value: string, tokens: AnsiToken[]): void {
    this.#sequenceBytes += codePointByteLength(value)
    if (this.#sequenceBytes > this.#maximumSequenceBytes) {
      this.#overflow(tokens, "invalid")
      return
    }
    if (isC0(value)) {
      const code = controlCode(value)
      if (code) tokens.push({ kind: "control", code })
      return
    }
    if (value === ESC) {
      this.#state = "escape"
      this.#parameters = ""
      this.#intermediates = ""
      this.#privateMarker = ""
      return
    }
    if (isParameter(value)) {
      if (
        this.#state === "csi-entry"
        && this.#parameters.length === 0
        && this.#privateMarker.length === 0
        && "<=>?".includes(value)
      ) {
        this.#privateMarker = value
      } else if (this.#parameters.length < 4_096) {
        this.#parameters += value
      } else {
        this.#overflow(tokens, "invalid")
        return
      }
      this.#state = "csi-parameter"
      return
    }
    if (isIntermediate(value)) {
      if (this.#intermediates.length < 8) {
        this.#intermediates += value
        this.#state = "csi-intermediate"
      } else {
        this.#overflow(tokens, "invalid")
      }
      return
    }
    if (isFinal(value)) {
      tokens.push({
        kind: "csi",
        final: value,
        privateMarker: this.#privateMarker,
        intermediates: this.#intermediates,
        parameters: parseParameters(this.#parameters),
      })
      this.#finishSequence()
      return
    }
    this.#overflow(tokens, "invalid")
  }

  #consumeCsiIntermediate(value: string, tokens: AnsiToken[]): void {
    this.#sequenceBytes += codePointByteLength(value)
    if (isC0(value)) {
      const code = controlCode(value)
      if (code) tokens.push({ kind: "control", code })
      return
    }
    if (value === ESC) {
      this.#state = "escape"
      this.#parameters = ""
      this.#intermediates = ""
      this.#privateMarker = ""
      return
    }
    if (isIntermediate(value)) {
      if (this.#intermediates.length < 8) {
        this.#intermediates += value
      } else {
        this.#overflow(tokens, "invalid")
      }
      return
    }
    if (isFinal(value)) {
      const hasCsiMaterial = this.#parameters.length > 0 || this.#privateMarker.length > 0
      if (hasCsiMaterial) {
        tokens.push({
          kind: "csi",
          final: value,
          privateMarker: this.#privateMarker,
          intermediates: this.#intermediates,
          parameters: parseParameters(this.#parameters),
        })
      } else {
        tokens.push({
          kind: "escape",
          final: value,
          intermediates: this.#intermediates,
        })
      }
      this.#finishSequence()
      return
    }
    this.#overflow(tokens, "invalid")
  }

  #consumeOsc(value: string, tokens: AnsiToken[]): void {
    if (value === BEL || value === C1_ST) {
      tokens.push(parseOsc(this.#osc, this.#oscTruncated))
      this.#finishSequence()
      return
    }
    if (value === ESC) {
      this.#state = "osc-escape"
      return
    }
    const bytes = codePointByteLength(value)
    this.#sequenceBytes += bytes
    this.#oscBytes += bytes
    if (this.#sequenceBytes > this.#maximumSequenceBytes) {
      tokens.push(parseOsc(this.#osc, true))
      this.#beginIgnored("invalid")
      return
    }
    if (this.#oscBytes <= this.#maximumOscBytes) {
      this.#osc += value
      return
    }
    this.#oscTruncated = true
  }

  #consumeOscEscape(value: string, tokens: AnsiToken[]): void {
    if (value === "\\") {
      tokens.push(parseOsc(this.#osc, this.#oscTruncated))
      this.#finishSequence()
      return
    }
    if (value === ESC) return
    this.#state = "osc-string"
    this.#consumeOsc(value, tokens)
  }

  #consumeIgnored(value: string, tokens: AnsiToken[]): void {
    if (value === C1_ST) {
      tokens.push({
        kind: "ignored",
        family: this.#family,
        byteLength: this.#ignoredBytes,
      })
      this.#finishSequence()
      return
    }
    if (value === ESC) {
      this.#state = "dcs-escape"
      return
    }
    this.#ignoredBytes += codePointByteLength(value)
    if (this.#ignoredBytes > this.#maximumSequenceBytes) {
      tokens.push({
        kind: "ignored",
        family: this.#family,
        byteLength: this.#ignoredBytes,
      })
      this.#finishSequence()
    }
  }

  #consumeIgnoredEscape(value: string, tokens: AnsiToken[]): void {
    if (value === "\\") {
      tokens.push({
        kind: "ignored",
        family: this.#family,
        byteLength: this.#ignoredBytes,
      })
      this.#finishSequence()
      return
    }
    if (value === ESC) return
    this.#state = "dcs-string"
    this.#ignoredBytes += 1 + codePointByteLength(value)
  }

  #begin(state: AnsiParserState, family: AnsiIgnoredToken["family"]): void {
    this.#state = state
    this.#family = family
    this.#sequenceBytes = 1
    this.#parameters = ""
    this.#intermediates = ""
    this.#privateMarker = ""
    this.#ignoredBytes = 0
  }

  #beginOsc(): void {
    this.#begin("osc-string", "invalid")
    this.#osc = ""
    this.#oscBytes = 0
    this.#oscTruncated = false
  }

  #beginIgnored(family: AnsiIgnoredToken["family"]): void {
    this.#begin("dcs-string", family)
    this.#ignoredBytes = 0
  }

  #finishSequence(): void {
    this.#state = "ground"
    this.#parameters = ""
    this.#intermediates = ""
    this.#privateMarker = ""
    this.#osc = ""
    this.#oscBytes = 0
    this.#oscTruncated = false
    this.#ignoredBytes = 0
    this.#sequenceBytes = 0
    this.#family = "invalid"
  }

  #overflow(tokens: AnsiToken[], family: AnsiIgnoredToken["family"]): void {
    tokens.push({
      kind: "ignored",
      family,
      byteLength: Math.max(1, this.#sequenceBytes),
    })
    this.#finishSequence()
  }

  #flushText(tokens: AnsiToken[]): void {
    if (!this.#text) return
    tokens.push({ kind: "text", value: this.#text })
    this.#text = ""
  }
}

export function stripUnsafeTerminalControls(input: string): string {
  const parser = new AnsiStreamParser()
  const tokens = [...parser.push(input), ...parser.finish()]
  let output = ""
  for (const token of tokens) {
    if (token.kind === "text") {
      output += token.value
      continue
    }
    if (token.kind === "control") {
      if (token.code === "tab") output += "\t"
      if (token.code === "line-feed") output += "\n"
      if (token.code === "carriage-return") output += "\r"
      if (token.code === "backspace") output += "\b"
      continue
    }
    if (token.kind === "csi") {
      const parameters = token.parameters
        .map((group) => group.map((value) => value ?? "").join(":"))
        .join(";")
      output += `${ESC}[${token.privateMarker}${parameters}${token.intermediates}${token.final}`
      continue
    }
    if (token.kind === "escape") {
      const allowed = new Set(["7", "8", "D", "E", "H", "M", "c"])
      if (allowed.has(token.final)) output += `${ESC}${token.intermediates}${token.final}`
      continue
    }
    if (token.kind === "osc") {
      if (token.command === 0 || token.command === 1 || token.command === 2) {
        const title = token.data
          .replace(/[\u0000-\u001f\u007f-\u009f]/gu, "")
          .slice(0, 256)
        output += `${ESC}]${token.command};${title}${BEL}`
      }
    }
  }
  return output
}
