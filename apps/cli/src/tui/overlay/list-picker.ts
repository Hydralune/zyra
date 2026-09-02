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
  kind?: "picker" | "menu" | "approval" | "question"
  description?: readonly string[]
  bracketedPaste?: boolean
  onChange: (overlay?: ProductOverlay) => void
}): Promise<ProductPickerItem | undefined> {
  const stdin = input.stdin as RawInput
  const decoder = new StringDecoder("utf8")
  const query = new PromptDraft()
  let selected = 0
  const update = () => {
    const searchable = (input.kind ?? "picker") === "picker"
    const matches = filterItems(input.items, searchable ? query.snapshot().text : "")
    selected = Math.max(0, Math.min(matches.length - 1, selected))
    const start = Math.max(0, selected - 11)
    input.onChange({
      kind: input.kind ?? "picker",
      title: input.title,
      description: input.description,
      query: searchable ? query.snapshot().text : undefined,
      rows: matches.slice(start, start + 12),
      selected: selected - start,
      footer: input.footer ?? "↑↓ 选择 · Enter 确认 · Esc 返回",
    })
    return matches
  }

  stdin.setRawMode?.(true)
  stdin.resume()
  if (input.bracketedPaste !== false) input.output.write("\u001b[?2004h")
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
            if (/^[1-9]$/u.test(char) && query.empty) {
              const matches = update()
              finish(matches[Number(char) - 1])
              return
            }
            if (input.kind === "approval" && query.empty && /^[yYaAdD]$/u.test(char)) {
              const matches = update()
              const shortcut = char.toLocaleLowerCase()
              const choice = shortcut === "d"
                ? matches.find((item) => item.id.startsWith("deny:"))
                : shortcut === "a"
                  ? matches.find((item) => item.id === "allow:session") ?? matches.find((item) => item.id.startsWith("allow:"))
                  : matches.find((item) => item.id === "allow:once") ?? matches.find((item) => item.id.startsWith("allow:"))
              finish(choice)
              return
            }
            if (char === "\r" || char === "\n") {
              finish(update()[selected])
              return
            }
            if (char === "\u007f" || char === "\b") query.deleteBackward()
            else if (char >= " " && (input.kind ?? "picker") === "picker") query.insert(char)
            selected = 0
            update()
          }
        } catch (error) {
          reject(error)
        }
      }
      stdin.on("data", data)
      stdin.once("end", end)
      // Publishing the overlay is the observable readiness boundary.  Attach
      // input first so a user (or PTY automation) cannot press Enter in the
      // small window between the first paint and listener registration.
      update()
    })
  } finally {
    stdin.setRawMode?.(false)
    stdin.pause()
    input.output.write("\u001b[?2004l")
    input.onChange(undefined)
  }
}

export async function promptProductText(input: {
  stdin: Readable
  output: Writable
  title: string
  description?: readonly string[]
  footer?: string
  maximumCharacters?: number
  bracketedPaste?: boolean
  onChange: (overlay?: ProductOverlay) => void
}): Promise<string | undefined> {
  const stdin = input.stdin as RawInput
  const decoder = new StringDecoder("utf8")
  const draft = new PromptDraft()
  const maximum = Math.max(1, Math.min(16_384, input.maximumCharacters ?? 4_096))
  const update = () => input.onChange({
    kind: "question",
    title: input.title,
    description: input.description,
    query: draft.snapshot().text,
    rows: [],
    selected: -1,
    footer: input.footer ?? "Enter 提交 · Esc 返回",
  })
  stdin.setRawMode?.(true)
  stdin.resume()
  if (input.bracketedPaste !== false) input.output.write("\u001b[?2004h")
  try {
    return await new Promise<string | undefined>((resolve, reject) => {
      let pending = ""
      const finish = (value?: string) => {
        stdin.off("data", data)
        stdin.off("end", end)
        resolve(value)
      }
      const end = () => finish()
      const data = (chunk: Buffer | string) => {
        try {
          pending += typeof chunk === "string" ? chunk : decoder.write(chunk)
          while (pending) {
            if (pending.startsWith("\u001b") || pending.startsWith("\u0003") || pending.startsWith("\u0004")) {
              pending = pending.slice(1)
              finish()
              return
            }
            const char = [...pending][0]!
            pending = pending.slice(char.length)
            if (char === "\r" || char === "\n") {
              const answer = draft.snapshot().text.trim()
              if (answer) finish(answer)
              return
            }
            if (char === "\u007f" || char === "\b") draft.deleteBackward()
            else if (char >= " " && draft.snapshot().text.length + char.length <= maximum) draft.insert(char)
            update()
          }
        } catch (error) {
          reject(error)
        }
      }
      stdin.on("data", data)
      stdin.once("end", end)
      update()
    })
  } finally {
    stdin.setRawMode?.(false)
    stdin.pause()
    input.output.write("\u001b[?2004l")
    input.onChange(undefined)
  }
}
