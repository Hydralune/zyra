import { createHash, randomBytes, randomUUID } from "node:crypto";
import { EnvelopeValidationError } from "./errors.ts";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function assertJsonValue(value: unknown, path = "$", seen = new Set<object>()): asserts value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new EnvelopeValidationError("JSON number must be finite", { path, value });
    return;
  }
  if (typeof value !== "object") {
    throw new EnvelopeValidationError("value is not JSON serializable", { path, value_type: typeof value });
  }
  if (seen.has(value)) throw new EnvelopeValidationError("cyclic JSON value is forbidden", { path });
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      for (let index = 0; index < value.length; index += 1) assertJsonValue(value[index], `${path}[${index}]`, seen);
      return;
    }
    if (!isPlainObject(value)) {
      throw new EnvelopeValidationError("non-plain object is forbidden in canonical JSON", {
        path,
        constructor: value.constructor?.name ?? "unknown",
      });
    }
    for (const [key, item] of Object.entries(value)) {
      if (!key) throw new EnvelopeValidationError("empty JSON object key is forbidden", { path });
      assertJsonValue(item, `${path}.${key}`, seen);
    }
  } finally {
    seen.delete(value);
  }
}

export function canonicalize(value: JsonValue): JsonValue {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => canonicalize(item));
  const result: Record<string, JsonValue> = {};
  for (const key of Object.keys(value).sort()) result[key] = canonicalize(value[key]!);
  return result;
}

export function canonicalJson(value: unknown): string {
  assertJsonValue(value);
  return JSON.stringify(canonicalize(value));
}

export function canonicalBytes(value: unknown): Buffer {
  return Buffer.from(canonicalJson(value), "utf8");
}

export function sha256Bytes(value: Uint8Array | string): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

export function digestJson(value: unknown): string {
  return sha256Bytes(canonicalBytes(value));
}

export function digestParts(...values: unknown[]): string {
  return digestJson(values);
}

export function byteLength(value: unknown): number {
  return canonicalBytes(value).byteLength;
}

export function cloneJson<T extends JsonValue>(value: T): T {
  return JSON.parse(canonicalJson(value)) as T;
}

export function freezeJson<T extends JsonValue>(value: T): Readonly<T> {
  if (value !== null && typeof value === "object") {
    for (const item of Array.isArray(value) ? value : Object.values(value)) {
      if (item !== null && typeof item === "object" && !Object.isFrozen(item)) freezeJson(item);
    }
    Object.freeze(value);
  }
  return value;
}

export function stableId(prefix: string, ...identity: unknown[]): string {
  const normalized = prefix.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "id";
  return `${normalized}_${digestParts(identity).slice("sha256:".length, "sha256:".length + 24)}`;
}

export function newId(prefix: string): string {
  const normalized = prefix.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "id";
  return `${normalized}_${randomUUID().replaceAll("-", "")}`;
}

export function newToken(bytes = 24): string {
  return randomBytes(bytes).toString("base64url");
}

export function utcNow(): string {
  return new Date().toISOString();
}

export function parseTimestamp(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) throw new EnvelopeValidationError(`${field} is required`, { field });
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) throw new EnvelopeValidationError(`${field} is invalid`, { field, value });
  return timestamp.toISOString();
}

export function requireString(value: unknown, field: string, maxBytes = 1024): string {
  if (typeof value !== "string") throw new EnvelopeValidationError(`${field} must be a string`, { field });
  const normalized = value.trim();
  if (!normalized) throw new EnvelopeValidationError(`${field} must not be empty`, { field });
  const bytes = Buffer.byteLength(normalized, "utf8");
  if (bytes > maxBytes) throw new EnvelopeValidationError(`${field} exceeds byte limit`, { field, bytes, max_bytes: maxBytes });
  return normalized;
}

export function optionalString(value: unknown, field: string, maxBytes = 1024): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  return requireString(value, field, maxBytes);
}

export function requireInteger(value: unknown, field: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum) {
    throw new EnvelopeValidationError(`${field} must be an integer >= ${minimum}`, { field, value });
  }
  return Number(value);
}

export function optionalInteger(value: unknown, field: string, minimum = 0): number | undefined {
  if (value === undefined || value === null) return undefined;
  return requireInteger(value, field, minimum);
}

export function requireFinite(value: unknown, field: string, minimum?: number, maximum?: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new EnvelopeValidationError(`${field} must be finite`, { field, value });
  }
  if (minimum !== undefined && value < minimum) {
    throw new EnvelopeValidationError(`${field} is below minimum`, { field, value, minimum });
  }
  if (maximum !== undefined && value > maximum) {
    throw new EnvelopeValidationError(`${field} is above maximum`, { field, value, maximum });
  }
  return value;
}

export function requireBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw new EnvelopeValidationError(`${field} must be boolean`, { field, value });
  return value;
}

export function requireRecord(value: unknown, field: string): Record<string, JsonValue> {
  if (!isPlainObject(value)) throw new EnvelopeValidationError(`${field} must be an object`, { field });
  assertJsonValue(value, field);
  return value as Record<string, JsonValue>;
}

export function optionalRecord(value: unknown, field: string): Record<string, JsonValue> {
  if (value === undefined || value === null) return {};
  return requireRecord(value, field);
}

export function stringArray(value: unknown, field: string, maximum = 256): string[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new EnvelopeValidationError(`${field} must be an array`, { field });
  if (value.length > maximum) throw new EnvelopeValidationError(`${field} has too many entries`, { field, count: value.length, maximum });
  const output: string[] = [];
  const seen = new Set<string>();
  for (let index = 0; index < value.length; index += 1) {
    const item = requireString(value[index], `${field}[${index}]`, 512);
    if (!seen.has(item)) {
      output.push(item);
      seen.add(item);
    }
  }
  return output;
}

export function encodeCursor(value: Record<string, JsonValue>): string {
  return Buffer.from(canonicalJson(value), "utf8").toString("base64url");
}

export function decodeCursor(value: string): Record<string, JsonValue> {
  try {
    const decoded = JSON.parse(Buffer.from(value, "base64url").toString("utf8"));
    return requireRecord(decoded, "cursor");
  } catch (error) {
    if (error instanceof EnvelopeValidationError) throw error;
    throw new EnvelopeValidationError("cursor is invalid", { cursor: value.slice(0, 64) });
  }
}

export function compareIso(left: string, right: string): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

export function boundedText(value: unknown, maxBytes: number): string {
  const text = typeof value === "string" ? value : String(value ?? "");
  if (Buffer.byteLength(text, "utf8") <= maxBytes) return text;
  const buffer = Buffer.from(text, "utf8");
  let end = Math.min(buffer.byteLength, maxBytes);
  while (end > 0 && (buffer[end]! & 0xc0) === 0x80) end -= 1;
  return buffer.subarray(0, end).toString("utf8");
}

export function normalizeIdentifier(value: unknown, field: string, maximum = 256): string {
  const text = requireString(value, field, maximum);
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/.test(text)) {
    throw new EnvelopeValidationError(`${field} contains forbidden characters`, { field, value: text });
  }
  return text;
}

export function isDigest(value: unknown): value is string {
  return typeof value === "string" && /^sha256:[a-f0-9]{64}$/.test(value);
}
