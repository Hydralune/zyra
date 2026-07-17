import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";

const unsafeKeys = new Set(["__proto__", "constructor", "prototype"]);

export class McpCanonicalError extends Error {
  readonly path: string;

  constructor(message: string, path: string) {
    super(`${message} at ${path || "$"}`);
    this.name = "McpCanonicalError";
    this.path = path || "$";
  }
}

export function canonicalJson(value: unknown, path = "$", seen = new Set<object>()): JsonValue {
  if (value === null) return null;
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new McpCanonicalError("non-finite number", path);
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) {
    if (seen.has(value)) throw new McpCanonicalError("cyclic array", path);
    seen.add(value);
    const output = value.map((item, index) => canonicalJson(item, `${path}[${index}]`, seen));
    seen.delete(value);
    return output;
  }
  if (typeof value === "object") {
    const source = value as Record<string, unknown>;
    if (seen.has(source)) throw new McpCanonicalError("cyclic object", path);
    seen.add(source);
    const output: JsonObject = {};
    for (const key of Object.keys(source).sort((left, right) => left.localeCompare(right))) {
      if (unsafeKeys.has(key)) throw new McpCanonicalError(`unsafe key ${key}`, path);
      const child = source[key];
      if (child === undefined) continue;
      output[key] = canonicalJson(child, `${path}.${key}`, seen);
    }
    seen.delete(source);
    return output;
  }
  throw new McpCanonicalError(`unsupported value ${typeof value}`, path);
}

export function canonicalObject(value: unknown, label = "value"): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new McpCanonicalError(`${label} must be an object`, "$ ");
  }
  return canonicalJson(value) as JsonObject;
}

export function canonicalString(value: unknown): string {
  return JSON.stringify(canonicalJson(value));
}

export function sha256(value: unknown): string {
  return createHash("sha256").update(canonicalString(value), "utf8").digest("hex");
}

export function sha256Text(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function sha256Bytes(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

export function deterministicMcpId(prefix: string, value: unknown, width = 24): string {
  const normalized = prefix
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "mcp";
  return `${normalized}-${sha256(value).slice(0, Math.max(12, width))}`;
}

export function randomMcpSecret(bytes = 32): string {
  if (!Number.isSafeInteger(bytes) || bytes < 16 || bytes > 256) {
    throw new McpCanonicalError("secret byte count must be between 16 and 256", "bytes");
  }
  return randomBytes(bytes).toString("base64url");
}

export function cloneJson<T>(value: T): T {
  return canonicalJson(value) as T;
}

export function mergeJson(base: JsonObject, patch: JsonObject): JsonObject {
  const output = cloneJson(base);
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) {
      delete output[key];
      continue;
    }
    const current = output[key];
    if (
      current !== null
      && typeof current === "object"
      && !Array.isArray(current)
      && typeof value === "object"
      && !Array.isArray(value)
    ) {
      output[key] = mergeJson(current as JsonObject, value as JsonObject);
      continue;
    }
    output[key] = cloneJson(value);
  }
  return output;
}

export function constantTimeTextEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  if (leftBytes.length !== rightBytes.length) return false;
  return timingSafeEqual(leftBytes, rightBytes);
}

export function normalizeText(value: unknown, label: string, maximum = 16_384): string {
  if (typeof value !== "string") throw new McpCanonicalError(`${label} must be a string`, label);
  const normalized = value.normalize("NFC");
  if (normalized.length > maximum) throw new McpCanonicalError(`${label} exceeds ${maximum} characters`, label);
  if (/\0/.test(normalized)) throw new McpCanonicalError(`${label} contains NUL`, label);
  return normalized;
}

export function requiredText(value: unknown, label: string, maximum = 16_384): string {
  const normalized = normalizeText(value, label, maximum).trim();
  if (!normalized) throw new McpCanonicalError(`${label} is required`, label);
  return normalized;
}

export function optionalText(value: unknown, label: string, maximum = 16_384): string | null {
  if (value === undefined || value === null || value === "") return null;
  return normalizeText(value, label, maximum);
}

export function identifier(value: unknown, label: string, maximum = 180): string {
  const text = requiredText(value, label, maximum).normalize("NFKC");
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/.test(text)) {
    throw new McpCanonicalError(`${label} contains unsupported characters`, label);
  }
  return text;
}

export function uriText(value: unknown, label: string): string {
  const text = requiredText(value, label, 32_768);
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    throw new McpCanonicalError(`${label} must be an absolute URI`, label);
  }
  if (!parsed.protocol || parsed.username || parsed.password) {
    throw new McpCanonicalError(`${label} is not an allowed absolute URI`, label);
  }
  return parsed.toString();
}

export function httpUrl(value: unknown, label: string): string {
  const text = uriText(value, label);
  const parsed = new URL(text);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new McpCanonicalError(`${label} must use http or https`, label);
  }
  return parsed.toString();
}

export function nonNegativeInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new McpCanonicalError(`${label} must be a non-negative safe integer`, label);
  }
  return value as number;
}

export function positiveInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) {
    throw new McpCanonicalError(`${label} must be a positive safe integer`, label);
  }
  return value as number;
}

export function boundedInteger(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw new McpCanonicalError(`${label} must be an integer in [${minimum}, ${maximum}]`, label);
  }
  return value as number;
}

export function booleanValue(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function stringList(value: unknown, label: string, maximum = 4_096): string[] {
  if (!Array.isArray(value)) throw new McpCanonicalError(`${label} must be an array`, label);
  if (value.length > maximum) throw new McpCanonicalError(`${label} exceeds ${maximum} entries`, label);
  const output: string[] = [];
  const seen = new Set<string>();
  for (const [index, item] of value.entries()) {
    const text = requiredText(item, `${label}[${index}]`);
    if (seen.has(text)) continue;
    seen.add(text);
    output.push(text);
  }
  return output;
}

export function objectList(value: unknown, label: string, maximum = 4_096): JsonObject[] {
  if (!Array.isArray(value)) throw new McpCanonicalError(`${label} must be an array`, label);
  if (value.length > maximum) throw new McpCanonicalError(`${label} exceeds ${maximum} entries`, label);
  return value.map((item, index) => canonicalObject(item, `${label}[${index}]`));
}

export function enumValue<T extends string>(
  value: unknown,
  allowed: readonly T[],
  label: string,
): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw new McpCanonicalError(`${label} must be one of ${allowed.join(", ")}`, label);
  }
  return value as T;
}

export function optionalEnum<T extends string>(
  value: unknown,
  allowed: readonly T[],
  fallback: T,
  label: string,
): T {
  if (value === undefined || value === null || value === "") return fallback;
  return enumValue(value, allowed, label);
}

export function assertIsoTimestamp(value: unknown, label: string): string {
  const text = requiredText(value, label, 64);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(text)) {
    throw new McpCanonicalError(`${label} must be a UTC ISO timestamp`, label);
  }
  if (Number.isNaN(Date.parse(text))) throw new McpCanonicalError(`${label} is not a valid timestamp`, label);
  return text;
}

export function redactMcpSecrets(value: unknown): JsonValue {
  const walk = (child: unknown, key: string): JsonValue => {
    if (/token|secret|password|authorization|cookie|credential|api[-_]?key/i.test(key)) {
      if (child === undefined || child === null) return null;
      return `[redacted:${sha256Text(String(child)).slice(0, 12)}]`;
    }
    if (Array.isArray(child)) return child.map((entry) => walk(entry, key));
    if (child !== null && typeof child === "object") {
      const output: JsonObject = {};
      for (const [nestedKey, nestedValue] of Object.entries(child as Record<string, unknown>)) {
        output[nestedKey] = walk(nestedValue, nestedKey);
      }
      return canonicalJson(output);
    }
    return canonicalJson(child);
  };
  return walk(value, "$");
}

export function byteLength(value: unknown): number {
  return Buffer.byteLength(canonicalString(value), "utf8");
}

export function boundedJson(value: unknown, maximumBytes: number, label: string): JsonValue {
  const canonical = canonicalJson(value);
  const length = byteLength(canonical);
  if (length > maximumBytes) {
    throw new McpCanonicalError(`${label} exceeds ${maximumBytes} bytes (received ${length})`, label);
  }
  return canonical;
}

export function compareRevision(left: number, right: number): -1 | 0 | 1 {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

export function sortedUnique<T>(values: readonly T[], key: (value: T) => string): T[] {
  const byKey = new Map<string, T>();
  for (const value of values) byKey.set(key(value), value);
  return [...byKey.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([, value]) => value);
}

export function hashChain(previousHash: string, value: unknown): string {
  return sha256({ previous_hash: previousHash, value: canonicalJson(value) });
}

export function verifyHashChain<T>(
  rows: readonly T[],
  initialHash: string,
  previous: (row: T) => string,
  current: (row: T) => string,
  payload: (row: T) => unknown,
): string {
  let head = initialHash;
  for (const [index, row] of rows.entries()) {
    if (!constantTimeTextEqual(previous(row), head)) {
      throw new McpCanonicalError(`hash chain previous mismatch at row ${index}`, "rows");
    }
    const expected = hashChain(head, payload(row));
    if (!constantTimeTextEqual(current(row), expected)) {
      throw new McpCanonicalError(`hash chain digest mismatch at row ${index}`, "rows");
    }
    head = expected;
  }
  return head;
}

export function monotonicNow(previous: string | null, now: () => Date = () => new Date()): string {
  const current = now().toISOString();
  if (!previous || current > previous) return current;
  return new Date(Date.parse(previous) + 1).toISOString();
}

export function mapObject<T>(value: Record<string, T>, map: (entry: T, key: string) => JsonValue): JsonObject {
  const output: JsonObject = {};
  for (const key of Object.keys(value).sort()) output[key] = map(value[key], key);
  return output;
}

export function parseJsonObject(text: string, label: string): JsonObject {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw new McpCanonicalError(
      `${label} is invalid JSON: ${error instanceof Error ? error.message : String(error)}`,
      label,
    );
  }
  return canonicalObject(value, label);
}

export function isJsonObject(value: unknown): value is JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  try {
    canonicalObject(value);
    return true;
  } catch {
    return false;
  }
}
