import { commandBytes } from "./identity.ts"
import type {
  ArgumentSuggestion,
  BoundCommandArguments,
  CommandArgumentSpec,
  CommandCompletion,
  CommandParseError,
  CommandToken,
  ParsedCommand,
} from "./contracts.ts"
import type { CommandRegistry } from "./registry.ts"

const MAX_COMMAND_BYTES = 256 * 1024

function error(
  code: CommandParseError["code"],
  message: string,
  start: number,
  end: number,
  argument?: string,
): CommandParseError {
  return {
    code,
    message,
    start,
    end,
    argument,
  }
}

export function tokenizeCommand(value: string): {
  tokens: CommandToken[]
  errors: CommandParseError[]
} {
  const tokens: CommandToken[] = []
  const errors: CommandParseError[] = []
  let start = -1
  let raw = ""
  let cooked = ""
  let quote: "'" | '"' | undefined
  let quoted = false
  let escaped = false

  const commit = (end: number) => {
    if (start < 0) return
    tokens.push({
      value: cooked,
      raw,
      start,
      end,
      quoted,
      quote,
    })
    start = -1
    raw = ""
    cooked = ""
    quoted = false
    quote = undefined
    escaped = false
  }

  for (let index = 0; index < value.length; index += 1) {
    const character = value[index]!
    if (escaped) {
      raw += character
      if (
        character === "\\" ||
        character === "'" ||
        character === '"' ||
        /\s/.test(character)
      ) {
        cooked += character
      } else {
        cooked += `\\${character}`
        errors.push(
          error(
            "invalid-escape",
            `Unsupported escape sequence: \\${character}`,
            Math.max(0, index - 1),
            index + 1,
          ),
        )
      }
      escaped = false
      continue
    }
    if (character === "\\") {
      if (start < 0) start = index
      raw += character
      escaped = true
      continue
    }
    if (quote) {
      raw += character
      if (character === quote) {
        quote = undefined
        quoted = true
      } else {
        cooked += character
      }
      continue
    }
    if (character === "'" || character === '"') {
      if (start < 0) start = index
      quote = character
      raw += character
      quoted = true
      continue
    }
    if (/\s/.test(character)) {
      commit(index)
      continue
    }
    if (start < 0) start = index
    raw += character
    cooked += character
  }

  if (escaped) {
    cooked += "\\"
    errors.push(
      error(
        "invalid-escape",
        "Trailing command escape has no escaped character.",
        Math.max(0, value.length - 1),
        value.length,
      ),
    )
  }
  if (quote) {
    errors.push(
      error(
        "unclosed-quote",
        `Unclosed ${quote === "'" ? "single" : "double"} quote.`,
        Math.max(0, start),
        value.length,
      ),
    )
  }
  commit(value.length)
  return {
    tokens,
    errors,
  }
}

function flagNames(spec: CommandArgumentSpec): string[] {
  return [spec.flag, ...(spec.aliases ?? [])]
    .filter((value): value is string => Boolean(value))
    .map((value) => value.toLowerCase())
}

function parseBoolean(
  value: string | boolean,
  spec: CommandArgumentSpec,
  token?: CommandToken,
): { value?: boolean; error?: CommandParseError } {
  if (typeof value === "boolean") return { value }
  const normalized = value.trim().toLowerCase()
  if (["true", "yes", "1", "on"].includes(normalized)) {
    return { value: true }
  }
  if (["false", "no", "0", "off"].includes(normalized)) {
    return { value: false }
  }
  return {
    error: error(
      "invalid-value",
      `${spec.name} must be true or false.`,
      token?.start ?? 0,
      token?.end ?? 0,
      spec.name,
    ),
  }
}

function parseInteger(
  value: string,
  spec: CommandArgumentSpec,
  token?: CommandToken,
): { value?: number; error?: CommandParseError } {
  if (!/^-?\d+$/.test(value)) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} must be an integer.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed)) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} is outside the safe integer range.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  if (spec.minimum !== undefined && parsed < spec.minimum) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} must be at least ${spec.minimum}.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  if (spec.maximum !== undefined && parsed > spec.maximum) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} must be at most ${spec.maximum}.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  return { value: parsed }
}

function parseDuration(
  value: string,
  spec: CommandArgumentSpec,
  token?: CommandToken,
): { value?: number; error?: CommandParseError } {
  const matched = /^(\d+)(ms|s|m|h|d)?$/i.exec(value.trim())
  if (!matched) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} must be a duration such as 500ms, 30s, or 5m.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  const amount = Number(matched[1])
  const units = matched[2]?.toLowerCase() ?? "ms"
  const multiplier = {
    ms: 1,
    s: 1000,
    m: 60_000,
    h: 3_600_000,
    d: 86_400_000,
  }[units]!
  const milliseconds = amount * multiplier
  if (!Number.isSafeInteger(milliseconds)) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} duration is too large.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  return parseInteger(String(milliseconds), spec, token)
}

function parseJson(
  value: string,
  spec: CommandArgumentSpec,
  token?: CommandToken,
): { value?: unknown; error?: CommandParseError } {
  try {
    const parsed: unknown = JSON.parse(value)
    if (parsed === undefined) {
      throw new TypeError("JSON value is undefined.")
    }
    return { value: parsed }
  } catch (cause) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} must be valid JSON: ${
          cause instanceof Error ? cause.message : String(cause)
        }`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
}

function parseIdentity(
  value: string,
  spec: CommandArgumentSpec,
  token?: CommandToken,
): { value?: string; error?: CommandParseError } {
  const normalized = value.trim()
  if (
    normalized.length < 1 ||
    normalized.length > 255 ||
    !/^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/.test(normalized)
  ) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} is not a valid identity.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  return { value: normalized }
}

function parseString(
  value: string,
  spec: CommandArgumentSpec,
  token?: CommandToken,
): { value?: string; error?: CommandParseError } {
  const normalized = value.trim()
  const maximum = spec.maximumBytes ?? 64 * 1024
  if (commandBytes(normalized) > maximum) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} exceeds ${maximum} bytes.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  if (spec.choices && !spec.choices.includes(normalized)) {
    return {
      error: error(
        "invalid-value",
        `${spec.name} must be one of ${spec.choices.join(", ")}.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  return { value: normalized }
}

function parseValue(
  value: string | boolean,
  spec: CommandArgumentSpec,
  token?: CommandToken,
): { value?: unknown; error?: CommandParseError } {
  if (spec.kind === "boolean") {
    return parseBoolean(value, spec, token)
  }
  if (typeof value === "boolean") {
    return {
      error: error(
        "missing-value",
        `${spec.name} requires a value.`,
        token?.start ?? 0,
        token?.end ?? 0,
        spec.name,
      ),
    }
  }
  if (spec.kind === "integer") {
    return parseInteger(value, spec, token)
  }
  if (spec.kind === "duration") {
    return parseDuration(value, spec, token)
  }
  if (spec.kind === "json") {
    return parseJson(value, spec, token)
  }
  if (spec.kind === "identity") {
    return parseIdentity(value, spec, token)
  }
  return parseString(value, spec, token)
}

function splitArguments(tokens: readonly CommandToken[]): {
  positional: CommandToken[]
  flags: Map<string, { value: string | boolean; token: CommandToken }>
  errors: CommandParseError[]
} {
  const positional: CommandToken[] = []
  const flags = new Map<string, { value: string | boolean; token: CommandToken }>()
  const errors: CommandParseError[] = []
  let positionalOnly = false
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]!
    if (positionalOnly) {
      positional.push(token)
      continue
    }
    if (token.value === "--") {
      positionalOnly = true
      continue
    }
    if (!token.value.startsWith("-") || token.value === "-") {
      positional.push(token)
      continue
    }
    const equals = token.value.indexOf("=")
    const name = (
      equals >= 0 ?
        token.value.slice(0, equals)
      : token.value
    ).toLowerCase()
    if (!/^--?[a-z][a-z0-9-]{0,40}$/.test(name)) {
      errors.push(
        error(
          "invalid-flag",
          `Invalid command flag: ${name}`,
          token.start,
          token.end,
        ),
      )
      continue
    }
    if (flags.has(name)) {
      errors.push(
        error(
          "duplicate-flag",
          `Duplicate command flag: ${name}`,
          token.start,
          token.end,
        ),
      )
      continue
    }
    if (equals >= 0) {
      flags.set(name, {
        value: token.value.slice(equals + 1),
        token,
      })
      continue
    }
    const next = tokens[index + 1]
    if (next && !next.value.startsWith("-")) {
      flags.set(name, {
        value: next.value,
        token: next,
      })
      index += 1
      continue
    }
    flags.set(name, {
      value: true,
      token,
    })
  }
  return {
    positional,
    flags,
    errors,
  }
}

export function bindCommandArguments(
  tokens: readonly CommandToken[],
  specs: readonly CommandArgumentSpec[],
): BoundCommandArguments {
  const split = splitArguments(tokens)
  const values: Record<string, unknown> = {}
  const rawFlags: Record<string, string | boolean> = {}
  const errors = [...split.errors]
  const hints: string[] = []
  const knownFlags = new Map<string, CommandArgumentSpec>()

  for (const spec of specs) {
    for (const name of flagNames(spec)) {
      knownFlags.set(name, spec)
    }
  }

  for (const [name, flag] of split.flags) {
    const spec = knownFlags.get(name)
    rawFlags[name.replace(/^-+/, "")] = flag.value
    if (!spec) {
      errors.push(
        error(
          "invalid-flag",
          `Unknown command flag: ${name}`,
          flag.token.start,
          flag.token.end,
        ),
      )
      continue
    }
    const parsed = parseValue(flag.value, spec, flag.token)
    if (parsed.error) {
      errors.push(parsed.error)
      continue
    }
    values[spec.name] = parsed.value
  }

  const positionalSpecs = specs.filter((spec) => !spec.flag)
  let cursor = 0
  for (const spec of positionalSpecs) {
    if (spec.variadic) {
      const selected = split.positional.slice(cursor)
      if (!selected.length) {
        if (spec.required) {
          errors.push(
            error(
              "missing-value",
              `Missing required argument: ${spec.name}`,
              tokens.at(-1)?.end ?? 0,
              tokens.at(-1)?.end ?? 0,
              spec.name,
            ),
          )
        }
        hints.push(spec.description)
        continue
      }
      const combined = selected.map((token) => token.value).join(" ")
      const parsed = parseValue(combined, spec, selected[0])
      if (parsed.error) {
        errors.push(parsed.error)
      } else {
        values[spec.name] = parsed.value
      }
      cursor = split.positional.length
      continue
    }
    const token = split.positional[cursor]
    if (!token) {
      if (spec.required) {
        errors.push(
          error(
            "missing-value",
            `Missing required argument: ${spec.name}`,
            tokens.at(-1)?.end ?? 0,
            tokens.at(-1)?.end ?? 0,
            spec.name,
          ),
        )
      }
      hints.push(spec.description)
      continue
    }
    const parsed = parseValue(token.value, spec, token)
    if (parsed.error) {
      errors.push(parsed.error)
    } else {
      values[spec.name] = parsed.value
    }
    cursor += 1
  }

  for (const spec of specs.filter((candidate) => candidate.flag)) {
    if (
      spec.required &&
      !Object.prototype.hasOwnProperty.call(values, spec.name)
    ) {
      errors.push(
        error(
          "missing-value",
          `Missing required flag: ${spec.flag}`,
          tokens.at(-1)?.end ?? 0,
          tokens.at(-1)?.end ?? 0,
          spec.name,
        ),
      )
    }
    if (!Object.prototype.hasOwnProperty.call(values, spec.name)) {
      hints.push(spec.description)
    }
  }

  if (cursor < split.positional.length) {
    for (const token of split.positional.slice(cursor)) {
      errors.push(
        error(
          "unexpected-argument",
          `Unexpected command argument: ${token.value}`,
          token.start,
          token.end,
        ),
      )
    }
  }

  return {
    values,
    positional: split.positional.map((token) => token.value),
    flags: rawFlags,
    errors,
    hints,
  }
}

export function parseCommand(
  raw: string,
  registry: CommandRegistry,
): ParsedCommand {
  const normalized = raw.trim()
  const initialErrors: CommandParseError[] = []
  if (!normalized) {
    initialErrors.push(error("empty", "Command input is empty.", 0, raw.length))
  }
  if (normalized && !normalized.startsWith("/")) {
    initialErrors.push(
      error(
        "not-command",
        "Control command input must begin with a slash.",
        0,
        Math.min(raw.length, normalized.length),
      ),
    )
  }
  if (commandBytes(normalized) > MAX_COMMAND_BYTES) {
    initialErrors.push(
      error(
        "input-too-large",
        `Command input exceeds ${MAX_COMMAND_BYTES} bytes.`,
        0,
        raw.length,
      ),
    )
  }
  const tokenized = tokenizeCommand(normalized)
  const commandToken = tokenized.tokens[0]
  const trigger = commandToken?.value.toLowerCase() ?? ""
  const descriptor = trigger ? registry.resolve(trigger) : undefined
  if (trigger && !descriptor) {
    initialErrors.push(
      error(
        "unknown-command",
        `Unknown Zyra control command: ${trigger}`,
        commandToken?.start ?? 0,
        commandToken?.end ?? raw.length,
      ),
    )
  }
  const bound = bindCommandArguments(
    tokenized.tokens.slice(1),
    descriptor?.arguments ?? [],
  )
  const errors = [
    ...initialErrors,
    ...tokenized.errors,
    ...bound.errors,
  ]
  return {
    raw,
    normalized,
    trigger,
    descriptor,
    tokens: tokenized.tokens,
    arguments: {
      ...bound,
      errors,
    },
    errors,
  }
}

export function completionFor(
  raw: string,
  cursor: number,
  registry: CommandRegistry,
): CommandCompletion {
  const position = Math.max(0, Math.min(raw.length, cursor))
  const prefix = raw.slice(0, position)
  const tokenized = tokenizeCommand(prefix)
  const tokens = tokenized.tokens
  const command = tokens[0]
  if (
    !command ||
    position <= command.end ||
    !/\s/.test(prefix.slice(command.end))
  ) {
    return {
      kind: "command",
      query: command?.value.replace(/^\//, "") ?? prefix.replace(/^\s*\//, ""),
      replaceStart: Math.max(0, prefix.search(/\S/)),
      replaceEnd: command?.end ?? position,
      argumentIndex: -1,
    }
  }
  const descriptor = registry.resolve(command.value)
  const current = tokens.at(-1)
  const afterCommand = tokens.slice(1)
  const currentIsFlag = Boolean(current?.value.startsWith("-"))
  const previous = afterCommand.at(-2)
  const previousSpec = descriptor?.arguments.find((spec) =>
    flagNames(spec).includes(previous?.value.toLowerCase() ?? ""),
  )
  if (currentIsFlag) {
    return {
      kind: "flag",
      query: current?.value ?? "",
      replaceStart: current?.start ?? position,
      replaceEnd: current?.end ?? position,
      argumentIndex: Math.max(0, afterCommand.length - 1),
    }
  }
  if (previousSpec?.flag) {
    return {
      kind: "value",
      query: current?.value ?? "",
      replaceStart: current?.start ?? position,
      replaceEnd: current?.end ?? position,
      argumentIndex: Math.max(0, afterCommand.length - 1),
      argumentName: previousSpec.name,
    }
  }
  const positional = afterCommand.filter((token) => !token.value.startsWith("-"))
  const argumentIndex = Math.max(0, positional.length - 1)
  const spec = descriptor?.arguments.filter((argument) => !argument.flag)[argumentIndex]
  return {
    kind: "argument",
    query: current?.value ?? "",
    replaceStart: current?.start ?? position,
    replaceEnd: current?.end ?? position,
    argumentIndex,
    argumentName: spec?.name,
  }
}

function matchRanges(value: string, query: string): [number, number][] {
  const source = value.toLowerCase()
  const target = query.toLowerCase().replace(/^-+/, "")
  if (!target) return []
  const direct = source.indexOf(target)
  if (direct >= 0) return [[direct, direct + target.length]]
  let cursor = 0
  const ranges: [number, number][] = []
  for (const character of target) {
    const found = source.indexOf(character, cursor)
    if (found < 0) return []
    const previous = ranges.at(-1)
    if (previous && previous[1] === found) {
      previous[1] += 1
    } else {
      ranges.push([found, found + 1])
    }
    cursor = found + 1
  }
  return ranges
}

export function argumentSuggestions(input: {
  parsed: ParsedCommand
  completion: CommandCompletion
  identities?: readonly { id: string; label?: string; description?: string }[]
  history?: readonly string[]
  limit?: number
}): ArgumentSuggestion[] {
  const descriptor = input.parsed.descriptor
  if (!descriptor) return []
  const spec =
    input.completion.argumentName ?
      descriptor.arguments.find(
        (argument) => argument.name === input.completion.argumentName,
      )
    : descriptor.arguments[input.completion.argumentIndex]
  const suggestions: ArgumentSuggestion[] = []
  const query = input.completion.query

  if (input.completion.kind === "flag") {
    const used = new Set(
      Object.keys(input.parsed.arguments.flags).map((name) => `--${name}`),
    )
    for (const argument of descriptor.arguments) {
      if (!argument.flag || used.has(argument.flag)) continue
      const matched = matchRanges(argument.flag, query)
      if (query && !matched.length) continue
      suggestions.push({
        id: `${descriptor.id}:flag:${argument.name}`,
        value: argument.flag,
        label: argument.flag,
        description: argument.description,
        kind: "flag",
        matched,
      })
    }
  }

  for (const choice of spec?.choices ?? []) {
    const matched = matchRanges(choice, query)
    if (query && !matched.length) continue
    suggestions.push({
      id: `${descriptor.id}:choice:${spec?.name}:${choice}`,
      value: choice,
      label: choice,
      description: spec?.description ?? "",
      kind: "choice",
      matched,
    })
  }

  if (spec?.kind === "identity") {
    for (const identity of input.identities ?? []) {
      const matched = matchRanges(
        `${identity.id} ${identity.label ?? ""}`,
        query,
      )
      if (query && !matched.length) continue
      suggestions.push({
        id: `${descriptor.id}:identity:${identity.id}`,
        value: identity.id,
        label: identity.label ?? identity.id,
        description: identity.description ?? identity.id,
        kind: "identity",
        matched,
      })
    }
  }

  for (const value of input.history ?? []) {
    const matched = matchRanges(value, query)
    if (query && !matched.length) continue
    suggestions.push({
      id: `${descriptor.id}:history:${value}`,
      value,
      label: value,
      description: "Previously submitted value",
      kind: "history",
      matched,
    })
  }

  const unique = new Map<string, ArgumentSuggestion>()
  for (const suggestion of suggestions) {
    if (!unique.has(suggestion.value)) {
      unique.set(suggestion.value, suggestion)
    }
  }
  return [...unique.values()]
    .sort((left, right) => {
      const leftExact = left.value === query ? 1 : 0
      const rightExact = right.value === query ? 1 : 0
      if (leftExact !== rightExact) return rightExact - leftExact
      if (left.matched.length !== right.matched.length) {
        return left.matched.length - right.matched.length
      }
      return left.label.localeCompare(right.label)
    })
    .slice(0, Math.max(1, Math.min(50, Math.floor(input.limit ?? 12))))
}

export function applyCommandCompletion(
  raw: string,
  completion: CommandCompletion,
  value: string,
): { value: string; cursor: number } {
  const before = raw.slice(0, completion.replaceStart)
  const after = raw.slice(completion.replaceEnd)
  const normalized =
    completion.kind === "command" ?
      `${value.startsWith("/") ? value : `/${value}`} `
    : `${value}${after && !/^\s/.test(after) ? " " : ""}`
  return {
    value: `${before}${normalized}${after}`,
    cursor: before.length + normalized.length,
  }
}
