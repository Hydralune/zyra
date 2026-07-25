import type {
  CommandReceipt,
  CommandUsageReceipt,
} from "./contracts.ts"
import { assertSideQuestionReceipt } from "./receipts.ts"

export type SideQuestionPhase =
  | "idle"
  | "submitting"
  | "answered"
  | "failed"
  | "cancelled"
  | "closed"

export interface SideQuestionSnapshot {
  phase: SideQuestionPhase
  question: string
  answer: string
  receipt?: CommandReceipt
  usage?: CommandUsageReceipt
  error?: string
  openedAt?: number
  settledAt?: number
  closedAt?: number
  revision: number
}

function answerFrom(receipt: CommandReceipt): string {
  const values = [
    receipt.data.answer,
    receipt.data.response,
    receipt.data.text,
    receipt.displayText,
    receipt.summary,
  ]
  return values
    .find((value): value is string => typeof value === "string" && Boolean(value.trim()))
    ?.trim() ?? ""
}

export class SideQuestionSession {
  readonly #listeners = new Set<() => void>()
  #snapshot: SideQuestionSnapshot = Object.freeze({
    phase: "idle",
    question: "",
    answer: "",
    revision: 0,
  })
  #controller?: AbortController
  #closed = false

  getSnapshot = (): SideQuestionSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  begin(question: string): AbortSignal {
    this.#assertOpen()
    const normalized = question.trim()
    if (!normalized) {
      throw new TypeError("Side question must not be empty.")
    }
    if (new TextEncoder().encode(normalized).byteLength > 64 * 1024) {
      throw new TypeError("Side question exceeds 64 KiB.")
    }
    if (this.#snapshot.phase === "submitting") {
      throw new TypeError("A side question is already in flight.")
    }
    this.#controller = new AbortController()
    this.#replace({
      phase: "submitting",
      question: normalized,
      answer: "",
      receipt: undefined,
      usage: undefined,
      error: undefined,
      openedAt: Date.now(),
      settledAt: undefined,
      closedAt: undefined,
    })
    return this.#controller.signal
  }

  settle(receiptValue: CommandReceipt): SideQuestionSnapshot {
    this.#assertOpen()
    const receipt = assertSideQuestionReceipt(receiptValue)
    if (this.#snapshot.phase !== "submitting") {
      throw new TypeError("No side question is awaiting a receipt.")
    }
    if (receipt.phase === "applied") {
      const answer = answerFrom(receipt)
      if (!answer) {
        throw new TypeError("Side-question receipt has no answer.")
      }
      this.#replace({
        phase: "answered",
        answer,
        receipt,
        usage: receipt.usage,
        settledAt: Date.now(),
      })
      this.#controller = undefined
      return this.#snapshot
    }
    if (receipt.phase === "cancelled") {
      this.#replace({
        phase: "cancelled",
        receipt,
        error: receipt.error?.message ?? "Side question was cancelled.",
        settledAt: Date.now(),
      })
      this.#controller = undefined
      return this.#snapshot
    }
    this.#replace({
      phase: "failed",
      receipt,
      error:
        receipt.error?.message ??
        (receipt.summary || "Side question failed."),
      settledAt: Date.now(),
    })
    this.#controller = undefined
    return this.#snapshot
  }

  fail(cause: unknown): SideQuestionSnapshot {
    this.#assertOpen()
    if (this.#snapshot.phase !== "submitting") return this.#snapshot
    const aborted = this.#controller?.signal.aborted ?? false
    this.#replace({
      phase: aborted ? "cancelled" : "failed",
      error:
        cause instanceof Error
          ? cause.message
          : String(cause || "Side question failed."),
      settledAt: Date.now(),
    })
    this.#controller = undefined
    return this.#snapshot
  }

  cancel(reason = "Side question cancelled."): boolean {
    this.#assertOpen()
    if (!this.#controller || this.#controller.signal.aborted) return false
    this.#controller.abort(reason)
    this.#replace({
      phase: "cancelled",
      error: reason,
      settledAt: Date.now(),
    })
    this.#controller = undefined
    return true
  }

  reset(): void {
    this.#assertOpen()
    if (this.#snapshot.phase === "submitting") {
      throw new TypeError("Cancel the active side question before reset.")
    }
    this.#replace({
      phase: "idle",
      question: "",
      answer: "",
      receipt: undefined,
      usage: undefined,
      error: undefined,
      openedAt: undefined,
      settledAt: undefined,
      closedAt: undefined,
    })
  }

  close(reason = "Side question closed."): void {
    if (this.#closed) return
    if (this.#controller && !this.#controller.signal.aborted) {
      this.#controller.abort(reason)
    }
    this.#replace({
      phase: "closed",
      error:
        this.#snapshot.phase === "submitting"
          ? reason
          : this.#snapshot.error,
      closedAt: Date.now(),
    })
    this.#closed = true
    this.#controller = undefined
    this.#listeners.clear()
  }

  #replace(patch: Partial<SideQuestionSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // A side-question view cannot mutate the isolated result.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new TypeError("Side-question session is closed.")
    }
  }
}

export function sideQuestionAudit(receipt: CommandReceipt): {
  isolated: boolean
  failures: string[]
} {
  const failures: string[] = []
  if (receipt.name !== "/btw") failures.push("wrong-command")
  if (receipt.phase === "applied" && !answerFrom(receipt)) failures.push("missing-answer")
  if (receipt.data.tools_used === true) failures.push("tool-use")
  if (receipt.data.main_session_mutated === true) failures.push("main-session-mutation")
  if (receipt.data.message_appended === true) failures.push("message-appended")
  if (receipt.phase === "applied" && !receipt.usage) failures.push("missing-usage")
  return {
    isolated: failures.length === 0,
    failures,
  }
}
