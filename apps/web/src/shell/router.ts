const TASK_ID_PATTERN = /^task[_:-][A-Za-z0-9][A-Za-z0-9._:-]{2,240}$/

export type WorkbenchRoute =
  | { kind: "tasks"; path: "/tasks"; query: RouteQuery }
  | { kind: "task"; path: string; taskId: string; query: RouteQuery }
  | { kind: "evidence"; path: string; taskId: string; query: RouteQuery }
  | { kind: "new-task"; path: "/tasks/new"; query: RouteQuery }
  | { kind: "settings"; path: "/settings"; query: RouteQuery }
  | { kind: "not-found"; path: string; attemptedPath: string; query: RouteQuery }

export interface RouteQuery {
  focus?: "command" | "task-list" | "task-detail"
  status?: string
  cursor?: string
  overlay?: string
}

export interface RouteLocation {
  pathname: string
  search: string
  hash: string
}

export interface HistoryLike {
  readonly state: unknown
  pushState(data: unknown, unused: string, url?: string | URL | null): void
  replaceState(data: unknown, unused: string, url?: string | URL | null): void
  back(): void
}

export interface WindowLike {
  readonly location: RouteLocation
  readonly history: HistoryLike
  addEventListener(type: "popstate", listener: () => void): void
  removeEventListener(type: "popstate", listener: () => void): void
}

export interface RouteTransition {
  from: WorkbenchRoute
  to: WorkbenchRoute
  kind: "push" | "replace" | "pop" | "recover"
  timestamp: number
}

function decodePathSegment(value: string): string | undefined {
  try {
    return decodeURIComponent(value)
  } catch {
    return undefined
  }
}

function normalizePathname(value: string): string {
  const raw = value.trim() || "/"
  const prefixed = raw.startsWith("/") ? raw : `/${raw}`
  const collapsed = prefixed.replace(/\/{2,}/g, "/")
  if (collapsed === "/") return collapsed
  return collapsed.replace(/\/+$/g, "")
}

function normalizeStatus(value: string | null): string | undefined {
  const normalized = value?.trim().toLowerCase()
  if (!normalized) return undefined
  if (!/^[a-z][a-z0-9_.-]{1,63}$/.test(normalized)) return undefined
  return normalized
}

function normalizeCursor(value: string | null): string | undefined {
  const normalized = value?.trim()
  if (!normalized || normalized.length > 8192) return undefined
  return normalized
}

function normalizeOverlay(value: string | null): string | undefined {
  const normalized = value?.trim().toLowerCase()
  if (!normalized) return undefined
  if (!/^[a-z][a-z0-9-]{1,63}$/.test(normalized)) return undefined
  return normalized
}

function normalizeFocus(value: string | null): RouteQuery["focus"] {
  if (value === "command" || value === "task-list" || value === "task-detail") {
    return value
  }
  return undefined
}

export function parseRouteQuery(search: string): RouteQuery {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search)
  const focus = normalizeFocus(params.get("focus"))
  const status = normalizeStatus(params.get("status"))
  const cursor = normalizeCursor(params.get("cursor"))
  const overlay = normalizeOverlay(params.get("overlay"))
  return {
    ...(focus ? { focus } : {}),
    ...(status ? { status } : {}),
    ...(cursor ? { cursor } : {}),
    ...(overlay ? { overlay } : {}),
  }
}

export function serializeRouteQuery(query: RouteQuery): string {
  const params = new URLSearchParams()
  if (query.focus) params.set("focus", query.focus)
  if (query.status) params.set("status", query.status)
  if (query.cursor) params.set("cursor", query.cursor)
  if (query.overlay) params.set("overlay", query.overlay)
  const value = params.toString()
  return value ? `?${value}` : ""
}

export function parseWorkbenchRoute(location: Pick<RouteLocation, "pathname" | "search">): WorkbenchRoute {
  const path = normalizePathname(location.pathname)
  const query = parseRouteQuery(location.search)
  if (path === "/" || path === "/tasks") {
    return { kind: "tasks", path: "/tasks", query }
  }
  if (path === "/tasks/new") {
    return { kind: "new-task", path: "/tasks/new", query }
  }
  if (path === "/settings") {
    return { kind: "settings", path: "/settings", query }
  }
  const parts = path.split("/").filter(Boolean)
  if (parts.length === 3 && parts[0] === "tasks" && parts[2] === "evidence") {
    const taskId = decodePathSegment(parts[1] ?? "")
    if (taskId && TASK_ID_PATTERN.test(taskId)) {
      return {
        kind: "evidence",
        path: `/tasks/${encodeURIComponent(taskId)}/evidence`,
        taskId,
        query,
      }
    }
  }
  if (parts.length === 2 && parts[0] === "tasks") {
    const taskId = decodePathSegment(parts[1] ?? "")
    if (taskId && TASK_ID_PATTERN.test(taskId)) {
      return {
        kind: "task",
        path: `/tasks/${encodeURIComponent(taskId)}`,
        taskId,
        query,
      }
    }
  }
  return {
    kind: "not-found",
    path,
    attemptedPath: `${path}${serializeRouteQuery(query)}`,
    query,
  }
}

export function routeHref(route: WorkbenchRoute): string {
  return `${route.path}${serializeRouteQuery(route.query)}`
}

export function taskRoute(taskId: string, query: RouteQuery = {}): WorkbenchRoute {
  if (!TASK_ID_PATTERN.test(taskId)) {
    throw new TypeError(`Invalid task route identity: ${taskId}`)
  }
  return {
    kind: "task",
    path: `/tasks/${encodeURIComponent(taskId)}`,
    taskId,
    query: { ...query },
  }
}

export function evidenceRoute(taskId: string, query: RouteQuery = {}): WorkbenchRoute {
  if (!TASK_ID_PATTERN.test(taskId)) {
    throw new TypeError(`Invalid evidence route identity: ${taskId}`)
  }
  return {
    kind: "evidence",
    path: `/tasks/${encodeURIComponent(taskId)}/evidence`,
    taskId,
    query: { ...query },
  }
}

export function tasksRoute(query: RouteQuery = {}): WorkbenchRoute {
  return { kind: "tasks", path: "/tasks", query: { ...query } }
}

export function newTaskRoute(query: RouteQuery = {}): WorkbenchRoute {
  return { kind: "new-task", path: "/tasks/new", query: { ...query } }
}

export function settingsRoute(query: RouteQuery = {}): WorkbenchRoute {
  return { kind: "settings", path: "/settings", query: { ...query } }
}

export function routeEquals(left: WorkbenchRoute, right: WorkbenchRoute): boolean {
  return routeHref(left) === routeHref(right)
}

export function withRouteQuery(route: WorkbenchRoute, patch: Partial<RouteQuery>): WorkbenchRoute {
  const query: RouteQuery = { ...route.query }
  for (const [key, value] of Object.entries(patch) as [keyof RouteQuery, string | undefined][]) {
    if (value === undefined || value === "") delete query[key]
    else Object.assign(query, { [key]: value })
  }
  return { ...route, query } as WorkbenchRoute
}

export function recoverRoute(route: WorkbenchRoute): WorkbenchRoute {
  if (route.kind !== "not-found") return route
  return tasksRoute({ focus: "task-list" })
}

export class WorkbenchRouter {
  readonly #window: WindowLike
  readonly #listeners = new Set<(route: WorkbenchRoute, transition: RouteTransition) => void>()
  readonly #history: RouteTransition[] = []
  readonly #onPopState: () => void
  #current: WorkbenchRoute
  #started = false
  #closed = false

  constructor(windowLike: WindowLike) {
    this.#window = windowLike
    this.#current = parseWorkbenchRoute(windowLike.location)
    this.#onPopState = () => {
      if (this.#closed) return
      const next = parseWorkbenchRoute(this.#window.location)
      this.#commit(next, "pop")
    }
  }

  get current(): WorkbenchRoute {
    return this.#current
  }

  get started(): boolean {
    return this.#started
  }

  start(options: { normalize?: boolean } = {}): WorkbenchRoute {
    if (this.#closed) throw new TypeError("Workbench router is closed.")
    if (this.#started) return this.#current
    this.#started = true
    this.#window.addEventListener("popstate", this.#onPopState)
    const parsed = parseWorkbenchRoute(this.#window.location)
    this.#current = parsed
    if (options.normalize !== false && parsed.kind !== "not-found") {
      const canonical = routeHref(parsed)
      const actual = `${this.#window.location.pathname}${this.#window.location.search}`
      if (canonical !== actual) {
        this.#window.history.replaceState(
          this.#historyState(parsed),
          "",
          canonical,
        )
      }
    }
    return parsed
  }

  listen(listener: (route: WorkbenchRoute, transition: RouteTransition) => void): () => void {
    if (this.#closed) throw new TypeError("Workbench router is closed.")
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  navigate(
    route: WorkbenchRoute,
    options: { replace?: boolean; preserveOverlay?: boolean } = {},
  ): WorkbenchRoute {
    this.#assertReady()
    const next =
      options.preserveOverlay && this.#current.query.overlay && !route.query.overlay
        ? withRouteQuery(route, { overlay: this.#current.query.overlay })
        : route
    if (routeEquals(next, this.#current)) return this.#current
    const kind = options.replace ? "replace" : "push"
    const href = routeHref(next)
    if (kind === "replace") {
      this.#window.history.replaceState(this.#historyState(next), "", href)
    } else {
      this.#window.history.pushState(this.#historyState(next), "", href)
    }
    this.#commit(next, kind)
    return next
  }

  updateQuery(patch: Partial<RouteQuery>, options: { replace?: boolean } = {}): WorkbenchRoute {
    return this.navigate(withRouteQuery(this.#current, patch), {
      replace: options.replace ?? true,
    })
  }

  openTask(taskId: string, options: { replace?: boolean; focus?: RouteQuery["focus"] } = {}): WorkbenchRoute {
    return this.navigate(taskRoute(taskId, {
      focus: options.focus ?? "task-detail",
    }), { replace: options.replace })
  }

  openEvidence(taskId: string, options: { replace?: boolean } = {}): WorkbenchRoute {
    return this.navigate(evidenceRoute(taskId, { focus: "task-detail" }), options)
  }

  openTasks(options: { replace?: boolean; status?: string; cursor?: string } = {}): WorkbenchRoute {
    return this.navigate(tasksRoute({
      focus: "task-list",
      status: normalizeStatus(options.status ?? null),
      cursor: normalizeCursor(options.cursor ?? null),
    }), { replace: options.replace })
  }

  openNewTask(options: { replace?: boolean } = {}): WorkbenchRoute {
    return this.navigate(newTaskRoute({ focus: "command" }), options)
  }

  openSettings(options: { replace?: boolean } = {}): WorkbenchRoute {
    return this.navigate(settingsRoute(), options)
  }

  recover(): WorkbenchRoute {
    this.#assertReady()
    const next = recoverRoute(this.#current)
    if (routeEquals(next, this.#current)) return this.#current
    this.#window.history.replaceState(this.#historyState(next), "", routeHref(next))
    this.#commit(next, "recover")
    return next
  }

  back(): void {
    this.#assertReady()
    this.#window.history.back()
  }

  transitions(limit = 100): RouteTransition[] {
    const count = Math.max(0, Math.floor(limit))
    return this.#history.slice(Math.max(0, this.#history.length - count)).map((item) => ({
      ...item,
      from: { ...item.from, query: { ...item.from.query } } as WorkbenchRoute,
      to: { ...item.to, query: { ...item.to.query } } as WorkbenchRoute,
    }))
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    if (this.#started) {
      this.#window.removeEventListener("popstate", this.#onPopState)
    }
    this.#listeners.clear()
  }

  #commit(next: WorkbenchRoute, kind: RouteTransition["kind"]): void {
    const previous = this.#current
    if (routeEquals(previous, next) && kind === "pop") return
    this.#current = next
    const transition: RouteTransition = {
      from: previous,
      to: next,
      kind,
      timestamp: Date.now(),
    }
    this.#history.push(transition)
    if (this.#history.length > 500) this.#history.splice(0, this.#history.length - 500)
    for (const listener of this.#listeners) {
      try {
        listener(next, transition)
      } catch {
        // A view observer cannot change route semantics.
      }
    }
  }

  #historyState(route: WorkbenchRoute): Record<string, unknown> {
    return {
      zyraWorkbench: true,
      route: route.kind,
      taskId:
        route.kind === "task" || route.kind === "evidence"
          ? route.taskId
          : undefined,
      updatedAt: Date.now(),
    }
  }

  #assertReady(): void {
    if (this.#closed) throw new TypeError("Workbench router is closed.")
    if (!this.#started) this.start()
  }
}

export function createBrowserRouter(): WorkbenchRouter {
  if (typeof window === "undefined") {
    throw new TypeError("Browser router requires window.")
  }
  return new WorkbenchRouter(window)
}
