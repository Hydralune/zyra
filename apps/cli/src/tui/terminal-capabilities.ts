import type { Readable, Writable } from "node:stream"

export const TERMINAL_CAPABILITIES_SCHEMA = "zyra.terminal-capabilities/v1" as const

export interface TerminalCapabilities {
  schema: typeof TERMINAL_CAPABILITIES_SCHEMA
  inputTty: boolean
  outputTty: boolean
  columns: number
  rows: number
  colorLevel: 0 | 1 | 2 | 3
  trueColor: boolean
  unicode: boolean
  inline: true
  alternateScreen: false
  bracketedPaste: boolean
  terminalFamily: string
}

function dimension(value: unknown, fallback: number, minimum: number, maximum: number): number {
  return Number.isSafeInteger(value)
    ? Math.max(minimum, Math.min(maximum, Number(value)))
    : fallback
}

function forcedColor(value: string | undefined): 0 | 1 | 2 | 3 | undefined {
  if (value === undefined) return undefined
  const selected = value.trim().toLowerCase()
  if (["", "1", "true", "always"].includes(selected)) return 1
  if (["0", "false", "never"].includes(selected)) return 0
  if (selected === "2" || selected === "256") return 2
  if (selected === "3" || selected === "truecolor" || selected === "24bit") return 3
  return 1
}

function terminalFamily(env: NodeJS.ProcessEnv, platform: NodeJS.Platform): string {
  if (env.WT_SESSION) return "windows-terminal"
  if (env.TERM_PROGRAM?.trim()) return env.TERM_PROGRAM.trim().toLowerCase().slice(0, 64)
  if (env.ConEmuANSI === "ON") return "conemu"
  if (env.ANSICON) return "ansicon"
  if (env.TERM?.trim()) return env.TERM.trim().toLowerCase().slice(0, 64)
  return platform === "win32" ? "windows-console" : "unknown"
}

export function probeTerminalCapabilities(input: {
  stdin: Readable
  output: Writable
  env?: NodeJS.ProcessEnv
  platform?: NodeJS.Platform
}): TerminalCapabilities {
  const env = input.env ?? process.env
  const platform = input.platform ?? process.platform
  const stdin = input.stdin as Readable & { isTTY?: boolean }
  const output = input.output as Writable & {
    isTTY?: boolean
    columns?: number
    rows?: number
    getColorDepth?: (env?: NodeJS.ProcessEnv) => number
  }
  const inputTty = stdin.isTTY === true
  const outputTty = output.isTTY === true
  const dumb = env.TERM?.trim().toLowerCase() === "dumb"
  const forced = forcedColor(env.FORCE_COLOR)
  let colorLevel: 0 | 1 | 2 | 3 = 0
  if (forced !== undefined) colorLevel = forced
  else if (env.NO_COLOR !== undefined || !outputTty || dumb) colorLevel = 0
  else if (/^(truecolor|24bit)$/iu.test(env.COLORTERM?.trim() ?? "") || env.WT_SESSION) colorLevel = 3
  else {
    const depth = output.getColorDepth?.(env)
    colorLevel = depth !== undefined && depth >= 24 ? 3 : depth !== undefined && depth >= 8 ? 2 : 1
  }
  const bracketedPaste = inputTty && outputTty && !dumb && platform !== "win32"
  return Object.freeze({
    schema: TERMINAL_CAPABILITIES_SCHEMA,
    inputTty,
    outputTty,
    columns: dimension(output.columns, 80, 20, 500),
    rows: dimension(output.rows, 24, 8, 300),
    colorLevel,
    trueColor: colorLevel === 3,
    unicode: !dumb,
    inline: true,
    alternateScreen: false,
    bracketedPaste,
    terminalFamily: terminalFamily(env, platform),
  })
}

export function formatTerminalCapabilities(value: TerminalCapabilities): string {
  return [
    `terminal · ${value.terminalFamily} · ${value.columns}×${value.rows}`,
    `render · inline · color ${value.colorLevel} · unicode ${value.unicode ? "yes" : "fallback"}`,
    `paste · ${value.bracketedPaste ? "bracketed" : "bounded burst detector"} · alternate screen off`,
  ].join("\n")
}
