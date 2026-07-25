const FORBIDDEN_KEYS = new Set(["__proto__", "prototype", "constructor"])
const SECRET_KEY =
  /(?:^|[_-])(token|secret|password|passwd|authorization|cookie|credential|api[_-]?key|private[_-]?key|access[_-]?key)(?:$|[_-])/i

export function canonicalPermissionValue(
  value: unknown,
  seen = new Set<object>(),
): unknown {
  if (value === null) return null
  if (typeof value === "string" || typeof value === "boolean") return value
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("Permission canonical JSON rejects non-finite numbers.")
    }
    return Object.is(value, -0) ? 0 : value
  }
  if (typeof value === "bigint") return value.toString()
  if (value === undefined) return undefined
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) {
      throw new TypeError("Permission canonical JSON rejects invalid dates.")
    }
    return value.toISOString()
  }
  if (Array.isArray(value)) {
    if (seen.has(value)) {
      throw new TypeError("Permission canonical JSON rejects cyclic arrays.")
    }
    seen.add(value)
    const result = value.map((entry) => {
      const normalized = canonicalPermissionValue(entry, seen)
      return normalized === undefined ? null : normalized
    })
    seen.delete(value)
    return result
  }
  if (typeof value === "object") {
    if (seen.has(value)) {
      throw new TypeError("Permission canonical JSON rejects cyclic objects.")
    }
    seen.add(value)
    const result: Record<string, unknown> = {}
    const entries = Object.entries(value as Record<string, unknown>).sort(
      ([left], [right]) => left.localeCompare(right),
    )
    for (const [key, child] of entries) {
      if (FORBIDDEN_KEYS.has(key)) {
        throw new TypeError(`Permission canonical JSON rejects key ${key}.`)
      }
      const normalized = canonicalPermissionValue(child, seen)
      if (normalized !== undefined) result[key] = normalized
    }
    seen.delete(value)
    return result
  }
  throw new TypeError(
    `Permission canonical JSON cannot encode ${typeof value}.`,
  )
}

export function canonicalPermissionJson(value: unknown): string {
  const serialized = JSON.stringify(canonicalPermissionValue(value))
  if (serialized === undefined) {
    throw new TypeError("Permission canonical JSON produced undefined.")
  }
  return serialized
}

export async function sha256PermissionValue(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalPermissionJson(value))
  const subtle = globalThis.crypto?.subtle
  if (subtle) {
    const result = await subtle.digest("SHA-256", bytes)
    return hex(new Uint8Array(result))
  }
  return sha256Fallback(bytes)
}

export function stablePermissionId(
  prefix: string,
  ...parts: readonly unknown[]
): string {
  const safePrefix =
    String(prefix || "permission")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "permission"
  const source = canonicalPermissionJson(parts)
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (const byte of new TextEncoder().encode(source)) {
    first = Math.imul(first ^ byte, 0x01000193) >>> 0
    second ^= byte + 0x9e3779b9 + ((second << 6) >>> 0) + (second >>> 2)
    second >>>= 0
  }
  const suffix =
    first.toString(16).padStart(8, "0")
    + second.toString(16).padStart(8, "0")
  return `${safePrefix}_${suffix}`
}

export function freshPermissionId(
  prefix: string,
  now: Date = new Date(),
): string {
  const random = globalThis.crypto?.randomUUID?.()
  if (random) return identifier(`${prefix}_${random}`, prefix)
  return stablePermissionId(prefix, now.toISOString(), Math.random())
}

export function identifier(value: unknown, label: string): string {
  const rendered = boundedPermissionText(value, {
    label,
    maximumBytes: 512,
    allowEmpty: false,
    singleLine: true,
  })
  if (
    rendered === "."
    || rendered === ".."
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$/.test(rendered)
  ) {
    throw new TypeError(`${label} contains an invalid identifier.`)
  }
  return rendered
}

export function sha256Digest(value: unknown, label: string): string {
  const rendered = boundedPermissionText(value, {
    label,
    maximumBytes: 64,
    allowEmpty: false,
    singleLine: true,
  }).toLowerCase()
  if (!/^[0-9a-f]{64}$/.test(rendered)) {
    throw new TypeError(`${label} must be a SHA-256 hex digest.`)
  }
  return rendered
}

export function nonNegativeRevision(value: unknown, label: string): number {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim()
        ? Number(value)
        : Number.NaN
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new TypeError(`${label} must be a non-negative safe integer.`)
  }
  return parsed
}

export function isoTimestamp(
  value: unknown,
  label: string,
  fallback?: Date,
): string {
  if (
    (value === undefined || value === null || value === "")
    && fallback
  ) {
    return fallback.toISOString()
  }
  const rendered = boundedPermissionText(value, {
    label,
    maximumBytes: 128,
    allowEmpty: false,
    singleLine: true,
  })
  const timestamp = Date.parse(rendered)
  if (!Number.isFinite(timestamp)) {
    throw new TypeError(`${label} must be an ISO-8601 timestamp.`)
  }
  return new Date(timestamp).toISOString()
}

export function boundedPermissionText(
  value: unknown,
  options: {
    label: string
    maximumBytes: number
    allowEmpty?: boolean
    singleLine?: boolean
  },
): string {
  if (typeof value !== "string") {
    if (value === undefined || value === null) {
      if (options.allowEmpty) return ""
    }
    throw new TypeError(`${options.label} must be a string.`)
  }
  const rendered = value.trim()
  if (!rendered && !options.allowEmpty) {
    throw new TypeError(`${options.label} must not be empty.`)
  }
  if (options.singleLine && /[\u0000\r\n]/.test(rendered)) {
    throw new TypeError(`${options.label} must be a single line.`)
  }
  if (new TextEncoder().encode(rendered).byteLength > options.maximumBytes) {
    throw new TypeError(
      `${options.label} exceeds ${options.maximumBytes} bytes.`,
    )
  }
  return rendered
}

export function optionalPermissionText(
  value: unknown,
  label: string,
  maximumBytes = 4_096,
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return boundedPermissionText(value, {
    label,
    maximumBytes,
    allowEmpty: true,
  })
}

export function permissionRecord(
  value: unknown,
  label: string,
): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object.`)
  }
  return value as Record<string, unknown>
}

export function optionalPermissionRecord(
  value: unknown,
): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

export function permissionArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

export function secretFieldName(key: string): boolean {
  return SECRET_KEY.test(
    key
      .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
      .replace(/\s+/g, "_"),
  )
}

export function safePermissionClone<T>(value: T): T {
  return canonicalPermissionValue(value) as T
}

export function comparePermissionTime(
  left: string | undefined,
  right: string | undefined,
): number {
  const leftMs = left ? Date.parse(left) : Number.NaN
  const rightMs = right ? Date.parse(right) : Number.NaN
  if (Number.isFinite(leftMs) && Number.isFinite(rightMs)) {
    return leftMs - rightMs
  }
  if (Number.isFinite(leftMs)) return -1
  if (Number.isFinite(rightMs)) return 1
  return String(left ?? "").localeCompare(String(right ?? ""))
}

export function secondsUntil(value: string, now: Date): number {
  const delta = Date.parse(value) - now.getTime()
  if (!Number.isFinite(delta)) return 0
  return Math.max(0, Math.ceil(delta / 1_000))
}

function hex(value: Uint8Array): string {
  return [...value]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
}

function sha256Fallback(bytes: Uint8Array): string {
  const words = new Uint32Array(64)
  const constants = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ])
  const bitLength = bytes.length * 8
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64
  const padded = new Uint8Array(paddedLength)
  padded.set(bytes)
  padded[bytes.length] = 0x80
  const view = new DataView(padded.buffer)
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 2 ** 32), false)
  view.setUint32(paddedLength - 4, bitLength >>> 0, false)
  let h0 = 0x6a09e667
  let h1 = 0xbb67ae85
  let h2 = 0x3c6ef372
  let h3 = 0xa54ff53a
  let h4 = 0x510e527f
  let h5 = 0x9b05688c
  let h6 = 0x1f83d9ab
  let h7 = 0x5be0cd19
  const rotate = (value: number, count: number) =>
    (value >>> count) | (value << (32 - count))
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + index * 4, false)
    }
    for (let index = 16; index < 64; index += 1) {
      const x = words[index - 15]!
      const y = words[index - 2]!
      const s0 = rotate(x, 7) ^ rotate(x, 18) ^ (x >>> 3)
      const s1 = rotate(y, 17) ^ rotate(y, 19) ^ (y >>> 10)
      words[index] =
        (words[index - 16]! + s0 + words[index - 7]! + s1) >>> 0
    }
    let a = h0
    let b = h1
    let c = h2
    let d = h3
    let e = h4
    let f = h5
    let g = h6
    let h = h7
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)
      const choice = (e & f) ^ (~e & g)
      const t1 = (h + sum1 + choice + constants[index]! + words[index]!) >>> 0
      const sum0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)
      const majority = (a & b) ^ (a & c) ^ (b & c)
      const t2 = (sum0 + majority) >>> 0
      h = g
      g = f
      f = e
      e = (d + t1) >>> 0
      d = c
      c = b
      b = a
      a = (t1 + t2) >>> 0
    }
    h0 = (h0 + a) >>> 0
    h1 = (h1 + b) >>> 0
    h2 = (h2 + c) >>> 0
    h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0
    h5 = (h5 + f) >>> 0
    h6 = (h6 + g) >>> 0
    h7 = (h7 + h) >>> 0
  }
  return [h0, h1, h2, h3, h4, h5, h6, h7]
    .map((word) => word.toString(16).padStart(8, "0"))
    .join("")
}
