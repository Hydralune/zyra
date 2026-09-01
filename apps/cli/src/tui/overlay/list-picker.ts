import type { Readable, Writable } from "node:stream"
import { StringDecoder } from "node:string_decoder"
import { PromptDraft } from "../../input/draft.ts"
import type { ProductOverlay, ProductPickerItem } from "./model.ts"

type RawInput = Readable & { setRawMode?: (enabled: boolean) => void }

function filterItems(items: readonly ProductPickerItem[], query: string): ProductPickerItem[] {
  const selected = query.trim().toLocaleLowerCase()
  if (!selected) return [...items]
  return items.filter((item) => [item.label, item.detail, ...(item.keywords ?? [])]
    .some((value) => value?.toLocaleLowerCase().includes(selected)))
}

export async function pickProductItem(input: {
  stdin: Readable
  output: Writable
  title: string
  items: readonly ProductPickerItem[]
  footer?: string
  bracketedPaste?: boolean
  onChange: (overlay?: ProductOverlay) => void
}): Promise<ProductPickerItem | undefined> {
  const stdin = input.stdin as RawInput
  const decoder = new StringDecoder("utf8")
  const query = new PromptDraft()
  let selected = 0
  const update = () => {
    const matches = filterItems(input.items, query.snapshot().text)
    selected = Math.max(0, Math.min(matches.length - 1, selected))
    const start = Math.max(0, selected - 11)
    input.onChange({
      title: input.title,
      query: query.snapshot().text,
      rows: matches.slice(start, start + 12),
      selected: selected - start,
      footer: input.footer ?? "↑↓ 选择 · Enter 确认 · Esc 返回",
    })
    return matches
  }

  stdin.setRawMode?.(true)
  stdin.resume()
  if (input.bracketedPaste !== false) input.output.write("\u001b[?2004h")
  update()
  try {
    return await new Promise<ProductPickerItem | undefined>((resolve, reject) => {
      let pending = ""
      const finish = (value?: ProductPickerItem) => {
        stdin.off("data", data)
        stdin.off("end", end)
        resolve(value)
      }
      const end = () => finish()
      const data = (chunk: Buffer | string) => {
        try {
          pending += typeof chunk === "string" ? chunk : decoder.write(chunk)
          while (pending) {
            const arrow = pending.match(/^\u001b\[[AB]/u)?.[0]
            if (arrow) {
              pending = pending.slice(arrow.length)
              const matches = update()
              if (matches.length) selected = (selected + (arrow.endsWith("A") ? -1 : 1) + matches.length) % matches.length
              update()
              continue
            }
            if (pending.startsWith("\u001b") || pending.startsWith("\u0003") || pending.startsWith("\u0004")) {
              pending = pending.slice(1)
              finish()
              return
            }
            const char = [...pending][0]!
            pending = pending.slice(char.length)
            if (char === "\r" || char === "\n") {
              finish(update()[selected])
              return
            }
            if (char === "\u007f" || char === "\b") query.deleteBackward()
            else if (char >= " ") query.insert(char)
            selected = 0
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
    input.output.write("\u001b[?2004l")
    input.onChange(undefined)
  }
}
