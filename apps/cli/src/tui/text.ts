const ANSI_ESCAPE = /\u001b(?:\[[0-?]*[ -/]*[@-~]|\][^\u0007]*(?:\u0007|\u001b\\)|[PX^_].*?\u001b\\)/gs
const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g
const MARKS = /\p{Mark}/u
const EMOJI = /\p{Extended_Pictographic}/u

const segmenter = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
  : undefined

export function sanitizeTerminalText(value: string): string {
  return value
    .replace(ANSI_ESCAPE, "")
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n")
    .replace(CONTROL, "�")
}

export function graphemes(value: string): string[] {
  const safe = sanitizeTerminalText(value)
  return segmenter
    ? [...segmenter.segment(safe)].map((item) => item.segment)
    : Array.from(safe)
}

function codePointWidth(code: number): number {
  if (
    code >= 0x1100 && (
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
      || (code >= 0x20000 && code <= 0x3fffd)
    )
  ) return 2
  return 1
}

export function graphemeWidth(value: string): number {
  if (!value || value === "\n") return 0
  if (value === "\t") return 4
  if (EMOJI.test(value) || value.includes("\u200d")) return 2
  for (const rune of Array.from(value)) {
    if (MARKS.test(rune) || rune === "\ufe0e" || rune === "\ufe0f") continue
    return codePointWidth(rune.codePointAt(0) ?? 0)
  }
  return 0
}

export function displayWidth(value: string): number {
  return graphemes(value).reduce((sum, item) => sum + graphemeWidth(item), 0)
}

export function takeDisplayWidth(value: string, width: number): { head: string; tail: string } {
  const selected = graphemes(value)
  let used = 0
  let index = 0
  while (index < selected.length) {
    const next = graphemeWidth(selected[index]!)
    if (used + next > Math.max(0, width)) break
    used += next
    index += 1
  }
  return { head: selected.slice(0, index).join(""), tail: selected.slice(index).join("") }
}

export function clipDisplay(value: string, width: number): string {
  const safe = sanitizeTerminalText(value).replaceAll("\n", " ")
  if (displayWidth(safe) <= width) return safe
  return `${takeDisplayWidth(safe, Math.max(1, width - 1)).head}…`
}

export function padDisplay(value: string, width: number): string {
  const clipped = clipDisplay(value, width)
  return clipped + " ".repeat(Math.max(0, width - displayWidth(clipped)))
}

export function wrapDisplay(value: string, width: number): string[] {
  const safe = sanitizeTerminalText(value)
  const limit = Math.max(1, width)
  const output: string[] = []
  for (const source of safe.split("\n")) {
    if (!source) {
      output.push("")
      continue
    }
    let remaining = source
    while (displayWidth(remaining) > limit) {
      let selected = takeDisplayWidth(remaining, limit)
      const whitespace = selected.head.match(/^(.*\S)\s+\S*$/u)?.[1]
      if (whitespace && displayWidth(whitespace) >= Math.floor(limit * 0.5)) {
        const consumed = selected.head.slice(whitespace.length).length
        selected = { head: whitespace, tail: `${selected.head.slice(selected.head.length - consumed).trimStart()}${selected.tail}` }
      }
      if (!selected.head) break
      output.push(selected.head)
      remaining = selected.tail
    }
    output.push(remaining)
  }
  return output
}
