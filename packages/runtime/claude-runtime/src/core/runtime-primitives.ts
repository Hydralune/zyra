import { createHash, randomUUID } from "node:crypto";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | JsonRecord;
export type JsonRecord = { [key: string]: JsonValue };

export interface Clock {
  now(): number;
}

export class SystemClock implements Clock {
  now(): number {
    return Date.now();
  }
}

export class ManualClock implements Clock {
  private current: number;

  constructor(initial = 0) {
    assertFiniteNumber(initial, "initial");
    this.current = initial;
  }

  now(): number {
    return this.current;
  }

  set(value: number): void {
    assertFiniteNumber(value, "value");
    if (value < this.current) {
      throw new RuntimeInvariantError("clock_regression", {
        current: this.current,
        requested: value,
      });
    }
    this.current = value;
  }

  advance(delta: number): number {
    assertFiniteNumber(delta, "delta");
    if (delta < 0) {
      throw new RuntimeInvariantError("negative_clock_delta", { delta });
    }
    this.current += delta;
    return this.current;
  }
}

export interface IdFactory {
  next(namespace: string): string;
}

export class RandomIdFactory implements IdFactory {
  next(namespace: string): string {
    return `${normalizeIdentifier(namespace)}:${randomUUID()}`;
  }
}

export class DeterministicIdFactory implements IdFactory {
  private sequence: number;

  constructor(
    private readonly seed: string,
    initialSequence = 0,
  ) {
    assertNonEmpty(seed, "seed");
    assertNonNegativeInteger(initialSequence, "initialSequence");
    this.sequence = initialSequence;
  }

  next(namespace: string): string {
    this.sequence += 1;
    return `${normalizeIdentifier(namespace)}:${digestText(
      `${this.seed}:${namespace}:${this.sequence}`,
    ).slice(0, 24)}`;
  }

  currentSequence(): number {
    return this.sequence;
  }
}

export class RuntimeInvariantError extends Error {
  readonly code: string;
  readonly detail: JsonRecord;

  constructor(code: string, detail: JsonRecord = {}) {
    super(code);
    this.name = "RuntimeInvariantError";
    this.code = code;
    this.detail = deepClone(detail);
  }
}

export type Outcome<T, E = RuntimeInvariantError> =
  | { ok: true; value: T }
  | { ok: false; error: E };

export function success<T>(value: T): Outcome<T, never> {
  return { ok: true, value };
}

export function failure<E>(error: E): Outcome<never, E> {
  return { ok: false, error };
}

export function assertNonEmpty(value: string, name: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new RuntimeInvariantError("empty_string", { name, value });
  }
  return value;
}

export function assertFiniteNumber(value: number, name: string): number {
  if (!Number.isFinite(value)) {
    throw new RuntimeInvariantError("non_finite_number", { name, value });
  }
  return value;
}

export function assertNonNegativeInteger(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new RuntimeInvariantError("invalid_non_negative_integer", {
      name,
      value,
    });
  }
  return value;
}

export function assertPositiveInteger(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new RuntimeInvariantError("invalid_positive_integer", {
      name,
      value,
    });
  }
  return value;
}

export function assertEnum<T extends string>(
  value: string,
  values: readonly T[],
  name: string,
): T {
  if (!values.includes(value as T)) {
    throw new RuntimeInvariantError("invalid_enum", {
      name,
      value,
      allowed: [...values],
    });
  }
  return value as T;
}

export function normalizeIdentifier(value: string): string {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return assertNonEmpty(normalized, "identifier");
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(asJsonValue(value)));
}

export function digestJson(value: unknown): string {
  return digestText(canonicalJson(value));
}

export function digestText(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function deepClone<T>(value: T): T {
  return structuredClone(value);
}

export function deepFreeze<T>(value: T): Readonly<T> {
  if (value !== null && typeof value === "object") {
    Object.freeze(value);
    for (const child of Object.values(value as Record<string, unknown>)) {
      deepFreeze(child);
    }
  }
  return value;
}

function canonicalize(value: JsonValue): JsonValue {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalize(item));
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value).sort(([left], [right]) =>
      left.localeCompare(right),
    );
    const record: JsonRecord = {};
    for (const [key, child] of entries) {
      record[key] = canonicalize(child);
    }
    return record;
  }
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw new RuntimeInvariantError("non_json_number", { value });
  }
  return value;
}

export function asJsonValue(value: unknown, path = "$"): JsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new RuntimeInvariantError("non_json_number", { path, value });
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((child, index) => asJsonValue(child, `${path}[${index}]`));
  }
  if (typeof value === "object") {
    const record: JsonRecord = {};
    for (const [key, child] of Object.entries(value)) {
      if (child === undefined) {
        continue;
      }
      record[key] = asJsonValue(child, `${path}.${key}`);
    }
    return record;
  }
  throw new RuntimeInvariantError("non_json_value", {
    path,
    type: typeof value,
  });
}

export function jsonRecord(value: unknown, name: string): JsonRecord {
  const converted = asJsonValue(value, name);
  if (
    converted === null ||
    Array.isArray(converted) ||
    typeof converted !== "object"
  ) {
    throw new RuntimeInvariantError("expected_json_record", { name });
  }
  return converted;
}

export function boundedString(
  value: string,
  maximumCharacters: number,
  suffix = "",
): string {
  assertNonNegativeInteger(maximumCharacters, "maximumCharacters");
  if (value.length <= maximumCharacters) {
    return value;
  }
  if (suffix.length >= maximumCharacters) {
    return suffix.slice(0, maximumCharacters);
  }
  return `${value.slice(0, maximumCharacters - suffix.length)}${suffix}`;
}

export function clamp(value: number, minimum: number, maximum: number): number {
  assertFiniteNumber(value, "value");
  assertFiniteNumber(minimum, "minimum");
  assertFiniteNumber(maximum, "maximum");
  if (minimum > maximum) {
    throw new RuntimeInvariantError("invalid_clamp_bounds", {
      minimum,
      maximum,
    });
  }
  return Math.min(maximum, Math.max(minimum, value));
}

export function compareStrings(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function compareNumbers(left: number, right: number): number {
  return left - right;
}

export function uniqueSorted(values: Iterable<string>): string[] {
  return [...new Set(values)].sort(compareStrings);
}

export function partition<T>(
  values: Iterable<T>,
  predicate: (value: T) => boolean,
): [T[], T[]] {
  const accepted: T[] = [];
  const rejected: T[] = [];
  for (const value of values) {
    (predicate(value) ? accepted : rejected).push(value);
  }
  return [accepted, rejected];
}

export interface Page<T> {
  items: T[];
  nextCursor: string | null;
}

export function paginate<T>(
  values: readonly T[],
  limit: number,
  cursor: string | null,
): Page<T> {
  assertPositiveInteger(limit, "limit");
  const start = cursor === null ? 0 : decodeCursor(cursor);
  if (start > values.length) {
    throw new RuntimeInvariantError("cursor_out_of_range", {
      start,
      length: values.length,
    });
  }
  const end = Math.min(values.length, start + limit);
  return {
    items: values.slice(start, end),
    nextCursor: end < values.length ? encodeCursor(end) : null,
  };
}

function encodeCursor(offset: number): string {
  return Buffer.from(String(offset), "utf8").toString("base64url");
}

function decodeCursor(cursor: string): number {
  try {
    const value = Number.parseInt(
      Buffer.from(cursor, "base64url").toString("utf8"),
      10,
    );
    return assertNonNegativeInteger(value, "cursor");
  } catch (error) {
    throw new RuntimeInvariantError("invalid_cursor", {
      cursor,
      cause: error instanceof Error ? error.message : String(error),
    });
  }
}

export class RingBuffer<T> {
  private readonly values: T[] = [];

  constructor(private readonly capacity: number) {
    assertPositiveInteger(capacity, "capacity");
  }

  push(value: T): T | null {
    this.values.push(value);
    return this.values.length > this.capacity ? (this.values.shift() ?? null) : null;
  }

  toArray(): T[] {
    return [...this.values];
  }

  clear(): void {
    this.values.length = 0;
  }

  get length(): number {
    return this.values.length;
  }
}

export class AsyncMutex {
  private tail: Promise<void> = Promise.resolve();

  async runExclusive<T>(operation: () => Promise<T> | T): Promise<T> {
    let release: (() => void) | undefined;
    const current = new Promise<void>((resolve) => {
      release = resolve;
    });
    const previous = this.tail;
    this.tail = previous.then(() => current);
    await previous;
    try {
      return await operation();
    } finally {
      release?.();
    }
  }
}

export class KeyedMutex {
  private readonly mutexes = new Map<string, AsyncMutex>();
  private readonly references = new Map<string, number>();

  async runExclusive<T>(
    key: string,
    operation: () => Promise<T> | T,
  ): Promise<T> {
    assertNonEmpty(key, "key");
    const mutex = this.mutexes.get(key) ?? new AsyncMutex();
    this.mutexes.set(key, mutex);
    this.references.set(key, (this.references.get(key) ?? 0) + 1);
    try {
      return await mutex.runExclusive(operation);
    } finally {
      const remaining = (this.references.get(key) ?? 1) - 1;
      if (remaining === 0) {
        this.references.delete(key);
        this.mutexes.delete(key);
      } else {
        this.references.set(key, remaining);
      }
    }
  }
}

export interface Deadline {
  startedAt: number;
  expiresAt: number;
}

export function createDeadline(
  clock: Clock,
  timeoutMilliseconds: number,
): Deadline {
  assertNonNegativeInteger(timeoutMilliseconds, "timeoutMilliseconds");
  const startedAt = clock.now();
  return {
    startedAt,
    expiresAt: startedAt + timeoutMilliseconds,
  };
}

export function deadlineRemaining(clock: Clock, deadline: Deadline): number {
  return Math.max(0, deadline.expiresAt - clock.now());
}

export function deadlineExpired(clock: Clock, deadline: Deadline): boolean {
  return deadlineRemaining(clock, deadline) === 0;
}

export async function withTimeout<T>(
  promise: Promise<T>,
  timeoutMilliseconds: number,
  label: string,
): Promise<T> {
  assertNonNegativeInteger(timeoutMilliseconds, "timeoutMilliseconds");
  if (timeoutMilliseconds === 0) {
    throw new RuntimeInvariantError("operation_timed_out", { label });
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new RuntimeInvariantError("operation_timed_out", { label })),
          timeoutMilliseconds,
        );
      }),
    ]);
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  }
}

export function mapToRecord<T extends JsonValue>(
  map: ReadonlyMap<string, T>,
): JsonRecord {
  const record: JsonRecord = {};
  for (const [key, value] of [...map.entries()].sort(([left], [right]) =>
    compareStrings(left, right),
  )) {
    record[key] = deepClone(value);
  }
  return record;
}

export function recordToMap<T extends JsonValue>(
  record: JsonRecord,
  decode: (value: JsonValue, key: string) => T,
): Map<string, T> {
  const map = new Map<string, T>();
  for (const [key, value] of Object.entries(record)) {
    map.set(key, decode(value, key));
  }
  return map;
}

export function assertNever(value: never, label: string): never {
  throw new RuntimeInvariantError("unreachable_variant", {
    label,
    value: asJsonValue(value),
  });
}
