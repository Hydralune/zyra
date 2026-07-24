export type TerminalStructuredKind =
  | "command"
  | "prompt"
  | "diagnostic"
  | "test"
  | "progress"
  | "path"
  | "url"
  | "json"
  | "table"
  | "plain"

export interface TerminalStructuredLine {
  kind: TerminalStructuredKind
  text: string
  level?: "debug" | "info" | "warning" | "error" | "success"
  command?: string
  path?: string
  line?: number
  column?: number
  url?: string
  progress?: number
  fields?: Readonly<Record<string, string | number | boolean | null>>
}

const CREDENTIAL_PATTERNS = [
  /\b(?:sk|pk|rk|ghp|github_pat|glpat|xox[baprs])[-_A-Za-z0-9]{12,}\b/giu,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b/giu,
  /\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|credential)\s*[:=]\s*["']?[^\s"',;]{4,}/giu,
  /\bAKIA[A-Z0-9]{16}\b/gu,
]

export function redactTerminalText(value: string, secrets: readonly string[] = []): {
  text: string
  redacted: boolean
} {
  let text = value
  let redacted = false
  for (const secret of [...new Set(secrets)].sort((a, b) => b.length - a.length)) {
    if (secret.length < 4 || !text.includes(secret)) continue
    text = text.split(secret).join("[REDACTED]")
    redacted = true
  }
  for (const pattern of CREDENTIAL_PATTERNS) {
    text = text.replace(pattern, (match) => {
      redacted = true
      const separator = match.search(/[:=]/u)
      return separator >= 0 ? `${match.slice(0, separator + 1)}[REDACTED]` : "[REDACTED]"
    })
  }
  return { text, redacted }
}

function safeJson(value: unknown): Readonly<Record<string, string | number | boolean | null>> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  const output: Record<string, string | number | boolean | null> = {}
  for (const [key, item] of Object.entries(value)) {
    if (!/^[A-Za-z0-9_.-]{1,128}$/u.test(key)) continue
    if (typeof item === "string") output[key] = item.slice(0, 2_048)
    else if (typeof item === "number" && Number.isFinite(item)) output[key] = item
    else if (typeof item === "boolean" || item === null) output[key] = item
    if (Object.keys(output).length >= 64) break
  }
  return Object.keys(output).length ? Object.freeze(output) : undefined
}

export function classifyTerminalLine(raw: string): TerminalStructuredLine {
  const text = raw.replace(/\r$/u, "").slice(0, 64 * 1_024)
  const command = /^\s*(?:[$#>]|PS [^>]+>)\s+(.+)$/u.exec(text)
  if (command) return { kind: "command", text, command: command[1]!.slice(0, 8_192) }
  const diagnostic = /^(.+?):(\d+)(?::(\d+))?:\s*(error|warning|warn|info|note)?\s*:?\s*(.*)$/iu.exec(text)
  if (diagnostic) {
    const level = diagnostic[4]?.toLowerCase()
    return {
      kind: "diagnostic",
      text,
      level: level === "error" ? "error" : level === "warning" || level === "warn" ? "warning" : "info",
      path: diagnostic[1]!.slice(0, 4_096),
      line: Number(diagnostic[2]),
      column: diagnostic[3] ? Number(diagnostic[3]) : undefined,
    }
  }
  const test = /^\s*(PASS|PASSED|OK|FAIL|FAILED|ERROR|SKIP|SKIPPED)\b(.*)$/iu.exec(text)
  if (test) {
    const value = test[1]!.toUpperCase()
    return {
      kind: "test",
      text,
      level: value === "PASS" || value === "PASSED" || value === "OK"
        ? "success"
        : value.startsWith("SKIP")
          ? "info"
          : "error",
    }
  }
  const progress = /(?:^|\s)(\d{1,3}(?:\.\d+)?)\s*%(?:\s|$)/u.exec(text)
  if (progress) {
    const value = Math.min(100, Math.max(0, Number(progress[1])))
    return { kind: "progress", text, progress: value, level: "info" }
  }
  const url = /\bhttps?:\/\/[^\s<>"']{1,2048}/iu.exec(text)
  if (url) {
    try {
      const parsed = new URL(url[0])
      parsed.username = ""
      parsed.password = ""
      return { kind: "url", text, url: parsed.toString() }
    } catch {
      return { kind: "plain", text }
    }
  }
  if (/^\s*[{[]/u.test(text) && /[}\]]\s*$/u.test(text)) {
    try {
      const fields = safeJson(JSON.parse(text))
      if (fields) return { kind: "json", text, fields }
    } catch {
      // Incomplete or non-object JSON stays plain terminal output.
    }
  }
  if (/^\s*\|.+\|\s*$/u.test(text) || /^\s*\+[-+]+\+\s*$/u.test(text)) {
    return { kind: "table", text }
  }
  if (/^\s*(?:[A-Za-z]:[\\/]|\/)[^\u0000]*$/u.test(text) && text.length <= 4_096) {
    return { kind: "path", text, path: text.trim() }
  }
  if (/^\s*(?:error|fatal|panic|exception)\b/iu.test(text)) {
    return { kind: "diagnostic", text, level: "error" }
  }
  if (/^\s*(?:warn|warning)\b/iu.test(text)) {
    return { kind: "diagnostic", text, level: "warning" }
  }
  if (/^\s*(?:success|done|completed)\b/iu.test(text)) {
    return { kind: "plain", text, level: "success" }
  }
  return { kind: "plain", text }
}

export class TerminalStructuredOutput {
  readonly #maximumLines: number
  #pending = ""
  #lines: TerminalStructuredLine[] = []
  #revision = 0

  constructor(maximumLines = 10_000) {
    this.#maximumLines = Math.min(100_000, Math.max(1, Math.floor(maximumLines)))
  }

  push(value: string): readonly TerminalStructuredLine[] {
    this.#pending += value
    const parts = this.#pending.split(/\n/u)
    this.#pending = parts.pop() ?? ""
    const added = parts.map(classifyTerminalLine)
    if (added.length) {
      this.#lines.push(...added)
      if (this.#lines.length > this.#maximumLines) {
        this.#lines.splice(0, this.#lines.length - this.#maximumLines)
      }
      this.#revision += 1
    }
    return Object.freeze(added)
  }

  finish(): readonly TerminalStructuredLine[] {
    if (!this.#pending) return Object.freeze([])
    const line = classifyTerminalLine(this.#pending)
    this.#pending = ""
    this.#lines.push(line)
    if (this.#lines.length > this.#maximumLines) this.#lines.shift()
    this.#revision += 1
    return Object.freeze([line])
  }

  snapshot(): Readonly<{ lines: readonly TerminalStructuredLine[]; pending: string; revision: number }> {
    return Object.freeze({
      lines: Object.freeze([...this.#lines]),
      pending: this.#pending,
      revision: this.#revision,
    })
  }
}
