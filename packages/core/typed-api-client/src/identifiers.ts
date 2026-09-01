import {
  IDENTITY_KINDS,
  IDENTITY_PREFIXES,
  MAX_IDENTIFIER_BYTES,
  MAX_IDEMPOTENCY_KEY_BYTES,
  boundedString,
} from "./constants.ts"

export type IdentityKind = (typeof IDENTITY_KINDS)[number]
export type IdentityValue = string & { readonly __identity: unique symbol }

export interface IdentityBinding {
  sessionId?: string
  runId?: string
  taskId?: string
  spanId?: string
  checkpointId?: string
  toolId?: string
  artifactId?: string
  controlCommandId?: string
  requestId?: string
  receiptId?: string
  eventId?: string
}

export interface ParsedIdentity {
  kind: IdentityKind
  prefix: string
  value: string
  timestamp?: number
  entropy?: string
}

const KIND_BY_PREFIX = new Map<string, IdentityKind>(
  Object.entries(IDENTITY_PREFIXES).map(([kind, prefix]) => [prefix, kind as IdentityKind]),
)

const BINDING_KEYS: Record<IdentityKind, keyof IdentityBinding> = {
  session: "sessionId",
  run: "runId",
  task: "taskId",
  span: "spanId",
  checkpoint: "checkpointId",
  tool: "toolId",
  artifact: "artifactId",
  control_command: "controlCommandId",
  request: "requestId",
  receipt: "receiptId",
  event: "eventId",
}

let monotonicTimestamp = 0
let monotonicSequence = 0

function nowMonotonic(): number {
  const now = Date.now()
  if (now > monotonicTimestamp) {
    monotonicTimestamp = now
    monotonicSequence = 0
  } else {
    monotonicSequence += 1
    if (monotonicSequence > 0xfff) {
      monotonicTimestamp += 1
      monotonicSequence = 0
    }
  }
  return monotonicTimestamp
}

function randomBytes(length: number): Uint8Array {
  const result = new Uint8Array(length)
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(result)
    return result
  }
  for (let index = 0; index < length; index += 1) {
    result[index] = Math.floor(Math.random() * 256)
  }
  return result
}

function hex(bytes: Uint8Array): string {
  let result = ""
  for (const value of bytes) result += value.toString(16).padStart(2, "0")
  return result
}

function timestampHex(timestamp: number, sequence: number): string {
  const millis = BigInt(Math.max(0, Math.floor(timestamp)))
  const encoded = millis * 0x1000n + BigInt(sequence & 0xfff)
  return encoded.toString(16).padStart(15, "0")
}

function parseTimestamp(encoded: string): number | undefined {
  if (!/^[0-9a-f]{15}$/i.test(encoded)) return undefined
  try {
    return Number(BigInt(`0x${encoded}`) / 0x1000n)
  } catch {
    return undefined
  }
}

function canonicalPrefix(kind: IdentityKind): string {
  return IDENTITY_PREFIXES[kind]
}

function identityPattern(prefix: string): RegExp {
  return new RegExp(`^${prefix}_[0-9a-f]{15}_[0-9a-f]{20}$`, "i")
}

function legacyIdentityPattern(prefix: string): RegExp {
  return new RegExp(`^${prefix}[_:-][A-Za-z0-9][A-Za-z0-9._:-]{2,240}$`)
}

export function createIdentity(kind: IdentityKind, timestamp = nowMonotonic()): IdentityValue {
  if (!IDENTITY_KINDS.includes(kind)) throw new TypeError(`Unsupported identity kind: ${kind}`)
  const sequence = monotonicSequence
  const prefix = canonicalPrefix(kind)
  const value = `${prefix}_${timestampHex(timestamp, sequence)}_${hex(randomBytes(10))}`
  return value as IdentityValue
}

export function createRequestId(): IdentityValue {
  return createIdentity("request")
}

export function createReceiptId(): IdentityValue {
  return createIdentity("receipt")
}

export function createSessionId(): IdentityValue {
  return createIdentity("session")
}

export function createSpanId(): IdentityValue {
  return createIdentity("span")
}

export function parseIdentity(value: unknown, expected?: IdentityKind): ParsedIdentity {
  let normalized = boundedString(value, MAX_IDENTIFIER_BYTES, expected ? `${expected} id` : "identity")
  if (expected === "session" && /^task:task_[A-Za-z0-9._:-]+$/.test(normalized)) {
    normalized = `session_${normalized.slice("task:".length)}`
  }
  // Product TUI builds before the canonical session generator shipped used
  // this exact bounded alias. Preserve it as an input/binding value so those
  // durable tasks remain resumable, while every new session is canonical.
  const legacyProductSession = expected === "session" && /^product:[0-9a-f]{32}$/i.test(normalized)
  const prefix = normalized.split(/[_:-]/, 1)[0] ?? ""
  const inferred = legacyProductSession ? "session" : KIND_BY_PREFIX.get(prefix)
  const kind = expected ?? inferred
  if (!kind) throw new TypeError(`Unknown identity prefix: ${prefix}`)
  const canonical = canonicalPrefix(kind)
  if (!legacyProductSession && !identityPattern(canonical).test(normalized) && !legacyIdentityPattern(canonical).test(normalized)) {
    throw new TypeError(`Invalid ${kind} identity: ${normalized}`)
  }
  if (expected && prefix !== canonical && !legacyProductSession) {
    throw new TypeError(`Expected ${canonical} identity, received ${prefix}`)
  }
  const parts = normalized.split("_")
  const timestamp = parts.length >= 3 ? parseTimestamp(parts[1] ?? "") : undefined
  return {
    kind,
    prefix: canonical,
    value: normalized,
    timestamp,
    entropy: parts.length >= 3 ? parts.slice(2).join("_") : undefined,
  }
}

export function normalizeIdentity(kind: IdentityKind, value: unknown): string {
  return parseIdentity(value, kind).value
}

export function optionalIdentity(kind: IdentityKind, value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return normalizeIdentity(kind, value)
}

export function isIdentity(kind: IdentityKind, value: unknown): boolean {
  try {
    normalizeIdentity(kind, value)
    return true
  } catch {
    return false
  }
}

export function identityTimestamp(value: unknown, kind?: IdentityKind): number | undefined {
  return parseIdentity(value, kind).timestamp
}

export function compareIdentities(left: unknown, right: unknown, kind?: IdentityKind): number {
  const a = parseIdentity(left, kind)
  const b = parseIdentity(right, kind)
  if (a.value === b.value) return 0
  const aTime = a.timestamp
  const bTime = b.timestamp
  if (aTime !== undefined && bTime !== undefined && aTime !== bTime) return aTime < bTime ? -1 : 1
  return a.value < b.value ? -1 : 1
}

export function identityAgeMs(value: unknown, now = Date.now(), kind?: IdentityKind): number | undefined {
  const timestamp = identityTimestamp(value, kind)
  if (timestamp === undefined) return undefined
  return Math.max(0, now - timestamp)
}

export function bindIdentity(binding: IdentityBinding, kind: IdentityKind, value: unknown): IdentityBinding {
  const key = BINDING_KEYS[kind]
  const normalized = normalizeIdentity(kind, value)
  const current = binding[key]
  if (current && current !== normalized) {
    throw new TypeError(`Identity binding conflict for ${kind}: ${current} != ${normalized}`)
  }
  return { ...binding, [key]: normalized }
}

export function bindIdentities(base: IdentityBinding, additions: Partial<IdentityBinding>): IdentityBinding {
  let result = { ...base }
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    const value = additions[key]
    if (value !== undefined) result = bindIdentity(result, kind, value)
  }
  validateBinding(result)
  return result
}

export function validateBinding(binding: IdentityBinding): IdentityBinding {
  const result: IdentityBinding = {}
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    const value = binding[key]
    if (value !== undefined) result[key] = normalizeIdentity(kind, value)
  }
  if (result.checkpointId && !result.runId) {
    throw new TypeError("checkpoint identity requires run identity")
  }
  if (result.controlCommandId && !result.taskId) {
    throw new TypeError("control command identity requires task identity")
  }
  if (result.toolId && !result.spanId && !result.taskId) {
    throw new TypeError("tool identity requires span or task identity")
  }
  if (result.artifactId && !result.taskId) {
    throw new TypeError("artifact identity requires task identity")
  }
  if (result.receiptId && !result.requestId) {
    throw new TypeError("receipt identity requires request identity")
  }
  return result
}

export function bindingKey(binding: IdentityBinding): string {
  const validated = validateBinding(binding)
  const pairs: string[] = []
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    const value = validated[key]
    if (value) pairs.push(`${kind}=${value}`)
  }
  return pairs.join("|")
}

export function bindingEquals(left: IdentityBinding, right: IdentityBinding): boolean {
  return bindingKey(left) === bindingKey(right)
}

export function bindingContains(container: IdentityBinding, candidate: IdentityBinding): boolean {
  const outer = validateBinding(container)
  const inner = validateBinding(candidate)
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    const value = inner[key]
    if (value !== undefined && outer[key] !== value) return false
  }
  return true
}

export function assertBindingMatches(
  expected: IdentityBinding,
  actual: IdentityBinding,
  label = "identity binding",
): void {
  const left = validateBinding(expected)
  const right = validateBinding(actual)
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    if (left[key] && right[key] && left[key] !== right[key]) {
      throw new TypeError(`${label} mismatch for ${kind}: ${left[key]} != ${right[key]}`)
    }
  }
}

export function pickBinding(binding: IdentityBinding, kinds: readonly IdentityKind[]): IdentityBinding {
  const result: IdentityBinding = {}
  for (const kind of kinds) {
    const key = BINDING_KEYS[kind]
    const value = binding[key]
    if (value) result[key] = normalizeIdentity(kind, value)
  }
  return result
}

export function bindingFromUnknown(value: unknown): IdentityBinding {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Identity binding must be an object")
  }
  const record = value as Record<string, unknown>
  const aliases: Record<keyof IdentityBinding, string[]> = {
    sessionId: ["sessionId", "session_id", "sessionID"],
    runId: ["runId", "run_id", "runID"],
    taskId: ["taskId", "task_id", "taskID"],
    spanId: ["spanId", "span_id", "spanID"],
    checkpointId: ["checkpointId", "checkpoint_id", "checkpointID"],
    toolId: ["toolId", "tool_id", "toolID", "tool_call_id", "toolCallId"],
    artifactId: ["artifactId", "artifact_id", "artifactID"],
    controlCommandId: ["controlCommandId", "control_command_id", "command_id", "commandId"],
    requestId: ["requestId", "request_id", "requestID"],
    receiptId: ["receiptId", "receipt_id", "receiptID"],
    eventId: ["eventId", "event_id", "eventID"],
  }
  const result: IdentityBinding = {}
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    for (const alias of aliases[key]) {
      if (record[alias] !== undefined && record[alias] !== null && record[alias] !== "") {
        try {
          result[key] = normalizeIdentity(kind, record[alias])
          break
        } catch {
          // Payloads may use generic keys such as receipt_id for a
          // domain-specific execution receipt. Only bind values whose prefix
          // proves that they belong to this transport identity domain.
        }
      }
    }
  }
  return validateBinding(result)
}

export function bindingToSnakeCase(binding: IdentityBinding): Record<string, string> {
  const value = validateBinding(binding)
  const result: Record<string, string> = {}
  if (value.sessionId) result.session_id = value.sessionId
  if (value.runId) result.run_id = value.runId
  if (value.taskId) result.task_id = value.taskId
  if (value.spanId) result.span_id = value.spanId
  if (value.checkpointId) result.checkpoint_id = value.checkpointId
  if (value.toolId) result.tool_id = value.toolId
  if (value.artifactId) result.artifact_id = value.artifactId
  if (value.controlCommandId) result.control_command_id = value.controlCommandId
  if (value.requestId) result.request_id = value.requestId
  if (value.receiptId) result.receipt_id = value.receiptId
  if (value.eventId) result.event_id = value.eventId
  return result
}

export function bindingToCamelCase(binding: IdentityBinding): Record<string, string> {
  const value = validateBinding(binding)
  const result: Record<string, string> = {}
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    const entry = value[key]
    if (entry) result[key] = entry
  }
  return result
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`
  const record = value as Record<string, unknown>
  const entries = Object.keys(record)
    .sort()
    .filter((key) => record[key] !== undefined)
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
  return `{${entries.join(",")}}`
}

function fnv1a64(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let hash = 0xcbf29ce484222325n
  for (const byte of bytes) {
    hash ^= BigInt(byte)
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return hash.toString(16).padStart(16, "0")
}

export function createIdempotencyKey(
  operation: string,
  binding: IdentityBinding,
  payload: unknown,
  nonce?: string,
): string {
  const normalizedOperation = boundedString(operation, 128, "operation")
  const validated = validateBinding(binding)
  const material = canonicalJson({
    operation: normalizedOperation,
    binding: bindingToSnakeCase(validated),
    payload,
    nonce: nonce || undefined,
  })
  const scope = validated.taskId ?? validated.runId ?? validated.sessionId ?? "global"
  const digest = fnv1a64(material)
  return boundedString(`${normalizedOperation}:${scope}:${digest}`, MAX_IDEMPOTENCY_KEY_BYTES, "idempotency key")
}

export function normalizeIdempotencyKey(value: unknown): string {
  const key = boundedString(value, MAX_IDEMPOTENCY_KEY_BYTES, "idempotency key")
  if (!/^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{2,255}$/.test(key)) {
    throw new TypeError("Idempotency key contains unsupported characters")
  }
  return key
}

export function redactIdentity(value: unknown): string {
  if (typeof value !== "string" || value.length < 12) return "[redacted]"
  return `${value.slice(0, 8)}…${value.slice(-4)}`
}

export function summarizeBinding(binding: IdentityBinding): string {
  const validated = validateBinding(binding)
  const parts: string[] = []
  for (const kind of IDENTITY_KINDS) {
    const key = BINDING_KEYS[kind]
    const value = validated[key]
    if (value) parts.push(`${kind}:${redactIdentity(value)}`)
  }
  return parts.join(" ")
}

export class IdentityRegistry {
  readonly #values = new Map<IdentityKind, Set<string>>()
  readonly #relationships = new Map<string, Set<string>>()

  constructor(seed: readonly IdentityBinding[] = []) {
    for (const binding of seed) this.remember(binding)
  }

  remember(binding: IdentityBinding): IdentityBinding {
    const validated = validateBinding(binding)
    const present: string[] = []
    for (const kind of IDENTITY_KINDS) {
      const key = BINDING_KEYS[kind]
      const value = validated[key]
      if (!value) continue
      const set = this.#values.get(kind) ?? new Set<string>()
      set.add(value)
      this.#values.set(kind, set)
      present.push(`${kind}:${value}`)
    }
    for (const source of present) {
      const related = this.#relationships.get(source) ?? new Set<string>()
      for (const target of present) if (target !== source) related.add(target)
      this.#relationships.set(source, related)
    }
    return validated
  }

  has(kind: IdentityKind, value: unknown): boolean {
    const normalized = normalizeIdentity(kind, value)
    return this.#values.get(kind)?.has(normalized) === true
  }

  related(kind: IdentityKind, value: unknown): IdentityBinding {
    const normalized = normalizeIdentity(kind, value)
    const source = `${kind}:${normalized}`
    const result: IdentityBinding = { [BINDING_KEYS[kind]]: normalized }
    for (const target of this.#relationships.get(source) ?? []) {
      const separator = target.indexOf(":")
      const targetKind = target.slice(0, separator) as IdentityKind
      const targetValue = target.slice(separator + 1)
      const key = BINDING_KEYS[targetKind]
      const existing = result[key]
      if (!existing) result[key] = targetValue
    }
    return validateBinding(result)
  }

  forget(kind: IdentityKind, value: unknown): boolean {
    const normalized = normalizeIdentity(kind, value)
    const source = `${kind}:${normalized}`
    const removed = this.#values.get(kind)?.delete(normalized) === true
    this.#relationships.delete(source)
    for (const relations of this.#relationships.values()) relations.delete(source)
    return removed
  }

  values(kind: IdentityKind): string[] {
    return [...(this.#values.get(kind) ?? [])].sort((left, right) => compareIdentities(left, right, kind))
  }

  snapshot(): Record<IdentityKind, string[]> {
    const result = {} as Record<IdentityKind, string[]>
    for (const kind of IDENTITY_KINDS) result[kind] = this.values(kind)
    return result
  }

  clear(): void {
    this.#values.clear()
    this.#relationships.clear()
  }
}
