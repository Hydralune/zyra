import type { Readable, Writable } from "node:stream"
import { StringDecoder } from "node:string_decoder"
import { editDraftExternally } from "../input/editor.ts"
import { MAX_PROMPT_BYTES, PromptDraft, PromptHistory, PromptInputLimitError, type DraftPersistenceSnapshot, type DraftSnapshot } from "../input/draft.ts"
import { acceptCompletion, completionState, type CompletionState } from "./overlay/completion.ts"
import { PasteBurstDetector, type PasteBurstFlush } from "./paste-burst.ts"
import { TerminalSessionGuard } from "./terminal-session.ts"

const PASTE_START = "\u001b[200~"
const PASTE_END = "\u001b[201~"

type RawInput = Readable & { isTTY?: boolean; setRawMode?: (enabled: boolean) => void }

export type ProductComposerResult =
  | { kind: "submit"; text: string; queue: boolean }
  | { kind: "interrupt" }
  | { kind: "permission" }
  | { kind: "shortcuts" }
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
  readonly #terminalSession: TerminalSessionGuard
  readonly #pasteBurst: PasteBurstDetector | undefined
  readonly #candidates: () => readonly string[]
  readonly #decoder = new StringDecoder("utf8")
  readonly #onChange: (snapshot: DraftSnapshot) => void
  readonly #onNotice: (notice?: string) => void
  readonly #onScroll: (direction: "up" | "down") => void
  readonly #onCompletion: (completion?: CompletionState) => void
  readonly #onPersistence: (snapshot: DraftPersistenceSnapshot) => void
  readonly #running: () => boolean
  readonly #editDraft: (initial: string) => Promise<string>
  #pending = ""
  #paste = ""
  #pasting = false
  #discardingPaste = false
  #busy = false
  #dispose: (() => void) | undefined
  #settle: ((value: ProductComposerResult) => void) | undefined
  #completion: CompletionState | undefined
  #pasteBurstTimer: ReturnType<typeof setTimeout> | undefined

  constructor(input: {
    stdin: Readable
    output: Writable
    candidates?: readonly string[]
    candidateProvider?: () => readonly string[]
    running: () => boolean
    initialDraft?: DraftPersistenceSnapshot
    onChange: (snapshot: DraftSnapshot) => void
    onPersistence?: (snapshot: DraftPersistenceSnapshot) => void
    onNotice: (notice?: string) => void
    onScroll: (direction: "up" | "down") => void
    onCompletion?: (completion?: CompletionState) => void
    bracketedPaste?: boolean
    editDraft?: (initial: string) => Promise<string>
  }) {
    this.#input = input.stdin as RawInput
    this.#output = input.output
    const bracketedPaste = input.bracketedPaste
      ?? (input.stdin !== process.stdin || process.platform !== "win32")
    this.#terminalSession = new TerminalSessionGuard(input.stdin, input.output, { bracketedPaste })
    this.#pasteBurst = bracketedPaste ? undefined : new PasteBurstDetector(MAX_PROMPT_BYTES)
    const candidates = [...new Set(input.candidates ?? [])].sort()
    this.#candidates = input.candidateProvider ?? (() => candidates)
    this.#running = input.running
    this.#onChange = input.onChange
    this.#onNotice = input.onNotice
    this.#onScroll = input.onScroll
    this.#onCompletion = input.onCompletion ?? (() => undefined)
    this.#onPersistence = input.onPersistence ?? (() => undefined)
    this.#editDraft = input.editDraft ?? editDraftExternally
    if (input.initialDraft) this.draft.set(input.initialDraft.text, input.initialDraft.cursor, false)
  }

  get snapshot(): DraftSnapshot { return this.draft.snapshot() }

  async read(): Promise<ProductComposerResult> {
    if (this.#settle) throw new TypeError("ProductComposer already has an active read.")
    this.#terminalSession.enter()
    return new Promise<ProductComposerResult>((resolve, reject) => {
      const data = (chunk: Buffer | string) => {
        void this.#consume(typeof chunk === "string" ? chunk : this.#decoder.write(chunk)).catch((error) => {
          if (error instanceof PromptInputLimitError) {
            this.#pending = ""
            this.#paste = ""
            this.#pasting = false
            this.#discardingPaste = false
            this.#onNotice(error.message)
            this.#changed()
            return
          }
          this.#dispose?.()
          this.#dispose = undefined
          this.#settle = undefined
          reject(error)
        })
      }
      const end = () => this.#finish({ kind: "exit" })
      this.#settle = resolve
      this.#input.on("data", data)
      this.#input.once("end", end)
      this.#dispose = () => {
        this.#input.off("data", data)
        this.#input.off("end", end)
        this.#terminalSession.restore()
        this.#clearPasteBurstTimer()
      }
      // The first rendered draft is the user-visible readiness boundary.
      // Publish it only after the input listeners and settle callback exist.
      this.#changed()
    })
  }

  close(): void { this.#finish({ kind: "closed" }) }

  yieldForPermission(): void { this.#finish({ kind: "permission" }) }

  async #consume(value: string): Promise<void> {
    this.#pending += value
    if (this.#busy) return
    while (this.#pending) {
      if (this.#pasting) {
        const end = this.#pending.indexOf(PASTE_END)
        if (end < 0) {
          if (!this.#discardingPaste) {
            if (Buffer.byteLength(this.#paste, "utf8") + Buffer.byteLength(this.#pending, "utf8") > MAX_PROMPT_BYTES) {
              this.#paste = ""
              this.#discardingPaste = true
              this.#onNotice(`粘贴超过 ${MAX_PROMPT_BYTES} bytes，已丢弃；请改用文件或 artifact 引用。`)
            } else {
              this.#paste += this.#pending
            }
          }
          this.#pending = ""
          return
        }
        if (!this.#discardingPaste) {
          const tail = this.#pending.slice(0, end)
          if (Buffer.byteLength(this.#paste, "utf8") + Buffer.byteLength(tail, "utf8") <= MAX_PROMPT_BYTES) {
            this.#paste += tail
            this.draft.paste(this.#paste)
          } else {
            this.#onNotice(`粘贴超过 ${MAX_PROMPT_BYTES} bytes，已丢弃；请改用文件或 artifact 引用。`)
          }
        }
        this.#paste = ""
        this.#pasting = false
        this.#discardingPaste = false
        this.#pending = this.#pending.slice(end + PASTE_END.length)
        this.#changed()
        continue
      }
      if (this.#pending.startsWith(PASTE_START)) {
        this.#flushPasteBurstBeforeControl()
        this.#pending = this.#pending.slice(PASTE_START.length)
        this.#pasting = true
        continue
      }
      const page = this.#pending.match(/^\u001b\[[56]~/)?.[0]
      if (page) {
        this.#flushPasteBurstBeforeControl()
        this.#pending = this.#pending.slice(page.length)
        this.#onScroll(page === "\u001b[5~" ? "up" : "down")
        continue
      }
      const key = this.#pending.match(/^\u001b\[(?:1;5[CD]|[ABCDHF]|[134]~)/)?.[0]
      if (key) {
        this.#flushPasteBurstBeforeControl()
        this.#pending = this.#pending.slice(key.length)
        const snapshot = this.draft.snapshot()
        const position = linePosition(snapshot.text, snapshot.cursor)
        if ((key === "\u001b[A" || key === "\u001b[B") && this.#completion) {
          const direction = key === "\u001b[A" ? -1 : 1
          this.#completion = completionState(snapshot, this.#candidates(), this.#completion.selected + direction)
          this.#onCompletion(this.#completion)
        } else if (key === "\u001b[A") {
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
      if (/^\u001b\[[0-9;?]*$/u.test(this.#pending) && this.#pending.length < 32) return
      const unknownControl = this.#pending.match(/^\u001b\[[0-?]*[ -/]*[@-~]/u)?.[0]
      if (unknownControl) {
        this.#flushPasteBurstBeforeControl()
        this.#pending = this.#pending.slice(unknownControl.length)
        continue
      }
      if (this.#pending.startsWith("\u001b") && this.#pending.length === 2) return
      if (this.#pending.startsWith("\u001b")) {
        this.#flushPasteBurstBeforeControl()
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
        if (this.#pasteBurst) {
          this.#applyPasteBurstFlush(this.#pasteBurst.flushIfDue(performance.now()))
          const newline = this.#pasteBurst.onNewline(performance.now())
          if (newline === "buffer") { this.#schedulePasteBurstFlush(); continue }
          if (newline === "insert") { this.draft.newline(); this.#changed(); continue }
        }
        if (this.#completion && this.#completion.matches[this.#completion.selected] !== this.#completion.token) {
          const accepted = acceptCompletion(this.draft.snapshot(), this.#completion)
          this.draft.set(accepted.text, accepted.cursor)
          this.#changed()
          continue
        }
        const submitted = this.draft.submit()
        if (!submitted) { this.#changed(); continue }
        this.history.push(submitted)
        this.#onNotice(undefined)
        this.#changed()
        this.#finish({ kind: "submit", text: submitted, queue: false })
        return
      }
      if (char === "\n") {
        if (this.#pasteBurst) {
          this.#applyPasteBurstFlush(this.#pasteBurst.flushIfDue(performance.now()))
          if (this.#pasteBurst.onNewline(performance.now()) === "buffer") {
            this.#schedulePasteBurstFlush()
            continue
          }
        }
        this.draft.newline(); this.#changed(); continue
      }
      if (char < " " || char === "\u007f") this.#flushPasteBurstBeforeControl()
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
        if (this.#completion) {
          const accepted = acceptCompletion(this.draft.snapshot(), this.#completion)
          this.draft.set(accepted.text, accepted.cursor)
        } else if (this.#running() && !this.draft.empty) {
          const submitted = this.draft.submit()
          if (submitted) {
            this.history.push(submitted)
            this.#finish({ kind: "submit", text: submitted, queue: true })
            return
          }
        }
        this.#changed()
        continue
      }
      if (char === "\u0005") {
        this.#busy = true
        this.#terminalSession.restore()
        const initial = this.draft.snapshot().text
        try {
          this.draft.set(await this.#editDraft(initial))
          this.#onNotice(undefined)
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error)
          this.#onNotice(`外部编辑器未完成：${detail} 草稿已保留。`)
        } finally {
          this.#terminalSession.enter()
          this.#busy = false
          this.#changed()
        }
        continue
      }
      if (char === "?" && this.draft.empty) {
        this.#finish({ kind: "shortcuts" })
        return
      }
      if (char >= " ") {
        if (!this.#pasteBurst) {
          this.draft.insert(char)
        } else {
          const now = performance.now()
          this.#applyPasteBurstFlush(this.#pasteBurst.flushIfDue(now))
          const decision = this.#pasteBurst.onCharacter(char, now)
          if (decision.kind === "insert") this.draft.insert(char)
          if (decision.kind === "retro") {
            const snapshot = this.draft.snapshot()
            const before = snapshot.text.slice(0, snapshot.cursor)
            const grabbedCandidate = [...before].slice(-decision.characters).join("")
            const start = before.length - grabbedCandidate.length
            const grabbed = before.slice(start)
            const looksPasted = /\s/u.test(grabbed) || [...grabbed].length >= 16
            if (looksPasted) {
              this.draft.set(`${snapshot.text.slice(0, start)}${snapshot.text.slice(snapshot.cursor)}`, start, false)
              this.#pasteBurst.acceptRetroactive(grabbed, char, now)
            } else {
              this.draft.insert(char)
            }
          }
          this.#schedulePasteBurstFlush()
        }
        this.#onNotice(undefined)
        this.#changed()
      }
    }
  }

  #schedulePasteBurstFlush(): void {
    if (!this.#pasteBurst) return
    this.#clearPasteBurstTimer()
    const delay = this.#pasteBurst.nextFlushDelay(performance.now())
    if (delay === undefined) return
    this.#pasteBurstTimer = setTimeout(() => {
      this.#pasteBurstTimer = undefined
      this.#applyPasteBurstFlush(this.#pasteBurst!.flushIfDue(performance.now()))
      this.#schedulePasteBurstFlush()
    }, delay)
  }

  #clearPasteBurstTimer(): void {
    if (this.#pasteBurstTimer !== undefined) clearTimeout(this.#pasteBurstTimer)
    this.#pasteBurstTimer = undefined
  }

  #flushPasteBurstBeforeControl(): void {
    if (!this.#pasteBurst) return
    this.#clearPasteBurstTimer()
    this.#applyPasteBurstFlush(this.#pasteBurst.flushBeforeControl())
  }

  #applyPasteBurstFlush(result: PasteBurstFlush): void {
    if (result.kind === "none") return
    try {
      if (result.kind === "typed") this.draft.insert(result.text)
      if (result.kind === "paste") this.draft.paste(result.text)
      if (result.kind === "overflow") {
        this.#onNotice(`粘贴超过 ${MAX_PROMPT_BYTES} bytes，已丢弃；请改用文件或 artifact 引用。`)
      }
      this.#changed()
    } catch (error) {
      if (!(error instanceof PromptInputLimitError)) throw error
      this.#onNotice(error.message)
      this.#changed()
    }
  }

  #changed(): void {
    const snapshot = this.draft.snapshot()
    const prior = this.#completion?.matches[this.#completion.selected]
    const next = completionState(snapshot, this.#candidates())
    const selected = prior && next ? next.matches.indexOf(prior) : -1
    this.#completion = selected >= 0 ? completionState(snapshot, this.#candidates(), selected) : next
    this.#onChange(snapshot)
    this.#onPersistence(this.draft.persistenceSnapshot())
    this.#onCompletion(this.#completion)
  }

  #finish(value: ProductComposerResult): void {
    const settle = this.#settle
    if (!settle) return
    this.#flushPasteBurstBeforeControl()
    this.#settle = undefined
    this.#dispose?.()
    this.#dispose = undefined
    this.#completion = undefined
    this.#onCompletion(undefined)
    settle(value)
  }
}
