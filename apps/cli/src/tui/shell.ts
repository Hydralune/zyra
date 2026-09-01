import type { Readable, Writable } from "node:stream"
import type { DraftSnapshot } from "../input/draft.ts"
import { ProductSessionState, type ProductViewState } from "../product/state/session-state.ts"
import type { ProductDraftStore } from "../product/session/local-state.ts"
import type { ZyraUiEvent } from "../presentation/events.ts"
import { renderProductState } from "../presentation/renderer.ts"
import { ProductComposer, type ProductComposerResult } from "./composer.ts"
import { LiveProductRenderer, type LiveRendererDiagnostics } from "./live-renderer.ts"
import { PRODUCT_COMMAND_REGISTRY } from "../product/commands/registry.ts"
import type { CompletionState } from "./overlay/completion.ts"
import { pickProductItem } from "./overlay/list-picker.ts"
import type { ProductOverlay, ProductPickerItem } from "./overlay/model.ts"
import { pageProductText } from "./overlay/pager.ts"
import { probeTerminalCapabilities, type TerminalCapabilities } from "./terminal-capabilities.ts"

export class ProductTuiShell {
  readonly #workspace: string
  readonly #interactive: boolean
  readonly #input: Readable
  readonly #output: Writable
  #candidates: readonly string[]
  #availableCandidateCache: { running: boolean; source: readonly string[]; values: readonly string[] } | undefined
  readonly #renderer: LiveProductRenderer
  readonly #composer: ProductComposer
  readonly #draftStore?: ProductDraftStore
  readonly #bracketedPaste: boolean
  readonly #terminalCapabilities: TerminalCapabilities
  readonly #state = new ProductSessionState()
  #archivedEvents: readonly ZyraUiEvent[] = Object.freeze([])
  #taskEvents: readonly ZyraUiEvent[] = Object.freeze([])
  #events: readonly ZyraUiEvent[] = Object.freeze([])
  #draft: DraftSnapshot = Object.freeze({ text: "", cursor: 0, display: "", pasteRefs: Object.freeze([]) })
  #notice: string | undefined
  #running = false
  #scrollOffset = 0
  #closed = false
  #overlay: ProductOverlay | undefined

  constructor(input: {
    stdin: Readable
    output: Writable
    workspace: string
    candidates?: readonly string[]
    draftStore?: ProductDraftStore
    bracketedPaste?: boolean
  }) {
    this.#workspace = input.workspace
    this.#input = input.stdin
    this.#output = input.output
    this.#candidates = Object.freeze([...(input.candidates ?? [])])
    this.#draftStore = input.draftStore
    this.#terminalCapabilities = probeTerminalCapabilities({ stdin: input.stdin, output: input.output })
    this.#bracketedPaste = input.bracketedPaste
      ?? (input.stdin !== process.stdin ? true : this.#terminalCapabilities.bracketedPaste)
    if (input.draftStore?.restored.text) {
      const restored = input.draftStore.restored
      this.#draft = Object.freeze({ text: restored.text, cursor: restored.cursor, display: restored.text, pasteRefs: Object.freeze([]) })
    }
    this.#notice = input.draftStore?.warning
    this.#interactive = (input.stdin as Readable & { isTTY?: boolean }).isTTY === true
    this.#renderer = new LiveProductRenderer(input.output, () => renderProductState(this.#state.snapshot(), {
      width: this.#renderer.width,
      height: this.#renderer.height,
      workspace: this.#workspace,
      composerText: this.#draft.display,
      notice: this.#notice,
      running: this.#running,
      scrollOffset: this.#scrollOffset,
      overlay: this.#overlay,
    }))
    this.#composer = new ProductComposer({
      stdin: input.stdin,
      output: input.output,
      candidates: input.candidates,
      candidateProvider: () => this.#availableCandidates(),
      running: () => this.#running,
      initialDraft: input.draftStore?.restored,
      bracketedPaste: this.#bracketedPaste,
      onChange: (snapshot) => {
        this.#draft = snapshot
        this.#scrollOffset = 0
        this.#renderer.renderNow()
      },
      onNotice: (notice) => {
        this.#notice = notice
        this.#renderer.renderNow()
      },
      onPersistence: (snapshot) => input.draftStore?.schedule(snapshot),
      onScroll: (direction) => {
        this.#scrollOffset = Math.max(0, this.#scrollOffset + (direction === "up" ? 5 : -5))
        this.#renderer.renderNow()
      },
      onCompletion: (completion) => {
        this.#overlay = this.#completionOverlay(completion)
        this.#renderer.renderNow()
      },
    })
  }

  get alternateScreenUsed(): false { return this.#renderer.alternateScreenUsed }
  get interactive(): boolean { return this.#interactive }
  get workspace(): string { return this.#workspace }
  get view(): ProductViewState { return this.#state.snapshot() }
  get renderDiagnostics(): LiveRendererDiagnostics { return this.#renderer.diagnostics }
  get terminalCapabilities(): TerminalCapabilities { return this.#terminalCapabilities }

  addCandidates(candidates: readonly string[]): void {
    this.#candidates = Object.freeze([...new Set([...this.#candidates, ...candidates])].sort())
    this.#availableCandidateCache = undefined
  }

  start(): void { this.#renderer.start() }

  update(events: readonly ZyraUiEvent[]): void {
    this.#taskEvents = events
    this.#events = Object.freeze([...this.#archivedEvents, ...events])
    this.#state.reconcile(this.#events)
    this.#renderer.render()
  }

  append(events: readonly ZyraUiEvent[]): void {
    this.#taskEvents = Object.freeze([...this.#taskEvents, ...events])
    this.#events = Object.freeze([...this.#archivedEvents, ...this.#taskEvents])
    this.#state.reconcile(this.#events)
    this.#renderer.render()
  }

  beginTask(): void {
    this.#archivedEvents = Object.freeze([...this.#archivedEvents, ...this.#taskEvents].slice(-20_000))
    this.#taskEvents = Object.freeze([])
    this.#events = this.#archivedEvents
    this.#state.reconcile(this.#events)
    this.#scrollOffset = 0
    this.#renderer.renderNow()
  }

  clearTranscript(): void {
    this.#archivedEvents = Object.freeze([])
    this.#taskEvents = Object.freeze([])
    this.#events = Object.freeze([])
    this.#state.reconcile(this.#events)
    this.#scrollOffset = 0
    this.#renderer.renderNow()
  }

  notice(message?: string): void {
    this.#notice = message
    this.#renderer.renderNow()
  }

  async read(running = false): Promise<ProductComposerResult> {
    this.#running = running
    this.#renderer.renderNow()
    const result = await this.#composer.read()
    this.#running = false
    this.#renderer.renderNow()
    return result
  }

  async pick(title: string, items: readonly ProductPickerItem[], footer?: string): Promise<ProductPickerItem | undefined> {
    if (!this.#interactive) return undefined
    return pickProductItem({
      stdin: this.#input,
      output: this.#output,
      title,
      items,
      footer,
      bracketedPaste: this.#bracketedPaste,
      onChange: (overlay) => {
        this.#overlay = overlay
        this.#renderer.renderNow()
      },
    })
  }

  async page(title: string, lines: readonly string[]): Promise<void> {
    if (!this.#interactive) return
    await pageProductText({
      stdin: this.#input,
      output: this.#output,
      title,
      lines,
      onChange: (overlay) => {
        this.#overlay = overlay
        this.#renderer.renderNow()
      },
    })
  }

  detachInput(): void {
    this.#running = false
    this.#composer.close()
    this.#renderer.renderNow()
  }

  finish(events?: readonly ZyraUiEvent[]): void {
    if (events) {
      this.#taskEvents = events
      this.#events = Object.freeze([...this.#archivedEvents, ...events])
      this.#state.reconcile(this.#events)
    }
    this.#running = false
    this.#composer.close()
    this.#renderer.finish()
    this.#closed = true
  }

  close(): void {
    if (this.#closed) return
    this.#composer.close()
    this.#renderer.close()
    this.#closed = true
  }

  async flushLocalState(): Promise<void> {
    await this.#draftStore?.flush()
  }

  #completionOverlay(completion?: CompletionState): ProductOverlay | undefined {
    if (!completion) return undefined
    const rows = completion.matches.slice(0, 8).map((value) => {
      const command = value.startsWith("/")
        ? PRODUCT_COMMAND_REGISTRY.find((item) => `/${item.name}` === value)
        : undefined
      return { id: value, label: value, detail: command?.description }
    })
    return {
      title: completion.token.startsWith("/") ? "命令" : "工作区引用",
      rows,
      selected: Math.min(completion.selected, rows.length - 1),
      footer: "↑↓ 选择 · Tab/Enter 接受",
    }
  }

  #availableCandidates(): readonly string[] {
    const cached = this.#availableCandidateCache
    if (cached?.source === this.#candidates && cached.running === this.#running) return cached.values
    const values = Object.freeze(this.#candidates.filter((value) => {
      if (!value.startsWith("/")) return true
      const command = PRODUCT_COMMAND_REGISTRY.find((item) => `/${item.name}` === value)
      return !command || command.availability === "always" || command.availability === (this.#running ? "running" : "idle")
    }))
    this.#availableCandidateCache = { running: this.#running, source: this.#candidates, values }
    return values
  }
}
