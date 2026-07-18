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
import { TypeScriptControlRuntime } from "./runtime.ts";

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
    const decision = TypeScriptControlRuntime.decideAgentTerminalMutation(task, {
      action: "cancel",
      expectedRevision: envelope.expected_revision,
    });
    if (decision.replay)
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
    const decision = TypeScriptControlRuntime.decideAgentTerminalMutation(task, {
      action: "kill",
      expectedRevision: envelope.expected_revision,
    });
    if (decision.replay)
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

  private async ack(
    envelope: E03ControlEnvelope,
    task: E03TaskState,
  ): Promise<E03ControlResponse> {
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
    return this.registry.acknowledgeDurably(
      envelope.idempotency_key,
      acknowledged,
    );
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

export interface ControlSessionLease {
  leaseId: string;
  sessionId: string;
  principalId: string;
  generation: number;
  state: "opening" | "attached" | "detached" | "closing" | "closed" | "expired";
  connectionId: string | null;
  resumeTokenDigest: string;
  issuedAt: string;
  attachedAt: string | null;
  detachedAt: string | null;
  lastHeartbeatAt: string | null;
  expiresAt: string;
  requestCursor: number;
  responseCursor: number;
  acknowledgedResponseCursor: number;
  revision: number;
  digest: string;
}
export interface ControlSessionReplayEntry {
  entryId: string;
  leaseId: string;
  sessionId: string;
  direction: "request" | "response";
  cursor: number;
  requestId: string;
  command: ControlCommand;
  payload: JsonObject;
  state: "pending" | "delivered" | "acknowledged" | "discarded";
  createdAt: string;
  deliveredAt: string | null;
  acknowledgedAt: string | null;
  previousDigest: string;
  revision: number;
  digest: string;
}
function assertControlSessionLease(value: ControlSessionLease): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_session_lease_digest",
      `control session lease ${value.leaseId} is corrupt`,
    );
  if (
    !value.leaseId ||
    !value.sessionId ||
    !value.principalId ||
    !value.resumeTokenDigest ||
    !Number.isSafeInteger(value.generation) ||
    value.generation < 1 ||
    !Number.isSafeInteger(value.requestCursor) ||
    value.requestCursor < 0 ||
    !Number.isSafeInteger(value.responseCursor) ||
    value.responseCursor < 0 ||
    !Number.isSafeInteger(value.acknowledgedResponseCursor) ||
    value.acknowledgedResponseCursor < 0 ||
    value.acknowledgedResponseCursor > value.responseCursor ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "control_session_lease",
      `control session lease ${value.leaseId} is invalid`,
    );
  if (value.state === "attached" && (!value.connectionId || !value.attachedAt))
    throw new E03RuntimeError(
      "control_session_attachment",
      `attached control session lease ${value.leaseId} lacks connection`,
    );
}
function assertControlReplayEntry(value: ControlSessionReplayEntry): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_session_replay_digest",
      `control session replay entry ${value.entryId} is corrupt`,
    );
  if (
    !value.entryId ||
    !value.leaseId ||
    !value.sessionId ||
    !value.requestId ||
    !Number.isSafeInteger(value.cursor) ||
    value.cursor < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_session_replay",
      `control session replay entry ${value.entryId} is invalid`,
    );
}
export class ControlSessionLeaseRuntime {
  private leases = new Map<string, ControlSessionLease>();
  private activeBySession = new Map<string, string>();
  private replay = new Map<string, ControlSessionReplayEntry[]>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  open(input: {
    sessionId: string;
    principalId: string;
    resumeToken: string;
    ttlMs: number;
  }): ControlSessionLease {
    if (
      !input.sessionId.trim() ||
      !input.principalId.trim() ||
      !input.resumeToken ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "control_session_open_input",
        "control session open input is invalid",
      );
    const activeId = this.activeBySession.get(input.sessionId);
    if (activeId) {
      const active = this.requireLease(activeId);
      if (active.state !== "closed" && active.state !== "expired")
        throw new E03RuntimeError(
          "control_session_already_open",
          `control session ${input.sessionId} already has a lease`,
        );
    }
    const generation =
      Math.max(
        0,
        ...[...this.leases.values()]
          .filter((value) => value.sessionId === input.sessionId)
          .map((value) => value.generation),
      ) + 1;
    const payload = {
      leaseId: createId("control-session-lease"),
      sessionId: input.sessionId.trim(),
      principalId: input.principalId.trim(),
      generation,
      state: "opening" as const,
      connectionId: null,
      resumeTokenDigest: digest(input.resumeToken),
      issuedAt: this.clock.now(),
      attachedAt: null,
      detachedAt: null,
      lastHeartbeatAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      requestCursor: 0,
      responseCursor: 0,
      acknowledgedResponseCursor: 0,
      revision: 1,
    };
    const lease = { ...payload, digest: digest(payload) };
    assertControlSessionLease(lease);
    this.leases.set(lease.leaseId, lease);
    this.activeBySession.set(lease.sessionId, lease.leaseId);
    return structuredClone(lease);
  }
  attach(input: {
    leaseId: string;
    expectedRevision: number;
    principalId: string;
    resumeToken: string;
    connectionId: string;
    ttlMs: number;
  }): ControlSessionLease {
    const lease = this.requireLease(input.leaseId);
    this.assertLeaseRevision(lease, input.expectedRevision);
    if (
      lease.principalId !== input.principalId ||
      lease.resumeTokenDigest !== digest(input.resumeToken)
    )
      throw new E03RuntimeError(
        "control_session_resume_credentials",
        `control session lease ${lease.leaseId} resume credentials are invalid`,
      );
    if (lease.state !== "opening" && lease.state !== "detached")
      throw new E03RuntimeError(
        "control_session_attach_state",
        `control session lease ${lease.leaseId} is ${lease.state}`,
      );
    if (Date.parse(lease.expiresAt) <= Date.parse(this.clock.now()))
      return this.transitionLease(lease, {
        state: "expired",
        connectionId: null,
      });
    if (
      !input.connectionId.trim() ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "control_session_connection",
        "control session connection is invalid",
      );
    return this.transitionLease(lease, {
      state: "attached",
      connectionId: input.connectionId.trim(),
      attachedAt: this.clock.now(),
      lastHeartbeatAt: this.clock.now(),
      detachedAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
    });
  }
  heartbeat(
    leaseId: string,
    expectedRevision: number,
    connectionId: string,
    ttlMs: number,
  ): ControlSessionLease {
    const lease = this.requireLease(leaseId);
    this.assertLeaseRevision(lease, expectedRevision);
    if (lease.state !== "attached" || lease.connectionId !== connectionId)
      throw new E03RuntimeError(
        "control_session_heartbeat_state",
        `control session lease ${leaseId} is not attached to ${connectionId}`,
      );
    if (!Number.isSafeInteger(ttlMs) || ttlMs < 1)
      throw new E03RuntimeError(
        "control_session_heartbeat_ttl",
        "control session heartbeat TTL is invalid",
      );
    return this.transitionLease(lease, {
      lastHeartbeatAt: this.clock.now(),
      expiresAt: new Date(Date.parse(this.clock.now()) + ttlMs).toISOString(),
    });
  }
  detach(
    leaseId: string,
    expectedRevision: number,
    connectionId: string,
  ): ControlSessionLease {
    const lease = this.requireLease(leaseId);
    this.assertLeaseRevision(lease, expectedRevision);
    if (lease.state !== "attached" || lease.connectionId !== connectionId)
      throw new E03RuntimeError(
        "control_session_detach_state",
        `control session lease ${leaseId} is not attached to ${connectionId}`,
      );
    return this.transitionLease(lease, {
      state: "detached",
      connectionId: null,
      detachedAt: this.clock.now(),
    });
  }
  appendRequest(
    leaseId: string,
    expectedRevision: number,
    envelope: E03ControlEnvelope,
  ): { lease: ControlSessionLease; entry: ControlSessionReplayEntry } {
    const lease = this.requireLease(leaseId);
    this.assertLeaseRevision(lease, expectedRevision);
    if (lease.state !== "attached")
      throw new E03RuntimeError(
        "control_session_request_state",
        `control session lease ${leaseId} is ${lease.state}`,
      );
    if (envelope.session_id !== lease.sessionId)
      throw new E03RuntimeError(
        "control_session_request_custody",
        `control request ${envelope.request_id} belongs to another session`,
      );
    const duplicate = this.entries(leaseId).find(
      (value) =>
        value.direction === "request" &&
        value.requestId === envelope.request_id,
    );
    if (duplicate)
      return {
        lease: structuredClone(lease),
        entry: structuredClone(duplicate),
      };
    const nextLease = this.transitionLease(lease, {
      requestCursor: lease.requestCursor + 1,
    });
    const entry = this.appendEntry(nextLease, {
      direction: "request",
      cursor: nextLease.requestCursor,
      requestId: envelope.request_id,
      command: envelope.command,
      payload: structuredClone(envelope.body),
      state: "delivered",
    });
    return { lease: nextLease, entry };
  }
  appendResponse(
    leaseId: string,
    expectedRevision: number,
    responseValue: E03ControlResponse,
  ): { lease: ControlSessionLease; entry: ControlSessionReplayEntry } {
    const lease = this.requireLease(leaseId);
    this.assertLeaseRevision(lease, expectedRevision);
    if (lease.state !== "attached" && lease.state !== "detached")
      throw new E03RuntimeError(
        "control_session_response_state",
        `control session lease ${leaseId} is ${lease.state}`,
      );
    const duplicate = this.entries(leaseId).find(
      (value) =>
        value.direction === "response" &&
        value.requestId === responseValue.request_id,
    );
    if (duplicate)
      return {
        lease: structuredClone(lease),
        entry: structuredClone(duplicate),
      };
    const nextLease = this.transitionLease(lease, {
      responseCursor: lease.responseCursor + 1,
    });
    const entry = this.appendEntry(nextLease, {
      direction: "response",
      cursor: nextLease.responseCursor,
      requestId: responseValue.request_id,
      command: responseValue.command,
      payload: structuredClone(responseValue as unknown as JsonObject),
      state: "pending",
    });
    return { lease: nextLease, entry };
  }
  markDelivered(
    entryId: string,
    expectedRevision: number,
  ): ControlSessionReplayEntry {
    const entry = this.requireEntry(entryId);
    this.assertEntryRevision(entry, expectedRevision);
    if (entry.direction !== "response" || entry.state !== "pending")
      throw new E03RuntimeError(
        "control_session_replay_deliver_state",
        `control session replay entry ${entryId} is ${entry.state}`,
      );
    return this.transitionEntry(entry, {
      state: "delivered",
      deliveredAt: this.clock.now(),
    });
  }
  acknowledge(
    leaseId: string,
    expectedRevision: number,
    responseCursor: number,
  ): { lease: ControlSessionLease; entries: ControlSessionReplayEntry[] } {
    const lease = this.requireLease(leaseId);
    this.assertLeaseRevision(lease, expectedRevision);
    if (
      !Number.isSafeInteger(responseCursor) ||
      responseCursor <= lease.acknowledgedResponseCursor ||
      responseCursor > lease.responseCursor
    )
      throw new E03RuntimeError(
        "control_session_ack_cursor",
        `control session response cursor ${responseCursor} is invalid`,
      );
    const candidates = this.entries(leaseId).filter(
      (value) =>
        value.direction === "response" &&
        value.cursor <= responseCursor &&
        (value.state === "pending" || value.state === "delivered"),
    );
    const entries = candidates.map((value) =>
      this.transitionEntry(value, {
        state: "acknowledged",
        acknowledgedAt: this.clock.now(),
      }),
    );
    const nextLease = this.transitionLease(lease, {
      acknowledgedResponseCursor: responseCursor,
    });
    return { lease: nextLease, entries };
  }
  replayResponses(
    leaseId: string,
    afterCursor: number,
  ): ControlSessionReplayEntry[] {
    const lease = this.requireLease(leaseId);
    if (
      !Number.isSafeInteger(afterCursor) ||
      afterCursor < 0 ||
      afterCursor > lease.responseCursor
    )
      throw new E03RuntimeError(
        "control_session_replay_cursor",
        `control session replay cursor ${afterCursor} is invalid`,
      );
    return this.entries(leaseId)
      .filter(
        (value) =>
          value.direction === "response" &&
          value.cursor > afterCursor &&
          value.state !== "discarded",
      )
      .map((value) => structuredClone(value));
  }
  close(leaseId: string, expectedRevision: number): ControlSessionLease {
    const lease = this.requireLease(leaseId);
    this.assertLeaseRevision(lease, expectedRevision);
    if (lease.state === "closed" || lease.state === "expired")
      return structuredClone(lease);
    if (lease.responseCursor !== lease.acknowledgedResponseCursor)
      throw new E03RuntimeError(
        "control_session_unacknowledged_responses",
        `control session lease ${leaseId} has unacknowledged responses`,
      );
    const next = this.transitionLease(lease, {
      state: "closed",
      connectionId: null,
    });
    this.activeBySession.delete(next.sessionId);
    return next;
  }
  expire(now = this.clock.now()): ControlSessionLease[] {
    const timestamp = Date.parse(now);
    if (Number.isNaN(timestamp))
      throw new E03RuntimeError(
        "control_session_expire_time",
        "control session expiry time is invalid",
      );
    const values: ControlSessionLease[] = [];
    for (const lease of this.leases.values())
      if (
        lease.state !== "closed" &&
        lease.state !== "expired" &&
        Date.parse(lease.expiresAt) <= timestamp
      ) {
        const next = this.transitionLease(lease, {
          state: "expired",
          connectionId: null,
        });
        this.activeBySession.delete(next.sessionId);
        values.push(next);
      }
    return values;
  }
  snapshot(): {
    leases: ControlSessionLease[];
    replay: ControlSessionReplayEntry[];
    activeBySession: Array<[string, string]>;
  } {
    return {
      leases: [...this.leases.values()].map((value) => structuredClone(value)),
      replay: [...this.replay.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeBySession: [...this.activeBySession.entries()].map(
        ([sessionId, leaseId]) => [sessionId, leaseId],
      ),
    };
  }
  restore(snapshot: {
    leases: readonly ControlSessionLease[];
    replay: readonly ControlSessionReplayEntry[];
    activeBySession: ReadonlyArray<readonly [string, string]>;
  }): void {
    const leases = new Map<string, ControlSessionLease>();
    const replay = new Map<string, ControlSessionReplayEntry[]>();
    const activeBySession = new Map<string, string>();
    for (const value of snapshot.leases) {
      assertControlSessionLease(value);
      if (leases.has(value.leaseId))
        throw new E03RuntimeError(
          "control_session_restore_duplicate",
          `duplicate control session lease ${value.leaseId}`,
        );
      leases.set(value.leaseId, structuredClone(value));
    }
    for (const value of snapshot.replay) {
      assertControlReplayEntry(value);
      const lease = leases.get(value.leaseId);
      if (!lease || lease.sessionId !== value.sessionId)
        throw new E03RuntimeError(
          "control_session_replay_restore",
          `control session replay entry ${value.entryId} is invalid`,
        );
      const entries = replay.get(value.leaseId) ?? [];
      if (entries.some((entry) => entry.entryId === value.entryId))
        throw new E03RuntimeError(
          "control_session_replay_restore_duplicate",
          `duplicate control session replay entry ${value.entryId}`,
        );
      entries.push(structuredClone(value));
      replay.set(value.leaseId, entries);
    }
    for (const [sessionId, leaseId] of snapshot.activeBySession) {
      const lease = leases.get(leaseId);
      if (
        !lease ||
        lease.sessionId !== sessionId ||
        lease.state === "closed" ||
        lease.state === "expired" ||
        activeBySession.has(sessionId)
      )
        throw new E03RuntimeError(
          "control_session_active_restore",
          `control session active index ${sessionId} is invalid`,
        );
      activeBySession.set(sessionId, leaseId);
    }
    this.leases = leases;
    this.replay = replay;
    this.activeBySession = activeBySession;
    for (const lease of leases.values()) this.verifyReplay(lease.leaseId);
  }
  private appendEntry(
    lease: ControlSessionLease,
    input: Pick<
      ControlSessionReplayEntry,
      "direction" | "cursor" | "requestId" | "command" | "payload" | "state"
    >,
  ): ControlSessionReplayEntry {
    const entries = this.entries(lease.leaseId);
    const payload = {
      ...input,
      entryId: createId("control-session-replay"),
      leaseId: lease.leaseId,
      sessionId: lease.sessionId,
      createdAt: this.clock.now(),
      deliveredAt: input.state === "delivered" ? this.clock.now() : null,
      acknowledgedAt: null,
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
      revision: 1,
    };
    const entry = { ...payload, digest: digest(payload) };
    assertControlReplayEntry(entry);
    entries.push(entry);
    this.replay.set(lease.leaseId, entries);
    return structuredClone(entry);
  }
  private verifyReplay(leaseId: string): void {
    let previousDigest = "root";
    const requestCursors = new Set<number>();
    const responseCursors = new Set<number>();
    for (const entry of this.entries(leaseId)) {
      assertControlReplayEntry(entry);
      if (entry.previousDigest !== previousDigest)
        throw new E03RuntimeError(
          "control_session_replay_chain",
          `control session replay entry ${entry.entryId} breaks chain`,
        );
      const cursors =
        entry.direction === "request" ? requestCursors : responseCursors;
      if (cursors.has(entry.cursor))
        throw new E03RuntimeError(
          "control_session_replay_duplicate_cursor",
          `control session replay cursor ${entry.cursor} is duplicated`,
        );
      cursors.add(entry.cursor);
      previousDigest = entry.digest;
    }
  }
  private entries(leaseId: string): ControlSessionReplayEntry[] {
    return this.replay.get(leaseId) ?? [];
  }
  private requireLease(id: string): ControlSessionLease {
    const value = this.leases.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_session_lease_missing",
        `control session lease ${id} does not exist`,
      );
    assertControlSessionLease(value);
    return value;
  }
  private requireEntry(id: string): ControlSessionReplayEntry {
    const value = [...this.replay.values()]
      .flat()
      .find((entry) => entry.entryId === id);
    if (!value)
      throw new E03RuntimeError(
        "control_session_replay_missing",
        `control session replay entry ${id} does not exist`,
      );
    assertControlReplayEntry(value);
    return value;
  }
  private assertLeaseRevision(
    value: ControlSessionLease,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_session_lease_stale_revision",
        `control session lease ${value.leaseId} revision is stale`,
      );
  }
  private assertEntryRevision(
    value: ControlSessionReplayEntry,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_session_replay_stale_revision",
        `control session replay entry ${value.entryId} revision is stale`,
      );
  }
  private transitionLease(
    value: ControlSessionLease,
    patch: Partial<
      Omit<ControlSessionLease, "leaseId" | "revision" | "digest">
    >,
  ): ControlSessionLease {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      leaseId: value.leaseId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlSessionLease(next);
    this.leases.set(next.leaseId, next);
    return structuredClone(next);
  }
  private transitionEntry(
    value: ControlSessionReplayEntry,
    patch: Partial<
      Omit<ControlSessionReplayEntry, "entryId" | "revision" | "digest">
    >,
  ): ControlSessionReplayEntry {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      entryId: value.entryId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlReplayEntry(next);
    const entries = this.entries(value.leaseId);
    const index = entries.findIndex((entry) => entry.entryId === value.entryId);
    entries[index] = next;
    this.replay.set(value.leaseId, entries);
    return structuredClone(next);
  }
}

export type ControlTransactionState =
  | "open"
  | "prepared"
  | "committing"
  | "committed"
  | "aborting"
  | "aborted"
  | "in_doubt";

export interface ControlTransaction {
  transactionId: string;
  sessionId: string;
  ownerId: string;
  idempotencyKey: string;
  expectedSessionRevision: number;
  state: ControlTransactionState;
  commandCount: number;
  preparedCount: number;
  committedCount: number;
  abortedCount: number;
  deadlineAt: string;
  createdAt: string;
  updatedAt: string;
  commitDigest: string;
  terminalReason: string;
  revision: number;
  digest: string;
}

export interface ControlTransactionCommand {
  commandId: string;
  transactionId: string;
  ordinal: number;
  command: ControlCommand;
  envelopeDigest: string;
  expectedResourceRevision: number;
  resourceKey: string;
  state: "staged" | "prepared" | "applied" | "compensated" | "rejected";
  prepareToken: string;
  effectReceiptDigest: string;
  responseDigest: string;
  compensationDigest: string;
  errorCode: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface ControlTransactionLock {
  lockId: string;
  transactionId: string;
  resourceKey: string;
  fencingToken: number;
  acquiredAt: string;
  expiresAt: string;
  releasedAt: string;
  state: "held" | "released" | "expired";
  revision: number;
  digest: string;
}

export interface ControlTransactionDecision {
  decisionId: string;
  transactionId: string;
  decision: "commit" | "abort" | "recover_commit" | "recover_abort";
  commandDigest: string;
  previousDigest: string;
  decidedAt: string;
  actorId: string;
  reason: string;
  digest: string;
}

export interface ControlTransactionSnapshot {
  transactions: ControlTransaction[];
  commands: ControlTransactionCommand[];
  locks: ControlTransactionLock[];
  decisions: ControlTransactionDecision[];
  transactionByIdempotencyKey: [string, string][];
  activeLockByResource: [string, string][];
  nextFenceByResource: [string, number][];
}

function assertControlTransaction(value: ControlTransaction): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.transactionId ||
    !value.sessionId ||
    !value.ownerId ||
    !value.idempotencyKey ||
    value.expectedSessionRevision < 0 ||
    value.commandCount < 0 ||
    value.preparedCount < 0 ||
    value.committedCount < 0 ||
    value.abortedCount < 0 ||
    value.preparedCount > value.commandCount ||
    value.committedCount > value.commandCount ||
    value.abortedCount > value.commandCount ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transaction_corrupt",
      `control transaction ${value.transactionId || "<empty>"} is corrupt`,
    );
}

function assertControlTransactionCommand(
  value: ControlTransactionCommand,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.commandId ||
    !value.transactionId ||
    !Number.isSafeInteger(value.ordinal) ||
    value.ordinal < 0 ||
    !value.command ||
    !value.envelopeDigest ||
    !value.resourceKey ||
    value.expectedResourceRevision < 0 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transaction_command_corrupt",
      `control transaction command ${value.commandId || "<empty>"} is corrupt`,
    );
}

function assertControlTransactionLock(value: ControlTransactionLock): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.lockId ||
    !value.transactionId ||
    !value.resourceKey ||
    value.fencingToken < 1 ||
    !Number.isSafeInteger(value.fencingToken) ||
    !value.acquiredAt ||
    !value.expiresAt ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transaction_lock_corrupt",
      `control transaction lock ${value.lockId || "<empty>"} is corrupt`,
    );
}

function assertControlTransactionDecision(
  value: ControlTransactionDecision,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.decisionId ||
    !value.transactionId ||
    !value.commandDigest ||
    !value.decidedAt ||
    !value.actorId ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transaction_decision_corrupt",
      `control transaction decision ${value.decisionId || "<empty>"} is corrupt`,
    );
}

export class ControlTransactionRuntime {
  private transactions = new Map<string, ControlTransaction>();
  private commands = new Map<string, ControlTransactionCommand[]>();
  private locks = new Map<string, ControlTransactionLock>();
  private decisions = new Map<string, ControlTransactionDecision[]>();
  private transactionByIdempotencyKey = new Map<string, string>();
  private activeLockByResource = new Map<string, string>();
  private nextFenceByResource = new Map<string, number>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  open(input: {
    transactionId?: string;
    sessionId: string;
    ownerId: string;
    idempotencyKey: string;
    expectedSessionRevision: number;
    deadlineAt: string;
  }): ControlTransaction {
    const duplicate = this.transactionByIdempotencyKey.get(
      input.idempotencyKey,
    );
    if (duplicate) return structuredClone(this.requireTransaction(duplicate));
    if (!input.sessionId || !input.ownerId || !input.idempotencyKey)
      throw new E03RuntimeError(
        "control_transaction_identity",
        "control transaction requires session, owner, and idempotency key",
      );
    if (input.expectedSessionRevision < 0)
      throw new E03RuntimeError(
        "control_transaction_session_revision",
        "control transaction session revision is invalid",
      );
    const now = this.clock.now();
    if (Date.parse(input.deadlineAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "control_transaction_deadline",
        "control transaction deadline must be in the future",
      );
    const transactionId =
      input.transactionId ?? createId("control-transaction");
    if (this.transactions.has(transactionId))
      throw new E03RuntimeError(
        "control_transaction_duplicate",
        `control transaction ${transactionId} already exists`,
      );
    const payload = {
      transactionId,
      sessionId: input.sessionId,
      ownerId: input.ownerId,
      idempotencyKey: input.idempotencyKey,
      expectedSessionRevision: input.expectedSessionRevision,
      state: "open" as const,
      commandCount: 0,
      preparedCount: 0,
      committedCount: 0,
      abortedCount: 0,
      deadlineAt: input.deadlineAt,
      createdAt: now,
      updatedAt: now,
      commitDigest: "",
      terminalReason: "",
      revision: 1,
    };
    const transaction = { ...payload, digest: digest(payload) };
    assertControlTransaction(transaction);
    this.transactions.set(transactionId, transaction);
    this.commands.set(transactionId, []);
    this.decisions.set(transactionId, []);
    this.transactionByIdempotencyKey.set(input.idempotencyKey, transactionId);
    return structuredClone(transaction);
  }

  stage(input: {
    commandId?: string;
    transactionId: string;
    command: ControlCommand;
    envelopeDigest: string;
    resourceKey: string;
    expectedResourceRevision: number;
  }): ControlTransactionCommand {
    const transaction = this.requireTransaction(input.transactionId);
    if (transaction.state !== "open")
      throw new E03RuntimeError(
        "control_transaction_stage_state",
        `control transaction ${transaction.transactionId} is ${transaction.state}`,
      );
    if (!input.envelopeDigest || !input.resourceKey)
      throw new E03RuntimeError(
        "control_transaction_stage_binding",
        "control transaction command requires envelope and resource binding",
      );
    if (input.expectedResourceRevision < 0)
      throw new E03RuntimeError(
        "control_transaction_stage_revision",
        "control transaction command resource revision is invalid",
      );
    const entries = this.entries(transaction.transactionId);
    if (entries.some((value) => value.resourceKey === input.resourceKey))
      throw new E03RuntimeError(
        "control_transaction_resource_duplicate",
        `control transaction already stages ${input.resourceKey}`,
      );
    if (entries.some((value) => value.envelopeDigest === input.envelopeDigest))
      throw new E03RuntimeError(
        "control_transaction_envelope_duplicate",
        "control transaction already stages this envelope",
      );
    const commandId =
      input.commandId ?? createId("control-transaction-command");
    if (
      [...this.commands.values()].some((values) =>
        values.some((value) => value.commandId === commandId),
      )
    )
      throw new E03RuntimeError(
        "control_transaction_command_duplicate",
        `control transaction command ${commandId} already exists`,
      );
    const now = this.clock.now();
    const payload = {
      commandId,
      transactionId: transaction.transactionId,
      ordinal: entries.length,
      command: input.command,
      envelopeDigest: input.envelopeDigest,
      expectedResourceRevision: input.expectedResourceRevision,
      resourceKey: input.resourceKey,
      state: "staged" as const,
      prepareToken: "",
      effectReceiptDigest: "",
      responseDigest: "",
      compensationDigest: "",
      errorCode: "",
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const command = { ...payload, digest: digest(payload) };
    assertControlTransactionCommand(command);
    entries.push(command);
    this.commands.set(transaction.transactionId, entries);
    this.transitionTransaction(transaction, { commandCount: entries.length });
    return structuredClone(command);
  }

  acquireLocks(
    transactionId: string,
    expectedRevision: number,
    ttlMs: number,
  ): ControlTransactionLock[] {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (transaction.state !== "open")
      throw new E03RuntimeError(
        "control_transaction_lock_state",
        `control transaction ${transactionId} is ${transaction.state}`,
      );
    if (!Number.isSafeInteger(ttlMs) || ttlMs < 1)
      throw new E03RuntimeError(
        "control_transaction_lock_ttl",
        "control transaction lock TTL must be positive",
      );
    const entries = this.entries(transactionId);
    if (!entries.length)
      throw new E03RuntimeError(
        "control_transaction_empty",
        `control transaction ${transactionId} has no commands`,
      );
    const conflicts = entries
      .map((value) => value.resourceKey)
      .filter((resourceKey) => {
        const lockId = this.activeLockByResource.get(resourceKey);
        if (!lockId) return false;
        const lock = this.requireLock(lockId);
        return lock.state === "held" && lock.transactionId !== transactionId;
      });
    if (conflicts.length)
      throw new E03RuntimeError(
        "control_transaction_lock_conflict",
        `control transaction conflicts on ${conflicts.sort().join(",")}`,
      );
    const now = this.clock.now();
    const expiresAt = new Date(Date.parse(now) + ttlMs).toISOString();
    const acquired: ControlTransactionLock[] = [];
    for (const resourceKey of entries
      .map((value) => value.resourceKey)
      .sort()) {
      const existingId = this.activeLockByResource.get(resourceKey);
      if (existingId) {
        const existing = this.requireLock(existingId);
        if (existing.transactionId === transactionId) {
          acquired.push(structuredClone(existing));
          continue;
        }
      }
      const fencingToken = (this.nextFenceByResource.get(resourceKey) ?? 0) + 1;
      const lockId = createId("control-transaction-lock");
      const payload = {
        lockId,
        transactionId,
        resourceKey,
        fencingToken,
        acquiredAt: now,
        expiresAt,
        releasedAt: "",
        state: "held" as const,
        revision: 1,
      };
      const lock = { ...payload, digest: digest(payload) };
      assertControlTransactionLock(lock);
      this.locks.set(lockId, lock);
      this.activeLockByResource.set(resourceKey, lockId);
      this.nextFenceByResource.set(resourceKey, fencingToken);
      acquired.push(structuredClone(lock));
    }
    return acquired;
  }

  prepareCommand(
    commandId: string,
    expectedRevision: number,
    input: {
      prepareToken: string;
      observedResourceRevision: number;
      fencingToken: number;
    },
  ): ControlTransactionCommand {
    const command = this.requireCommand(commandId);
    this.assertCommandRevision(command, expectedRevision);
    const transaction = this.requireTransaction(command.transactionId);
    if (transaction.state !== "open")
      throw new E03RuntimeError(
        "control_transaction_prepare_state",
        `control transaction ${transaction.transactionId} is ${transaction.state}`,
      );
    if (command.state !== "staged")
      throw new E03RuntimeError(
        "control_transaction_command_prepare_state",
        `control transaction command ${commandId} is ${command.state}`,
      );
    if (!input.prepareToken)
      throw new E03RuntimeError(
        "control_transaction_prepare_token",
        "control transaction prepare token is required",
      );
    if (input.observedResourceRevision !== command.expectedResourceRevision)
      throw new E03RuntimeError(
        "control_transaction_resource_stale_revision",
        `control transaction resource ${command.resourceKey} revision is stale`,
      );
    const lockId = this.activeLockByResource.get(command.resourceKey);
    const lock = lockId ? this.requireLock(lockId) : null;
    if (
      !lock ||
      lock.transactionId !== transaction.transactionId ||
      lock.state !== "held" ||
      lock.fencingToken !== input.fencingToken
    )
      throw new E03RuntimeError(
        "control_transaction_fencing_token",
        `control transaction resource ${command.resourceKey} is not fenced`,
      );
    const next = this.transitionCommand(command, {
      state: "prepared",
      prepareToken: input.prepareToken,
    });
    const current = this.requireTransaction(transaction.transactionId);
    const preparedCount = this.entries(transaction.transactionId).filter(
      (value) => value.state === "prepared",
    ).length;
    this.transitionTransaction(current, { preparedCount });
    return next;
  }

  decideCommit(
    transactionId: string,
    expectedRevision: number,
    actorId: string,
  ): ControlTransactionDecision {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (transaction.state !== "open")
      throw new E03RuntimeError(
        "control_transaction_commit_decision_state",
        `control transaction ${transactionId} is ${transaction.state}`,
      );
    const entries = this.entries(transactionId);
    if (!entries.length || entries.some((value) => value.state !== "prepared"))
      throw new E03RuntimeError(
        "control_transaction_not_fully_prepared",
        `control transaction ${transactionId} is not fully prepared`,
      );
    if (Date.parse(transaction.deadlineAt) <= Date.parse(this.clock.now()))
      throw new E03RuntimeError(
        "control_transaction_commit_deadline",
        `control transaction ${transactionId} exceeded its deadline`,
      );
    const commandDigest = digest(
      entries.map((value) => ({
        ordinal: value.ordinal,
        commandId: value.commandId,
        prepareToken: value.prepareToken,
        digest: value.digest,
      })),
    );
    const decision = this.appendDecision(
      transaction,
      "commit",
      commandDigest,
      actorId,
      "all_commands_prepared",
    );
    this.transitionTransaction(transaction, {
      state: "committing",
      commitDigest: decision.digest,
      preparedCount: entries.length,
    });
    return decision;
  }

  applyCommand(
    commandId: string,
    expectedRevision: number,
    input: {
      prepareToken: string;
      effectReceiptDigest: string;
      responseDigest: string;
      fencingToken: number;
    },
  ): ControlTransactionCommand {
    const command = this.requireCommand(commandId);
    this.assertCommandRevision(command, expectedRevision);
    const transaction = this.requireTransaction(command.transactionId);
    if (transaction.state !== "committing")
      throw new E03RuntimeError(
        "control_transaction_apply_state",
        `control transaction ${transaction.transactionId} is ${transaction.state}`,
      );
    if (
      command.state !== "prepared" ||
      command.prepareToken !== input.prepareToken
    )
      throw new E03RuntimeError(
        "control_transaction_prepare_token_mismatch",
        `control transaction command ${commandId} prepare token is invalid`,
      );
    if (!input.responseDigest)
      throw new E03RuntimeError(
        "control_transaction_response_receipt",
        `control transaction command ${commandId} lacks response receipt`,
      );
    const lockId = this.activeLockByResource.get(command.resourceKey);
    const lock = lockId ? this.requireLock(lockId) : null;
    if (
      !lock ||
      lock.transactionId !== transaction.transactionId ||
      lock.fencingToken !== input.fencingToken ||
      lock.state !== "held"
    )
      throw new E03RuntimeError(
        "control_transaction_apply_fence",
        `control transaction command ${commandId} lost its fence`,
      );
    const next = this.transitionCommand(command, {
      state: "applied",
      effectReceiptDigest: input.effectReceiptDigest,
      responseDigest: input.responseDigest,
    });
    const current = this.requireTransaction(transaction.transactionId);
    const committedCount = this.entries(transaction.transactionId).filter(
      (value) => value.state === "applied",
    ).length;
    this.transitionTransaction(current, { committedCount });
    return next;
  }

  finalizeCommit(
    transactionId: string,
    expectedRevision: number,
  ): ControlTransaction {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (transaction.state !== "committing")
      throw new E03RuntimeError(
        "control_transaction_finalize_state",
        `control transaction ${transactionId} is ${transaction.state}`,
      );
    const entries = this.entries(transactionId);
    if (entries.some((value) => value.state !== "applied"))
      throw new E03RuntimeError(
        "control_transaction_finalize_incomplete",
        `control transaction ${transactionId} has unapplied commands`,
      );
    const next = this.transitionTransaction(transaction, {
      state: "committed",
      committedCount: entries.length,
      terminalReason: "commit_complete",
    });
    this.releaseLocks(transactionId);
    return next;
  }

  decideAbort(
    transactionId: string,
    expectedRevision: number,
    actorId: string,
    reason: string,
  ): ControlTransactionDecision {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (
      !["open", "prepared", "committing", "in_doubt"].includes(
        transaction.state,
      )
    )
      throw new E03RuntimeError(
        "control_transaction_abort_decision_state",
        `control transaction ${transactionId} is ${transaction.state}`,
      );
    if (!reason)
      throw new E03RuntimeError(
        "control_transaction_abort_reason",
        "control transaction abort requires a reason",
      );
    const entries = this.entries(transactionId);
    const commandDigest = digest(
      entries.map((value) => ({
        commandId: value.commandId,
        state: value.state,
        digest: value.digest,
      })),
    );
    const decision = this.appendDecision(
      transaction,
      "abort",
      commandDigest,
      actorId,
      reason,
    );
    this.transitionTransaction(transaction, {
      state: "aborting",
      commitDigest: decision.digest,
      terminalReason: reason,
    });
    return decision;
  }

  compensateCommand(
    commandId: string,
    expectedRevision: number,
    compensationDigest: string,
  ): ControlTransactionCommand {
    const command = this.requireCommand(commandId);
    this.assertCommandRevision(command, expectedRevision);
    const transaction = this.requireTransaction(command.transactionId);
    if (transaction.state !== "aborting")
      throw new E03RuntimeError(
        "control_transaction_compensate_state",
        `control transaction ${transaction.transactionId} is ${transaction.state}`,
      );
    if (!["staged", "prepared", "applied", "rejected"].includes(command.state))
      throw new E03RuntimeError(
        "control_transaction_command_compensate_state",
        `control transaction command ${commandId} is ${command.state}`,
      );
    if (command.state === "applied" && !compensationDigest)
      throw new E03RuntimeError(
        "control_transaction_compensation_receipt",
        `applied control command ${commandId} needs compensation`,
      );
    const next = this.transitionCommand(command, {
      state: "compensated",
      compensationDigest,
    });
    const current = this.requireTransaction(transaction.transactionId);
    const abortedCount = this.entries(transaction.transactionId).filter(
      (value) => value.state === "compensated",
    ).length;
    this.transitionTransaction(current, { abortedCount });
    return next;
  }

  finalizeAbort(
    transactionId: string,
    expectedRevision: number,
  ): ControlTransaction {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (transaction.state !== "aborting")
      throw new E03RuntimeError(
        "control_transaction_abort_finalize_state",
        `control transaction ${transactionId} is ${transaction.state}`,
      );
    const entries = this.entries(transactionId);
    if (entries.some((value) => value.state !== "compensated"))
      throw new E03RuntimeError(
        "control_transaction_abort_incomplete",
        `control transaction ${transactionId} has uncompensated commands`,
      );
    const next = this.transitionTransaction(transaction, {
      state: "aborted",
      abortedCount: entries.length,
    });
    this.releaseLocks(transactionId);
    return next;
  }

  markInDoubt(
    transactionId: string,
    expectedRevision: number,
    reason: string,
  ): ControlTransaction {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (!["committing", "aborting"].includes(transaction.state))
      throw new E03RuntimeError(
        "control_transaction_in_doubt_state",
        `control transaction ${transactionId} cannot become in-doubt`,
      );
    return this.transitionTransaction(transaction, {
      state: "in_doubt",
      terminalReason: reason || "coordinator_lost",
    });
  }

  recover(
    transactionId: string,
    expectedRevision: number,
    actorId: string,
  ): ControlTransactionDecision {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (transaction.state !== "in_doubt")
      throw new E03RuntimeError(
        "control_transaction_recover_state",
        `control transaction ${transactionId} is ${transaction.state}`,
      );
    const decisions = this.decisionEntries(transactionId);
    const durableDecision = decisions.at(-1);
    if (!durableDecision)
      throw new E03RuntimeError(
        "control_transaction_recover_decision_missing",
        `control transaction ${transactionId} has no durable decision`,
      );
    const recoverCommit =
      durableDecision.decision === "commit" ||
      durableDecision.decision === "recover_commit";
    const recovery = this.appendDecision(
      transaction,
      recoverCommit ? "recover_commit" : "recover_abort",
      durableDecision.commandDigest,
      actorId,
      `replay:${durableDecision.decisionId}`,
    );
    this.transitionTransaction(transaction, {
      state: recoverCommit ? "committing" : "aborting",
      commitDigest: recovery.digest,
    });
    return recovery;
  }

  expireLocks(at = this.clock.now()): ControlTransactionLock[] {
    const expired: ControlTransactionLock[] = [];
    for (const lock of [...this.locks.values()]) {
      if (lock.state !== "held" || Date.parse(lock.expiresAt) > Date.parse(at))
        continue;
      const next = this.transitionLock(lock, {
        state: "expired",
        releasedAt: at,
      });
      this.activeLockByResource.delete(lock.resourceKey);
      expired.push(next);
      const transaction = this.requireTransaction(lock.transactionId);
      if (!["committed", "aborted"].includes(transaction.state))
        this.transitionTransaction(transaction, {
          state: "in_doubt",
          terminalReason: `lock_expired:${lock.resourceKey}`,
        });
    }
    return expired;
  }

  snapshot(): ControlTransactionSnapshot {
    return {
      transactions: [...this.transactions.values()].map((value) =>
        structuredClone(value),
      ),
      commands: [...this.commands.values()]
        .flat()
        .map((value) => structuredClone(value)),
      locks: [...this.locks.values()].map((value) => structuredClone(value)),
      decisions: [...this.decisions.values()]
        .flat()
        .map((value) => structuredClone(value)),
      transactionByIdempotencyKey: [
        ...this.transactionByIdempotencyKey.entries(),
      ],
      activeLockByResource: [...this.activeLockByResource.entries()],
      nextFenceByResource: [...this.nextFenceByResource.entries()],
    };
  }

  restore(snapshot: ControlTransactionSnapshot): void {
    const transactions = new Map<string, ControlTransaction>();
    const commands = new Map<string, ControlTransactionCommand[]>();
    const locks = new Map<string, ControlTransactionLock>();
    const decisions = new Map<string, ControlTransactionDecision[]>();
    const transactionByIdempotencyKey = new Map<string, string>();
    const activeLockByResource = new Map<string, string>();
    const nextFenceByResource = new Map<string, number>();
    for (const value of snapshot.transactions) {
      assertControlTransaction(value);
      if (transactions.has(value.transactionId))
        throw new E03RuntimeError(
          "control_transaction_restore_duplicate",
          `duplicate control transaction ${value.transactionId}`,
        );
      transactions.set(value.transactionId, structuredClone(value));
      commands.set(value.transactionId, []);
      decisions.set(value.transactionId, []);
    }
    for (const value of [...snapshot.commands].sort(
      (a, b) => a.ordinal - b.ordinal,
    )) {
      assertControlTransactionCommand(value);
      if (!transactions.has(value.transactionId))
        throw new E03RuntimeError(
          "control_transaction_command_restore_parent",
          `control transaction command ${value.commandId} has no transaction`,
        );
      const entries = commands.get(value.transactionId)!;
      if (
        entries.some((entry) => entry.commandId === value.commandId) ||
        entries.some((entry) => entry.ordinal === value.ordinal) ||
        value.ordinal !== entries.length
      )
        throw new E03RuntimeError(
          "control_transaction_command_restore_order",
          `control transaction command ${value.commandId} order is invalid`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.locks) {
      assertControlTransactionLock(value);
      if (!transactions.has(value.transactionId) || locks.has(value.lockId))
        throw new E03RuntimeError(
          "control_transaction_lock_restore",
          `control transaction lock ${value.lockId} is invalid`,
        );
      locks.set(value.lockId, structuredClone(value));
    }
    for (const value of snapshot.decisions) {
      assertControlTransactionDecision(value);
      if (!transactions.has(value.transactionId))
        throw new E03RuntimeError(
          "control_transaction_decision_restore_parent",
          `control transaction decision ${value.decisionId} has no transaction`,
        );
      const entries = decisions.get(value.transactionId)!;
      const previousDigest = entries.at(-1)?.digest ?? "";
      if (
        value.previousDigest !== previousDigest ||
        entries.some((entry) => entry.decisionId === value.decisionId)
      )
        throw new E03RuntimeError(
          "control_transaction_decision_restore_chain",
          `control transaction decision ${value.decisionId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    for (const [key, transactionId] of snapshot.transactionByIdempotencyKey) {
      const transaction = transactions.get(transactionId);
      if (
        !transaction ||
        transaction.idempotencyKey !== key ||
        transactionByIdempotencyKey.has(key)
      )
        throw new E03RuntimeError(
          "control_transaction_restore_idempotency",
          `control transaction idempotency index ${key} is invalid`,
        );
      transactionByIdempotencyKey.set(key, transactionId);
    }
    for (const [resourceKey, lockId] of snapshot.activeLockByResource) {
      const lock = locks.get(lockId);
      if (
        !lock ||
        lock.resourceKey !== resourceKey ||
        lock.state !== "held" ||
        activeLockByResource.has(resourceKey)
      )
        throw new E03RuntimeError(
          "control_transaction_restore_active_lock",
          `control transaction active lock ${resourceKey} is invalid`,
        );
      activeLockByResource.set(resourceKey, lockId);
    }
    for (const [resourceKey, fence] of snapshot.nextFenceByResource) {
      const maximum = Math.max(
        0,
        ...[...locks.values()]
          .filter((value) => value.resourceKey === resourceKey)
          .map((value) => value.fencingToken),
      );
      if (!Number.isSafeInteger(fence) || fence < maximum)
        throw new E03RuntimeError(
          "control_transaction_restore_fence",
          `control transaction fence ${resourceKey} is invalid`,
        );
      nextFenceByResource.set(resourceKey, fence);
    }
    for (const transaction of transactions.values()) {
      const entries = commands.get(transaction.transactionId)!;
      if (entries.length !== transaction.commandCount)
        throw new E03RuntimeError(
          "control_transaction_restore_command_count",
          `control transaction ${transaction.transactionId} command count is invalid`,
        );
      const prepared = entries.filter((value) =>
        ["prepared", "applied"].includes(value.state),
      ).length;
      const applied = entries.filter(
        (value) => value.state === "applied",
      ).length;
      const compensated = entries.filter(
        (value) => value.state === "compensated",
      ).length;
      if (
        prepared < transaction.preparedCount ||
        applied !== transaction.committedCount ||
        compensated !== transaction.abortedCount
      )
        throw new E03RuntimeError(
          "control_transaction_restore_counts",
          `control transaction ${transaction.transactionId} counters are invalid`,
        );
    }
    this.transactions = transactions;
    this.commands = commands;
    this.locks = locks;
    this.decisions = decisions;
    this.transactionByIdempotencyKey = transactionByIdempotencyKey;
    this.activeLockByResource = activeLockByResource;
    this.nextFenceByResource = nextFenceByResource;
  }

  private appendDecision(
    transaction: ControlTransaction,
    decision: ControlTransactionDecision["decision"],
    commandDigest: string,
    actorId: string,
    reason: string,
  ): ControlTransactionDecision {
    const entries = this.decisionEntries(transaction.transactionId);
    const payload = {
      decisionId: createId("control-transaction-decision"),
      transactionId: transaction.transactionId,
      decision,
      commandDigest,
      previousDigest: entries.at(-1)?.digest ?? "",
      decidedAt: this.clock.now(),
      actorId,
      reason,
    };
    const value = { ...payload, digest: digest(payload) };
    assertControlTransactionDecision(value);
    entries.push(value);
    this.decisions.set(transaction.transactionId, entries);
    return structuredClone(value);
  }

  private releaseLocks(transactionId: string): void {
    for (const lock of [...this.locks.values()]) {
      if (lock.transactionId !== transactionId || lock.state !== "held")
        continue;
      this.transitionLock(lock, {
        state: "released",
        releasedAt: this.clock.now(),
      });
      this.activeLockByResource.delete(lock.resourceKey);
    }
  }

  private entries(transactionId: string): ControlTransactionCommand[] {
    return this.commands.get(transactionId) ?? [];
  }

  private decisionEntries(transactionId: string): ControlTransactionDecision[] {
    return this.decisions.get(transactionId) ?? [];
  }

  private requireTransaction(id: string): ControlTransaction {
    const value = this.transactions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_transaction_missing",
        `control transaction ${id} does not exist`,
      );
    assertControlTransaction(value);
    return value;
  }

  private requireCommand(id: string): ControlTransactionCommand {
    const value = [...this.commands.values()]
      .flat()
      .find((entry) => entry.commandId === id);
    if (!value)
      throw new E03RuntimeError(
        "control_transaction_command_missing",
        `control transaction command ${id} does not exist`,
      );
    assertControlTransactionCommand(value);
    return value;
  }

  private requireLock(id: string): ControlTransactionLock {
    const value = this.locks.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_transaction_lock_missing",
        `control transaction lock ${id} does not exist`,
      );
    assertControlTransactionLock(value);
    return value;
  }

  private assertTransactionRevision(
    value: ControlTransaction,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_transaction_stale_revision",
        `control transaction ${value.transactionId} revision is stale`,
      );
  }

  private assertCommandRevision(
    value: ControlTransactionCommand,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_transaction_command_stale_revision",
        `control transaction command ${value.commandId} revision is stale`,
      );
  }

  private transitionTransaction(
    value: ControlTransaction,
    patch: Partial<
      Omit<ControlTransaction, "transactionId" | "revision" | "digest">
    >,
  ): ControlTransaction {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      transactionId: value.transactionId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTransaction(next);
    this.transactions.set(next.transactionId, next);
    return structuredClone(next);
  }

  private transitionCommand(
    value: ControlTransactionCommand,
    patch: Partial<
      Omit<ControlTransactionCommand, "commandId" | "revision" | "digest">
    >,
  ): ControlTransactionCommand {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      commandId: value.commandId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTransactionCommand(next);
    const entries = this.entries(value.transactionId);
    const index = entries.findIndex(
      (entry) => entry.commandId === value.commandId,
    );
    if (index < 0)
      throw new E03RuntimeError(
        "control_transaction_command_index_missing",
        `control transaction command ${value.commandId} index is missing`,
      );
    entries[index] = next;
    this.commands.set(value.transactionId, entries);
    return structuredClone(next);
  }

  private transitionLock(
    value: ControlTransactionLock,
    patch: Partial<
      Omit<ControlTransactionLock, "lockId" | "revision" | "digest">
    >,
  ): ControlTransactionLock {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      lockId: value.lockId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTransactionLock(next);
    this.locks.set(next.lockId, next);
    return structuredClone(next);
  }
}
