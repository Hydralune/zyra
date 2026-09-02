import type { Readable, Writable } from "node:stream"
import type { DraftSnapshot } from "../input/draft.ts"
import { ProductSessionState, type ProductViewState } from "../product/state/session-state.ts"
import type { ProductDraftStore } from "../product/session/local-state.ts"
import type { ZyraUiEvent } from "../presentation/events.ts"
import { renderProductFrame, type ProductChromeState, type ProductLocalHistoryItem } from "../presentation/renderer.ts"
import { ProductComposer, type ProductComposerResult } from "./composer.ts"
import { LiveProductRenderer, type LiveRendererDiagnostics } from "./live-renderer.ts"
import { PRODUCT_COMMAND_REGISTRY } from "../product/commands/registry.ts"
import type { CompletionState } from "./overlay/completion.ts"
import { pickProductItem, promptProductText } from "./overlay/list-picker.ts"
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
  #localHistory: ProductLocalHistoryItem[] = []
  #localHistorySequence = 0
  #running = false
  #showCurrentActivity = true
  #acceptingInput = false
  #scrollOffset = 0
  #closed = false
  #overlay: ProductOverlay | undefined
  #chrome: ProductChromeState = Object.freeze({ model: "自动选择", mode: "标准" })
  #permissionDispatchKey: string | undefined
  #userInputDispatchKey: string | undefined
  #overlayAbort: AbortController | undefined

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
    this.#renderer = new LiveProductRenderer(input.output, () => renderProductFrame(this.#state.snapshot(), {
      width: this.#renderer.width,
      height: this.#renderer.height,
      workspace: this.#workspace,
      composerText: this.#draft.display,
      composerCursor: Math.min(this.#draft.cursor, this.#draft.display.length),
      notice: this.#notice,
      localHistory: this.#localHistory,
      running: this.#running,
      showCurrentActivity: this.#showCurrentActivity,
      acceptingInput: this.#acceptingInput,
      scrollOffset: this.#scrollOffset,
      overlay: this.#overlay,
      chrome: this.#chrome,
      color: this.#terminalCapabilities.colorLevel > 0,
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

  setChrome(chrome: ProductChromeState): void {
    this.#chrome = Object.freeze({ ...this.#chrome, ...chrome })
    this.#renderer.renderNow()
  }

  start(): void { this.#renderer.start() }

  update(events: readonly ZyraUiEvent[]): void {
    this.#showCurrentActivity = true
    this.#taskEvents = events
    this.#events = Object.freeze([...this.#archivedEvents, ...events])
    this.#state.reconcile(this.#events)
    this.#dispatchBlockingPrompt()
    this.#renderer.render()
  }

  append(events: readonly ZyraUiEvent[]): void {
    this.#showCurrentActivity = true
    this.#taskEvents = Object.freeze([...this.#taskEvents, ...events])
    this.#events = Object.freeze([...this.#archivedEvents, ...this.#taskEvents])
    this.#state.reconcile(this.#events)
    this.#dispatchBlockingPrompt()
    this.#renderer.render()
  }

  beginTask(): void {
    this.#showCurrentActivity = true
    this.#archivedEvents = Object.freeze([...this.#archivedEvents, ...this.#taskEvents].slice(-20_000))
    this.#taskEvents = Object.freeze([])
    this.#events = this.#archivedEvents
    this.#state.reconcile(this.#events)
    this.#permissionDispatchKey = undefined
    this.#userInputDispatchKey = undefined
    this.#scrollOffset = 0
    this.#renderer.renderNow()
  }

  restoreDraft(text: string): void {
    this.#composer.restoreDraft(text)
  }

  clearTranscript(): void {
    this.#archivedEvents = Object.freeze([])
    this.#taskEvents = Object.freeze([])
    this.#events = Object.freeze([])
    this.#state.reconcile(this.#events)
    this.#localHistory = []
    this.#permissionDispatchKey = undefined
    this.#userInputDispatchKey = undefined
    this.#scrollOffset = 0
    this.#renderer.renderNow()
  }

  notice(message?: string): void {
    this.#notice = undefined
    if (message) {
      const text = message.length > 16_384 ? `${message.slice(0, 16_384)}\n…[本地结果已截断]` : message
      if (this.#localHistory.at(-1)?.text !== text) {
        const afterOrder = this.#state.snapshot().timeline?.at(-1)?.order ?? 0
        this.#localHistory.push(Object.freeze({
          id: `local:${++this.#localHistorySequence}`,
          text,
          afterOrder,
          sequence: this.#localHistorySequence,
        }))
        this.#localHistory = this.#localHistory.slice(-64)
      }
    }
    this.#renderer.renderNow()
  }

  status(message?: string): void {
    this.#notice = message
    this.#renderer.renderNow()
  }

  async read(running = false): Promise<ProductComposerResult> {
    this.#running = running
    this.#acceptingInput = true
    try {
      const permissionKey = this.#permissionKey()
      if (running && permissionKey && permissionKey !== this.#permissionDispatchKey) {
        this.#permissionDispatchKey = permissionKey
        this.#renderer.renderNow()
        return { kind: "permission" }
      }
      const userInputKey = this.#userInputKey()
      if (running && userInputKey && userInputKey !== this.#userInputDispatchKey) {
        this.#userInputDispatchKey = userInputKey
        this.#renderer.renderNow()
        return { kind: "question" }
      }
      return await this.#composer.read()
    } finally {
      this.#acceptingInput = false
      this.#running = false
      this.#renderer.renderNow()
    }
  }

  async pick(
    title: string,
    items: readonly ProductPickerItem[],
    footer?: string,
    kind: "picker" | "menu" | "approval" | "question" = "picker",
    description?: readonly string[],
  ): Promise<ProductPickerItem | undefined> {
    if (!this.#interactive) return undefined
    const controller = this.#beginOverlay()
    try {
      return await pickProductItem({
        stdin: this.#input,
        output: this.#output,
        title,
        items,
        footer,
        kind,
        description,
        bracketedPaste: this.#bracketedPaste,
        signal: controller.signal,
        onChange: (overlay) => {
          this.#overlay = overlay
          this.#renderer.renderNow()
        },
      })
    } finally {
      this.#endOverlay(controller)
    }
  }

  async prompt(title: string, description?: readonly string[]): Promise<string | undefined> {
    if (!this.#interactive) return undefined
    const controller = this.#beginOverlay()
    try {
      return await promptProductText({
        stdin: this.#input,
        output: this.#output,
        title,
        description,
        bracketedPaste: this.#bracketedPaste,
        signal: controller.signal,
        onChange: (overlay) => {
          this.#overlay = overlay
          this.#renderer.renderNow()
        },
      })
    } finally {
      this.#endOverlay(controller)
    }
  }

  rearmUserInput(): void {
    this.#userInputDispatchKey = undefined
  }

  async page(title: string, lines: readonly string[]): Promise<void> {
    if (!this.#interactive) return
    const controller = this.#beginOverlay()
    try {
      await pageProductText({
        stdin: this.#input,
        output: this.#output,
        title,
        lines,
        pageSize: Math.max(8, this.#renderer.height - 8),
        signal: controller.signal,
        onChange: (overlay) => {
          this.#overlay = overlay
          this.#renderer.renderNow()
        },
      })
    } finally {
      this.#endOverlay(controller)
    }
  }

  detachInput(): void {
    this.#overlayAbort?.abort()
    this.#overlayAbort = undefined
    this.#overlay = undefined
    this.#acceptingInput = false
    this.#running = false
    this.#showCurrentActivity = false
    this.#composer.close()
    this.#renderer.renderNow()
  }

  finish(events?: readonly ZyraUiEvent[]): void {
    if (events) {
      this.#taskEvents = events
      this.#events = Object.freeze([...this.#archivedEvents, ...events])
      this.#state.reconcile(this.#events)
    }
    this.#overlayAbort?.abort()
    this.#overlayAbort = undefined
    this.#overlay = undefined
    this.#acceptingInput = false
    this.#running = false
    this.#showCurrentActivity = false
    this.#composer.close()
    this.#renderer.finish()
    this.#closed = true
  }

  close(): void {
    if (this.#closed) return
    this.#overlayAbort?.abort()
    this.#overlayAbort = undefined
    this.#overlay = undefined
    this.#acceptingInput = false
    this.#composer.close()
    this.#renderer.close()
    this.#closed = true
  }

  async flushLocalState(): Promise<void> {
    await this.#draftStore?.flush()
  }

  #beginOverlay(): AbortController {
    this.#overlayAbort?.abort()
    const controller = new AbortController()
    this.#overlayAbort = controller
    return controller
  }

  #endOverlay(controller: AbortController): void {
    if (this.#overlayAbort === controller) this.#overlayAbort = undefined
    this.#overlay = undefined
    this.#renderer.renderNow()
  }

  #completionOverlay(completion?: CompletionState): ProductOverlay | undefined {
    if (!completion) return undefined
    const rows = completion.matches.slice(0, 8).map((value) => {
      const command = value.startsWith("/")
        ? PRODUCT_COMMAND_REGISTRY.find((item) => `/${item.name}` === value)
        : undefined
      return { id: value, label: value, detail: command ? `${command.description}${command.usage ? ` · ${command.usage}` : ""}` : undefined }
    })
    return {
      kind: "completion",
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

  #permissionKey(): string | undefined {
    const requests = this.#state.snapshot().permissions
    return requests.length ? requests.map((request) => request.requestId).sort().join("|") : undefined
  }

  #userInputKey(): string | undefined {
    const requests = this.#state.snapshot().userInputs
    return requests.length
      ? requests.map((request) => `${request.requestId}:${request.revision}`).sort().join("|")
      : undefined
  }

  #dispatchBlockingPrompt(): void {
    const permissionKey = this.#permissionKey()
    if (!permissionKey) {
      this.#permissionDispatchKey = undefined
    } else if (this.#running && this.#acceptingInput && permissionKey !== this.#permissionDispatchKey) {
      this.#permissionDispatchKey = permissionKey
      this.#composer.yieldForPermission()
      return
    }
    const userInputKey = this.#userInputKey()
    if (!userInputKey) {
      this.#userInputDispatchKey = undefined
      return
    }
    if (!this.#running || !this.#acceptingInput || userInputKey === this.#userInputDispatchKey) return
    this.#userInputDispatchKey = userInputKey
    this.#composer.yieldForUserInput()
  }
}
