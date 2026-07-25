import type {
  PermissionApi,
  PermissionSessionBinding,
  PermissionSessionClaim,
} from "../../api/permission-api.ts"
import { identifier, stablePermissionId } from "./canonical.ts"
import type { PermissionTaskBindingInput } from "./contracts.ts"

export type PermissionSessionPhase =
  | "opening"
  | "ready"
  | "resuming"
  | "error"
  | "released"

export interface PermissionSessionRosterEntry
  extends PermissionSessionBinding {
  rosterId: string
  primary: boolean
  phase: PermissionSessionPhase
  custodyId?: string
  custodyFingerprint?: string
  custodyVerified: boolean
  custodyInMemoryOnly: true
  tokenProjected: false
  openedAt?: string
  resumedAt?: string
  errorCode?: string
  errorMessage?: string
  generation: number
}

export interface PermissionSessionRosterSnapshot {
  schema: "zyra.permission-session-roster/v1"
  taskId?: string
  runId?: string
  entries: readonly PermissionSessionRosterEntry[]
  readyBindings: readonly PermissionSessionBinding[]
  generation: number
  openCount: number
  resumeCount: number
  failureCount: number
  custodyInMemoryOnly: true
  tokenProjected: false
  revision: number
}

export class PermissionSessionRoster {
  readonly #api: PermissionApi
  readonly #entries = new Map<string, PermissionSessionRosterEntry>()
  readonly #now: () => Date
  #taskId?: string
  #runId?: string
  #generation = 0
  #openCount = 0
  #resumeCount = 0
  #failureCount = 0
  #revision = 0
  #closed = false

  constructor(
    api: PermissionApi,
    options: { now?: () => Date } = {},
  ) {
    this.#api = api
    this.#now = options.now ?? (() => new Date())
  }

  async bind(
    input: PermissionTaskBindingInput,
    options: {
      signal?: AbortSignal
      timeoutMs?: number
    } = {},
  ): Promise<PermissionSessionRosterSnapshot> {
    this.#assertOpen()
    const desired = desiredBindings(input)
    const generation = ++this.#generation
    this.#taskId = input.taskId
    this.#runId = input.runId
    const desiredKeys = new Set(desired.map(bindingKey))
    for (const [key, entry] of this.#entries) {
      if (desiredKeys.has(key)) continue
      this.#entries.set(
        key,
        freezeEntry({
          ...entry,
          phase: "released",
          generation,
        }),
      )
    }
    for (let index = 0; index < desired.length; index += 1) {
      const binding = desired[index]!
      if (options.signal?.aborted) {
        throw options.signal.reason ?? new Error("Permission roster bind aborted.")
      }
      const key = bindingKey(binding)
      const prior = this.#entries.get(key)
      this.#entries.set(
        key,
        freezeEntry({
          ...(prior ?? baseEntry(binding, index === 0, generation)),
          phase: "opening",
          primary: index === 0,
          generation,
          errorCode: undefined,
          errorMessage: undefined,
        }),
      )
      try {
        const token = input.custodyTokens?.[binding.sessionId]
        if (token) {
          this.#api.adoptCustody(binding, token)
        }
        const claim = await this.#api.openSession(binding, {
          externalSessionExists: Boolean(token),
          custodyToken: token,
          signal: options.signal,
          timeoutMs: options.timeoutMs,
        })
        if (generation !== this.#generation) return this.snapshot()
        this.#rememberClaim(claim, index === 0, generation, "open")
      } catch (error) {
        if (generation !== this.#generation) return this.snapshot()
        this.#failureCount += 1
        this.#entries.set(
          key,
          freezeEntry({
            ...(this.#entries.get(key)
              ?? baseEntry(binding, index === 0, generation)),
            phase: "error",
            custodyVerified: false,
            generation,
            errorCode: errorCode(error),
            errorMessage: errorMessage(error),
          }),
        )
        this.#revision += 1
        if (index === 0) throw error
      }
    }
    this.#pruneReleased()
    return this.snapshot()
  }

  async resumeAll(options: {
    signal?: AbortSignal
    timeoutMs?: number
  } = {}): Promise<PermissionSessionRosterSnapshot> {
    this.#assertOpen()
    const generation = this.#generation
    const ready = this.entries().filter(
      (entry) => entry.phase === "ready",
    )
    for (const entry of ready) {
      if (options.signal?.aborted) {
        throw options.signal.reason ?? new Error("Permission roster resume aborted.")
      }
      const key = bindingKey(entry)
      this.#entries.set(
        key,
        freezeEntry({ ...entry, phase: "resuming", generation }),
      )
      try {
        await this.#api.resumeSession(entry, {
          signal: options.signal,
          timeoutMs: options.timeoutMs,
        })
        if (generation !== this.#generation) return this.snapshot()
        this.#resumeCount += 1
        this.#entries.set(
          key,
          freezeEntry({
            ...entry,
            phase: "ready",
            resumedAt: this.#now().toISOString(),
            generation,
            errorCode: undefined,
            errorMessage: undefined,
          }),
        )
        this.#revision += 1
      } catch (error) {
        if (generation !== this.#generation) return this.snapshot()
        this.#failureCount += 1
        this.#entries.set(
          key,
          freezeEntry({
            ...entry,
            phase: "error",
            generation,
            errorCode: errorCode(error),
            errorMessage: errorMessage(error),
          }),
        )
        this.#revision += 1
        if (entry.primary) throw error
      }
    }
    return this.snapshot()
  }

  readyBindings(): PermissionSessionBinding[] {
    return this.entries()
      .filter((entry) => entry.phase === "ready")
      .map((entry) =>
        Object.freeze({
          taskId: entry.taskId,
          runId: entry.runId,
          sessionId: entry.sessionId,
        }),
      )
  }

  entries(): PermissionSessionRosterEntry[] {
    return [...this.#entries.values()]
      .sort(
        (left, right) =>
          Number(right.primary) - Number(left.primary)
          || left.sessionId.localeCompare(right.sessionId),
      )
      .map(freezeEntry)
  }

  snapshot(): PermissionSessionRosterSnapshot {
    return Object.freeze({
      schema: "zyra.permission-session-roster/v1",
      taskId: this.#taskId,
      runId: this.#runId,
      entries: Object.freeze(this.entries()),
      readyBindings: Object.freeze(this.readyBindings()),
      generation: this.#generation,
      openCount: this.#openCount,
      resumeCount: this.#resumeCount,
      failureCount: this.#failureCount,
      custodyInMemoryOnly: true,
      tokenProjected: false,
      revision: this.#revision,
    })
  }

  release(): PermissionSessionRosterSnapshot {
    this.#generation += 1
    for (const [key, entry] of this.#entries) {
      this.#entries.set(
        key,
        freezeEntry({
          ...entry,
          phase: "released",
          generation: this.#generation,
        }),
      )
    }
    this.#revision += 1
    this.#pruneReleased()
    return this.snapshot()
  }

  close(): void {
    if (this.#closed) return
    this.release()
    this.#closed = true
    this.#entries.clear()
    this.#taskId = undefined
    this.#runId = undefined
  }

  #rememberClaim(
    claim: PermissionSessionClaim,
    primary: boolean,
    generation: number,
    source: "open",
  ): void {
    const key = bindingKey(claim)
    const prior = this.#entries.get(key)
    if (
      prior
      && (
        prior.taskId !== claim.taskId
        || prior.runId !== claim.runId
        || prior.sessionId !== claim.sessionId
      )
    ) {
      throw rosterError(
        "permission_custody_binding_changed",
        "Permission custody claim changed its exact task/run/session binding.",
      )
    }
    this.#openCount += source === "open" ? 1 : 0
    this.#entries.set(
      key,
      freezeEntry({
        ...(prior ?? baseEntry(claim, primary, generation)),
        phase: "ready",
        primary,
        custodyId: claim.custodyId,
        custodyFingerprint: claim.custodyFingerprint,
        custodyVerified: claim.verified,
        openedAt: this.#now().toISOString(),
        generation,
        errorCode: undefined,
        errorMessage: undefined,
      }),
    )
    this.#revision += 1
  }

  #pruneReleased(): void {
    for (const [key, entry] of this.#entries) {
      if (entry.phase === "released") this.#entries.delete(key)
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw rosterError(
        "permission_session_roster_closed",
        "Permission session roster is closed.",
      )
    }
  }
}

function desiredBindings(
  input: PermissionTaskBindingInput,
): PermissionSessionBinding[] {
  const taskId = identifier(input.taskId, "permission task id")
  const runId = identifier(input.runId, "permission run id")
  const sessions = [
    input.sessionId,
    ...(input.additionalSessionIds ?? []),
  ].map((value) => identifier(value, "permission session id"))
  return [...new Set(sessions)].map((sessionId) =>
    Object.freeze({ taskId, runId, sessionId }),
  )
}

function baseEntry(
  binding: PermissionSessionBinding,
  primary: boolean,
  generation: number,
): PermissionSessionRosterEntry {
  return {
    ...binding,
    rosterId: stablePermissionId(
      "permission_session_roster",
      binding.taskId,
      binding.runId,
      binding.sessionId,
    ),
    primary,
    phase: "opening",
    custodyVerified: false,
    custodyInMemoryOnly: true,
    tokenProjected: false,
    generation,
  }
}

function bindingKey(binding: PermissionSessionBinding): string {
  return `${binding.taskId}\u0000${binding.runId}\u0000${binding.sessionId}`
}

function freezeEntry(
  value: PermissionSessionRosterEntry,
): PermissionSessionRosterEntry {
  return Object.freeze({ ...value })
}

function errorCode(error: unknown): string {
  if (!error || typeof error !== "object") {
    return "permission_session_roster_failed"
  }
  const code = (error as Record<string, unknown>).code
  return typeof code === "string" && code.trim()
    ? code.trim().slice(0, 256)
    : "permission_session_roster_failed"
}

function errorMessage(error: unknown): string {
  return (
    error instanceof Error
      ? error.message
      : String(error || "Permission session roster operation failed.")
  ).slice(0, 2_000)
}

function rosterError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionSessionRosterError",
    code,
  })
}
