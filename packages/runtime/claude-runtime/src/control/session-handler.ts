import {
  createId,
  digest,
  type CommitPhase,
  type ControlCommand,
  type E03Clock,
  SystemE03Clock,
} from "../e03/contracts.ts";
import { type ControlCommandDescriptor } from "./schema.ts";
import type { JsonObject, RuntimeRunInput } from "../contracts.ts";
import {
  E03RuntimeError,
  isTerminal,
  response,
  sealTask,
  type E03ControlEnvelope,
  type E03ControlResponse,
  type E03TaskState,
} from "../e03/contracts.ts";
import { AgentExecutionRuntime } from "../agents/execution-runtime.ts";
import { DurableTaskRegistry, taskProjection } from "../tasks/registry.ts";
import { TaskStateMachine } from "../tasks/state-machine.ts";
import { TeamMailbox } from "../team/mailbox.ts";
import { TeamSteeringQueue } from "../team/mailbox.ts";

export class AgentControlHandler {
  private readonly machine = new TaskStateMachine();
  private readonly mailbox = new TeamMailbox();
  private readonly steeringQueue = new TeamSteeringQueue();

  constructor(
    private readonly registry: DurableTaskRegistry,
    private readonly execution: AgentExecutionRuntime,
  ) {}

  async execute(
    envelope: E03ControlEnvelope,
    parentInput?: RuntimeRunInput,
  ): Promise<E03ControlResponse> {
    const recovered = this.registry.recoverLostAck(envelope.idempotency_key);
    if (recovered) return recovered;
    switch (envelope.command) {
      case "agent.status":
        return this.status(envelope);
      case "agent.list":
        return this.list(envelope);
      case "agent.wait":
        return this.wait(envelope);
      case "agent.result":
        return this.result(envelope);
      case "agent.cancel":
        return this.cancel(envelope);
      case "agent.kill":
        return this.kill(envelope);
      case "agent.steer":
        return this.steer(envelope);
      case "agent.resume":
        return this.resume(envelope, parentInput);
      case "team.send":
        return this.send(envelope);
      case "team.collect":
        return this.collect(envelope);
      default:
        throw new E03RuntimeError(
          "unsupported_agent_control",
          `${envelope.command} is not handled by AgentControlHandler`,
        );
    }
  }

  recoverLostAck(idempotencyKey: string): E03ControlResponse | null {
    return this.registry.recoverLostAck(idempotencyKey);
  }

  async cancel(envelope: E03ControlEnvelope): Promise<E03ControlResponse> {
    const taskId = text(envelope.body.task_id, "task_id");
    const task = this.requireAuthority(envelope, taskId);
    if (task.status === "cancelled")
      return response({
        ok: true,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "ack",
        revision: task.revision,
        replayed: true,
        state: taskProjection(task),
        dispatch_count: 1,
      });
    const state = await this.execution.abort(
      taskId,
      envelope.expected_revision,
      text(envelope.body.reason ?? "user_cancelled", "reason"),
      "cancel",
      envelope.request_id,
      envelope.idempotency_key,
    );
    return this.ack(envelope, state);
  }

  async kill(envelope: E03ControlEnvelope): Promise<E03ControlResponse> {
    const taskId = text(envelope.body.task_id, "task_id");
    const task = this.requireAuthority(envelope, taskId);
    if (task.status === "killed")
      return response({
        ok: true,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "ack",
        revision: task.revision,
        replayed: true,
        state: taskProjection(task),
        dispatch_count: 1,
      });
    const state = await this.execution.abort(
      taskId,
      envelope.expected_revision,
      text(envelope.body.reason ?? "user_killed", "reason"),
      "kill",
      envelope.request_id,
      envelope.idempotency_key,
    );
    return this.ack(envelope, state);
  }

  async wait(envelope: E03ControlEnvelope): Promise<E03ControlResponse> {
    const task = this.requireAuthority(
      envelope,
      text(envelope.body.task_id, "task_id"),
    );
    const timeout =
      envelope.body.timeout_ms === undefined
        ? undefined
        : integer(envelope.body.timeout_ms, "timeout_ms", 1, 3_600_000);
    const settled = await this.execution
      .wait(task.identity.taskId, timeout)
      .catch((error) => {
        if (error instanceof E03RuntimeError && error.code === "wait_timeout")
          return this.registry.require(task.identity.taskId);
        throw error;
      });
    return response({
      ok: isTerminal(settled.status),
      request_id: envelope.request_id,
      command: envelope.command,
      phase: "ack",
      revision: settled.revision,
      restored: true,
      state: taskProjection(settled),
      result: settled.result,
      error: isTerminal(settled.status) ? settled.error : "task_not_terminal",
    });
  }

  result(envelope: E03ControlEnvelope): E03ControlResponse {
    const task = this.requireAuthority(
      envelope,
      text(envelope.body.task_id, "task_id"),
    );
    const result = this.execution.result(task.identity.taskId);
    return {
      ...result,
      request_id: envelope.request_id,
      command: envelope.command,
      restored: true,
    };
  }

  status(envelope: E03ControlEnvelope): E03ControlResponse {
    const task = this.requireAuthority(
      envelope,
      text(envelope.body.task_id, "task_id"),
    );
    return response({
      ok: true,
      request_id: envelope.request_id,
      command: envelope.command,
      phase: "ack",
      revision: task.revision,
      restored: true,
      state: taskProjection(task),
      result: task.result,
      error: task.error,
    });
  }

  list(envelope: E03ControlEnvelope): E03ControlResponse {
    const parentTaskId =
      typeof envelope.body.parent_task_id === "string"
        ? envelope.body.parent_task_id
        : envelope.parent_task_id;
    const tasks = this.registry
      .list(parentTaskId)
      .filter(
        (task) =>
          task.identity.runId === envelope.run_id &&
          (task.identity.parentSessionId === envelope.session_id ||
            task.identity.sessionId === envelope.session_id),
      );
    return response({
      ok: true,
      request_id: envelope.request_id,
      command: envelope.command,
      phase: "ack",
      revision: Math.max(0, ...tasks.map((task) => task.revision)),
      restored: true,
      state: { tasks: tasks.map(taskProjection) },
    });
  }

  private async steer(
    envelope: E03ControlEnvelope,
  ): Promise<E03ControlResponse> {
    const task = this.requireAuthority(
      envelope,
      text(envelope.body.task_id, "task_id"),
    );
    if (task.revision !== envelope.expected_revision)
      throw new E03RuntimeError(
        "stale_revision",
        `steer expected ${envelope.expected_revision}, current ${task.revision}`,
      );
    const parent = this.syntheticParent(task, envelope);
    const steering = this.steeringQueue.enqueue(task, {
      intent: "message",
      body: text(envelope.body.message, "message"),
      idempotencyKey: envelope.idempotency_key,
    });
    const claimed = this.steeringQueue
      .claim(task)
      .find((item) => item.steeringId === steering.steeringId);
    if (!claimed)
      throw new E03RuntimeError(
        "steering_not_claimed",
        "steering queue did not claim the new message",
      );
    const sent = this.mailbox.steer(parent, task, {
      body: text(envelope.body.message, "message"),
      idempotencyKey: envelope.idempotency_key,
    });
    const proposed = sealTask({
      ...sent.recipient,
      revision: task.revision + 1,
      updatedAt: new Date().toISOString(),
      checksum: "",
    });
    const prepared = this.registry.prepare({
      requestId: envelope.request_id,
      idempotencyKey: envelope.idempotency_key,
      writerId: "typescript.E03AgentControlCoordinator",
      taskId: task.identity.taskId,
      expectedRevision: task.revision,
      proposed,
      effectKind: "persist",
      effectOperation: "persist_agent_steer",
      effectPayload: { message_id: sent.message.messageId },
    });
    const receipt = await this.registry.recordReceipt(envelope.idempotency_key);
    const committed = await this.registry.commit(
      envelope.idempotency_key,
      receipt,
    );
    this.steeringQueue.applied(steering.steeringId, committed.state);
    return this.ack(envelope, committed.state);
  }

  private async resume(
    envelope: E03ControlEnvelope,
    parentInput?: RuntimeRunInput,
  ): Promise<E03ControlResponse> {
    if (!parentInput)
      throw new E03RuntimeError(
        "resume_runtime_unavailable",
        "agent resume requires a parent QueryEngine input",
      );
    const task = this.requireAuthority(
      envelope,
      text(envelope.body.task_id, "task_id"),
    );
    const state = await this.execution.resume(
      task.identity.taskId,
      envelope.expected_revision,
      parentInput,
      { prompt: envelope.body.prompt ?? task.prompt },
      envelope.request_id,
      envelope.idempotency_key,
    );
    return this.ack(envelope, state);
  }

  private async send(
    envelope: E03ControlEnvelope,
  ): Promise<E03ControlResponse> {
    const sender = this.requireAuthority(
      envelope,
      text(envelope.body.sender_task_id, "sender_task_id"),
    );
    const recipient = this.requireAuthority(
      envelope,
      text(envelope.body.recipient_task_id, "recipient_task_id"),
    );
    if (recipient.revision !== envelope.expected_revision)
      throw new E03RuntimeError(
        "stale_revision",
        `message expected ${envelope.expected_revision}, current ${recipient.revision}`,
      );
    const sent = this.mailbox.send(sender, recipient, {
      body: text(envelope.body.message, "message"),
      idempotencyKey: envelope.idempotency_key,
    });
    const proposed = sealTask({
      ...sent.recipient,
      revision: recipient.revision + 1,
      checksum: "",
    });
    const prepared = this.registry.prepare({
      requestId: envelope.request_id,
      idempotencyKey: envelope.idempotency_key,
      writerId: "typescript.E03AgentControlCoordinator",
      taskId: recipient.identity.taskId,
      expectedRevision: recipient.revision,
      proposed,
      effectKind: "persist",
      effectOperation: "persist_team_message",
      effectPayload: {
        message_id: sent.message.messageId,
        sender_task_id: sender.identity.taskId,
      },
    });
    const receipt = await this.registry.recordReceipt(envelope.idempotency_key);
    const committed = await this.registry.commit(
      envelope.idempotency_key,
      receipt,
    );
    return this.ack(envelope, committed.state);
  }

  private collect(envelope: E03ControlEnvelope): E03ControlResponse {
    const parent = text(envelope.body.parent_task_id, "parent_task_id");
    const tasks = this.registry
      .list(parent)
      .filter((task) => task.identity.runId === envelope.run_id);
    return response({
      ok: tasks.length > 0 && tasks.every((task) => isTerminal(task.status)),
      request_id: envelope.request_id,
      command: envelope.command,
      phase: "ack",
      revision: Math.max(0, ...tasks.map((task) => task.revision)),
      state: {
        parent_task_id: parent,
        completed: tasks
          .filter((task) => task.status === "completed")
          .map(taskProjection),
        failed: tasks
          .filter((task) => task.status === "failed")
          .map(taskProjection),
        cancelled: tasks
          .filter(
            (task) => task.status === "cancelled" || task.status === "killed",
          )
          .map(taskProjection),
        pending: tasks
          .filter((task) => !isTerminal(task.status))
          .map(taskProjection),
      },
    });
  }

  private requireAuthority(
    envelope: E03ControlEnvelope,
    taskId: string,
  ): E03TaskState {
    const task = this.registry.require(taskId);
    if (task.identity.runId !== envelope.run_id)
      throw new E03RuntimeError(
        "run_authority_denied",
        "control run id differs from task",
      );
    if (
      task.identity.parentSessionId !== envelope.session_id &&
      task.identity.sessionId !== envelope.session_id
    )
      throw new E03RuntimeError(
        "session_authority_denied",
        "control session id differs from task",
      );
    const parentRelated =
      task.identity.parentTaskId === envelope.parent_task_id ||
      task.identity.taskId === envelope.parent_task_id ||
      task.identity.lineage.includes(envelope.parent_task_id);
    if (!parentRelated)
      throw new E03RuntimeError(
        "task_authority_denied",
        "control parent task does not own target task",
      );
    return task;
  }

  private syntheticParent(
    task: E03TaskState,
    envelope: E03ControlEnvelope,
  ): E03TaskState {
    return sealTask({
      ...task,
      identity: {
        ...task.identity,
        taskId: envelope.parent_task_id,
        parentTaskId: task.identity.lineage.at(-2) ?? envelope.parent_task_id,
      },
      status: "running",
      checksum: "",
    });
  }

  private ack(
    envelope: E03ControlEnvelope,
    task: E03TaskState,
  ): E03ControlResponse {
    const acknowledged = response({
      ok: true,
      request_id: envelope.request_id,
      command: envelope.command,
      phase: "ack",
      revision: task.revision,
      state: taskProjection(task),
      result: task.result,
      error: task.error,
      dispatch_count: 1,
    });
    return this.registry.acknowledge(envelope.idempotency_key, acknowledged);
  }
}

function text(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim())
    throw new E03RuntimeError(
      "invalid_control_field",
      `${field} must be a non-empty string`,
    );
  return value.trim();
}

function integer(
  value: unknown,
  field: string,
  minimum: number,
  maximum: number,
): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < minimum ||
    (value as number) > maximum
  )
    throw new E03RuntimeError(
      "invalid_control_field",
      `${field} must be an integer between ${minimum} and ${maximum}`,
    );
  return value as number;
}

export interface ControlAuditRecord {
  auditId: string;
  requestId: string;
  idempotencyKey: string;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  targetTaskId: string;
  command: ControlCommand;
  descriptorDigest: string;
  phase: CommitPhase | "rejected";
  expectedRevision: number;
  resultingRevision: number;
  accepted: boolean;
  replayed: boolean;
  restored: boolean;
  dispatchCount: number;
  error: string;
  recordedAt: string;
  previousDigest: string;
  digest: string;
}

export class ControlAuditLedger {
  private records: ControlAuditRecord[] = [];
  private idempotency = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  record(
    envelope: E03ControlEnvelope,
    descriptor: ControlCommandDescriptor,
    response: E03ControlResponse,
  ): ControlAuditRecord {
    if (envelope.request_id !== response.request_id && !response.replayed)
      throw new E03RuntimeError(
        "control_audit_request_mismatch",
        "control response request differs from envelope",
      );
    if (envelope.command !== response.command)
      throw new E03RuntimeError(
        "control_audit_command_mismatch",
        "control response command differs from envelope",
      );
    if (
      response.runtime_origin !== "typescript.E03AgentControlCoordinator" ||
      response.python_logical_owner !== false ||
      response.python_fallback_attempted !== false
    )
      throw new E03RuntimeError(
        "control_audit_owner_mismatch",
        "control response reports a non-TypeScript logical owner",
      );
    const priorDigest = this.idempotency.get(envelope.idempotency_key);
    if (priorDigest) {
      const prior = this.records.find(
        (record) => record.digest === priorDigest,
      )!;
      if (
        prior.command !== envelope.command ||
        prior.targetTaskId !== targetTaskId(envelope)
      )
        throw new E03RuntimeError(
          "control_audit_idempotency_conflict",
          "control idempotency key changed command or task",
        );
    }
    const previousDigest = this.records.at(-1)?.digest ?? "";
    const payload = {
      auditId: `control-audit-${digest({ requestId: envelope.request_id, idempotencyKey: envelope.idempotency_key, previousDigest }).slice(0, 32)}`,
      requestId: envelope.request_id,
      idempotencyKey: envelope.idempotency_key,
      runId: envelope.run_id,
      sessionId: envelope.session_id,
      parentTaskId: envelope.parent_task_id,
      targetTaskId: targetTaskId(envelope),
      command: envelope.command,
      descriptorDigest: descriptor.digest,
      phase: response.phase,
      expectedRevision: envelope.expected_revision,
      resultingRevision: response.revision,
      accepted: response.ok,
      replayed: response.replayed,
      restored: response.restored,
      dispatchCount: response.dispatch_count,
      error: response.error,
      recordedAt: this.clock.now(),
      previousDigest,
    };
    const record = { ...payload, digest: digest(payload) };
    this.records.push(record);
    this.idempotency.set(record.idempotencyKey, record.digest);
    return structuredClone(record);
  }

  restore(records: readonly ControlAuditRecord[]): void {
    let previousDigest = "";
    const keys = new Map<string, string>();
    for (const record of records) {
      this.assertRecord(record, previousDigest);
      keys.set(record.idempotencyKey, record.digest);
      previousDigest = record.digest;
    }
    this.records = records.map((record) => structuredClone(record));
    this.idempotency = keys;
  }

  snapshot(): ControlAuditRecord[] {
    return this.records.map((record) => structuredClone(record));
  }

  forTask(taskId: string): ControlAuditRecord[] {
    return this.snapshot().filter(
      (record) =>
        record.targetTaskId === taskId || record.parentTaskId === taskId,
    );
  }

  forSession(runId: string, sessionId: string): ControlAuditRecord[] {
    return this.snapshot().filter(
      (record) => record.runId === runId && record.sessionId === sessionId,
    );
  }

  verifyChain(): {
    valid: boolean;
    count: number;
    headDigest: string;
    rejected: number;
    replayed: number;
  } {
    let previousDigest = "";
    for (const record of this.records) {
      this.assertRecord(record, previousDigest);
      previousDigest = record.digest;
    }
    return {
      valid: true,
      count: this.records.length,
      headDigest: previousDigest,
      rejected: this.records.filter((record) => !record.accepted).length,
      replayed: this.records.filter((record) => record.replayed).length,
    };
  }

  private assertRecord(
    record: ControlAuditRecord,
    previousDigest: string,
  ): void {
    const { digest: checksum, ...payload } = record;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "control_audit_digest",
        `audit ${record.auditId} digest is invalid`,
      );
    if (record.previousDigest !== previousDigest)
      throw new E03RuntimeError(
        "control_audit_chain",
        `audit ${record.auditId} does not extend prior digest`,
      );
    if (
      !record.auditId ||
      !record.requestId ||
      !record.idempotencyKey ||
      !record.runId ||
      !record.sessionId ||
      !record.parentTaskId ||
      !record.targetTaskId
    )
      throw new E03RuntimeError(
        "invalid_control_audit",
        "control audit identity is incomplete",
      );
    if (record.accepted && record.phase !== "ack")
      throw new E03RuntimeError(
        "invalid_control_audit_phase",
        "accepted control record is not ACKed",
      );
    if (!record.accepted && record.phase !== "rejected" && !record.error)
      throw new E03RuntimeError(
        "invalid_control_audit_rejection",
        "rejected control record has no error",
      );
    if (record.resultingRevision < 0 || record.expectedRevision < 0)
      throw new E03RuntimeError(
        "invalid_control_audit_revision",
        "control audit revision is negative",
      );
  }
}

function targetTaskId(envelope: E03ControlEnvelope): string {
  return String(
    envelope.body.task_id ??
      envelope.body.parent_task_id ??
      envelope.body.recipient_task_id ??
      envelope.parent_task_id,
  );
}

export type ControlRequestState =
  | "received"
  | "validated"
  | "authorized"
  | "prepared"
  | "effect-started"
  | "effect-received"
  | "committed"
  | "acknowledged"
  | "rejected"
  | "cancelled";

export interface SessionControlRequestRecord {
  recordId: string;
  requestId: string;
  idempotencyKey: string;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  targetTaskId: string;
  command: ControlCommand;
  state: ControlRequestState;
  priorState: ControlRequestState | null;
  expectedRevision: number;
  resultingRevision: number;
  leaseId: string | null;
  transitionId: string | null;
  effectId: string | null;
  receiptId: string | null;
  responseDigest: string | null;
  errorCode: string;
  errorDigest: string;
  bodyDigest: string;
  receivedAt: string;
  updatedAt: string;
  terminalAt: string | null;
  revision: number;
  previousDigest: string;
  digest: string;
}

export interface SessionControlProjection {
  runId: string;
  sessionId: string;
  parentTaskId: string;
  received: number;
  active: number;
  committed: number;
  acknowledged: number;
  rejected: number;
  cancelled: number;
  lostAckCandidates: string[];
  activeRequestIds: string[];
  terminalRequestIds: string[];
  headDigest: string;
  projectedAt: string;
  digest: string;
}

const CONTROL_REQUEST_TRANSITIONS: Readonly<
  Record<ControlRequestState, readonly ControlRequestState[]>
> = Object.freeze({
  received: ["validated", "rejected", "cancelled"],
  validated: ["authorized", "rejected", "cancelled"],
  authorized: ["prepared", "committed", "rejected", "cancelled"],
  prepared: ["effect-started", "committed", "rejected", "cancelled"],
  "effect-started": ["effect-received", "rejected", "cancelled"],
  "effect-received": ["committed", "rejected", "cancelled"],
  committed: ["acknowledged"],
  acknowledged: [],
  rejected: [],
  cancelled: [],
});

function terminalControlState(state: ControlRequestState): boolean {
  return (
    state === "acknowledged" || state === "rejected" || state === "cancelled"
  );
}

function assertControlRequestRecord(record: SessionControlRequestRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "session_control_record_checksum",
      `session control record ${record.recordId} checksum mismatch`,
    );
  if (
    !record.requestId ||
    !record.idempotencyKey ||
    !record.runId ||
    !record.sessionId ||
    !record.parentTaskId ||
    !record.targetTaskId ||
    record.expectedRevision < 0 ||
    record.resultingRevision < 0 ||
    record.revision < 1
  )
    throw new E03RuntimeError(
      "session_control_record_invalid",
      `session control record ${record.recordId} is invalid`,
    );
  if (terminalControlState(record.state) && !record.terminalAt)
    throw new E03RuntimeError(
      "session_control_terminal_time",
      `terminal session control record ${record.recordId} has no terminal time`,
    );
  if (record.state === "acknowledged" && !record.responseDigest)
    throw new E03RuntimeError(
      "session_control_response_digest",
      `acknowledged session control record ${record.recordId} has no response digest`,
    );
  if (record.state === "rejected" && !record.errorCode)
    throw new E03RuntimeError(
      "session_control_error_code",
      `rejected session control record ${record.recordId} has no error code`,
    );
}

export class SessionControlRequestJournal {
  private records: SessionControlRequestRecord[] = [];
  private currentByRequest = new Map<string, SessionControlRequestRecord>();
  private requestByIdempotency = new Map<string, string>();

  receive(
    envelope: E03ControlEnvelope,
    now = new Date().toISOString(),
  ): SessionControlRequestRecord {
    const bound = this.requestByIdempotency.get(envelope.idempotency_key);
    if (bound) {
      const record = this.currentByRequest.get(bound)!;
      if (
        record.command !== envelope.command ||
        record.targetTaskId !== targetTaskId(envelope) ||
        record.bodyDigest !== digest(envelope.body)
      )
        throw new E03RuntimeError(
          "session_control_idempotency_conflict",
          "control idempotency key identifies a different request",
        );
      return structuredClone(record);
    }
    if (this.currentByRequest.has(envelope.request_id))
      throw new E03RuntimeError(
        "session_control_request_duplicate",
        `control request ${envelope.request_id} already exists`,
      );
    const previousDigest = this.records.at(-1)?.digest ?? "";
    const payload = {
      recordId: `session-control-record-${digest({
        requestId: envelope.request_id,
        state: "received",
        previousDigest,
      }).slice(0, 32)}`,
      requestId: envelope.request_id,
      idempotencyKey: envelope.idempotency_key,
      runId: envelope.run_id,
      sessionId: envelope.session_id,
      parentTaskId: envelope.parent_task_id,
      targetTaskId: targetTaskId(envelope),
      command: envelope.command,
      state: "received" as const,
      priorState: null,
      expectedRevision: envelope.expected_revision,
      resultingRevision: envelope.expected_revision,
      leaseId: null,
      transitionId: null,
      effectId: null,
      receiptId: null,
      responseDigest: null,
      errorCode: "",
      errorDigest: "",
      bodyDigest: digest(envelope.body),
      receivedAt: now,
      updatedAt: now,
      terminalAt: null,
      revision: 1,
      previousDigest,
    };
    const record = { ...payload, digest: digest(payload) };
    assertControlRequestRecord(record);
    this.records.push(record);
    this.currentByRequest.set(record.requestId, record);
    this.requestByIdempotency.set(record.idempotencyKey, record.requestId);
    return structuredClone(record);
  }

  advance(input: {
    requestId: string;
    state: ControlRequestState;
    expectedRecordRevision: number;
    resultingRevision?: number;
    leaseId?: string;
    transitionId?: string;
    effectId?: string;
    receiptId?: string;
    response?: E03ControlResponse;
    errorCode?: string;
    error?: string;
    now?: string;
  }): SessionControlRequestRecord {
    const current = this.require(input.requestId);
    if (current.state === input.state) return structuredClone(current);
    if (terminalControlState(current.state))
      throw new E03RuntimeError(
        "session_control_terminal",
        `control request ${input.requestId} is already ${current.state}`,
      );
    if (current.revision !== input.expectedRecordRevision)
      throw new E03RuntimeError(
        "session_control_stale_revision",
        `control request ${input.requestId} record revision changed`,
      );
    if (!CONTROL_REQUEST_TRANSITIONS[current.state].includes(input.state))
      throw new E03RuntimeError(
        "session_control_state_transition",
        `control request cannot move ${current.state}->${input.state}`,
      );
    if (input.state === "acknowledged" && !input.response)
      throw new E03RuntimeError(
        "session_control_ack_response_missing",
        "acknowledged control request requires a response",
      );
    if (input.state === "rejected" && !input.errorCode?.trim())
      throw new E03RuntimeError(
        "session_control_rejection_code_missing",
        "rejected control request requires an error code",
      );
    const now = input.now ?? new Date().toISOString();
    const previousDigest = this.records.at(-1)?.digest ?? "";
    const payload = {
      ...current,
      recordId: `session-control-record-${digest({
        requestId: current.requestId,
        state: input.state,
        revision: current.revision + 1,
        previousDigest,
      }).slice(0, 32)}`,
      state: input.state,
      priorState: current.state,
      resultingRevision: input.resultingRevision ?? current.resultingRevision,
      leaseId: input.leaseId?.trim() || current.leaseId,
      transitionId: input.transitionId?.trim() || current.transitionId,
      effectId: input.effectId?.trim() || current.effectId,
      receiptId: input.receiptId?.trim() || current.receiptId,
      responseDigest: input.response
        ? digest(input.response)
        : current.responseDigest,
      errorCode: input.errorCode?.trim() || "",
      errorDigest: input.error ? digest(input.error) : "",
      updatedAt: now,
      terminalAt: terminalControlState(input.state) ? now : null,
      revision: current.revision + 1,
      previousDigest,
      digest: "",
    };
    const { digest: _, ...body } = payload;
    const next = { ...body, digest: digest(body) };
    assertControlRequestRecord(next);
    this.records.push(next);
    this.currentByRequest.set(next.requestId, next);
    return structuredClone(next);
  }

  recoverLostAck(idempotencyKey: string): SessionControlRequestRecord | null {
    const requestId = this.requestByIdempotency.get(idempotencyKey);
    if (!requestId) return null;
    const record = this.currentByRequest.get(requestId)!;
    if (record.state !== "committed" && record.state !== "acknowledged")
      return null;
    return structuredClone(record);
  }

  project(
    runId: string,
    sessionId: string,
    parentTaskId: string,
  ): SessionControlProjection {
    const records = [...this.currentByRequest.values()].filter(
      (record) =>
        record.runId === runId &&
        record.sessionId === sessionId &&
        record.parentTaskId === parentTaskId,
    );
    const active = records.filter(
      (record) => !terminalControlState(record.state),
    );
    const terminal = records.filter((record) =>
      terminalControlState(record.state),
    );
    const payload = {
      runId,
      sessionId,
      parentTaskId,
      received: records.length,
      active: active.length,
      committed: records.filter((record) => record.state === "committed")
        .length,
      acknowledged: records.filter((record) => record.state === "acknowledged")
        .length,
      rejected: records.filter((record) => record.state === "rejected").length,
      cancelled: records.filter((record) => record.state === "cancelled")
        .length,
      lostAckCandidates: records
        .filter((record) => record.state === "committed")
        .map((record) => record.requestId)
        .sort(),
      activeRequestIds: active.map((record) => record.requestId).sort(),
      terminalRequestIds: terminal.map((record) => record.requestId).sort(),
      headDigest: this.records.at(-1)?.digest ?? "",
      projectedAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(records: readonly SessionControlRequestRecord[]): void {
    const next: SessionControlRequestRecord[] = [];
    const currentByRequest = new Map<string, SessionControlRequestRecord>();
    const requestByIdempotency = new Map<string, string>();
    let previousDigest = "";
    for (const raw of records) {
      const record = structuredClone(raw);
      assertControlRequestRecord(record);
      if (record.previousDigest !== previousDigest)
        throw new E03RuntimeError(
          "session_control_record_chain",
          `session control record ${record.recordId} breaks digest chain`,
        );
      const current = currentByRequest.get(record.requestId);
      if (!current && record.priorState !== null)
        throw new E03RuntimeError(
          "session_control_restore_missing_receive",
          `control request ${record.requestId} does not start at received`,
        );
      if (current) {
        if (
          record.priorState !== current.state ||
          record.revision !== current.revision + 1 ||
          !CONTROL_REQUEST_TRANSITIONS[current.state].includes(record.state)
        )
          throw new E03RuntimeError(
            "session_control_restore_transition",
            `control request ${record.requestId} restore transition is invalid`,
          );
      }
      const bound = requestByIdempotency.get(record.idempotencyKey);
      if (bound && bound !== record.requestId)
        throw new E03RuntimeError(
          "session_control_restore_idempotency",
          "control request idempotency key conflicts during restore",
        );
      next.push(record);
      currentByRequest.set(record.requestId, record);
      requestByIdempotency.set(record.idempotencyKey, record.requestId);
      previousDigest = record.digest;
    }
    this.records = next;
    this.currentByRequest = currentByRequest;
    this.requestByIdempotency = requestByIdempotency;
  }

  snapshot(): SessionControlRequestRecord[] {
    return this.records.map((record) => structuredClone(record));
  }

  private require(requestId: string): SessionControlRequestRecord {
    const record = this.currentByRequest.get(requestId);
    if (!record)
      throw new E03RuntimeError(
        "session_control_request_missing",
        `control request ${requestId} is missing`,
      );
    assertControlRequestRecord(record);
    return record;
  }
}

export interface ControlTaskLock {
  lockId: string;
  taskId: string;
  sessionId: string;
  requestId: string;
  idempotencyKey: string;
  command: ControlCommand;
  mode: "shared" | "exclusive";
  expectedTaskRevision: number;
  taskLeaseId: string;
  ownerId: string;
  acquiredAt: string;
  expiresAt: string;
  releasedAt: string | null;
  releaseReason: string;
  revision: number;
  digest: string;
}

export interface ControlLockDecision {
  accepted: boolean;
  code: string;
  taskId: string;
  requestId: string;
  mode: "shared" | "exclusive";
  conflictingLockIds: string[];
  activeLockCount: number;
  staleLockIds: string[];
  decidedAt: string;
  digest: string;
}

function assertControlTaskLock(lock: ControlTaskLock): void {
  const { digest: checksum, ...payload } = lock;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_task_lock_checksum",
      `control task lock ${lock.lockId} checksum mismatch`,
    );
  if (
    !lock.taskId ||
    !lock.requestId ||
    !lock.taskLeaseId ||
    !lock.ownerId ||
    lock.expectedTaskRevision < 0 ||
    lock.revision < 1 ||
    Date.parse(lock.expiresAt) <= Date.parse(lock.acquiredAt)
  )
    throw new E03RuntimeError(
      "control_task_lock_invalid",
      `control task lock ${lock.lockId} is invalid`,
    );
  if (lock.releasedAt && !lock.releaseReason)
    throw new E03RuntimeError(
      "control_task_lock_release_reason",
      `released control task lock ${lock.lockId} has no reason`,
    );
}

export class ControlTaskLockRuntime {
  private locks = new Map<string, ControlTaskLock>();
  private byTask = new Map<string, Set<string>>();
  private byIdempotency = new Map<string, string>();

  decide(input: {
    task: E03TaskState;
    requestId: string;
    mode: "shared" | "exclusive";
    now?: string;
  }): ControlLockDecision {
    const now = input.now ?? new Date().toISOString();
    const active = this.forTask(input.task.identity.taskId).filter(
      (lock) => !lock.releasedAt,
    );
    const staleLockIds = active
      .filter(
        (lock) =>
          Date.parse(now) >= Date.parse(lock.expiresAt) ||
          lock.taskLeaseId !== input.task.identity.leaseId,
      )
      .map((lock) => lock.lockId)
      .sort();
    const live = active.filter((lock) => !staleLockIds.includes(lock.lockId));
    const conflictingLockIds = live
      .filter(
        (lock) =>
          lock.requestId !== input.requestId &&
          (input.mode === "exclusive" || lock.mode === "exclusive"),
      )
      .map((lock) => lock.lockId)
      .sort();
    let code = "control_task_lock_accepted";
    if (isTerminal(input.task.status)) code = "control_task_lock_terminal";
    else if (conflictingLockIds.length) code = "control_task_lock_conflict";
    const payload = {
      accepted: code === "control_task_lock_accepted",
      code,
      taskId: input.task.identity.taskId,
      requestId: input.requestId,
      mode: input.mode,
      conflictingLockIds,
      activeLockCount: live.length,
      staleLockIds,
      decidedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  acquire(input: {
    task: E03TaskState;
    requestId: string;
    idempotencyKey: string;
    command: ControlCommand;
    mode: "shared" | "exclusive";
    ownerId: string;
    ttlMs: number;
    now?: string;
  }): ControlTaskLock {
    const bound = this.byIdempotency.get(input.idempotencyKey);
    if (bound) return structuredClone(this.locks.get(bound)!);
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "control_task_lock_ttl_invalid",
        "control task lock TTL is invalid",
      );
    const decision = this.decide({
      task: input.task,
      requestId: input.requestId,
      mode: input.mode,
      now: input.now,
    });
    for (const staleLockId of decision.staleLockIds)
      this.release(staleLockId, "stale_lock_reaped", decision.decidedAt);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `control request ${input.requestId} cannot lock task ${input.task.identity.taskId}`,
        { conflictingLockIds: decision.conflictingLockIds },
      );
    const acquiredAt = input.now ?? new Date().toISOString();
    const payload = {
      lockId: `control-task-lock-${digest({
        taskId: input.task.identity.taskId,
        requestId: input.requestId,
        idempotencyKey: input.idempotencyKey,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      requestId: input.requestId,
      idempotencyKey: input.idempotencyKey,
      command: input.command,
      mode: input.mode,
      expectedTaskRevision: input.task.revision,
      taskLeaseId: input.task.identity.leaseId,
      ownerId: input.ownerId.trim(),
      acquiredAt,
      expiresAt: new Date(Date.parse(acquiredAt) + input.ttlMs).toISOString(),
      releasedAt: null,
      releaseReason: "",
      revision: 1,
    };
    const lock = { ...payload, digest: digest(payload) };
    assertControlTaskLock(lock);
    this.locks.set(lock.lockId, lock);
    this.byIdempotency.set(lock.idempotencyKey, lock.lockId);
    const ids = this.byTask.get(lock.taskId) ?? new Set<string>();
    ids.add(lock.lockId);
    this.byTask.set(lock.taskId, ids);
    return structuredClone(lock);
  }

  renew(input: {
    lockId: string;
    task: E03TaskState;
    expectedLockRevision: number;
    ttlMs: number;
    now?: string;
  }): ControlTaskLock {
    const current = this.require(input.lockId);
    if (current.releasedAt)
      throw new E03RuntimeError(
        "control_task_lock_released",
        `control task lock ${input.lockId} was released`,
      );
    if (current.revision !== input.expectedLockRevision)
      throw new E03RuntimeError(
        "control_task_lock_stale_revision",
        `control task lock ${input.lockId} revision changed`,
      );
    if (
      current.taskId !== input.task.identity.taskId ||
      current.taskLeaseId !== input.task.identity.leaseId
    )
      throw new E03RuntimeError(
        "control_task_lock_stale_lease",
        "control task lock belongs to another task lease",
      );
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "control_task_lock_ttl_invalid",
        "control task lock TTL is invalid",
      );
    const now = input.now ?? new Date().toISOString();
    const { digest: _, ...prior } = current;
    const payload = {
      ...prior,
      expectedTaskRevision: input.task.revision,
      expiresAt: new Date(Date.parse(now) + input.ttlMs).toISOString(),
      revision: current.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTaskLock(next);
    this.locks.set(next.lockId, next);
    return structuredClone(next);
  }

  release(
    lockId: string,
    reason: string,
    now = new Date().toISOString(),
  ): ControlTaskLock {
    const current = this.require(lockId);
    if (current.releasedAt) return structuredClone(current);
    const { digest: _, ...prior } = current;
    const payload = {
      ...prior,
      releasedAt: now,
      releaseReason: reason.trim() || "released",
      revision: current.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTaskLock(next);
    this.locks.set(lockId, next);
    return structuredClone(next);
  }

  assertHeld(input: {
    lockId: string;
    task: E03TaskState;
    requestId: string;
    command: ControlCommand;
    now?: string;
  }): ControlTaskLock {
    const lock = this.require(input.lockId);
    const now = input.now ?? new Date().toISOString();
    if (lock.releasedAt)
      throw new E03RuntimeError(
        "control_task_lock_released",
        `control task lock ${lock.lockId} was released`,
      );
    if (Date.parse(now) >= Date.parse(lock.expiresAt))
      throw new E03RuntimeError(
        "control_task_lock_expired",
        `control task lock ${lock.lockId} expired`,
      );
    if (
      lock.taskId !== input.task.identity.taskId ||
      lock.taskLeaseId !== input.task.identity.leaseId ||
      lock.requestId !== input.requestId ||
      lock.command !== input.command
    )
      throw new E03RuntimeError(
        "control_task_lock_custody",
        `control task lock ${lock.lockId} custody check failed`,
      );
    if (input.task.revision < lock.expectedTaskRevision)
      throw new E03RuntimeError(
        "control_task_lock_task_revision",
        "task revision is behind lock fence",
      );
    return lock;
  }

  restore(locks: readonly ControlTaskLock[]): void {
    const next = new Map<string, ControlTaskLock>();
    const byTask = new Map<string, Set<string>>();
    const byIdempotency = new Map<string, string>();
    for (const raw of locks) {
      const lock = structuredClone(raw);
      assertControlTaskLock(lock);
      if (next.has(lock.lockId) || byIdempotency.has(lock.idempotencyKey))
        throw new E03RuntimeError(
          "duplicate_control_task_lock",
          `control task lock ${lock.lockId} repeats`,
        );
      next.set(lock.lockId, lock);
      byIdempotency.set(lock.idempotencyKey, lock.lockId);
      const ids = byTask.get(lock.taskId) ?? new Set<string>();
      ids.add(lock.lockId);
      byTask.set(lock.taskId, ids);
    }
    for (const [taskId, ids] of byTask) {
      const live = [...ids]
        .map((lockId) => next.get(lockId)!)
        .filter((lock) => !lock.releasedAt);
      if (live.some((lock) => lock.mode === "exclusive") && live.length > 1)
        throw new E03RuntimeError(
          "control_task_lock_restore_conflict",
          `task ${taskId} has conflicting restored locks`,
        );
    }
    this.locks = next;
    this.byTask = byTask;
    this.byIdempotency = byIdempotency;
  }

  snapshot(): ControlTaskLock[] {
    return [...this.locks.values()]
      .sort(
        (left, right) =>
          left.taskId.localeCompare(right.taskId) ||
          left.acquiredAt.localeCompare(right.acquiredAt),
      )
      .map((lock) => structuredClone(lock));
  }

  private forTask(taskId: string): ControlTaskLock[] {
    return [...(this.byTask.get(taskId) ?? new Set<string>())].map(
      (lockId) => this.locks.get(lockId)!,
    );
  }

  private require(lockId: string): ControlTaskLock {
    const lock = this.locks.get(lockId);
    if (!lock)
      throw new E03RuntimeError(
        "control_task_lock_missing",
        `control task lock ${lockId} is missing`,
      );
    assertControlTaskLock(lock);
    return lock;
  }
}

export type ControlResultDeliveryState =
  | "pending"
  | "leased"
  | "delivered"
  | "acknowledged"
  | "expired"
  | "dead-lettered";

export interface ControlResultDelivery {
  deliveryId: string;
  requestId: string;
  idempotencyKey: string;
  sessionId: string;
  taskId: string;
  command: ControlCommand;
  response: E03ControlResponse;
  state: ControlResultDeliveryState;
  consumerId: string | null;
  attempt: number;
  availableAt: string;
  leaseExpiresAt: string | null;
  createdAt: string;
  deliveredAt: string | null;
  acknowledgedAt: string | null;
  lastError: string | null;
  revision: number;
  digest: string;
}

export interface ControlResultSubscription {
  subscriptionId: string;
  sessionId: string;
  consumerId: string;
  commandFilter: ControlCommand[];
  taskFilter: string[];
  maximumInFlight: number;
  leaseDurationMs: number;
  state: "active" | "paused" | "revoked";
  inFlightDeliveryIds: string[];
  acknowledgedDeliveryIds: string[];
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

function assertControlResultDelivery(delivery: ControlResultDelivery): void {
  const { digest: checksum, ...payload } = delivery;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_result_delivery_digest",
      `control result delivery ${delivery.deliveryId} digest is invalid`,
    );
  if (
    !delivery.deliveryId ||
    !delivery.requestId ||
    !delivery.idempotencyKey ||
    !delivery.sessionId ||
    !delivery.taskId
  )
    throw new E03RuntimeError(
      "control_result_delivery_identity",
      "control result delivery identity is incomplete",
    );
  if (delivery.response.request_id !== delivery.requestId)
    throw new E03RuntimeError(
      "control_result_delivery_response",
      "control result response belongs to another request",
    );
  if (!Number.isSafeInteger(delivery.attempt) || delivery.attempt < 0)
    throw new E03RuntimeError(
      "control_result_delivery_attempt",
      "control result delivery attempt is invalid",
    );
  if (!Number.isSafeInteger(delivery.revision) || delivery.revision < 1)
    throw new E03RuntimeError(
      "control_result_delivery_revision",
      "control result delivery revision is invalid",
    );
  if (
    delivery.state === "leased" &&
    (!delivery.consumerId || !delivery.leaseExpiresAt)
  )
    throw new E03RuntimeError(
      "control_result_delivery_lease",
      "leased control result requires consumer and lease expiry",
    );
}

function assertControlResultSubscription(
  subscription: ControlResultSubscription,
): void {
  const { digest: checksum, ...payload } = subscription;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_result_subscription_digest",
      `control result subscription ${subscription.subscriptionId} digest is invalid`,
    );
  if (
    !subscription.subscriptionId ||
    !subscription.sessionId ||
    !subscription.consumerId
  )
    throw new E03RuntimeError(
      "control_result_subscription_identity",
      "control result subscription identity is incomplete",
    );
  if (
    !Number.isSafeInteger(subscription.maximumInFlight) ||
    subscription.maximumInFlight < 1
  )
    throw new E03RuntimeError(
      "control_result_subscription_inflight",
      "control result subscription in-flight limit is invalid",
    );
  if (
    !Number.isSafeInteger(subscription.leaseDurationMs) ||
    subscription.leaseDurationMs < 100
  )
    throw new E03RuntimeError(
      "control_result_subscription_lease",
      "control result subscription lease duration is invalid",
    );
  if (!Number.isSafeInteger(subscription.revision) || subscription.revision < 1)
    throw new E03RuntimeError(
      "control_result_subscription_revision",
      "control result subscription revision is invalid",
    );
}

export class ControlResultDeliveryRuntime {
  private deliveries = new Map<string, ControlResultDelivery>();
  private subscriptions = new Map<string, ControlResultSubscription>();
  private byIdempotency = new Map<string, string>();

  constructor(
    private readonly clock: E03Clock = new SystemE03Clock(),
    private readonly maximumAttempts = 5,
  ) {
    if (!Number.isSafeInteger(maximumAttempts) || maximumAttempts < 1)
      throw new E03RuntimeError(
        "control_result_attempt_limit",
        "control result maximum attempts is invalid",
      );
  }

  publish(input: {
    envelope: E03ControlEnvelope;
    taskId: string;
    response: E03ControlResponse;
    availableAt?: string;
  }): ControlResultDelivery {
    const priorId = this.byIdempotency.get(input.envelope.idempotency_key);
    if (priorId) {
      const prior = this.requireDelivery(priorId);
      if (digest(prior.response) !== digest(input.response))
        throw new E03RuntimeError(
          "control_result_idempotency_conflict",
          `control result ${input.envelope.idempotency_key} changed`,
        );
      return structuredClone(prior);
    }
    if (
      input.response.request_id !== input.envelope.request_id ||
      input.response.command !== input.envelope.command
    )
      throw new E03RuntimeError(
        "control_result_response_custody",
        "control result response belongs to another request",
      );
    const now = this.clock.now();
    const payload = {
      deliveryId: createId("control-result-delivery"),
      requestId: input.envelope.request_id,
      idempotencyKey: input.envelope.idempotency_key,
      sessionId: input.envelope.session_id,
      taskId: input.taskId,
      command: input.envelope.command,
      response: structuredClone(input.response),
      state: "pending" as const,
      consumerId: null,
      attempt: 0,
      availableAt: input.availableAt ?? now,
      leaseExpiresAt: null,
      createdAt: now,
      deliveredAt: null,
      acknowledgedAt: null,
      lastError: null,
      revision: 1,
    };
    const delivery = { ...payload, digest: digest(payload) };
    assertControlResultDelivery(delivery);
    this.deliveries.set(delivery.deliveryId, delivery);
    this.byIdempotency.set(delivery.idempotencyKey, delivery.deliveryId);
    return structuredClone(delivery);
  }

  subscribe(input: {
    sessionId: string;
    consumerId: string;
    commandFilter?: readonly ControlCommand[];
    taskFilter?: readonly string[];
    maximumInFlight?: number;
    leaseDurationMs?: number;
  }): ControlResultSubscription {
    if (
      [...this.subscriptions.values()].some(
        (candidate) =>
          candidate.sessionId === input.sessionId &&
          candidate.consumerId === input.consumerId &&
          candidate.state !== "revoked",
      )
    )
      throw new E03RuntimeError(
        "control_result_subscription_duplicate",
        `control result consumer ${input.consumerId} is already subscribed`,
      );
    const now = this.clock.now();
    const payload = {
      subscriptionId: createId("control-result-subscription"),
      sessionId: input.sessionId.trim(),
      consumerId: input.consumerId.trim(),
      commandFilter: [...new Set(input.commandFilter ?? [])],
      taskFilter: [...new Set(input.taskFilter ?? [])],
      maximumInFlight: input.maximumInFlight ?? 16,
      leaseDurationMs: input.leaseDurationMs ?? 30_000,
      state: "active" as const,
      inFlightDeliveryIds: [],
      acknowledgedDeliveryIds: [],
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const subscription = { ...payload, digest: digest(payload) };
    assertControlResultSubscription(subscription);
    this.subscriptions.set(subscription.subscriptionId, subscription);
    return structuredClone(subscription);
  }

  lease(
    subscriptionId: string,
    expectedRevision: number,
  ): {
    subscription: ControlResultSubscription;
    delivery: ControlResultDelivery | null;
  } {
    let subscription = this.requireSubscription(subscriptionId);
    this.assertSubscriptionRevision(subscription, expectedRevision);
    if (subscription.state !== "active")
      throw new E03RuntimeError(
        "control_result_subscription_state",
        `control result subscription ${subscriptionId} is ${subscription.state}`,
      );
    this.sweep(this.clock.now());
    subscription = this.requireSubscription(subscriptionId);
    if (subscription.inFlightDeliveryIds.length >= subscription.maximumInFlight)
      throw new E03RuntimeError(
        "control_result_subscription_backpressure",
        `control result subscription ${subscriptionId} reached capacity`,
      );
    const now = this.clock.now();
    const acknowledged = new Set(subscription.acknowledgedDeliveryIds);
    const candidate = [...this.deliveries.values()]
      .filter((delivery) => delivery.sessionId === subscription.sessionId)
      .filter((delivery) => delivery.state === "pending")
      .filter((delivery) => Date.parse(delivery.availableAt) <= Date.parse(now))
      .filter((delivery) => !acknowledged.has(delivery.deliveryId))
      .filter(
        (delivery) =>
          !subscription.commandFilter.length ||
          subscription.commandFilter.includes(delivery.command),
      )
      .filter(
        (delivery) =>
          !subscription.taskFilter.length ||
          subscription.taskFilter.includes(delivery.taskId),
      )
      .sort(
        (left, right) =>
          left.createdAt.localeCompare(right.createdAt) ||
          left.deliveryId.localeCompare(right.deliveryId),
      )[0];
    if (!candidate)
      return { subscription: structuredClone(subscription), delivery: null };
    const delivery = this.transitionDelivery(candidate, {
      state: "leased",
      consumerId: subscription.consumerId,
      attempt: candidate.attempt + 1,
      leaseExpiresAt: new Date(
        Date.parse(now) + subscription.leaseDurationMs,
      ).toISOString(),
    });
    subscription = this.transitionSubscription(subscription, {
      inFlightDeliveryIds: [
        ...subscription.inFlightDeliveryIds,
        delivery.deliveryId,
      ],
    });
    return { subscription, delivery };
  }

  delivered(
    deliveryId: string,
    consumerId: string,
    expectedRevision: number,
  ): ControlResultDelivery {
    const delivery = this.requireDelivery(deliveryId);
    this.assertDeliveryRevision(delivery, expectedRevision);
    if (delivery.state !== "leased" || delivery.consumerId !== consumerId)
      throw new E03RuntimeError(
        "control_result_delivery_custody",
        `control result delivery ${deliveryId} is not leased by ${consumerId}`,
      );
    return this.transitionDelivery(delivery, {
      state: "delivered",
      deliveredAt: this.clock.now(),
    });
  }

  acknowledge(input: {
    deliveryId: string;
    subscriptionId: string;
    expectedDeliveryRevision: number;
    expectedSubscriptionRevision: number;
  }): {
    delivery: ControlResultDelivery;
    subscription: ControlResultSubscription;
  } {
    const delivery = this.requireDelivery(input.deliveryId);
    const subscription = this.requireSubscription(input.subscriptionId);
    this.assertDeliveryRevision(delivery, input.expectedDeliveryRevision);
    this.assertSubscriptionRevision(
      subscription,
      input.expectedSubscriptionRevision,
    );
    if (
      delivery.state !== "delivered" ||
      delivery.consumerId !== subscription.consumerId ||
      !subscription.inFlightDeliveryIds.includes(delivery.deliveryId)
    )
      throw new E03RuntimeError(
        "control_result_ack_custody",
        `control result delivery ${delivery.deliveryId} is not in flight`,
      );
    const nextDelivery = this.transitionDelivery(delivery, {
      state: "acknowledged",
      acknowledgedAt: this.clock.now(),
      leaseExpiresAt: null,
    });
    const nextSubscription = this.transitionSubscription(subscription, {
      inFlightDeliveryIds: subscription.inFlightDeliveryIds.filter(
        (deliveryId) => deliveryId !== delivery.deliveryId,
      ),
      acknowledgedDeliveryIds: [
        ...new Set([
          ...subscription.acknowledgedDeliveryIds,
          delivery.deliveryId,
        ]),
      ],
    });
    return { delivery: nextDelivery, subscription: nextSubscription };
  }

  fail(input: {
    deliveryId: string;
    subscriptionId: string;
    expectedDeliveryRevision: number;
    expectedSubscriptionRevision: number;
    error: string;
    retryDelayMs?: number;
  }): {
    delivery: ControlResultDelivery;
    subscription: ControlResultSubscription;
  } {
    const delivery = this.requireDelivery(input.deliveryId);
    const subscription = this.requireSubscription(input.subscriptionId);
    this.assertDeliveryRevision(delivery, input.expectedDeliveryRevision);
    this.assertSubscriptionRevision(
      subscription,
      input.expectedSubscriptionRevision,
    );
    if (delivery.consumerId !== subscription.consumerId)
      throw new E03RuntimeError(
        "control_result_failure_custody",
        `control result delivery ${delivery.deliveryId} belongs to another consumer`,
      );
    const deadLetter = delivery.attempt >= this.maximumAttempts;
    const nextDelivery = this.transitionDelivery(delivery, {
      state: deadLetter ? "dead-lettered" : "pending",
      consumerId: null,
      availableAt: deadLetter
        ? delivery.availableAt
        : new Date(
            Date.parse(this.clock.now()) + (input.retryDelayMs ?? 1_000),
          ).toISOString(),
      leaseExpiresAt: null,
      lastError: input.error.trim() || "control result delivery failed",
    });
    const nextSubscription = this.transitionSubscription(subscription, {
      inFlightDeliveryIds: subscription.inFlightDeliveryIds.filter(
        (deliveryId) => deliveryId !== delivery.deliveryId,
      ),
    });
    return { delivery: nextDelivery, subscription: nextSubscription };
  }

  sweep(now: string): ControlResultDelivery[] {
    const expired: ControlResultDelivery[] = [];
    for (const current of this.deliveries.values()) {
      if (
        current.state !== "leased" ||
        current.leaseExpiresAt === null ||
        Date.parse(now) < Date.parse(current.leaseExpiresAt)
      )
        continue;
      const subscription = [...this.subscriptions.values()].find(
        (candidate) =>
          candidate.consumerId === current.consumerId &&
          candidate.inFlightDeliveryIds.includes(current.deliveryId),
      );
      const deadLetter = current.attempt >= this.maximumAttempts;
      expired.push(
        this.transitionDelivery(current, {
          state: deadLetter ? "dead-lettered" : "pending",
          consumerId: null,
          leaseExpiresAt: null,
          lastError: "delivery lease expired",
        }),
      );
      if (subscription)
        this.transitionSubscription(subscription, {
          inFlightDeliveryIds: subscription.inFlightDeliveryIds.filter(
            (deliveryId) => deliveryId !== current.deliveryId,
          ),
        });
    }
    return expired;
  }

  snapshot(): {
    deliveries: ControlResultDelivery[];
    subscriptions: ControlResultSubscription[];
  } {
    return {
      deliveries: [...this.deliveries.values()].map((value) =>
        structuredClone(value),
      ),
      subscriptions: [...this.subscriptions.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    deliveries: readonly ControlResultDelivery[];
    subscriptions: readonly ControlResultSubscription[];
  }): void {
    const deliveries = new Map<string, ControlResultDelivery>();
    const subscriptions = new Map<string, ControlResultSubscription>();
    const byIdempotency = new Map<string, string>();
    for (const delivery of input.deliveries) {
      assertControlResultDelivery(delivery);
      if (
        deliveries.has(delivery.deliveryId) ||
        byIdempotency.has(delivery.idempotencyKey)
      )
        throw new E03RuntimeError(
          "control_result_delivery_restore_duplicate",
          `duplicate control result delivery ${delivery.deliveryId}`,
        );
      deliveries.set(delivery.deliveryId, structuredClone(delivery));
      byIdempotency.set(delivery.idempotencyKey, delivery.deliveryId);
    }
    for (const subscription of input.subscriptions) {
      assertControlResultSubscription(subscription);
      if (subscriptions.has(subscription.subscriptionId))
        throw new E03RuntimeError(
          "control_result_subscription_restore_duplicate",
          `duplicate control result subscription ${subscription.subscriptionId}`,
        );
      for (const deliveryId of [
        ...subscription.inFlightDeliveryIds,
        ...subscription.acknowledgedDeliveryIds,
      ])
        if (!deliveries.has(deliveryId))
          throw new E03RuntimeError(
            "control_result_subscription_restore_delivery",
            `control result subscription references missing delivery ${deliveryId}`,
          );
      subscriptions.set(
        subscription.subscriptionId,
        structuredClone(subscription),
      );
    }
    this.deliveries = deliveries;
    this.subscriptions = subscriptions;
    this.byIdempotency = byIdempotency;
  }

  private requireDelivery(deliveryId: string): ControlResultDelivery {
    const delivery = this.deliveries.get(deliveryId);
    if (!delivery)
      throw new E03RuntimeError(
        "control_result_delivery_missing",
        `control result delivery ${deliveryId} does not exist`,
      );
    assertControlResultDelivery(delivery);
    return delivery;
  }

  private requireSubscription(
    subscriptionId: string,
  ): ControlResultSubscription {
    const subscription = this.subscriptions.get(subscriptionId);
    if (!subscription)
      throw new E03RuntimeError(
        "control_result_subscription_missing",
        `control result subscription ${subscriptionId} does not exist`,
      );
    assertControlResultSubscription(subscription);
    return subscription;
  }

  private assertDeliveryRevision(
    delivery: ControlResultDelivery,
    expected: number,
  ): void {
    if (delivery.revision !== expected)
      throw new E03RuntimeError(
        "control_result_delivery_stale_revision",
        `control result delivery ${delivery.deliveryId} revision is stale`,
      );
  }

  private assertSubscriptionRevision(
    subscription: ControlResultSubscription,
    expected: number,
  ): void {
    if (subscription.revision !== expected)
      throw new E03RuntimeError(
        "control_result_subscription_stale_revision",
        `control result subscription ${subscription.subscriptionId} revision is stale`,
      );
  }

  private transitionDelivery(
    delivery: ControlResultDelivery,
    patch: Partial<
      Omit<ControlResultDelivery, "deliveryId" | "revision" | "digest">
    >,
  ): ControlResultDelivery {
    const { digest: _, ...prior } = delivery;
    const payload = {
      ...prior,
      ...patch,
      deliveryId: delivery.deliveryId,
      revision: delivery.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlResultDelivery(next);
    this.deliveries.set(next.deliveryId, next);
    return structuredClone(next);
  }

  private transitionSubscription(
    subscription: ControlResultSubscription,
    patch: Partial<
      Omit<ControlResultSubscription, "subscriptionId" | "revision" | "digest">
    >,
  ): ControlResultSubscription {
    const { digest: _, ...prior } = subscription;
    const payload = {
      ...prior,
      ...patch,
      subscriptionId: subscription.subscriptionId,
      updatedAt: this.clock.now(),
      revision: subscription.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlResultSubscription(next);
    this.subscriptions.set(next.subscriptionId, next);
    return structuredClone(next);
  }
}
