export type LayoutMode = "wide" | "compact" | "mobile"
export type ActivePane = "tasks" | "detail"

export interface LayoutSnapshot {
  mode: LayoutMode
  viewportWidth: number
  viewportHeight: number
  navigationWidth: number
  taskListWidth: number
  activePane: ActivePane
  detailVisible: boolean
  commandDockHeight: number
  revision: number
}

export interface LayoutWindow {
  innerWidth: number
  innerHeight: number
  addEventListener(type: "resize", listener: () => void, options?: AddEventListenerOptions): void
  removeEventListener(type: "resize", listener: () => void): void
  requestAnimationFrame(callback: FrameRequestCallback): number
  cancelAnimationFrame(handle: number): void
}

function modeForWidth(width: number): LayoutMode {
  if (width <= 760) return "mobile"
  if (width < 1180) return "compact"
  return "wide"
}

function navigationWidth(mode: LayoutMode): number {
  if (mode === "wide") return 190
  if (mode === "compact") return 76
  return 0
}

function defaultTaskListWidth(mode: LayoutMode, viewportWidth: number): number {
  const available = Math.max(320, viewportWidth - navigationWidth(mode))
  if (mode === "mobile") return available
  return Math.max(280, Math.min(520, Math.round(available * 0.36)))
}

export class LayoutRuntime {
  readonly #window: LayoutWindow
  readonly #listeners = new Set<() => void>()
  readonly #onResize: () => void
  #snapshot: LayoutSnapshot
  #animationFrame?: number
  #started = false
  #closed = false
  #manualTaskListWidth?: number

  constructor(windowLike: LayoutWindow) {
    this.#window = windowLike
    const mode = modeForWidth(windowLike.innerWidth)
    this.#snapshot = Object.freeze({
      mode,
      viewportWidth: windowLike.innerWidth,
      viewportHeight: windowLike.innerHeight,
      navigationWidth: navigationWidth(mode),
      taskListWidth: defaultTaskListWidth(mode, windowLike.innerWidth),
      activePane: "tasks",
      detailVisible: mode !== "mobile",
      commandDockHeight: 110,
      revision: 0,
    })
    this.#onResize = () => {
      if (this.#animationFrame !== undefined) return
      this.#animationFrame = this.#window.requestAnimationFrame(() => {
        this.#animationFrame = undefined
        this.#measure()
      })
    }
  }

  getSnapshot = (): LayoutSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  start(): LayoutSnapshot {
    this.#assertOpen()
    if (this.#started) return this.#snapshot
    this.#started = true
    this.#window.addEventListener("resize", this.#onResize, { passive: true })
    this.#measure()
    return this.#snapshot
  }

  showTaskList(): void {
    this.#assertOpen()
    this.#replace({
      activePane: "tasks",
      detailVisible: this.#snapshot.mode !== "mobile",
    })
  }

  showDetail(): void {
    this.#assertOpen()
    this.#replace({
      activePane: "detail",
      detailVisible: true,
    })
  }

  routeChanged(kind: string): void {
    if (kind === "task") this.showDetail()
    else if (kind === "tasks") this.showTaskList()
  }

  setTaskListWidth(width: number): number {
    this.#assertOpen()
    if (this.#snapshot.mode === "mobile") return this.#snapshot.taskListWidth
    const available = this.#snapshot.viewportWidth - this.#snapshot.navigationWidth
    const normalized = Math.max(280, Math.min(Math.max(280, available - 360), Math.round(width)))
    this.#manualTaskListWidth = normalized
    this.#replace({ taskListWidth: normalized })
    return normalized
  }

  resizeTaskList(delta: number): number {
    return this.setTaskListWidth(this.#snapshot.taskListWidth + delta)
  }

  resetTaskListWidth(): void {
    this.#assertOpen()
    this.#manualTaskListWidth = undefined
    this.#replace({
      taskListWidth: defaultTaskListWidth(
        this.#snapshot.mode,
        this.#snapshot.viewportWidth,
      ),
    })
  }

  setCommandDockHeight(height: number): number {
    this.#assertOpen()
    const maximum = Math.max(100, Math.floor(this.#snapshot.viewportHeight * 0.55))
    const normalized = Math.max(72, Math.min(maximum, Math.round(height)))
    this.#replace({ commandDockHeight: normalized })
    return normalized
  }

  visibleMainHeight(topBarHeight = 56): number {
    return Math.max(
      160,
      this.#snapshot.viewportHeight - topBarHeight - this.#snapshot.commandDockHeight,
    )
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    if (this.#started) this.#window.removeEventListener("resize", this.#onResize)
    if (this.#animationFrame !== undefined) {
      this.#window.cancelAnimationFrame(this.#animationFrame)
      this.#animationFrame = undefined
    }
    this.#listeners.clear()
  }

  #measure(): void {
    if (this.#closed) return
    const width = Math.max(320, Math.floor(this.#window.innerWidth))
    const height = Math.max(320, Math.floor(this.#window.innerHeight))
    const mode = modeForWidth(width)
    const changedMode = mode !== this.#snapshot.mode
    const listWidth =
      mode === "mobile"
        ? width
        : this.#manualTaskListWidth ?? defaultTaskListWidth(mode, width)
    this.#replace({
      mode,
      viewportWidth: width,
      viewportHeight: height,
      navigationWidth: navigationWidth(mode),
      taskListWidth: listWidth,
      detailVisible:
        mode === "mobile"
          ? this.#snapshot.activePane === "detail"
          : true,
      ...(changedMode && mode === "mobile" && this.#snapshot.activePane === "tasks"
        ? { detailVisible: false }
        : {}),
    })
  }

  #replace(patch: Partial<LayoutSnapshot>): void {
    const changed = Object.entries(patch).some(
      ([key, value]) => this.#snapshot[key as keyof LayoutSnapshot] !== value,
    )
    if (!changed) return
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Layout observers cannot change responsive state.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Layout runtime is closed.")
  }
}

export function createBrowserLayoutRuntime(): LayoutRuntime {
  if (typeof window === "undefined") throw new TypeError("Layout runtime requires window.")
  return new LayoutRuntime(window)
}
