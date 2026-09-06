import type { Readable, Writable } from "node:stream"
import { graphemes, graphemeWidth, sanitizeTerminalText } from "../text.ts"
import type { ProductOverlay } from "./model.ts"

import { terminalSessionOwnsInput } from "../terminal-session.ts"

type RawInput = Readable & { setRawMode?: (enabled: boolean) => void }

export async function pageProductText(input: {
  stdin: Readable
  output: Writable
  title: string
  lines: readonly string[]
  pageSize?: number
  signal?: AbortSignal
  onChange: (overlay?: ProductOverlay) => void
}): Promise<void> {
  const stdin = input.stdin as RawInput
  const terminal = input.output as Writable & { columns?: number; rows?: number }
  const source = input.lines.slice(0, 20_000).map(sanitizeTerminalText).join("\n")
  const bounded = source.slice(0, 1_000_000)
    + (source.length > 1_000_000 || input.lines.length > 20_000 ? "\n…预览已截断，请使用 /export 或 /ui 查看完整内容。" : "")
  let lines: string[] = []
  let pageSize = 16
  const layout = () => {
    const width = Math.max(20, (terminal.columns ?? 80) - 4)
    pageSize = Math.max(1, Math.min(200, input.pageSize ?? 16, (terminal.rows ?? 24) - 8))
    lines = []
    for (const sourceLine of bounded.split("\n")) {
      let line = ""
      let used = 0
      for (const char of graphemes(sourceLine)) {
        const cells = graphemeWidth(char)
        if (used + cells > width && line) { lines.push(line); line = ""; used = 0 }
        line += char
        used += cells
      }
      lines.push(line)
    }
  }
  layout()
  const row = (label: string, index: number) => ({
    id: String(index),
    label,
    tone: label.startsWith("+") && !label.startsWith("+++")
      ? "success" as const
      : label.startsWith("-") && !label.startsWith("---")
        ? "error" as const
        : label.startsWith("@@")
          ? "accent" as const
          : label.startsWith("+++") || label.startsWith("---") || label.startsWith("diff --git")
            ? "secondary" as const
            : "default" as const,
  })
  let offset = 0
  const update = () => {
    offset = Math.max(0, Math.min(Math.max(0, lines.length - pageSize), offset))
    input.onChange({
      kind: "pager",
      title: input.title,
      rows: lines.slice(offset, offset + pageSize).map((label, index) => row(label, offset + index)),
      selected: -1,
      footer: `${lines.length ? offset + 1 : 0}–${Math.min(lines.length, offset + pageSize)} / ${lines.length} · ↑↓/PgUp/PgDn · Home/End · Esc 返回`,
    })
  }
  const resize = () => { layout(); update() }
  input.output.on("resize", resize)
  stdin.setRawMode?.(true)
  stdin.resume()
  update()
  try {
    await new Promise<void>((resolve, reject) => {
      let pending = ""
      let settled = false
      const finish = () => {
        if (settled) return
        settled = true
        stdin.off("data", data)
        stdin.off("end", end)
        input.signal?.removeEventListener("abort", abort)
        resolve()
      }
      const abort = () => finish()
      const end = () => finish()
      const data = (chunk: Buffer | string) => {
        try {
          pending += chunk.toString()
          while (pending) {
            const key = pending.match(/^\u001b\[(?:[ABHF]|[56]~)/u)?.[0]
            if (key) {
              pending = pending.slice(key.length)
              if (key === "\u001b[A") offset -= 1
              else if (key === "\u001b[B") offset += 1
              else if (key === "\u001b[5~") offset -= pageSize
              else if (key === "\u001b[6~") offset += pageSize
              else if (key === "\u001b[H") offset = 0
              else if (key === "\u001b[F") offset = lines.length
              update()
              continue
            }
            if (pending.startsWith("\u001b") || pending.startsWith("\u0003") || pending.startsWith("q")) {
              pending = pending.slice(1)
              finish()
              return
            }
            const char = [...pending][0]!
            pending = pending.slice(char.length)
            if (char === "j") offset += 1
            else if (char === "k") offset -= 1
            else if (char === "g") offset = 0
            else if (char === "G") offset = lines.length
            else if (char === "\r" || char === "\n" || char === "\u0004") {
              finish()
              return
            }
            update()
          }
        } catch (error) {
          reject(error)
        }
      }
      stdin.on("data", data)
      stdin.once("end", end)
      input.signal?.addEventListener("abort", abort, { once: true })
      if (input.signal?.aborted) finish()
    })
  } finally {
    input.output.off("resize", resize)
    stdin.setRawMode?.(terminalSessionOwnsInput(stdin))
    stdin.pause()
    input.onChange(undefined)
  }
}
