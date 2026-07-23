export interface CollectionWindow {
  start: number
  end: number
  overscanStart: number
  overscanEnd: number
  beforeHeight: number
  afterHeight: number
  totalHeight: number
}

export interface CollectionWindowInput {
  itemCount: number
  itemHeight: number
  viewportHeight: number
  scrollTop: number
  overscan?: number
}

function finite(value: number, fallback = 0): number {
  return Number.isFinite(value) ? value : fallback
}

export function collectionWindow(input: CollectionWindowInput): CollectionWindow {
  const count = Math.max(0, Math.floor(finite(input.itemCount)))
  const itemHeight = Math.max(1, finite(input.itemHeight, 1))
  const viewport = Math.max(0, finite(input.viewportHeight))
  const totalHeight = count * itemHeight
  const maximumScroll = Math.max(0, totalHeight - viewport)
  const scrollTop = Math.max(0, Math.min(maximumScroll, finite(input.scrollTop)))
  const start = Math.min(count, Math.floor(scrollTop / itemHeight))
  const visibleCount = Math.max(1, Math.ceil(viewport / itemHeight))
  const end = Math.min(count, start + visibleCount)
  const overscan = Math.max(0, Math.min(100, Math.floor(input.overscan ?? 5)))
  const overscanStart = Math.max(0, start - overscan)
  const overscanEnd = Math.min(count, end + overscan)
  return {
    start,
    end,
    overscanStart,
    overscanEnd,
    beforeHeight: overscanStart * itemHeight,
    afterHeight: Math.max(0, (count - overscanEnd) * itemHeight),
    totalHeight,
  }
}

export class CollectionWindowController {
  #itemCount = 0
  #itemHeight: number
  #viewportHeight = 0
  #scrollTop = 0
  #overscan: number
  #window: CollectionWindow

  constructor(options: { itemHeight: number; overscan?: number }) {
    this.#itemHeight = Math.max(1, finite(options.itemHeight, 1))
    this.#overscan = Math.max(0, Math.floor(options.overscan ?? 5))
    this.#window = collectionWindow({
      itemCount: 0,
      itemHeight: this.#itemHeight,
      viewportHeight: 0,
      scrollTop: 0,
      overscan: this.#overscan,
    })
  }

  update(input: Partial<CollectionWindowInput>): CollectionWindow {
    if (input.itemCount !== undefined) this.#itemCount = Math.max(0, Math.floor(input.itemCount))
    if (input.itemHeight !== undefined) this.#itemHeight = Math.max(1, finite(input.itemHeight, 1))
    if (input.viewportHeight !== undefined) this.#viewportHeight = Math.max(0, finite(input.viewportHeight))
    if (input.scrollTop !== undefined) this.#scrollTop = Math.max(0, finite(input.scrollTop))
    if (input.overscan !== undefined) this.#overscan = Math.max(0, Math.floor(input.overscan))
    this.#window = collectionWindow({
      itemCount: this.#itemCount,
      itemHeight: this.#itemHeight,
      viewportHeight: this.#viewportHeight,
      scrollTop: this.#scrollTop,
      overscan: this.#overscan,
    })
    return { ...this.#window }
  }

  current(): CollectionWindow {
    return { ...this.#window }
  }

  scrollToIndex(index: number, align: "start" | "center" | "end" | "nearest" = "nearest"): number {
    const target = Math.max(0, Math.min(Math.max(0, this.#itemCount - 1), Math.floor(index)))
    const itemStart = target * this.#itemHeight
    const itemEnd = itemStart + this.#itemHeight
    const viewportEnd = this.#scrollTop + this.#viewportHeight
    let scrollTop = this.#scrollTop
    if (align === "start") scrollTop = itemStart
    else if (align === "center") scrollTop = itemStart - (this.#viewportHeight - this.#itemHeight) / 2
    else if (align === "end") scrollTop = itemEnd - this.#viewportHeight
    else if (itemStart < this.#scrollTop) scrollTop = itemStart
    else if (itemEnd > viewportEnd) scrollTop = itemEnd - this.#viewportHeight
    const maximum = Math.max(0, this.#itemCount * this.#itemHeight - this.#viewportHeight)
    this.#scrollTop = Math.max(0, Math.min(maximum, scrollTop))
    this.update({ scrollTop: this.#scrollTop })
    return this.#scrollTop
  }

  indexAtOffset(offset: number): number {
    if (!this.#itemCount) return -1
    return Math.max(0, Math.min(this.#itemCount - 1, Math.floor(Math.max(0, offset) / this.#itemHeight)))
  }
}

export function nextCollectionIndex(
  current: number,
  count: number,
  action: "next" | "previous" | "first" | "last" | "page-next" | "page-previous",
  pageSize = 10,
): number {
  if (count <= 0) return -1
  const normalized = Math.max(0, Math.min(count - 1, Math.floor(current)))
  if (action === "first") return 0
  if (action === "last") return count - 1
  if (action === "next") return Math.min(count - 1, normalized + 1)
  if (action === "previous") return Math.max(0, normalized - 1)
  const page = Math.max(1, Math.floor(pageSize))
  if (action === "page-next") return Math.min(count - 1, normalized + page)
  return Math.max(0, normalized - page)
}
