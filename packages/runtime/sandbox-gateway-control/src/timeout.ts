import { GatewayProtocolError, type JsonValue } from "./contracts.ts";

export interface DeadlineOptions {
  readonly timeoutMilliseconds: number;
  readonly parentSignal?: AbortSignal;
  readonly reason?: string;
}

export class CommandDeadline implements Disposable {
  readonly controller = new AbortController();
  readonly startedAt = Date.now();
  readonly deadlineAt: number;
  readonly timeoutMilliseconds: number;
  private readonly timer: ReturnType<typeof setTimeout>;
  private readonly parentSignal?: AbortSignal;
  private readonly parentAbort: () => void;
  private disposed = false;

  constructor(options: DeadlineOptions) {
    if (
      !Number.isFinite(options.timeoutMilliseconds) ||
      options.timeoutMilliseconds <= 0
    ) {
      throw new GatewayProtocolError(
        "deadline_invalid",
        "Command deadline must be positive",
      );
    }
    this.timeoutMilliseconds = options.timeoutMilliseconds;
    this.deadlineAt = this.startedAt + options.timeoutMilliseconds;
    this.parentSignal = options.parentSignal;
    this.parentAbort = () => {
      this.cancel(
        this.parentSignal?.reason ??
          new GatewayProtocolError(
            "command_cancelled",
            "Parent operation cancelled the command",
          ),
      );
    };
    if (this.parentSignal?.aborted) {
      this.parentAbort();
    } else {
      this.parentSignal?.addEventListener("abort", this.parentAbort, {
        once: true,
      });
    }
    this.timer = setTimeout(() => {
      this.cancel(
        new GatewayProtocolError(
          "command_timeout",
          options.reason ?? "Command deadline exceeded",
          { retryable: true },
        ),
      );
    }, options.timeoutMilliseconds);
    this.timer.unref?.();
  }

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  get aborted(): boolean {
    return this.signal.aborted;
  }

  get remainingMilliseconds(): number {
    return Math.max(0, this.deadlineAt - Date.now());
  }

  cancel(reason: unknown): boolean {
    if (this.controller.signal.aborted) {
      return false;
    }
    this.controller.abort(reason);
    return true;
  }

  async race<T>(operation: Promise<T>): Promise<T> {
    if (this.signal.aborted) {
      throw this.signal.reason;
    }
    return await Promise.race([
      operation,
      new Promise<never>((_resolve, reject) => {
        this.signal.addEventListener(
          "abort",
          () => reject(this.signal.reason),
          { once: true },
        );
      }),
    ]);
  }

  descriptor(): JsonValue {
    return {
      deadline: "CommandDeadline",
      sourceMechanism: "openclaw turn timeout cleanup",
      startedAt: this.startedAt,
      deadlineAt: this.deadlineAt,
      timeoutMilliseconds: this.timeoutMilliseconds,
      remainingMilliseconds: this.remainingMilliseconds,
      aborted: this.aborted,
      disposed: this.disposed,
    };
  }

  [Symbol.dispose](): void {
    this.dispose();
  }

  dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    clearTimeout(this.timer);
    this.parentSignal?.removeEventListener("abort", this.parentAbort);
  }
}

export async function withDeadline<T>(
  options: DeadlineOptions,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const deadline = new CommandDeadline(options);
  try {
    return await deadline.race(operation(deadline.signal));
  } finally {
    deadline.dispose();
  }
}
