export interface FocusTrapContainer {
  querySelectorAll<E extends Element = Element>(selectors: string): NodeListOf<E>
  contains(node: Node | null): boolean
  focus?(options?: FocusOptions): void
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "summary",
  "[tabindex]:not([tabindex='-1'])",
  "[contenteditable='true']",
].join(",")

function visible(element: HTMLElement): boolean {
  if (element.hidden) return false
  if (element.getAttribute("aria-hidden") === "true") return false
  const style = typeof getComputedStyle === "function" ? getComputedStyle(element) : undefined
  if (style?.display === "none" || style?.visibility === "hidden") return false
  return element.getClientRects().length > 0 || style === undefined
}

export function focusableElements(container: FocusTrapContainer): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].filter(visible)
}

export class FocusTrap {
  readonly #container: FocusTrapContainer
  #active = false
  #lastFocused?: HTMLElement

  constructor(container: FocusTrapContainer) {
    this.#container = container
  }

  get active(): boolean {
    return this.#active
  }

  activate(options: { initialSelector?: string; fallbackToContainer?: boolean } = {}): boolean {
    this.#active = true
    const elements = focusableElements(this.#container)
    const initial = options.initialSelector
      ? elements.find((element) => element.matches(options.initialSelector!))
      : undefined
    const target = initial ?? elements[0]
    if (target) {
      target.focus({ preventScroll: true })
      this.#lastFocused = target
      return true
    }
    if (options.fallbackToContainer !== false && this.#container.focus) {
      this.#container.focus({ preventScroll: true })
      return true
    }
    return false
  }

  deactivate(): void {
    this.#active = false
    this.#lastFocused = undefined
  }

  handleTab(event: Pick<KeyboardEvent, "key" | "shiftKey" | "preventDefault">): boolean {
    if (!this.#active || event.key !== "Tab") return false
    const elements = focusableElements(this.#container)
    if (!elements.length) {
      event.preventDefault()
      this.#container.focus?.({ preventScroll: true })
      return true
    }
    const active = document.activeElement
    let index = elements.findIndex((element) => element === active)
    if (index < 0 && this.#lastFocused) {
      index = elements.findIndex((element) => element === this.#lastFocused)
    }
    const next = event.shiftKey
      ? index <= 0 ? elements.length - 1 : index - 1
      : index < 0 || index >= elements.length - 1 ? 0 : index + 1
    event.preventDefault()
    const target = elements[next]!
    target.focus({ preventScroll: true })
    this.#lastFocused = target
    return true
  }

  containFocus(): boolean {
    if (!this.#active) return false
    if (this.#container.contains(document.activeElement)) {
      if (document.activeElement instanceof HTMLElement) this.#lastFocused = document.activeElement
      return true
    }
    if (this.#lastFocused && visible(this.#lastFocused)) {
      this.#lastFocused.focus({ preventScroll: true })
      return true
    }
    return this.activate()
  }
}
