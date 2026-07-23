import {
  ZYRA_API_MIN_VERSION_HEADER,
  ZYRA_API_VERSION,
  ZYRA_API_VERSION_HEADER,
  boundedString,
} from "./constants.ts"
import { ApiVersionMismatchError, ResponseValidationError } from "./errors.ts"

export interface ApiVersion {
  major: number
  minor: number
  patch: number
  prerelease?: string
  raw: string
}

export interface VersionPolicy {
  requested: string
  minimum?: string
  allowNewerMinor?: boolean
  requireResponseHeader?: boolean
}

const VERSION_PATTERN = /^(?:v)?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?$/

export function parseApiVersion(value: unknown): ApiVersion {
  const normalized = boundedString(value, 64, "API version")
  const match = VERSION_PATTERN.exec(normalized)
  if (!match) throw new TypeError(`Invalid API version: ${normalized}`)
  const major = Number.parseInt(match[1] ?? "0", 10)
  const minor = Number.parseInt(match[2] ?? "0", 10)
  const patch = Number.parseInt(match[3] ?? "0", 10)
  if (![major, minor, patch].every((entry) => Number.isSafeInteger(entry) && entry >= 0)) {
    throw new TypeError(`Invalid API version: ${normalized}`)
  }
  return {
    major,
    minor,
    patch,
    prerelease: match[4] || undefined,
    raw: `${major}.${minor}.${patch}${match[4] ? `-${match[4]}` : ""}`,
  }
}

export function formatApiVersion(version: ApiVersion, options: { omitPatch?: boolean } = {}): string {
  const base = options.omitPatch ? `${version.major}.${version.minor}` : `${version.major}.${version.minor}.${version.patch}`
  return version.prerelease ? `${base}-${version.prerelease}` : base
}

export function compareApiVersions(left: unknown, right: unknown): number {
  const a = parseApiVersion(left)
  const b = parseApiVersion(right)
  for (const key of ["major", "minor", "patch"] as const) {
    if (a[key] < b[key]) return -1
    if (a[key] > b[key]) return 1
  }
  if (a.prerelease === b.prerelease) return 0
  if (!a.prerelease) return 1
  if (!b.prerelease) return -1
  return a.prerelease < b.prerelease ? -1 : 1
}

export function apiVersionsCompatible(
  requested: unknown,
  provided: unknown,
  options: { allowNewerMinor?: boolean; minimum?: unknown } = {},
): boolean {
  const client = parseApiVersion(requested)
  const server = parseApiVersion(provided)
  if (client.major !== server.major) return false
  if (!options.allowNewerMinor && client.minor !== server.minor) return false
  if (options.allowNewerMinor && server.minor < client.minor) return false
  if (options.minimum !== undefined && compareApiVersions(server.raw, options.minimum) < 0) return false
  return true
}

export function normalizeVersionPolicy(policy: Partial<VersionPolicy> = {}): VersionPolicy {
  const requested = formatApiVersion(parseApiVersion(policy.requested ?? ZYRA_API_VERSION), { omitPatch: true })
  const minimum = policy.minimum
    ? formatApiVersion(parseApiVersion(policy.minimum), { omitPatch: true })
    : undefined
  if (minimum && compareApiVersions(minimum, requested) > 0) {
    throw new TypeError(`Minimum API version ${minimum} exceeds requested version ${requested}`)
  }
  return {
    requested,
    minimum,
    allowNewerMinor: policy.allowNewerMinor ?? false,
    requireResponseHeader: policy.requireResponseHeader ?? true,
  }
}

export function versionRequestHeaders(policy: VersionPolicy): Record<string, string> {
  const normalized = normalizeVersionPolicy(policy)
  const result: Record<string, string> = {
    [ZYRA_API_VERSION_HEADER]: normalized.requested,
  }
  if (normalized.minimum) result[ZYRA_API_MIN_VERSION_HEADER] = normalized.minimum
  return result
}

export function responseApiVersion(headers: Headers): string | undefined {
  const value = headers.get(ZYRA_API_VERSION_HEADER)
  if (!value?.trim()) return undefined
  return formatApiVersion(parseApiVersion(value), { omitPatch: true })
}

export function assertResponseVersion(
  headers: Headers,
  policy: VersionPolicy,
  context: { operation?: string; requestId?: string } = {},
): string {
  const normalized = normalizeVersionPolicy(policy)
  const provided = responseApiVersion(headers)
  if (!provided) {
    if (!normalized.requireResponseHeader) return normalized.requested
    throw new ResponseValidationError(
      `Zyra API response omitted ${ZYRA_API_VERSION_HEADER}.`,
      { requested_version: normalized.requested },
      context,
    )
  }
  if (
    !apiVersionsCompatible(normalized.requested, provided, {
      allowNewerMinor: normalized.allowNewerMinor,
      minimum: normalized.minimum,
    })
  ) {
    throw new ApiVersionMismatchError(normalized.requested, [provided], context)
  }
  return provided
}

export function chooseCompatibleVersion(
  supported: readonly string[],
  requested: string,
  options: { minimum?: string; allowNewerMinor?: boolean } = {},
): string | undefined {
  const normalized = [...new Set(supported.map((value) => formatApiVersion(parseApiVersion(value), { omitPatch: true })))]
  const compatible = normalized.filter((candidate) =>
    apiVersionsCompatible(requested, candidate, {
      minimum: options.minimum,
      allowNewerMinor: options.allowNewerMinor,
    }),
  )
  compatible.sort((left, right) => compareApiVersions(right, left))
  return compatible[0]
}

export class VersionNegotiator {
  #policy: VersionPolicy
  #observed = new Map<string, string>()

  constructor(policy: Partial<VersionPolicy> = {}) {
    this.#policy = normalizeVersionPolicy(policy)
  }

  get policy(): VersionPolicy {
    return { ...this.#policy }
  }

  update(policy: Partial<VersionPolicy>): void {
    this.#policy = normalizeVersionPolicy({ ...this.#policy, ...policy })
    this.#observed.clear()
  }

  requestHeaders(): Record<string, string> {
    return versionRequestHeaders(this.#policy)
  }

  validate(origin: string, headers: Headers, context: { operation?: string; requestId?: string } = {}): string {
    const version = assertResponseVersion(headers, this.#policy, context)
    const existing = this.#observed.get(origin)
    if (existing && existing !== version) {
      throw new ApiVersionMismatchError(existing, [version], context, 409)
    }
    this.#observed.set(origin, version)
    return version
  }

  observed(origin: string): string | undefined {
    return this.#observed.get(origin)
  }

  reset(origin?: string): void {
    if (origin) this.#observed.delete(origin)
    else this.#observed.clear()
  }

  snapshot(): Record<string, string> {
    return Object.fromEntries([...this.#observed.entries()].sort(([left], [right]) => left.localeCompare(right)))
  }
}
