import {
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react"
import type { TaskProjection } from "../../../../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../../../app/runtime.ts"
import {
  RecoveryControlAction,
  RecoveryControlRuntime,
  assessSealedControlReceipt,
  recoveryActionDescription,
  recoveryActionLabel,
  taskApiRecoveryControlTransport,
  type RecoveryControlActionValue,
  type RecoveryControlOwnerExpectation,
  type RecoveryControlReceipt,
  type RecoveryControlRequest,
} from "../control/index.ts"
import type { WorkerCausalTimelineProjection } from "../projection/index.ts"

const ACTIONS = Object.values(RecoveryControlAction)

function metadataBoolean(
  metadata: Record<string, unknown>,
  ...keys: string[]
): boolean {
  for (const key of keys) {
    const value = metadata[key]
    if (typeof value === "boolean") return value
    if (
      typeof value === "string" &&
      ["1", "true", "yes", "sealed", "sealed_autonomous"].includes(
        value.toLocaleLowerCase(),
      )
    ) {
      return true
    }
  }
  return false
}

function taskSealed(task: TaskProjection): boolean {
  const mode = String(
    task.metadata.competition_mode ??
      task.metadata.permission_mode ??
      task.metadata.benchmark_mode ??
      "",
  ).toLocaleLowerCase()
  return (
    metadataBoolean(
      task.metadata,
      "sealed",
      "formal_benchmark",
      "sealed_autonomous",
    ) || mode.includes("sealed")
  )
}

function metadataString(
  metadata: Record<string, unknown>,
  ...keys: string[]
): string | undefined {
  for (const key of keys) {
    const value = metadata[key]
    if (
      (typeof value === "string" || typeof value === "number") &&
      String(value).trim()
    ) {
      return String(value).trim()
    }
  }
  return undefined
}

function metadataNumber(
  metadata: Record<string, unknown>,
  ...keys: string[]
): number | undefined {
  for (const key of keys) {
    const value = metadata[key]
    if (typeof value === "number" && Number.isInteger(value) && value >= 0) {
      return value
    }
    if (typeof value === "string" && /^\d+$/.test(value)) {
      return Number(value)
    }
  }
  return undefined
}

function metadataRecord(
  metadata: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  const value = metadata[key]
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

export function recoveryControlOwnerExpectation(
  task: TaskProjection,
  projection: WorkerCausalTimelineProjection,
): RecoveryControlOwnerExpectation {
  const worker =
    [...projection.workerEpochs]
      .reverse()
      .find((epoch) => !epoch.terminal && epoch.leaseId) ??
    projection.workerEpochs.at(-1)
  const workerPool = metadataRecord(task.metadata, "worker_pool")
  const poolWorkerId = metadataString(workerPool, "worker_id")
  const poolLeaseId = metadataString(workerPool, "lease_id")
  const workerId = worker?.workerId ?? poolWorkerId
  const leaseId = worker?.leaseId ?? poolLeaseId
  const poolOwnerMatchesProjection =
    Boolean(poolWorkerId && poolLeaseId) &&
    poolWorkerId === workerId &&
    poolLeaseId === leaseId
  const attempt = projection.recoveryChains
    .flatMap((chain) => chain.attempts)
    .at(-1)
  return Object.freeze({
    taskId: task.taskId,
    runId: task.runId,
    sessionId: task.sessionId,
    workerId,
    leaseId,
    attemptId: poolOwnerMatchesProjection
      ? metadataString(workerPool, "attempt_id")
      : undefined,
    nodeId:
      worker?.nodeId ??
      metadataString(task.metadata, "node_id", "active_node_id") ??
      task.rootNodeId,
    graphId: metadataString(
      task.metadata,
      "graph_id",
      "dynamic_graph_id",
      "topology_id",
    ),
    checkpointId:
      attempt?.checkpointId ??
      metadataString(
        task.metadata,
        "checkpoint_id",
        "resume_checkpoint_id",
        "checkpoint_ref",
      ),
    ownerRevision: metadataNumber(
      task.metadata,
      "owner_revision",
      "worker_pool_revision",
    ),
    graphRevision: metadataNumber(
      task.metadata,
      "graph_revision",
      "topology_revision",
    ),
    sessionRevision: metadataNumber(
      task.metadata,
      "session_revision",
      "revision",
    ),
  })
}

function missingOwnerReason(
  action: RecoveryControlActionValue,
  owner: RecoveryControlOwnerExpectation,
): string | undefined {
  if (
    (action === RecoveryControlAction.KILL ||
      action === RecoveryControlAction.REASSIGN) &&
    (!owner.workerId || !owner.leaseId)
  ) {
    return "A live canonical worker lease is required."
  }
  if (action === RecoveryControlAction.STEER && !owner.nodeId) {
    return "A canonical graph node is required."
  }
  if (
    action === RecoveryControlAction.RESUME &&
    (!owner.sessionId || !owner.checkpointId)
  ) {
    return "Exact resume requires both session and checkpoint identities."
  }
  return undefined
}

function receiptTone(receipt: RecoveryControlReceipt): string {
  if (receipt.phase === "applied") return "success"
  if (receipt.phase === "denied" || receipt.phase === "failed") return "danger"
  if (receipt.phase === "timed-out") return "warning"
  return "active"
}

function ReceiptRow({
  control,
  receipt,
}: {
  control: RecoveryControlRuntime
  receipt: RecoveryControlReceipt
}) {
  const request = control.request(receipt.commandId)
  const sealed = receipt.sealed && request
    ? assessSealedControlReceipt(request, receipt)
    : undefined
  return (
    <li
      className={`timeline-control-receipt is-${receiptTone(receipt)}`}
      data-control-phase={receipt.phase}
      data-control-command={receipt.commandName}
    >
      <div>
        <strong>{receipt.commandName}</strong>
        <span>{receipt.phase}</span>
        {receipt.replayed ? <span>idempotent replay</span> : null}
        {receipt.detached ? <span>viewer detached</span> : null}
      </div>
      <p>{receipt.summary}</p>
      <code>{receipt.commandId}</code>
      {sealed ? (
        <dl>
          <div>
            <dt>Manual mutation</dt>
            <dd>{sealed.manualMutationApplied ? "unexpected" : "blocked"}</dd>
          </div>
          <div>
            <dt>Operator attempts</dt>
            <dd>{sealed.operatorInterventionAttemptCount ?? "recorded once"}</dd>
          </div>
          <div>
            <dt>Human intervention</dt>
            <dd>{sealed.humanInterventionCount ?? 0}</dd>
          </div>
          <div>
            <dt>Autonomous outcome</dt>
            <dd>{sealed.automaticRecoveryAction ?? (sealed.failClosed ? "failed closed" : "pending")}</dd>
          </div>
        </dl>
      ) : null}
    </li>
  )
}

export function RecoveryControlPanel({
  runtime,
  task,
  projection,
}: {
  runtime: WorkbenchRuntime
  task: TaskProjection
  projection: WorkerCausalTimelineProjection
}) {
  const sealed = taskSealed(task)
  const owner = useMemo(
    () => recoveryControlOwnerExpectation(task, projection),
    [task, projection],
  )
  const control = useMemo(
    () =>
      new RecoveryControlRuntime({
        taskId: task.taskId,
        runId: task.runId,
        transport: taskApiRecoveryControlTransport(runtime.api.tasks),
        defaultTimeoutMs: 30_000,
        maximumTimeoutMs: 120_000,
        detachOnClose: true,
      }),
    [runtime.api.tasks, task.runId, task.taskId],
  )
  const snapshot = useSyncExternalStore(
    control.subscribe,
    control.getSnapshot,
    control.getSnapshot,
  )
  const [action, setAction] = useState<RecoveryControlActionValue>(
    RecoveryControlAction.RETRY,
  )
  const [reason, setReason] = useState("Operator requested recovery control.")
  const [instruction, setInstruction] = useState("")
  const [actorId, setActorId] = useState("timeline-operator")
  const [submitError, setSubmitError] = useState<string>()
  const missing = missingOwnerReason(action, owner)

  useEffect(() => {
    control.observe(projection)
  }, [control, projection])
  useEffect(() => {
    for (const announcement of control.takeAnnouncements()) {
      runtime.announcer.announce(announcement)
    }
  }, [control, runtime, snapshot.revision])
  useEffect(
    () => () =>
      control.close(
        "Timeline viewer closed; durable recovery controls remain active.",
      ),
    [control],
  )

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setSubmitError(undefined)
    const request: RecoveryControlRequest = {
      action,
      taskId: task.taskId,
      runId: task.runId,
      actorId,
      reason,
      instruction:
        action === RecoveryControlAction.STEER
          ? instruction || reason
          : undefined,
      sealed,
      timeoutMs: 30_000,
      maximumAttempts:
        action === RecoveryControlAction.RETRY ? 1 : undefined,
      owner,
      metadata: {
        source: "worker-causal-timeline",
        projection_revision: projection.projectionRevision,
        committed_sequence: projection.diagnostics.committedSequence,
      },
    }
    try {
      const submission = control.submit(request)
      void submission.completion.then((receipt) => {
        if (receipt.phase === "applied") {
          runtime.notifications.push({
            id: `timeline-control-${receipt.commandId}`,
            title: `${receipt.commandName} applied`,
            message: receipt.summary,
            tone: "success",
            taskId: task.taskId,
          })
        }
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setSubmitError(message)
      runtime.announcer.announce(message, "assertive")
    }
  }

  return (
    <section
      className="timeline-control-panel"
      aria-labelledby="timeline-control-heading"
      data-sealed-control={sealed ? "true" : "false"}
    >
      <header>
        <div>
          <p className="eyebrow">Recovery control</p>
          <h4 id="timeline-control-heading">Canonical runtime commands</h4>
          <p>
            Kill, steer, retry, reassign and exact resume are fenced by the
            currently observed owner identities.
          </p>
        </div>
        <span className={`timeline-control-mode ${sealed ? "is-sealed" : ""}`}>
          {sealed ? "sealed autonomous" : "interactive"}
        </span>
      </header>
      {sealed ? (
        <p className="timeline-control-sealed-notice" role="note">
          Manual mutations are deterministically denied. The attempt is
          counted once, human intervention remains zero, and recovery replans
          autonomously or fails closed without an approval wait.
        </p>
      ) : null}
      <form onSubmit={submit}>
        <label>
          Action
          <select
            value={action}
            onChange={(event) =>
              setAction(event.target.value as RecoveryControlActionValue)
            }
          >
            {ACTIONS.map((value) => (
              <option value={value} key={value}>
                {recoveryActionLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Actor
          <input
            value={actorId}
            onChange={(event) => setActorId(event.target.value)}
            required
          />
        </label>
        <label className="timeline-control-reason">
          Reason
          <input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            required
          />
        </label>
        {action === RecoveryControlAction.STEER ? (
          <label className="timeline-control-instruction">
            New requirement
            <textarea
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="Describe the new goal, constraint or route."
              required
            />
          </label>
        ) : null}
        <div className="timeline-control-owner">
          <span>worker <code>{owner.workerId ?? "unbound"}</code></span>
          <span>lease <code>{owner.leaseId ?? "unbound"}</code></span>
          <span>node <code>{owner.nodeId ?? "unbound"}</code></span>
          <span>checkpoint <code>{owner.checkpointId ?? "unbound"}</code></span>
        </div>
        <div className="timeline-control-submit">
          <p>{recoveryActionDescription(action)}</p>
          <button
            type="submit"
            className="button button-primary"
            disabled={
              Boolean(missing) ||
              !reason.trim() ||
              !actorId.trim() ||
              snapshot.disabled
            }
          >
            {sealed ? "Record sealed attempt" : `Run /${action}`}
          </button>
        </div>
        {missing ? <p className="form-error">{missing}</p> : null}
        {submitError ? <p className="form-error" role="alert">{submitError}</p> : null}
      </form>
      <div className="timeline-control-ledger">
        <div>
          <strong>Durable receipt ledger</strong>
          <span>{snapshot.connection}</span>
          <span>{snapshot.ledger.pendingCount} pending</span>
        </div>
        {snapshot.ledger.receipts.length ? (
          <ol>
            {snapshot.ledger.receipts.map((receipt) => (
              <ReceiptRow key={receipt.id} control={control} receipt={receipt} />
            ))}
          </ol>
        ) : (
          <p>No recovery control has been submitted from this timeline.</p>
        )}
      </div>
    </section>
  )
}
