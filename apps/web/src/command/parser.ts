import type { CommandCatalog, CommandDefinition } from "./catalog.ts"

export type ParsedInput =
  | { kind: "empty"; raw: string }
  | { kind: "prompt"; raw: string; text: string }
  | {
      kind: "command"
      raw: string
      trigger: string
      definition?: CommandDefinition
      tokens: readonly ParsedToken[]
      arguments: readonly string[]
      flags: Readonly<Record<string, string | boolean>>
      rest: string
      error?: string
    }

export interface ParsedToken {
  value: string
  start: number
  end: number
  quoted: boolean
  quote?: "'" | '"'
}

export interface CompletionContext {
  kind: "none" | "command" | "argument"
  query: string
  trigger?: string
  argumentIndex?: number
  replaceStart: number
  replaceEnd: number
}

export interface ArgumentValidation {
  valid: boolean
  errors: string[]
  hints: string[]
}

function parseTokens(raw: string, offset = 0): { tokens: ParsedToken[]; error?: string } {
  const tokens: ParsedToken[] = []
  let value = ""
  let start = -1
  let quote: "'" | '"' | undefined
  let escaped = false
  let quoted = false

  const commit = (end: number) => {
    if (start < 0) return
    tokens.push({
      value,
      start: start + offset,
      end: end + offset,
      quoted,
      quote,
    })
    value = ""
    start = -1
    quoted = false
    quote = undefined
  }

  for (let index = 0; index < raw.length; index += 1) {
    const character = raw[index]!
    if (escaped) {
      if (start < 0) start = index - 1
      value += character
      escaped = false
      continue
    }
    if (character === "\\") {
      if (start < 0) start = index
      escaped = true
      continue
    }
    if (quote) {
      if (character === quote) {
        quote = undefined
        quoted = true
      } else {
        value += character
      }
      continue
    }
    if (character === "'" || character === '"') {
      if (start < 0) start = index
      quote = character
      quoted = true
      continue
    }
    if (/\s/.test(character)) {
      commit(index)
      continue
    }
    if (start < 0) start = index
    value += character
  }
  if (escaped) value += "\\"
  commit(raw.length)
  return {
    tokens,
    ...(quote ? { error: `Unclosed ${quote === "'" ? "single" : "double"} quote.` } : {}),
  }
}

function parseFlags(tokens: readonly ParsedToken[]): {
  positional: string[]
  flags: Record<string, string | boolean>
  error?: string
} {
  const positional: string[] = []
  const flags: Record<string, string | boolean> = {}
  let positionalOnly = false
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]!
    if (positionalOnly) {
      positional.push(token.value)
      continue
    }
    if (token.value === "--") {
      positionalOnly = true
      continue
    }
    if (!token.value.startsWith("--") || token.value.length === 2) {
      positional.push(token.value)
      continue
    }
    const equals = token.value.indexOf("=")
    const name = token.value.slice(2, equals >= 0 ? equals : undefined)
    if (!/^[a-z][a-z0-9-]{0,40}$/.test(name)) {
      return { positional, flags, error: `Invalid flag: ${token.value}` }
    }
    if (Object.prototype.hasOwnProperty.call(flags, name)) {
      return { positional, flags, error: `Duplicate flag: --${name}` }
    }
    if (equals >= 0) {
      flags[name] = token.value.slice(equals + 1)
      continue
    }
    const next = tokens[index + 1]
    if (next && !next.value.startsWith("--")) {
      flags[name] = next.value
      index += 1
    } else {
      flags[name] = true
    }
  }
  return { positional, flags }
}

export function parseInput(raw: string, catalog: CommandCatalog): ParsedInput {
  if (!raw.trim()) return { kind: "empty", raw }
  const firstNonWhitespace = raw.search(/\S/)
  if (firstNonWhitespace < 0 || raw[firstNonWhitespace] !== "/") {
    return { kind: "prompt", raw, text: raw.trim() }
  }
  const commandBody = raw.slice(firstNonWhitespace + 1)
  const parsed = parseTokens(commandBody, firstNonWhitespace + 1)
  const commandToken = parsed.tokens[0]
  if (!commandToken) {
    return {
      kind: "command",
      raw,
      trigger: "",
      tokens: [],
      arguments: [],
      flags: {},
      rest: "",
      error: parsed.error,
    }
  }
  const trigger = commandToken.value.toLowerCase()
  const argumentTokens = parsed.tokens.slice(1)
  const flags = parseFlags(argumentTokens)
  const start = argumentTokens[0]?.start ?? commandToken.end
  return {
    kind: "command",
    raw,
    trigger,
    definition: catalog.resolve(trigger),
    tokens: parsed.tokens,
    arguments: flags.positional,
    flags: flags.flags,
    rest: raw.slice(start).trim(),
    error: parsed.error ?? flags.error,
  }
}

export function completionContext(
  value: string,
  cursor: number,
  catalog: CommandCatalog,
): CompletionContext {
  const position = Math.max(0, Math.min(value.length, cursor))
  const before = value.slice(0, position)
  const firstNonWhitespace = before.search(/\S/)
  if (firstNonWhitespace < 0) {
    return { kind: "none", query: "", replaceStart: position, replaceEnd: position }
  }
  if (value[firstNonWhitespace] !== "/") {
    return { kind: "none", query: "", replaceStart: position, replaceEnd: position }
  }
  const parsed = parseTokens(value.slice(firstNonWhitespace + 1), firstNonWhitespace + 1)
  const command = parsed.tokens[0]
  if (!command || position <= command.end) {
    const query = value.slice(firstNonWhitespace + 1, position)
    return {
      kind: "command",
      query,
      replaceStart: firstNonWhitespace,
      replaceEnd: command?.end ?? position,
    }
  }
  const definition = catalog.resolve(command.value)
  const tokensBeforeCursor = parsed.tokens.filter((token) => token.start < position)
  const current = [...tokensBeforeCursor].reverse().find((token) => position <= token.end)
  const argumentIndex = Math.max(0, tokensBeforeCursor.length - 2)
  return {
    kind: "argument",
    query: current ? value.slice(current.start, position) : "",
    trigger: definition?.trigger ?? command.value,
    argumentIndex,
    replaceStart: current?.start ?? position,
    replaceEnd: current?.end ?? position,
  }
}

export function applyCompletion(
  value: string,
  completion: CompletionContext,
  replacement: string,
): { value: string; cursor: number } {
  const prefix = value.slice(0, completion.replaceStart)
  const suffix = value.slice(completion.replaceEnd)
  const normalized =
    completion.kind === "command"
      ? `/${replacement.replace(/^\//, "")} `
      : replacement
  const next = `${prefix}${normalized}${suffix}`
  return {
    value: next,
    cursor: prefix.length + normalized.length,
  }
}

export function validateCommandArguments(parsed: ParsedInput): ArgumentValidation {
  if (parsed.kind !== "command") return { valid: true, errors: [], hints: [] }
  const errors: string[] = []
  const hints: string[] = []
  if (parsed.error) errors.push(parsed.error)
  if (!parsed.definition) {
    if (parsed.trigger) errors.push(`Unknown command: /${parsed.trigger}`)
    return { valid: false, errors, hints }
  }
  const definitions = parsed.definition.arguments
  let consumed = 0
  for (let index = 0; index < definitions.length; index += 1) {
    const definition = definitions[index]!
    if (definition.variadic) {
      const remainder = parsed.arguments.slice(consumed)
      if (definition.required && remainder.length === 0) {
        errors.push(`Missing required argument: ${definition.label}`)
      }
      if (remainder.length === 0) hints.push(definition.description)
      consumed = parsed.arguments.length
      break
    }
    const value = parsed.arguments[consumed]
    if (!value) {
      if (definition.required) errors.push(`Missing required argument: ${definition.label}`)
      hints.push(definition.description)
      continue
    }
    if (definition.choices && !definition.choices.includes(value)) {
      errors.push(`${definition.label} must be one of ${definition.choices.join(", ")}.`)
    }
    consumed += 1
  }
  if (consumed < parsed.arguments.length && !definitions.some((argument) => argument.variadic)) {
    errors.push(`Unexpected argument: ${parsed.arguments[consumed]}`)
  }
  return { valid: errors.length === 0, errors, hints }
}

export function commandArgumentHint(parsed: ParsedInput, cursor: number): string | undefined {
  if (parsed.kind !== "command" || !parsed.definition) return undefined
  const position = Math.max(0, Math.min(parsed.raw.length, cursor))
  const completed = parsed.tokens.filter((token) => token.end < position).length
  const index = Math.max(0, completed - 1)
  const argument = parsed.definition.arguments[Math.min(index, parsed.definition.arguments.length - 1)]
  if (!argument) return undefined
  const options = argument.choices?.length ? ` (${argument.choices.join(" | ")})` : ""
  return `${argument.label}${options}: ${argument.description}`
}

export function promptText(parsed: ParsedInput): string {
  if (parsed.kind === "prompt") return parsed.text
  if (parsed.kind === "command") return parsed.rest
  return ""
}
