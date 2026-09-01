import type { Readable, Writable } from "node:stream"
import type { DraftSnapshot } from "../input/draft.ts"
import { ProductSessionState, type ProductViewState } from "../product/state/session-state.ts"
import type { ZyraUiEvent } from "../presentation/events.ts"
import { renderProductState } from "../presentation/renderer.ts"
import { ProductComposer, type ProductComposerResult } from "./composer.ts"
import { LiveProductRenderer } from "./live-renderer.ts"

export class ProductTuiShell {
  readonly #workspace: string
  readonly #interactive: boolean
  readonly #renderer: LiveProductRenderer
  readonly #composer: ProductComposer
  readonly #state = new ProductSessionState()
  #archivedEvents: readonly ZyraUiEvent[] = Object.freeze([])
  #taskEvents: readonly ZyraUiEvent[] = Object.freeze([])
  #events: readonly ZyraUiEvent[] = Object.freeze([])
  #draft: DraftSnapshot = Object.freeze({ text: "", cursor: 0, display: "", pasteRefs: Object.freeze([]) })
  #notice: string | undefined
  #running = false
  #scrollOffset = 0
  #closed = false

  constructor(input: {
    stdin: Readable
    output: Writable
    workspace: string
    candidates?: readonly string[]
  }) {
    this.#workspace = input.workspace
    this.#interactive = (input.stdin as Readable & { isTTY?: boolean }).isTTY === true
    this.#renderer = new LiveProductRenderer(input.output, () => renderProductState(this.#state.snapshot(), {
      width: this.#renderer.width,
      height: this.#renderer.height,
      workspace: this.#workspace,
      composerText: this.#draft.display,
      notice: this.#notice,
      running: this.#running,
      scrollOffset: this.#scrollOffset,
    }))
    this.#composer = new ProductComposer({
      stdin: input.stdin,
      output: input.output,
      candidates: input.candidates,
      running: () => this.#running,
      onChange: (snapshot) => {
        this.#draft = snapshot
        this.#scrollOffset = 0
        this.#renderer.render()
      },
      onNotice: (notice) => {
        this.#notice = notice
        this.#renderer.render()
      },
      onScroll: (direction) => {
        this.#scrollOffset = Math.max(0, this.#scrollOffset + (direction === "up" ? 5 : -5))
        this.#renderer.render()
      },
    })
  }

  get alternateScreenUsed(): false { return this.#renderer.alternateScreenUsed }
  get interactive(): boolean { return this.#interactive }
  get view(): ProductViewState { return this.#state.snapshot() }

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
    this.#renderer.render()
  }

  clearTranscript(): void {
    this.#archivedEvents = Object.freeze([])
    this.#taskEvents = Object.freeze([])
    this.#events = Object.freeze([])
    this.#state.reconcile(this.#events)
    this.#scrollOffset = 0
    this.#renderer.render()
  }

  notice(message?: string): void {
    this.#notice = message
    this.#renderer.render()
  }

  async read(running = false): Promise<ProductComposerResult> {
    this.#running = running
    this.#renderer.render()
    const result = await this.#composer.read()
    this.#running = false
    this.#renderer.render()
    return result
  }

  detachInput(): void {
    this.#running = false
    this.#composer.close()
    this.#renderer.render()
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
}
