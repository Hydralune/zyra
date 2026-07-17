import { createHash, randomUUID } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";

const FORBIDDEN_KEYS = new Set(["__proto__", "constructor", "prototype"]);

export function canonicalize(value: unknown): JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("canonical JSON rejects non-finite numbers");
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) return value.map((item) => canonicalize(item));
  if (typeof value === "object") {
    const output: JsonObject = {};
    for (const [key, child] of Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b))) {
      if (FORBIDDEN_KEYS.has(key)) throw new Error(`canonical JSON rejects unsafe key ${key}`);
      if (child === undefined) continue;
      output[key] = canonicalize(child);
    }
    return output;
  }
  throw new Error(`canonical JSON cannot encode ${typeof value}`);
}

export function canonicalString(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

export function digest(value: unknown): string {
  return createHash("sha256").update(canonicalString(value)).digest("hex");
}

export function digestBytes(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

export function deterministicId(prefix: string, value: unknown, length = 24): string {
  const normalized = prefix.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return `${normalized || "id"}-${digest(value).slice(0, Math.max(12, length))}`;
}

export function opaqueId(prefix: string): string {
  return `${prefix}-${randomUUID()}`;
}

export function cloneJson<T>(value: T): T {
  return canonicalize(value) as T;
}

export function jsonObject(value: unknown, label = "value"): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return canonicalize(value) as JsonObject;
}

export function optionalObject(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? canonicalize(value) as JsonObject
    : {};
}

export function stringValue(value: unknown, label: string, allowEmpty = false): string {
  if (typeof value !== "string" || (!allowEmpty && !value.trim())) {
    throw new Error(`${label} must be ${allowEmpty ? "a string" : "a non-empty string"}`);
  }
  return value;
}

export function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.length ? value : null;
}

export function integerValue(value: unknown, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    throw new Error(`${label} must be a safe integer >= ${minimum}`);
  }
  return value as number;
}

export function booleanValue(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${label} must be an array of strings`);
  }
  return [...new Set(value as string[])];
}

export function assertIsoTimestamp(value: string, label: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value) || Number.isNaN(Date.parse(value))) {
    throw new Error(`${label} must be an ISO-8601 UTC timestamp`);
  }
  return value;
}

export function compareTimestamps(left: string, right: string): number {
  return Date.parse(left) - Date.parse(right);
}

export function monotonicNow(previous: string | null, now: () => Date = () => new Date()): string {
  const current = now().toISOString();
  if (!previous || current > previous) return current;
  return new Date(Date.parse(previous) + 1).toISOString();
}

export function redactSecrets(value: unknown): JsonValue {
  const walk = (child: unknown, key = ""): JsonValue => {
    if (/token|secret|password|authorization|cookie|credential|api[-_]?key/i.test(key)) {
      if (child === null) return null;
      return `[redacted:${digest(String(child)).slice(0, 12)}]`;
    }
    if (Array.isArray(child)) return child.map((item) => walk(item));
    if (child && typeof child === "object") {
      const output: JsonObject = {};
      for (const [nestedKey, nested] of Object.entries(child as Record<string, unknown>)) {
        output[nestedKey] = walk(nested, nestedKey);
      }
      return canonicalize(output);
    }
    return canonicalize(child);
  };
  return walk(value);
}

export function constantTimeDigestEquals(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let index = 0; index < left.length; index += 1) {
    mismatch |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return mismatch === 0;
}

export function normalizeName(value: string, maximum = 128): string {
  const normalized = value.trim().normalize("NFKC");
  if (!normalized || normalized.length > maximum) throw new Error(`name length must be between 1 and ${maximum}`);
  if (/\p{C}/u.test(normalized)) throw new Error("name contains control characters");
  return normalized;
}

export function normalizeIdentifier(value: string, label = "identifier"): string {
  const normalized = normalizeName(value, 180).toLowerCase().replace(/[^a-z0-9._:-]+/g, "-");
  if (!/^[a-z0-9][a-z0-9._:-]*$/.test(normalized)) throw new Error(`${label} is invalid`);
  return normalized;
}

export function normalizePattern(value: string): string {
  const normalized = value.trim().normalize("NFKC");
  if (!normalized) return "*";
  if (normalized.length > 2_048) throw new Error("pattern exceeds 2048 characters");
  if (/\0|[\r\n]/.test(normalized)) throw new Error("pattern contains forbidden control characters");
  return normalized;
}

export function wildcardMatches(pattern: string, value: string, caseSensitive = false): boolean {
  const source = caseSensitive ? value : value.toLowerCase();
  const expected = caseSensitive ? pattern : pattern.toLowerCase();
  if (expected === "*" || expected === "") return true;
  let patternIndex = 0;
  let valueIndex = 0;
  let starIndex = -1;
  let retryIndex = -1;
  while (valueIndex < source.length) {
    if (patternIndex < expected.length && (expected[patternIndex] === "?" || expected[patternIndex] === source[valueIndex])) {
      patternIndex += 1;
      valueIndex += 1;
      continue;
    }
    if (patternIndex < expected.length && expected[patternIndex] === "*") {
      starIndex = patternIndex;
      retryIndex = valueIndex;
      patternIndex += 1;
      continue;
    }
    if (starIndex >= 0) {
      patternIndex = starIndex + 1;
      retryIndex += 1;
      valueIndex = retryIndex;
      continue;
    }
    return false;
  }
  while (patternIndex < expected.length && expected[patternIndex] === "*") patternIndex += 1;
  return patternIndex === expected.length;
}

export function deepEqual(left: unknown, right: unknown): boolean {
  return canonicalString(left) === canonicalString(right);
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
      && value !== null
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

export function boundedJson(value: unknown, maximumBytes: number, label: string): JsonValue {
  const canonical = canonicalize(value);
  const bytes = Buffer.byteLength(JSON.stringify(canonical), "utf8");
  if (bytes > maximumBytes) throw new Error(`${label} exceeds ${maximumBytes} UTF-8 bytes`);
  return canonical;
}

export function hashChain(previousHash: string, value: unknown): string {
  return digest({ previousHash, value: canonicalize(value) });
}

export function verifyHashChain<T>(
  rows: readonly T[],
  initialHash: string,
  previous: (row: T) => string,
  current: (row: T) => string,
  payload: (row: T) => unknown,
): string {
  let expectedPrevious = initialHash;
  for (const [index, row] of rows.entries()) {
    if (previous(row) !== expectedPrevious) throw new Error(`hash chain previous hash mismatch at ${index}`);
    const expected = hashChain(expectedPrevious, payload(row));
    if (!constantTimeDigestEquals(expected, current(row))) throw new Error(`hash chain digest mismatch at ${index}`);
    expectedPrevious = expected;
  }
  return expectedPrevious;
}

export function withoutKeys(value: JsonObject, keys: readonly string[]): JsonObject {
  const output = cloneJson(value);
  for (const key of keys) delete output[key];
  return output;
}
