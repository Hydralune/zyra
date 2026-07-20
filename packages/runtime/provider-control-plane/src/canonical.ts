import { createHash, randomUUID } from "node:crypto";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { readonly [key: string]: JsonValue };
export type JsonRecord = { readonly [key: string]: JsonValue };

export interface Clock {
  now(): number;
}

export interface IdFactory {
  next(prefix: string): string;
}

export class SystemClock implements Clock {
  now(): number {
    return Date.now();
  }
}

export class RandomIdFactory implements IdFactory {
  next(prefix: string): string {
    assertIdentifier(prefix, "prefix");
    return `${prefix}_${randomUUID().replaceAll("-", "")}`;
  }
}

export class SequenceIdFactory implements IdFactory {
  private value = 0;
  private readonly seed: string;

  constructor(seed = "test") {
    assertIdentifier(seed, "seed");
    this.seed = seed;
  }

  next(prefix: string): string {
    assertIdentifier(prefix, "prefix");
    this.value += 1;
    return `${prefix}_${this.seed}_${String(this.value).padStart(6, "0")}`;
  }
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

export function digestJson(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}

export function digestText(value: string): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

export function fingerprintSecret(value: string): string {
  const digest = createHash("sha256").update(value).digest("hex");
  return `sha256:${digest.slice(0, 16)}`;
}

export function canonicalize(value: unknown): JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("canonical JSON rejects non-finite numbers");
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) return value.map((item) => canonicalize(item));
  if (typeof value === "object") {
    const output: Record<string, JsonValue> = {};
    for (const key of Object.keys(value).sort(compareStrings)) {
      const item = (value as Record<string, unknown>)[key];
      if (item !== undefined) output[key] = canonicalize(item);
    }
    return output;
  }
  throw new TypeError(`unsupported canonical JSON value: ${typeof value}`);
}

export function deepClone<T>(value: T): T {
  return structuredClone(value);
}

export function deepFreeze<T>(value: T): Readonly<T> {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
  }
  return value;
}

export function uniqueSorted(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort(compareStrings);
}

export function compareStrings(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function compareNumbers(left: number, right: number): number {
  return left - right;
}

export function assertNonEmpty(value: string, name: string): void {
  if (typeof value !== "string" || value.trim() === "") throw new TypeError(`${name} must be a non-empty string`);
}

export function assertIdentifier(value: string, name: string): void {
  assertNonEmpty(value, name);
  if (!/^[a-zA-Z][a-zA-Z0-9_.:-]*$/.test(value)) throw new TypeError(`${name} is not a valid identifier`);
}

export function assertUrl(value: string, name: string): URL {
  assertNonEmpty(value, name);
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch (error) {
    throw new TypeError(`${name} must be an absolute URL`, { cause: error });
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new TypeError(`${name} must use http or https`);
  }
  return parsed;
}

export function assertNonNegativeInteger(value: number, name: string): void {
  if (!Number.isSafeInteger(value) || value < 0) throw new TypeError(`${name} must be a non-negative safe integer`);
}

export function assertPositiveInteger(value: number, name: string): void {
  if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError(`${name} must be a positive safe integer`);
}

export function redactHeaders(headers: Readonly<Record<string, string>>): Record<string, string> {
  const output: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    output[name.toLowerCase()] = isSensitiveHeader(name) ? "[REDACTED]" : value;
  }
  return output;
}

export function isSensitiveHeader(name: string): boolean {
  return /authorization|api[-_]key|token|secret|cookie|signature/i.test(name);
}

export function normalizeHeaders(headers: Readonly<Record<string, string>>): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [rawName, rawValue] of Object.entries(headers)) {
    const name = rawName.trim().toLowerCase();
    if (!name) throw new TypeError("header name must not be blank");
    if (/\r|\n/.test(name) || /\r|\n/.test(rawValue)) throw new TypeError("headers must not contain CR or LF");
    result[name] = rawValue;
  }
  return result;
}

export function joinUrl(baseUrl: string, path: string): string {
  const base = assertUrl(baseUrl, "baseUrl");
  const normalizedPath = path.startsWith("/") ? path.slice(1) : path;
  if (!base.pathname.endsWith("/")) base.pathname += "/";
  return new URL(normalizedPath, base).toString();
}

export function parseJsonRecord(value: string, name: string): JsonRecord {
  let decoded: unknown;
  try {
    decoded = JSON.parse(value);
  } catch (error) {
    throw new TypeError(`${name} is not valid JSON`, { cause: error });
  }
  if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) {
    throw new TypeError(`${name} must decode to an object`);
  }
  return canonicalize(decoded) as JsonRecord;
}

export function sleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
  assertNonNegativeInteger(milliseconds, "milliseconds");
  if (signal?.aborted) return Promise.reject(signal.reason ?? new Error("aborted"));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason ?? new Error("aborted"));
      },
      { once: true },
    );
  });
}
