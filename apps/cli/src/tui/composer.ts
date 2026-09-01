import type { Readable, Writable } from "node:stream"
import { StringDecoder } from "node:string_decoder"
import { editDraftExternally } from "../input/editor.ts"
import { PromptDraft, PromptHistory, type DraftSnapshot } from "../input/draft.ts"

const PASTE_START = "\u001b[200~"
const PASTE_END = "\u001b[201~"

type RawInput = Readable & { isTTY?: boolean; setRawMode?: (enabled: boolean) => void }

export type ProductComposerResult =
  | { kind: "submit"; text: string; queue: boolean }
  | { kind: "interrupt" }
  | { kind: "closed" }
  | { kind: "exit" }

function linePosition(text: string, cursor: number): { current: number; total: number } {
  const before = text.slice(0, cursor)
  return { current: before.split("\n").length - 1, total: text.split("\n").length }
}

export class ProductComposer {
  readonly draft = new PromptDraft()
  readonly history = new PromptHistory()
  readonly #input: RawInput
  readonly #output: Writable
  readonly #candidates: readonly string[]
  readonly #decoder = new StringDecoder("utf8")
  readonly #onChange: (snapshot: DraftSnapshot) => void
  readonly #onNotice: (notice?: string) => void
  readonly #onScroll: (direction: "up" | "down") => void
  readonly #running: () => boolean
  #pending = ""
  #paste = ""
  #pasting = false
  #busy = false
  #dispose: (() => void) | undefined
  #settle: ((value: ProductComposerResult) => void) | undefined

  constructor(input: {
    stdin: Readable
    output: Writable
    candidates?: readonly string[]
    running: () => boolean
    onChange: (snapshot: DraftSnapshot) => void
    onNotice: (notice?: string) => void
    onScroll: (direction: "up" | "down") => void
  }) {
    this.#input = input.stdin as RawInput
    this.#output = input.output
    this.#candidates = [...new Set(input.candidates ?? [])].sort()
    this.#running = input.running
    this.#onChange = input.onChange
    this.#onNotice = input.onNotice
    this.#onScroll = input.onScroll
  }

  get snapshot(): DraftSnapshot { return this.draft.snapshot() }

  async read(): Promise<ProductComposerResult> {
    if (this.#settle) throw new TypeError("ProductComposer already has an active read.")
    this.#input.setRawMode?.(true)
    this.#input.resume()
    this.#output.write("\u001b[?2004h")
    this.#changed()
    return new Promise<ProductComposerResult>((resolve, reject) => {
      const data = (chunk: Buffer | string) => {
        void this.#consume(typeof chunk === "string" ? chunk : this.#decoder.write(chunk)).catch(reject)
      }
      const end = () => this.#finish({ kind: "exit" })
      this.#settle = resolve
      this.#input.on("data", data)
      this.#input.once("end", end)
      this.#dispose = () => {
        this.#input.off("data", data)
        this.#input.off("end", end)
        this.#input.setRawMode?.(false)
        this.#input.pause()
        this.#output.write("\u001b[?2004l")
      }
    })
  }

  close(): void { this.#finish({ kind: "closed" }) }

  async #consume(value: string): Promise<void> {
    this.#pending += value
    if (this.#busy) return
    while (this.#pending) {
      if (this.#pasting) {
        const end = this.#pending.indexOf(PASTE_END)
        if (end < 0) {
          this.#paste += this.#pending
          this.#pending = ""
          return
        }
        this.#paste += this.#pending.slice(0, end)
        this.draft.paste(this.#paste)
        this.#paste = ""
        this.#pasting = false
        this.#pending = this.#pending.slice(end + PASTE_END.length)
        this.#changed()
        continue
      }
      if (this.#pending.startsWith(PASTE_START)) {
        this.#pending = this.#pending.slice(PASTE_START.length)
        this.#pasting = true
        continue
      }
      const page = this.#pending.match(/^\u001b\[[56]~/)?.[0]
      if (page) {
        this.#pending = this.#pending.slice(page.length)
        this.#onScroll(page === "\u001b[5~" ? "up" : "down")
        continue
      }
      const key = this.#pending.match(/^\u001b\[(?:1;5[CD]|[ABCDHF]|[134]~)/)?.[0]
      if (key) {
        this.#pending = this.#pending.slice(key.length)
        const snapshot = this.draft.snapshot()
        const position = linePosition(snapshot.text, snapshot.cursor)
        if (key === "\u001b[A") {
          const prior = this.history.previous(position.current)
          if (prior !== undefined) this.draft.set(prior)
        } else if (key === "\u001b[B") {
          const next = this.history.next(position.current, position.total)
          if (next !== undefined) this.draft.set(next)
        } else if (key === "\u001b[H" || key === "\u001b[1~") {
          this.draft.home()
        } else if (key === "\u001b[F" || key === "\u001b[4~") {
          this.draft.end()
        } else if (key === "\u001b[3~") {
          this.draft.deleteForward()
        } else if (key === "\u001b[1;5D") {
          this.draft.moveWord(-1)
        } else if (key === "\u001b[1;5C") {
          this.draft.moveWord(1)
        } else {
          this.draft.move(key === "\u001b[C" ? 1 : -1)
        }
        this.#changed()
        continue
      }
      if (this.#pending.startsWith("\u001b") && this.#pending.length === 2) return
      if (this.#pending.startsWith("\u001b")) {
        this.#pending = this.#pending.slice(1)
        if (this.#running()) {
          this.#finish({ kind: "interrupt" })
          return
        }
        if (!this.draft.empty) {
          this.draft.cancel()
          this.#onNotice("草稿已暂存；Ctrl+R 或 /restore 可恢复")
          this.#changed()
        }
        continue
      }
      const char = [...this.#pending][0]!
      this.#pending = this.#pending.slice(char.length)
      if (char === "\r") {
        const submitted = this.draft.submit()
        if (!submitted) { this.#changed(); continue }
        this.history.push(submitted)
        this.#onNotice(undefined)
        this.#changed()
        this.#finish({ kind: "submit", text: submitted, queue: false })
        return
      }
      if (char === "\n") { this.draft.newline(); this.#changed(); continue }
      if (char === "\u0004" && this.draft.empty) { this.#finish({ kind: "exit" }); return }
      if (char === "\u0003") {
        if (this.#running()) { this.#finish({ kind: "interrupt" }); return }
        if (this.draft.empty) { this.#finish({ kind: "exit" }); return }
        this.draft.cancel()
        this.#onNotice("草稿已暂存；再次 Ctrl+C 退出")
        this.#changed()
        continue
      }
      if (char === "\u007f" || char === "\b") { this.draft.deleteBackward(); this.#changed(); continue }
      if (char === "\u001a") { this.draft.undo(); this.#changed(); continue }
      if (char === "\u0019") { this.draft.redo(); this.#changed(); continue }
      if (char === "\u0001") { this.draft.home(); this.#changed(); continue }
      if (char === "\u0012") {
        const snapshot = this.draft.snapshot()
        const found = snapshot.text
          ? this.history.search(snapshot.text)
          : this.draft.restore()?.text ?? this.history.search("")
        if (found !== undefined) this.draft.set(found)
        this.#onNotice(undefined)
        this.#changed()
        continue
      }
      if (char === "\t") {
        if (this.#running() && !this.draft.empty) {
          const submitted = this.draft.submit()
          if (submitted) {
            this.history.push(submitted)
            this.#finish({ kind: "submit", text: submitted, queue: true })
            return
          }
        }
        const matches = this.draft.complete(this.#candidates)
        if (matches.length === 1) {
          const snapshot = this.draft.snapshot()
          const token = snapshot.text.slice(0, snapshot.cursor).match(/(?:^|\s)([/@][^\s]*)$/)?.[1] ?? ""
          this.draft.insert(matches[0]!.slice(token.length))
        } else if (matches.length > 1) {
          this.#onNotice(matches.join("  "))
        }
        this.#changed()
        continue
      }
      if (char === "\u0005") {
        this.#busy = true
        this.#input.setRawMode?.(false)
        this.#output.write("\u001b[?2004l")
        try {
          this.draft.set(await editDraftExternally(this.draft.snapshot().text))
        } finally {
          this.#input.setRawMode?.(true)
          this.#output.write("\u001b[?2004h")
          this.#busy = false
          this.#changed()
        }
        continue
      }
      if (char >= " ") { this.draft.insert(char); this.#onNotice(undefined); this.#changed() }
    }
  }

  #changed(): void { this.#onChange(this.draft.snapshot()) }

  #finish(value: ProductComposerResult): void {
    const settle = this.#settle
    if (!settle) return
    this.#settle = undefined
    this.#dispose?.()
    this.#dispose = undefined
    settle(value)
  }
}
