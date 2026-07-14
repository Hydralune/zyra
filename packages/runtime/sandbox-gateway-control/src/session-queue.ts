import { GatewayProtocolError, type JsonValue } from "./contracts.ts";

interface QueueItem<T> {
  readonly ticket: number;
  readonly operation: () => Promise<T> | T;
  readonly resolve: (value: T | PromiseLike<T>) => void;
  readonly reject: (reason: unknown) => void;
  readonly signal?: AbortSignal;
  readonly enqueuedAt: number;
}

interface SessionQueueState {
  nextTicket: number;
  activeTicket: number | null;
  pending: QueueItem<unknown>[];
  draining: boolean;
}

export class SessionActorQueue {
  readonly maximumDepth: number;
  private readonly states = new Map<string, SessionQueueState>();

  constructor(options: { readonly maximumDepth?: number } = {}) {
    this.maximumDepth = options.maximumDepth ?? 256;
  }

  run<T>(
    sessionId: string,
    operation: () => Promise<T> | T,
    options: { readonly signal?: AbortSignal } = {},
  ): Promise<T> {
    const state = this.state(sessionId);
    if (state.pending.length + Number(state.activeTicket !== null) >=
        this.maximumDepth) {
      return Promise.reject(
        new GatewayProtocolError(
          "session_queue_full",
          "Session actor queue is full",
          { retryable: true },
        ),
      );
    }
    if (options.signal?.aborted) {
      return Promise.reject(
        options.signal.reason ??
          new GatewayProtocolError(
            "session_queue_cancelled",
            "Queue operation was cancelled before enqueue",
          ),
      );
    }
    const ticket = state.nextTicket;
    state.nextTicket += 1;
    return new Promise<T>((resolve, reject) => {
      const item: QueueItem<T> = {
        ticket,
        operation,
        resolve,
        reject,
        signal: options.signal,
        enqueuedAt: Date.now(),
      };
      state.pending.push(item as QueueItem<unknown>);
      this.drain(sessionId, state);
    });
  }

  depth(sessionId: string): number {
    const state = this.states.get(sessionId);
    return state
      ? state.pending.length + Number(state.activeTicket !== null)
      : 0;
  }

  cancelPending(sessionId: string, reason: unknown): number {
    const state = this.states.get(sessionId);
    if (!state) {
      return 0;
    }
    const pending = state.pending.splice(0);
    for (const item of pending) {
      item.reject(reason);
    }
    this.prune(sessionId, state);
    return pending.length;
  }

  descriptor(): JsonValue {
    return {
      queue: "SessionActorQueue",
      sourceMechanism: "openclaw session actor queue",
      maximumDepth: this.maximumDepth,
      sessions: this.states.size,
      depths: Object.fromEntries(
        [...this.states.entries()]
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, state]) => [
            key,
            state.pending.length + Number(state.activeTicket !== null),
          ]),
      ),
      serializationScope: "per_session",
    };
  }

  private state(sessionId: string): SessionQueueState {
    let state = this.states.get(sessionId);
    if (!state) {
      state = {
        nextTicket: 0,
        activeTicket: null,
        pending: [],
        draining: false,
      };
      this.states.set(sessionId, state);
    }
    return state;
  }

  private drain(sessionId: string, state: SessionQueueState): void {
    if (state.draining || state.activeTicket !== null) {
      return;
    }
    state.draining = true;
    queueMicrotask(async () => {
      try {
        while (state.activeTicket === null && state.pending.length > 0) {
          const item = state.pending.shift() as QueueItem<unknown>;
          if (item.signal?.aborted) {
            item.reject(
              item.signal.reason ??
                new GatewayProtocolError(
                  "session_queue_cancelled",
                  "Queue operation was cancelled",
                ),
            );
            continue;
          }
          state.activeTicket = item.ticket;
          try {
            const value = await item.operation();
            item.resolve(value);
          } catch (error) {
            item.reject(error);
          } finally {
            state.activeTicket = null;
          }
        }
      } finally {
        state.draining = false;
        if (state.pending.length > 0 && state.activeTicket === null) {
          this.drain(sessionId, state);
        } else {
          this.prune(sessionId, state);
        }
      }
    });
  }

  private prune(sessionId: string, state: SessionQueueState): void {
    if (
      !state.draining &&
      state.activeTicket === null &&
      state.pending.length === 0
    ) {
      this.states.delete(sessionId);
    }
  }
}
