import { boundedString } from "./constants.ts"
import { AuthenticationError, RequestCancelledError } from "./errors.ts"
import type { AuthTokenProvider } from "./headers.ts"

export interface AuthToken {
  value: string
  expiresAt?: number
  issuedAt?: number
  scopes: string[]
  subject?: string
  source: string
}

export interface AuthSnapshot {
  configured: boolean
  authenticated: boolean
  expiresAt?: number
  remainingMs?: number
  scopes: string[]
  subject?: string
  source?: string
  failures: number
}

function normalizeScope(value: unknown): string {
  const scope = boundedString(value, 256, "authentication scope")
  if (!/^[A-Za-z0-9][A-Za-z0-9:._/-]*$/.test(scope)) throw new TypeError(`Invalid authentication scope: ${scope}`)
  return scope
}

export function normalizeAuthToken(value: string | AuthToken, source = "configured"): AuthToken {
  if (typeof value === "string") {
    return {
      value: boundedString(value.replace(/^Bearer\s+/i, ""), 8_192, "authentication token"),
      scopes: [],
      source,
    }
  }
  const token = boundedString(value.value.replace(/^Bearer\s+/i, ""), 8_192, "authentication token")
  const issuedAt = value.issuedAt
  const expiresAt = value.expiresAt
  if (issuedAt !== undefined && (!Number.isFinite(issuedAt) || issuedAt < 0)) {
    throw new TypeError("Authentication token issue time must be a non-negative timestamp")
  }
  if (expiresAt !== undefined && (!Number.isFinite(expiresAt) || expiresAt < 0)) {
    throw new TypeError("Authentication token expiry must be a non-negative timestamp")
  }
  if (issuedAt !== undefined && expiresAt !== undefined && expiresAt <= issuedAt) {
    throw new TypeError("Authentication token expiry must follow issue time")
  }
  return {
    value: token,
    issuedAt,
    expiresAt,
    scopes: [...new Set(value.scopes.map(normalizeScope))].sort(),
    subject: value.subject ? boundedString(value.subject, 512, "authentication subject") : undefined,
    source: boundedString(value.source || source, 128, "authentication source"),
  }
}

export function tokenExpired(token: AuthToken, now = Date.now(), skewMs = 0): boolean {
  return token.expiresAt !== undefined && token.expiresAt <= now + Math.max(0, skewMs)
}

export function tokenAllows(token: AuthToken, scope: string): boolean {
  const normalized = normalizeScope(scope)
  if (!token.scopes.length) return true
  if (token.scopes.includes("*") || token.scopes.includes(normalized)) return true
  const segments = normalized.split(":")
  while (segments.length > 1) {
    segments.pop()
    if (token.scopes.includes(`${segments.join(":")}:*`)) return true
  }
  return false
}

export class AuthTokenManager {
  readonly #provider?: () => AuthToken | string | undefined | null | Promise<AuthToken | string | undefined | null>
  readonly #now: () => number
  readonly #refreshSkewMs: number
  #token?: AuthToken
  #pending?: Promise<AuthToken | undefined>
  #failures = 0
  #generation = 0
  #disabled = false

  constructor(options: {
    token?: AuthToken | string
    provider?: () => AuthToken | string | undefined | null | Promise<AuthToken | string | undefined | null>
    now?: () => number
    refreshSkewMs?: number
  } = {}) {
    this.#provider = options.provider
    this.#now = options.now ?? Date.now
    this.#refreshSkewMs = Math.max(0, Math.floor(options.refreshSkewMs ?? 30_000))
    if (options.token) this.#token = normalizeAuthToken(options.token)
  }

  async resolve(options: { required?: boolean; scope?: string; signal?: AbortSignal } = {}): Promise<string | undefined> {
    if (this.#disabled) {
      if (options.required) throw new AuthenticationError("Authentication manager is disabled.")
      return undefined
    }
    if (options.signal?.aborted) throw new RequestCancelledError(options.signal.reason)
    let token = this.#token
    if (!token || tokenExpired(token, this.#now(), this.#refreshSkewMs)) token = await this.#refresh(options.signal)
    if (!token) {
      if (options.required) throw new AuthenticationError("Zyra API authentication is required.")
      return undefined
    }
    if (tokenExpired(token, this.#now())) {
      this.#token = undefined
      throw new AuthenticationError("Zyra API authentication token has expired.")
    }
    if (options.scope && !tokenAllows(token, options.scope)) {
      throw new AuthenticationError(`Authentication token does not grant ${options.scope}.`, 403)
    }
    return token.value
  }

  provider(options: { required?: boolean; scope?: string } = {}): AuthTokenProvider {
    return () => this.resolve(options)
  }

  set(value: AuthToken | string): void {
    this.#token = normalizeAuthToken(value, "manual")
    this.#generation += 1
    this.#failures = 0
  }

  clear(): void {
    this.#token = undefined
    this.#generation += 1
  }

  reject(): void {
    this.#failures += 1
    this.clear()
  }

  disable(): void {
    this.#disabled = true
    this.clear()
  }

  enable(): void {
    this.#disabled = false
  }

  snapshot(): AuthSnapshot {
    const token = this.#token
    const now = this.#now()
    return {
      configured: Boolean(this.#provider || token),
      authenticated: Boolean(token && !tokenExpired(token, now)),
      expiresAt: token?.expiresAt,
      remainingMs: token?.expiresAt === undefined ? undefined : Math.max(0, token.expiresAt - now),
      scopes: [...(token?.scopes ?? [])],
      subject: token?.subject,
      source: token?.source,
      failures: this.#failures,
    }
  }

  async #refresh(signal?: AbortSignal): Promise<AuthToken | undefined> {
    if (!this.#provider) return this.#token
    if (this.#pending) return this.#pending
    const generation = this.#generation
    const promise = Promise.resolve()
      .then(async () => {
        if (signal?.aborted) throw new RequestCancelledError(signal.reason)
        const value = await this.#provider!()
        if (signal?.aborted) throw new RequestCancelledError(signal.reason)
        if (value === undefined || value === null || value === "") return undefined
        return normalizeAuthToken(value, "provider")
      })
      .then((token) => {
        if (generation !== this.#generation) return this.#token
        this.#token = token
        if (token) this.#failures = 0
        return token
      })
      .catch((error) => {
        this.#failures += 1
        if (error instanceof RequestCancelledError) throw error
        throw new AuthenticationError("Authentication token provider failed.", 401, {}, {
          provider_error: error instanceof Error ? error.message : String(error),
        })
      })
      .finally(() => {
        if (this.#pending === promise) this.#pending = undefined
      })
    this.#pending = promise
    return promise
  }
}
