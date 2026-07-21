import { E03RuntimeError } from "../e03/contracts.ts";

/**
 * Abort-safe, dynamically resizable semaphore cropped from Oh My Pi's
 * task/parallel.ts and provider-concurrency.ts control flow. Waiters own no
 * permit until resolve; cancellation removes the waiter before rejection, so
 * an aborted queue entry cannot leak capacity.
 */
export class OmpAbortSafeSemaphore {
  private capacityValue: number;
  private activeValue = 0;
  private readonly queue: Waiter[] = [];

  constructor(capacity: number) {
    this.capacityValue = normalizeConcurrencyLimit(capacity);
  }

  get capacity(): number {
    return this.capacityValue;
  }

  get active(): number {
    return this.activeValue;
  }

  get pending(): number {
    return this.queue.length;
  }

  resize(capacity: number): void {
    this.capacityValue = normalizeConcurrencyLimit(capacity);
    this.dispatch();
  }

  async acquire(signal?: AbortSignal): Promise<() => void> {
    if (signal?.aborted) throw abortError(signal.reason);
    if (this.activeValue < this.capacityValue && this.queue.length === 0) {
      this.activeValue += 1;
      return this.releaseOnce();
    }
    return new Promise<() => void>((resolve, reject) => {
      const waiter: Waiter = {
        signal,
        resolve,
        reject,
        onAbort: undefined,
        settled: false,
      };
      if (signal) {
        waiter.onAbort = () => {
          if (waiter.settled) return;
          waiter.settled = true;
          const index = this.queue.indexOf(waiter);
          if (index >= 0) this.queue.splice(index, 1);
          reject(abortError(signal.reason));
        };
        signal.addEventListener("abort", waiter.onAbort, { once: true });
      }
      this.queue.push(waiter);
      this.dispatch();
    });
  }

  snapshot(): {
    capacity: number;
    active: number;
    pending: number;
    available: number;
  } {
    return {
      capacity: this.capacityValue,
      active: this.activeValue,
      pending: this.queue.length,
      available: Math.max(0, this.capacityValue - this.activeValue),
    };
  }

  private dispatch(): void {
    while (this.activeValue < this.capacityValue && this.queue.length) {
      const waiter = this.queue.shift()!;
      if (waiter.settled || waiter.signal?.aborted) {
        waiter.onAbort?.();
        continue;
      }
      waiter.settled = true;
      if (waiter.signal && waiter.onAbort)
        waiter.signal.removeEventListener("abort", waiter.onAbort);
      this.activeValue += 1;
      waiter.resolve(this.releaseOnce());
    }
  }

  private releaseOnce(): () => void {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      if (this.activeValue < 1)
        throw new E03RuntimeError(
          "omp_semaphore_underflow",
          "OMP semaphore released without an active permit",
        );
      this.activeValue -= 1;
      this.dispatch();
    };
  }
}

export interface OmpParallelOptions {
  signal?: AbortSignal;
  failureMode?: "fail-fast" | "collect";
}

/** Ordered bounded map retaining OMP's shared semaphore and fail-fast abort. */
export async function mapWithConcurrencyLimit<T, R>(
  values: readonly T[],
  concurrency: number,
  map: (value: T, index: number, signal: AbortSignal) => Promise<R>,
  options: OmpParallelOptions = {},
): Promise<R[]> {
  if (!values.length) return [];
  const maximum = Math.min(values.length, normalizeConcurrencyLimit(concurrency));
  const controller = new AbortController();
  const abortFromParent = () => controller.abort(options.signal?.reason);
  if (options.signal) {
    if (options.signal.aborted) abortFromParent();
    else options.signal.addEventListener("abort", abortFromParent, { once: true });
  }
  const semaphore = new OmpAbortSafeSemaphore(maximum);
  const output = Array<R>(values.length);
  let firstFailure: unknown = null;
  try {
    await Promise.all(values.map(async (value, index) => {
      if (controller.signal.aborted && options.failureMode !== "collect")
        throw abortError(controller.signal.reason);
      const release = await semaphore.acquire(controller.signal);
      try {
        output[index] = await map(value, index, controller.signal);
      } catch (error) {
        if (firstFailure === null) firstFailure = error;
        if (options.failureMode !== "collect" && !controller.signal.aborted)
          controller.abort(error);
        throw error;
      } finally {
        release();
      }
    }).map((operation) =>
      options.failureMode === "collect"
        ? operation.catch(() => undefined)
        : operation,
    ));
  } catch (error) {
    throw firstFailure ?? error;
  } finally {
    if (options.signal)
      options.signal.removeEventListener("abort", abortFromParent);
  }
  if (firstFailure !== null && options.failureMode !== "collect") throw firstFailure;
  return output;
}

export function normalizeConcurrencyLimit(value: number): number {
  if (!Number.isSafeInteger(value) || value < 1 || value > 128)
    throw new E03RuntimeError(
      "invalid_omp_concurrency",
      "OMP concurrency must be an integer in 1..128",
    );
  return value;
}

interface Waiter {
  signal?: AbortSignal;
  resolve(value: () => void): void;
  reject(error: Error): void;
  onAbort?: () => void;
  settled: boolean;
}

function abortError(reason: unknown): Error {
  const error = new Error(
    reason instanceof Error ? reason.message : String(reason || "operation aborted"),
  );
  error.name = "AbortError";
  return error;
}
