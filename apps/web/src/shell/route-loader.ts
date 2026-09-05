import type { WorkbenchRoute } from "./router.ts"
import type { WorkbenchController } from "./workbench-controller.ts"

export type RouteLoadPhase = "idle" | "loading" | "settled" | "failed" | "cancelled"

export interface RouteLoadRecord {
  id: string
  href: string
  routeKind: WorkbenchRoute["kind"]
  phase: RouteLoadPhase
  generation: number
  startedAt: number
  settledAt?: number
  taskId?: string
  error?: string
}

export interface RouteLoaderSnapshot {
  active?: RouteLoadRecord
  recent: readonly RouteLoadRecord[]
  revision: number
}

function routeHref(route: WorkbenchRoute): string {
  const query = new URLSearchParams()
  if (route.query.status) query.set("status", route.query.status)
  if (route.query.cursor) query.set("cursor", route.query.cursor)
  if (route.query.focus) query.set("focus", route.query.focus)
  if (route.query.overlay) query.set("overlay", route.query.overlay)
  const suffix = query.toString()
  return `${route.path}${suffix ? `?${suffix}` : ""}`
}

function cloneRecord(record: RouteLoadRecord): RouteLoadRecord {
  return { ...record }
}

export class WorkbenchRouteLoader {
  readonly #workbench: WorkbenchController
  readonly #listeners = new Set<() => void>()
  readonly #records: RouteLoadRecord[] = []
  #snapshot: RouteLoaderSnapshot = Object.freeze({
    recent: Object.freeze([]),
    revision: 0,
  })
  #generation = 0
  #active?: RouteLoadRecord
  #closed = false

  constructor(workbench: WorkbenchController) {
    this.#workbench = workbench
  }

  getSnapshot = (): RouteLoaderSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  async load(route: WorkbenchRoute): Promise<RouteLoadRecord> {
    this.#assertOpen()
    const href = routeHref(route)
    if (
      this.#active?.href === href &&
      (this.#active.phase === "loading" || this.#active.phase === "settled")
    ) {
      return cloneRecord(this.#active)
    }
    this.cancel("Route changed.")
    const generation = ++this.#generation
    const record: RouteLoadRecord = {
      id: `route_load_${generation.toString(36)}_${Date.now().toString(36)}`,
      href,
      routeKind: route.kind,
      phase: "loading",
      generation,
      startedAt: Date.now(),
      taskId:
        route.kind === "task" || route.kind === "evidence"
          ? route.taskId
          : undefined,
    }
    this.#active = record
    this.#remember(record)
    this.#publish()
    try {
      const operations = this.#operations(route)
      if (operations.length) {
        const results = await Promise.allSettled(operations)
        if (!this.#isCurrent(record)) return cloneRecord(record)
        const rejected = results.find(
          (result): result is PromiseRejectedResult => result.status === "rejected",
        )
        if (rejected) throw rejected.reason
      }
      if (!this.#isCurrent(record)) return cloneRecord(record)
      record.phase = "settled"
      record.settledAt = Date.now()
      this.#remember(record)
      this.#publish()
      return cloneRecord(record)
    } catch (error) {
      if (!this.#isCurrent(record)) return cloneRecord(record)
      record.phase = "failed"
      record.error = error instanceof Error ? error.message : String(error)
      record.settledAt = Date.now()
      this.#remember(record)
      this.#publish()
      return cloneRecord(record)
    }
  }

  async retry(): Promise<RouteLoadRecord | undefined> {
    const active = this.#active
    if (!active) return undefined
    const route = this.#routeFromRecord(active)
    this.#active = undefined
    return this.load(route)
  }

  cancel(reason = "Route load cancelled."): boolean {
    const active = this.#active
    if (!active || active.phase !== "loading") return false
    active.phase = "cancelled"
    active.error = reason
    active.settledAt = Date.now()
    if (active.routeKind === "task" || active.routeKind === "evidence") {
      this.#workbench.cancelRequest("detail", reason)
    }
    if (active.routeKind === "tasks") this.#workbench.cancelRequest("list", reason)
    this.#remember(active)
    this.#publish()
    return true
  }

  history(limit = 50): RouteLoadRecord[] {
    return this.#records
      .slice(0, Math.max(1, Math.min(200, Math.floor(limit))))
      .map(cloneRecord)
  }

  close(reason = "Route loader closed."): void {
    if (this.#closed) return
    this.cancel(reason)
    this.#closed = true
    this.#listeners.clear()
  }

  #operations(route: WorkbenchRoute): Promise<unknown>[] {
    const operations: Promise<unknown>[] = []
    if (!["task", "evidence"].includes(route.kind)) this.#workbench.selectTask(undefined)
    const runtime = this.#workbench.getSnapshot().runtime
    if (runtime.phase === "idle") operations.push(this.#workbench.refreshRuntime())
    if (route.kind === "tasks") {
      operations.push(this.#workbench.refreshTasks({
        status: route.query.status,
        cursor: route.query.cursor,
        preserveOnError: true,
      }))
    } else if (route.kind === "task" || route.kind === "evidence") {
      operations.push(this.#workbench.loadTask(route.taskId))
      if (this.#workbench.getSnapshot().list.phase === "idle") {
        operations.push(this.#workbench.refreshTasks({ preserveOnError: true }))
      }
    } else if (this.#workbench.getSnapshot().list.phase === "idle") {
      operations.push(this.#workbench.refreshTasks({ preserveOnError: true }))
    }
    return operations
  }

  #routeFromRecord(record: RouteLoadRecord): WorkbenchRoute {
    const parsed = new URL(record.href, "http://zyra.local")
    const query = {
      status: parsed.searchParams.get("status") ?? undefined,
      cursor: parsed.searchParams.get("cursor") ?? undefined,
      focus: parsed.searchParams.get("focus") as WorkbenchRoute["query"]["focus"] ?? undefined,
      overlay: parsed.searchParams.get("overlay") ?? undefined,
    }
    if (
      (record.routeKind === "task" || record.routeKind === "evidence")
      && record.taskId
    ) {
      return {
        kind: record.routeKind,
        taskId: record.taskId,
        path: parsed.pathname,
        query,
      }
    }
    if (record.routeKind === "tasks") return { kind: "tasks", path: "/tasks", query }
    if (record.routeKind === "new-task") return { kind: "new-task", path: "/tasks/new", query }
    if (record.routeKind === "settings") return { kind: "settings", path: "/settings", query }
    return {
      kind: "not-found",
      path: parsed.pathname,
      attemptedPath: record.href,
      query,
    }
  }

  #isCurrent(record: RouteLoadRecord): boolean {
    return !this.#closed && this.#active === record && record.generation === this.#generation
  }

  #remember(record: RouteLoadRecord): void {
    const index = this.#records.findIndex((entry) => entry.id === record.id)
    if (index >= 0) this.#records.splice(index, 1)
    this.#records.unshift(cloneRecord(record))
    if (this.#records.length > 200) this.#records.length = 200
  }

  #publish(): void {
    this.#snapshot = Object.freeze({
      active: this.#active ? cloneRecord(this.#active) : undefined,
      recent: Object.freeze(this.history(30)),
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Route observers cannot change load ownership.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Workbench route loader is closed.")
  }
}
