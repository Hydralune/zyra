import type { Readable, Writable } from "node:stream"
import { StringDecoder } from "node:string_decoder"
import { editDraftExternally } from "./editor.ts"
import { PromptDraft, PromptHistory } from "./draft.ts"

const PASTE_START = "\u001b[200~"
const PASTE_END = "\u001b[201~"
const CLEAR_LINE = "\r\u001b[2K"

export type PromptReadResult =
  | { kind: "submit"; text: string }
  | { kind: "exit" }

type RawInput = Readable & { isTTY?: boolean; setRawMode?: (enabled: boolean) => void }

function linePosition(text: string, cursor: number): { current: number; total: number } {
  const before = text.slice(0, cursor)
  return { current: before.split("\n").length - 1, total: text.split("\n").length }
}

export class TerminalPrompt {
  readonly draft = new PromptDraft()
  readonly history = new PromptHistory()
  readonly #input: RawInput
  readonly #output: Writable
  readonly #candidates: readonly string[]
  readonly #decoder = new StringDecoder("utf8")
  #pending = ""
  #paste = ""
  #pasting = false
  #busy = false
  #dispose: (() => void) | undefined
  #settle: ((value: PromptReadResult) => void) | undefined

  constructor(input: {
    stdin: Readable
    stderr: Writable
    candidates?: readonly string[]
  }) {
    this.#input = input.stdin as RawInput
    this.#output = input.stderr
    this.#candidates = [...new Set(input.candidates ?? [])].sort()
  }

  async read(): Promise<PromptReadResult> {
    if (this.#settle) throw new TypeError("TerminalPrompt already has an active read.")
    this.#input.setRawMode?.(true)
    this.#input.resume()
    this.#output.write("\u001b[?2004h")
    this.#render()
    return new Promise<PromptReadResult>((resolve, reject) => {
      const data = (chunk: Buffer | string) => { void this.#consume(typeof chunk === "string" ? chunk : this.#decoder.write(chunk)).catch(reject) }
      const end = () => this.#finish({ kind: "exit" })
      this.#settle = resolve
      this.#input.on("data", data)
      this.#input.once("end", end)
      this.#dispose = () => {
        this.#input.off("data", data)
        this.#input.off("end", end)
        this.#input.setRawMode?.(false)
        this.#output.write("\u001b[?2004l")
      }
    })
  }

  close(): void { this.#finish({ kind: "exit" }) }

  async #consume(value: string): Promise<void> {
    if (this.#busy) return
    this.#pending += value
    while (this.#pending) {
      if (this.#pasting) {
        const end = this.#pending.indexOf(PASTE_END)
        if (end < 0) { this.#paste += this.#pending; this.#pending = ""; return }
        this.#paste += this.#pending.slice(0, end)
        this.draft.paste(this.#paste)
        this.#paste = ""
        this.#pasting = false
        this.#pending = this.#pending.slice(end + PASTE_END.length)
        this.#render()
        continue
      }
      if (this.#pending.startsWith(PASTE_START)) {
        this.#pending = this.#pending.slice(PASTE_START.length)
        this.#pasting = true
        continue
      }
      const escape = this.#pending.match(/^\u001b\[[ABCD]/)?.[0]
      if (escape) {
        this.#pending = this.#pending.slice(escape.length)
        const snapshot = this.draft.snapshot()
        const position = linePosition(snapshot.text, snapshot.cursor)
        if (escape === "\u001b[A") {
          const prior = this.history.previous(position.current)
          if (prior !== undefined) this.draft.set(prior)
        } else if (escape === "\u001b[B") {
          const next = this.history.next(position.current, position.total)
          if (next !== undefined) this.draft.set(next)
        } else this.draft.move(escape === "\u001b[C" ? 1 : -1)
        this.#render()
        continue
      }
      const char = [...this.#pending][0]!
      this.#pending = this.#pending.slice(char.length)
      if (char === "\r") {
        const submitted = this.draft.submit()
        if (!submitted) { this.#render(); continue }
        this.history.push(submitted)
        this.#output.write("\r\n")
        this.#finish({ kind: "submit", text: submitted })
        return
      }
      if (char === "\n") { this.draft.newline(); this.#render(); continue }
      if (char === "\u0004" && this.draft.empty) { this.#finish({ kind: "exit" }); return }
      if (char === "\u007f" || char === "\b") { this.draft.deleteBackward(); this.#render(); continue }
      if (char === "\u001b") {
        if (!this.draft.empty) {
          this.draft.cancel()
          this.#output.write(`${CLEAR_LINE}draft stashed; Ctrl+R restores/searches history, /restore also recovers it\r\n`)
        }
        this.#render()
        continue
      }
      if (char === "\u0012") {
        const snapshot = this.draft.snapshot()
        const found = snapshot.text
          ? this.history.search(snapshot.text)
          : this.draft.restore()?.text ?? this.history.search("")
        if (found !== undefined) this.draft.set(found)
        this.#render()
        continue
      }
      if (char === "\t") {
        const matches = this.draft.complete(this.#candidates)
        if (matches.length === 1) {
          const snapshot = this.draft.snapshot()
          const token = snapshot.text.slice(0, snapshot.cursor).match(/(?:^|\s)([/@][^\s]*)$/)?.[1] ?? ""
          this.draft.insert(matches[0]!.slice(token.length))
        } else if (matches.length > 1) {
          this.#output.write(`${CLEAR_LINE}${matches.join("  ")}\r\n`)
        }
        this.#render()
        continue
      }
      if (char === "\u0005") {
        this.#busy = true
        this.#input.setRawMode?.(false)
        this.#output.write("\u001b[?2004l\r\n")
        const initial = this.draft.snapshot().text
        try {
          this.draft.set(await editDraftExternally(initial))
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error)
          this.#output.write(`${CLEAR_LINE}external editor did not complete: ${detail}; draft preserved\r\n`)
        } finally {
          this.#input.setRawMode?.(true)
          this.#output.write("\u001b[?2004h")
          this.#busy = false
          this.#render()
        }
        continue
      }
      if (char >= " ") { this.draft.insert(char); this.#render() }
    }
  }

  #render(): void {
    const snapshot = this.draft.snapshot()
    const display = snapshot.display.replaceAll("\n", " ↵ ")
    this.#output.write(`${CLEAR_LINE}${snapshot.text ? "...>" : "zyra>"} ${display}`)
  }

  #finish(value: PromptReadResult): void {
    const settle = this.#settle
    if (!settle) return
    this.#settle = undefined
    this.#dispose?.()
    this.#dispose = undefined
    settle(value)
  }
}
