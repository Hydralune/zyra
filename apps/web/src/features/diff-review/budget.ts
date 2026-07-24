export interface DiffBudgetLimits {
  maximumRequests: number
  maximumConcurrentRequests: number
  maximumBytes: number
  maximumLines: number
  maximumFiles: number
  maximumHunks: number
  maximumPagesPerFile: number
  requestTimeoutMs: number
}

export interface DiffBudgetUsage {
  requestsStarted: number
  requestsCompleted: number
  requestsCancelled: number
  requestsFailed: number
  activeRequests: number
  admittedBytes: number
  admittedLines: number
  admittedFiles: number
  admittedHunks: number
  admittedPages: number
  remainingRequests: number
  remainingBytes: number
  remainingLines: number
  exhausted: boolean
}

export interface DiffBudgetReservation {
  reservationId: string
  fileId: string
  pageIndex: number
  requestedBytes: number
  requestedLines: number
  createdAt: number
  expiresAt: number
  state: "reserved" | "committed" | "released"
}

export interface DiffBudgetCommit {
  reservationId: string
  admittedBytes: number
  admittedLines: number
  admittedHunks: number
  cacheHit: boolean
}

export interface DiffRequestTicket {
  ticketId: string
  fileId: string
  pageIndex: number
  priority: number
  reason: string
  enqueuedAt: number
  deadlineAt: number
  signal?: AbortSignal
}

export interface DiffRequestLease {
  ticket: DiffRequestTicket
  reservation: DiffBudgetReservation
  startedAt: number
  controller: AbortController
}

export class DiffBudgetError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    retryable = false,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "DiffBudgetError"
    this.code = code
    this.retryable = retryable
    this.details = Object.freeze({ ...details })
  }
}

const defaultLimits: DiffBudgetLimits = Object.freeze({
  maximumRequests: 2_000,
  maximumConcurrentRequests: 4,
  maximumBytes: 64 * 1024 * 1024,
  maximumLines: 2_000_000,
  maximumFiles: 10_000,
  maximumHunks: 100_000,
  maximumPagesPerFile: 10_000,
  requestTimeoutMs: 30_000,
})

function bounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new DiffBudgetError(
      "diff_budget_limit_invalid",
      `${name} must be an integer from ${minimum} through ${maximum}.`,
      false,
      { actual: value, minimum, maximum },
    )
  }
  return value
}

export function normalizeDiffBudgetLimits(
  input: Partial<DiffBudgetLimits> = {},
): DiffBudgetLimits {
  const maximumRequests = bounded(
    input.maximumRequests,
    defaultLimits.maximumRequests,
    1,
    1_000_000,
    "maximumRequests",
  )
  const maximumConcurrentRequests = bounded(
    input.maximumConcurrentRequests,
    defaultLimits.maximumConcurrentRequests,
    1,
    32,
    "maximumConcurrentRequests",
  )
  const maximumBytes = bounded(
    input.maximumBytes,
    defaultLimits.maximumBytes,
    64 * 1024,
    2 * 1024 * 1024 * 1024,
    "maximumBytes",
  )
  const maximumLines = bounded(
    input.maximumLines,
    defaultLimits.maximumLines,
    1_000,
    20_000_000,
    "maximumLines",
  )
  const maximumFiles = bounded(
    input.maximumFiles,
    defaultLimits.maximumFiles,
    1,
    100_000,
    "maximumFiles",
  )
  const maximumHunks = bounded(
    input.maximumHunks,
    defaultLimits.maximumHunks,
    1,
    1_000_000,
    "maximumHunks",
  )
  const maximumPagesPerFile = bounded(
    input.maximumPagesPerFile,
    defaultLimits.maximumPagesPerFile,
    1,
    100_000,
    "maximumPagesPerFile",
  )
  const requestTimeoutMs = bounded(
    input.requestTimeoutMs,
    defaultLimits.requestTimeoutMs,
    250,
    5 * 60_000,
    "requestTimeoutMs",
  )
  return Object.freeze({
    maximumRequests,
    maximumConcurrentRequests,
    maximumBytes,
    maximumLines,
    maximumFiles,
    maximumHunks,
    maximumPagesPerFile,
    requestTimeoutMs,
  })
}

function nowValue(value?: number): number {
  const now = value ?? Date.now()
  if (!Number.isFinite(now) || now < 0) {
    throw new DiffBudgetError(
      "diff_budget_clock_invalid",
      "Diff budget clock must be a non-negative finite timestamp.",
    )
  }
  return Math.floor(now)
}

function safeCount(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new DiffBudgetError(
      "diff_budget_count_invalid",
      `${name} must be a non-negative safe integer.`,
      false,
      { actual: value },
    )
  }
  return value
}

function reservationIdentity(
  sequence: number,
  fileId: string,
  pageIndex: number,
): string {
  return `diff-budget:${sequence}:${encodeURIComponent(fileId)}:${pageIndex}`
}

export class DiffBudgetLedger {
  readonly limits: DiffBudgetLimits
  readonly #reservations = new Map<string, DiffBudgetReservation>()
  readonly #pages = new Set<string>()
  readonly #files = new Set<string>()
  #requestsStarted = 0
  #requestsCompleted = 0
  #requestsCancelled = 0
  #requestsFailed = 0
  #activeRequests = 0
  #admittedBytes = 0
  #admittedLines = 0
  #admittedHunks = 0
  #admittedPages = 0
  #sequence = 0
  #closed = false

  constructor(input: Partial<DiffBudgetLimits> = {}) {
    this.limits = normalizeDiffBudgetLimits(input)
  }

  get usage(): DiffBudgetUsage {
    const remainingRequests = Math.max(
      0,
      this.limits.maximumRequests - this.#requestsStarted,
    )
    const remainingBytes = Math.max(
      0,
      this.limits.maximumBytes - this.#admittedBytes,
    )
    const remainingLines = Math.max(
      0,
      this.limits.maximumLines - this.#admittedLines,
    )
    return Object.freeze({
      requestsStarted: this.#requestsStarted,
      requestsCompleted: this.#requestsCompleted,
      requestsCancelled: this.#requestsCancelled,
      requestsFailed: this.#requestsFailed,
      activeRequests: this.#activeRequests,
      admittedBytes: this.#admittedBytes,
      admittedLines: this.#admittedLines,
      admittedFiles: this.#files.size,
      admittedHunks: this.#admittedHunks,
      admittedPages: this.#admittedPages,
      remainingRequests,
      remainingBytes,
      remainingLines,
      exhausted:
        remainingRequests === 0
        || remainingBytes === 0
        || remainingLines === 0,
    })
  }

  reserve(input: {
    fileId: string
    pageIndex: number
    requestedBytes: number
    requestedLines: number
    now?: number
  }): DiffBudgetReservation {
    this.#requireOpen()
    const fileId = String(input.fileId || "").trim()
    if (!fileId) {
      throw new DiffBudgetError(
        "diff_budget_file_missing",
        "Diff budget reservation requires file identity.",
      )
    }
    const pageIndex = safeCount(input.pageIndex, "pageIndex")
    if (pageIndex >= this.limits.maximumPagesPerFile) {
      throw new DiffBudgetError(
        "diff_budget_page_limit",
        `File page index exceeds ${this.limits.maximumPagesPerFile}.`,
        false,
        { fileId, pageIndex },
      )
    }
    const requestedBytes = safeCount(input.requestedBytes, "requestedBytes")
    const requestedLines = safeCount(input.requestedLines, "requestedLines")
    if (requestedBytes > this.limits.maximumBytes) {
      throw new DiffBudgetError(
        "diff_budget_single_request_bytes",
        "Single diff request exceeds the total byte budget.",
        false,
        {
          requestedBytes,
          maximumBytes: this.limits.maximumBytes,
        },
      )
    }
    if (requestedLines > this.limits.maximumLines) {
      throw new DiffBudgetError(
        "diff_budget_single_request_lines",
        "Single diff request exceeds the total line budget.",
        false,
        {
          requestedLines,
          maximumLines: this.limits.maximumLines,
        },
      )
    }
    if (this.#requestsStarted >= this.limits.maximumRequests) {
      throw new DiffBudgetError(
        "diff_budget_requests_exhausted",
        "Diff request budget is exhausted.",
      )
    }
    if (this.#activeRequests >= this.limits.maximumConcurrentRequests) {
      throw new DiffBudgetError(
        "diff_budget_concurrency",
        "Diff request concurrency is saturated.",
        true,
        {
          active: this.#activeRequests,
          maximum: this.limits.maximumConcurrentRequests,
        },
      )
    }
    const pendingBytes = [...this.#reservations.values()]
      .filter((reservation) => reservation.state === "reserved")
      .reduce((total, reservation) => total + reservation.requestedBytes, 0)
    const pendingLines = [...this.#reservations.values()]
      .filter((reservation) => reservation.state === "reserved")
      .reduce((total, reservation) => total + reservation.requestedLines, 0)
    if (
      this.#admittedBytes + pendingBytes + requestedBytes
      > this.limits.maximumBytes
    ) {
      throw new DiffBudgetError(
        "diff_budget_bytes_exhausted",
        "Diff byte budget cannot cover this reservation.",
        false,
        {
          admitted: this.#admittedBytes,
          pending: pendingBytes,
          requested: requestedBytes,
          maximum: this.limits.maximumBytes,
        },
      )
    }
    if (
      this.#admittedLines + pendingLines + requestedLines
      > this.limits.maximumLines
    ) {
      throw new DiffBudgetError(
        "diff_budget_lines_exhausted",
        "Diff line budget cannot cover this reservation.",
        false,
        {
          admitted: this.#admittedLines,
          pending: pendingLines,
          requested: requestedLines,
          maximum: this.limits.maximumLines,
        },
      )
    }
    const now = nowValue(input.now)
    const reservation: DiffBudgetReservation = Object.freeze({
      reservationId: reservationIdentity(++this.#sequence, fileId, pageIndex),
      fileId,
      pageIndex,
      requestedBytes,
      requestedLines,
      createdAt: now,
      expiresAt: now + this.limits.requestTimeoutMs,
      state: "reserved",
    })
    this.#reservations.set(reservation.reservationId, reservation)
    this.#requestsStarted += 1
    this.#activeRequests += 1
    return reservation
  }

  commit(input: DiffBudgetCommit): DiffBudgetUsage {
    this.#requireOpen()
    const reservation = this.#requireReservation(input.reservationId)
    if (reservation.state !== "reserved") {
      throw new DiffBudgetError(
        "diff_budget_reservation_terminal",
        "Diff budget reservation was already finalized.",
        false,
        { reservationId: input.reservationId, state: reservation.state },
      )
    }
    const admittedBytes = safeCount(input.admittedBytes, "admittedBytes")
    const admittedLines = safeCount(input.admittedLines, "admittedLines")
    const admittedHunks = safeCount(input.admittedHunks, "admittedHunks")
    if (admittedBytes > reservation.requestedBytes) {
      throw new DiffBudgetError(
        "diff_budget_commit_byte_overflow",
        "Diff response exceeded its byte reservation.",
        false,
        {
          reserved: reservation.requestedBytes,
          actual: admittedBytes,
        },
      )
    }
    if (admittedLines > reservation.requestedLines) {
      throw new DiffBudgetError(
        "diff_budget_commit_line_overflow",
        "Diff response exceeded its line reservation.",
        false,
        {
          reserved: reservation.requestedLines,
          actual: admittedLines,
        },
      )
    }
    const key = `${reservation.fileId}:${reservation.pageIndex}`
    const duplicate = this.#pages.has(key)
    if (duplicate !== input.cacheHit) {
      throw new DiffBudgetError(
        "diff_budget_cache_claim_mismatch",
        "Diff response cache-hit claim disagrees with admitted pages.",
        false,
        { key, duplicate, cacheHit: input.cacheHit },
      )
    }
    if (!duplicate) {
      const nextFiles = this.#files.has(reservation.fileId)
        ? this.#files.size
        : this.#files.size + 1
      if (nextFiles > this.limits.maximumFiles) {
        throw new DiffBudgetError(
          "diff_budget_files_exhausted",
          "Diff file budget is exhausted.",
        )
      }
      if (this.#admittedHunks + admittedHunks > this.limits.maximumHunks) {
        throw new DiffBudgetError(
          "diff_budget_hunks_exhausted",
          "Diff hunk budget is exhausted.",
        )
      }
      this.#pages.add(key)
      this.#files.add(reservation.fileId)
      this.#admittedBytes += admittedBytes
      this.#admittedLines += admittedLines
      this.#admittedHunks += admittedHunks
      this.#admittedPages += 1
    }
    this.#requestsCompleted += 1
    this.#activeRequests -= 1
    this.#reservations.set(
      reservation.reservationId,
      Object.freeze({ ...reservation, state: "committed" }),
    )
    return this.usage
  }

  release(
    reservationId: string,
    outcome: "cancelled" | "failed",
  ): DiffBudgetUsage {
    const reservation = this.#requireReservation(reservationId)
    if (reservation.state !== "reserved") return this.usage
    this.#reservations.set(
      reservation.reservationId,
      Object.freeze({ ...reservation, state: "released" }),
    )
    this.#activeRequests -= 1
    if (outcome === "cancelled") this.#requestsCancelled += 1
    else this.#requestsFailed += 1
    return this.usage
  }

  sweepExpired(nowInput?: number): readonly string[] {
    const now = nowValue(nowInput)
    const released: string[] = []
    for (const reservation of this.#reservations.values()) {
      if (
        reservation.state === "reserved"
        && reservation.expiresAt <= now
      ) {
        this.release(reservation.reservationId, "cancelled")
        released.push(reservation.reservationId)
      }
    }
    return Object.freeze(released)
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    for (const reservation of this.#reservations.values()) {
      if (reservation.state === "reserved") {
        this.#reservations.set(
          reservation.reservationId,
          Object.freeze({ ...reservation, state: "released" }),
        )
        this.#requestsCancelled += 1
      }
    }
    this.#activeRequests = 0
  }

  reservations(): readonly DiffBudgetReservation[] {
    return Object.freeze([...this.#reservations.values()])
  }

  #requireReservation(reservationId: string): DiffBudgetReservation {
    const reservation = this.#reservations.get(reservationId)
    if (!reservation) {
      throw new DiffBudgetError(
        "diff_budget_reservation_missing",
        "Diff budget reservation does not exist.",
        false,
        { reservationId },
      )
    }
    return reservation
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new DiffBudgetError(
        "diff_budget_closed",
        "Diff budget ledger is closed.",
      )
    }
  }
}

function compareTickets(left: DiffRequestTicket, right: DiffRequestTicket): number {
  if (left.priority !== right.priority) return right.priority - left.priority
  if (left.deadlineAt !== right.deadlineAt) return left.deadlineAt - right.deadlineAt
  if (left.enqueuedAt !== right.enqueuedAt) return left.enqueuedAt - right.enqueuedAt
  return left.ticketId.localeCompare(right.ticketId)
}

function mergedSignal(
  controller: AbortController,
  external: AbortSignal | undefined,
): () => void {
  if (!external) return () => undefined
  if (external.aborted) {
    controller.abort(external.reason)
    return () => undefined
  }
  const abort = () => controller.abort(external.reason)
  external.addEventListener("abort", abort, { once: true })
  return () => external.removeEventListener("abort", abort)
}

export class DiffRequestQueue {
  readonly #budget: DiffBudgetLedger
  readonly #pending: DiffRequestTicket[] = []
  readonly #active = new Map<string, {
    lease: DiffRequestLease
    disposeSignal: () => void
    timer: ReturnType<typeof setTimeout>
  }>()
  readonly #keys = new Set<string>()
  #sequence = 0
  #closed = false

  constructor(budget: DiffBudgetLedger) {
    this.#budget = budget
  }

  enqueue(input: {
    fileId: string
    pageIndex: number
    priority?: number
    reason?: string
    timeoutMs?: number
    signal?: AbortSignal
    now?: number
  }): DiffRequestTicket {
    this.#requireOpen()
    const fileId = String(input.fileId || "").trim()
    if (!fileId) {
      throw new DiffBudgetError(
        "diff_queue_file_missing",
        "Diff request requires file identity.",
      )
    }
    const pageIndex = safeCount(input.pageIndex, "pageIndex")
    const key = `${fileId}:${pageIndex}`
    if (this.#keys.has(key)) {
      throw new DiffBudgetError(
        "diff_queue_duplicate",
        "Diff page is already queued or active.",
        true,
        { fileId, pageIndex },
      )
    }
    const now = nowValue(input.now)
    const timeoutMs = bounded(
      input.timeoutMs,
      this.#budget.limits.requestTimeoutMs,
      250,
      5 * 60_000,
      "timeoutMs",
    )
    const priority = bounded(
      input.priority,
      0,
      -1_000_000,
      1_000_000,
      "priority",
    )
    const ticket: DiffRequestTicket = Object.freeze({
      ticketId: `diff-ticket:${++this.#sequence}:${encodeURIComponent(fileId)}:${pageIndex}`,
      fileId,
      pageIndex,
      priority,
      reason: String(input.reason || "viewport"),
      enqueuedAt: now,
      deadlineAt: now + timeoutMs,
      signal: input.signal,
    })
    this.#pending.push(ticket)
    this.#pending.sort(compareTickets)
    this.#keys.add(key)
    return ticket
  }

  next(input: {
    requestedBytes: number
    requestedLines: number
    now?: number
  }): DiffRequestLease | undefined {
    this.#requireOpen()
    if (
      this.#active.size >= this.#budget.limits.maximumConcurrentRequests
      || !this.#pending.length
    ) {
      return undefined
    }
    const now = nowValue(input.now)
    while (this.#pending.length) {
      const ticket = this.#pending.shift()!
      if (ticket.signal?.aborted || ticket.deadlineAt <= now) {
        this.#keys.delete(`${ticket.fileId}:${ticket.pageIndex}`)
        continue
      }
      let reservation: DiffBudgetReservation
      try {
        reservation = this.#budget.reserve({
          fileId: ticket.fileId,
          pageIndex: ticket.pageIndex,
          requestedBytes: input.requestedBytes,
          requestedLines: input.requestedLines,
          now,
        })
      } catch (error) {
        if (
          error instanceof DiffBudgetError
          && error.code === "diff_budget_concurrency"
        ) {
          this.#pending.unshift(ticket)
          return undefined
        }
        this.#keys.delete(`${ticket.fileId}:${ticket.pageIndex}`)
        throw error
      }
      const controller = new AbortController()
      const disposeSignal = mergedSignal(controller, ticket.signal)
      const remaining = Math.max(1, ticket.deadlineAt - now)
      const timer = setTimeout(
        () => controller.abort(new DiffBudgetError(
          "diff_queue_timeout",
          "Diff page request exceeded its deadline.",
          true,
          { ticketId: ticket.ticketId },
        )),
        remaining,
      )
      const lease: DiffRequestLease = Object.freeze({
        ticket,
        reservation,
        startedAt: now,
        controller,
      })
      this.#active.set(ticket.ticketId, { lease, disposeSignal, timer })
      return lease
    }
    return undefined
  }

  complete(
    ticketId: string,
    commit: Omit<DiffBudgetCommit, "reservationId">,
  ): DiffBudgetUsage {
    const active = this.#requireActive(ticketId)
    this.#finishActive(active)
    return this.#budget.commit({
      ...commit,
      reservationId: active.lease.reservation.reservationId,
    })
  }

  fail(
    ticketId: string,
    outcome: "cancelled" | "failed",
  ): DiffBudgetUsage {
    const active = this.#active.get(ticketId)
    if (!active) return this.#budget.usage
    this.#finishActive(active)
    return this.#budget.release(
      active.lease.reservation.reservationId,
      outcome,
    )
  }

  cancelFile(fileId: string, reason = "Diff file request cancelled."): number {
    let count = 0
    for (let index = this.#pending.length - 1; index >= 0; index -= 1) {
      const ticket = this.#pending[index]!
      if (ticket.fileId !== fileId) continue
      this.#pending.splice(index, 1)
      this.#keys.delete(`${ticket.fileId}:${ticket.pageIndex}`)
      count += 1
    }
    for (const active of this.#active.values()) {
      if (active.lease.ticket.fileId !== fileId) continue
      active.lease.controller.abort(reason)
      count += 1
    }
    return count
  }

  cancelAll(reason = "Diff request queue cancelled."): number {
    const pending = this.#pending.length
    for (const ticket of this.#pending) {
      this.#keys.delete(`${ticket.fileId}:${ticket.pageIndex}`)
    }
    this.#pending.length = 0
    for (const active of this.#active.values()) {
      active.lease.controller.abort(reason)
    }
    return pending + this.#active.size
  }

  close(reason = "Diff request queue closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.cancelAll(reason)
    for (const active of [...this.#active.values()]) {
      this.#finishActive(active)
      this.#budget.release(
        active.lease.reservation.reservationId,
        "cancelled",
      )
    }
    this.#active.clear()
  }

  snapshot(): {
    pending: readonly DiffRequestTicket[]
    active: readonly DiffRequestLease[]
    usage: DiffBudgetUsage
    closed: boolean
  } {
    return Object.freeze({
      pending: Object.freeze([...this.#pending]),
      active: Object.freeze(
        [...this.#active.values()].map((entry) => entry.lease),
      ),
      usage: this.#budget.usage,
      closed: this.#closed,
    })
  }

  #finishActive(active: {
    lease: DiffRequestLease
    disposeSignal: () => void
    timer: ReturnType<typeof setTimeout>
  }): void {
    clearTimeout(active.timer)
    active.disposeSignal()
    const ticket = active.lease.ticket
    this.#active.delete(ticket.ticketId)
    this.#keys.delete(`${ticket.fileId}:${ticket.pageIndex}`)
  }

  #requireActive(ticketId: string) {
    const active = this.#active.get(ticketId)
    if (!active) {
      throw new DiffBudgetError(
        "diff_queue_lease_missing",
        "Diff request lease does not exist.",
        false,
        { ticketId },
      )
    }
    return active
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new DiffBudgetError(
        "diff_queue_closed",
        "Diff request queue is closed.",
      )
    }
  }
}
