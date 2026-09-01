import type { Readable, Writable } from "node:stream"
import { sanitizeTerminalText } from "../text.ts"
import type { ProductOverlay } from "./model.ts"

type RawInput = Readable & { setRawMode?: (enabled: boolean) => void }

export async function pageProductText(input: {
  stdin: Readable
  output: Writable
  title: string
  lines: readonly string[]
  onChange: (overlay?: ProductOverlay) => void
}): Promise<void> {
  const stdin = input.stdin as RawInput
  const lines = input.lines.slice(0, 20_000).map((line) => sanitizeTerminalText(line).slice(0, 2_000))
  const pageSize = 16
  let offset = 0
  const update = () => {
    offset = Math.max(0, Math.min(Math.max(0, lines.length - pageSize), offset))
    input.onChange({
      title: input.title,
      rows: lines.slice(offset, offset + pageSize).map((label, index) => ({ id: String(offset + index), label })),
      selected: -1,
      footer: `${lines.length ? offset + 1 : 0}–${Math.min(lines.length, offset + pageSize)} / ${lines.length} · ↑↓/PgUp/PgDn · Home/End · Esc 返回`,
    })
  }
  stdin.setRawMode?.(true)
  stdin.resume()
  update()
  try {
    await new Promise<void>((resolve, reject) => {
      let pending = ""
      const finish = () => {
        stdin.off("data", data)
        stdin.off("end", end)
        resolve()
      }
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
    })
  } finally {
    stdin.setRawMode?.(false)
    stdin.pause()
    input.onChange(undefined)
  }
}
