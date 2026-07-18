import {
  createId,
  digest,
  E03RuntimeError,
  isTerminal,
  sealTask,
  type E03Clock,
  type E03Message,
  type E03TaskState,
  SystemE03Clock,
} from "../e03/contracts.ts";

export class TeamMailbox {
  private readonly dedupe = new Map<string, E03Message>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  send(
    sender: E03TaskState,
    recipient: E03TaskState,
    input: { body: string; kind?: E03Message["kind"]; idempotencyKey: string },
  ): { recipient: E03TaskState; message: E03Message } {
    this.assertOwnership(sender, recipient);
    if (!sender.scope.allowTeamMessaging || !recipient.scope.allowTeamMessaging)
      throw new E03RuntimeError(
        "team_message_not_permitted",
        "sender or recipient scope denies team messaging",
      );
    if (isTerminal(recipient.status))
      throw new E03RuntimeError(
        "terminal_recipient",
        `cannot message ${recipient.status} task`,
      );
    const prior =
      this.dedupe.get(input.idempotencyKey) ??
      recipient.messages.find(
        (message) => message.idempotencyKey === input.idempotencyKey,
      );
    if (prior) {
      if (
        prior.body !== input.body ||
        prior.senderTaskId !== sender.identity.taskId
      )
        throw new E03RuntimeError(
          "message_idempotency_conflict",
          "message idempotency key was reused with different content",
        );
      return {
        recipient: structuredClone(recipient),
        message: structuredClone(prior),
      };
    }
    const body = input.body.trim();
    if (!body || body.length > 256_000)
      throw new E03RuntimeError(
        "invalid_message",
        "team message must contain 1..256000 characters",
      );
    const payload = {
      messageId: createId("team-message"),
      taskId: recipient.identity.taskId,
      senderTaskId: sender.identity.taskId,
      recipientTaskId: recipient.identity.taskId,
      sequence: recipient.messages.length
        ? Math.max(...recipient.messages.map((message) => message.sequence)) + 1
        : 1,
      kind: input.kind ?? "prompt",
      body,
      idempotencyKey: input.idempotencyKey,
      createdAt: this.clock.now(),
      deliveredAt: null,
      acknowledgedAt: null,
    };
    const message = { ...payload, digest: digest(payload) };
    const next = sealTask({
      ...recipient,
      messages: [...recipient.messages, message],
      sequence: recipient.sequence + 1,
      updatedAt: this.clock.now(),
      checksum: "",
    });
    this.dedupe.set(input.idempotencyKey, message);
    return { recipient: next, message };
  }

  steer(
    sender: E03TaskState,
    recipient: E03TaskState,
    input: { body: string; idempotencyKey: string },
  ): { recipient: E03TaskState; message: E03Message } {
    if (
      recipient.status !== "created" &&
      recipient.status !== "queued" &&
      recipient.status !== "running" &&
      recipient.status !== "waiting"
    )
      throw new E03RuntimeError(
        "task_not_steerable",
        `task in ${recipient.status} cannot be steered`,
      );
    return this.send(sender, recipient, { ...input, kind: "steer" });
  }

  receive(
    task: E03TaskState,
    maximum = 32,
  ): { task: E03TaskState; messages: E03Message[] } {
    if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 10_000)
      throw new E03RuntimeError(
        "invalid_receive_limit",
        "mailbox receive limit is out of range",
      );
    const now = this.clock.now();
    const pending = task.messages
      .filter((message) => message.deliveredAt === null)
      .slice(0, maximum);
    if (!pending.length) return { task: structuredClone(task), messages: [] };
    const selected = new Set(pending.map((message) => message.messageId));
    const messages = task.messages.map((message) =>
      selected.has(message.messageId)
        ? resealMessage({ ...message, deliveredAt: now })
        : message,
    );
    return {
      task: sealTask({
        ...task,
        messages,
        sequence: task.sequence + 1,
        updatedAt: now,
        checksum: "",
      }),
      messages: messages
        .filter((message) => selected.has(message.messageId))
        .map((message) => structuredClone(message)),
    };
  }

  acknowledge(task: E03TaskState, messageId: string): E03TaskState {
    const selected = task.messages.find(
      (message) => message.messageId === messageId,
    );
    if (!selected)
      throw new E03RuntimeError(
        "unknown_message",
        `unknown message ${messageId}`,
      );
    if (selected.acknowledgedAt) return structuredClone(task);
    if (!selected.deliveredAt)
      throw new E03RuntimeError(
        "message_not_delivered",
        "cannot acknowledge an undelivered message",
      );
    const now = this.clock.now();
    const messages = task.messages.map((message) =>
      message.messageId === messageId
        ? resealMessage({ ...message, acknowledgedAt: now })
        : message,
    );
    return sealTask({
      ...task,
      messages,
      sequence: task.sequence + 1,
      updatedAt: now,
      checksum: "",
    });
  }

  rejectDuplicate(
    task: E03TaskState,
    idempotencyKey: string,
    bodyDigest?: string,
  ): E03Message | null {
    const selected =
      task.messages.find(
        (message) => message.idempotencyKey === idempotencyKey,
      ) ??
      this.dedupe.get(idempotencyKey) ??
      null;
    if (selected && bodyDigest && digest(selected.body) !== bodyDigest)
      throw new E03RuntimeError(
        "message_idempotency_conflict",
        "duplicate message content differs",
      );
    return selected ? structuredClone(selected) : null;
  }

  pending(task: E03TaskState): E03Message[] {
    return task.messages
      .filter((message) => message.acknowledgedAt === null)
      .map((message) => structuredClone(message));
  }

  private assertOwnership(sender: E03TaskState, recipient: E03TaskState): void {
    if (sender.identity.runId !== recipient.identity.runId)
      throw new E03RuntimeError(
        "cross_run_message",
        "team message cannot cross run authority",
      );
    const related =
      recipient.identity.parentTaskId === sender.identity.taskId ||
      sender.identity.parentTaskId === recipient.identity.taskId ||
      sender.identity.parentTaskId === recipient.identity.parentTaskId;
    if (!related)
      throw new E03RuntimeError(
        "message_ownership_denied",
        "tasks are not in the same parent/child team",
      );
  }
}

function resealMessage(message: E03Message): E03Message {
  const { digest: _digest, ...payload } = message;
  return { ...payload, digest: digest(payload) };
}

export type SteeringIntent =
  | "message"
  | "clarify"
  | "reprioritize"
  | "cancel"
  | "kill";
export type SteeringPhase = "queued" | "claimed" | "applied" | "rejected";

export interface SteeringRecord {
  steeringId: string;
  taskId: string;
  parentTaskId: string;
  runId: string;
  sessionId: string;
  expectedRevision: number;
  leaseId: string;
  intent: SteeringIntent;
  body: string;
  priority: number;
  sequence: number;
  phase: SteeringPhase;
  idempotencyKey: string;
  createdAt: string;
  claimedAt: string | null;
  appliedAt: string | null;
  error: string;
  digest: string;
}

export class TeamSteeringQueue {
  private readonly records = new Map<string, SteeringRecord>();
  private readonly idempotency = new Map<string, string>();

  constructor(
    private readonly clock: E03Clock = new SystemE03Clock(),
    private readonly maximumQueued = 256,
  ) {
    if (!Number.isSafeInteger(maximumQueued) || maximumQueued < 1)
      throw new E03RuntimeError(
        "invalid_steering_limit",
        "steering queue limit must be positive",
      );
  }

  enqueue(
    task: E03TaskState,
    input: {
      intent: SteeringIntent;
      body: string;
      priority?: number;
      idempotencyKey: string;
    },
  ): SteeringRecord {
    if (isTerminal(task.status))
      throw new E03RuntimeError(
        "terminal_recipient",
        `cannot steer ${task.status} task`,
      );
    if (
      !task.scope.allowTeamMessaging &&
      input.intent !== "cancel" &&
      input.intent !== "kill"
    )
      throw new E03RuntimeError(
        "team_message_not_permitted",
        "task scope denies steering messages",
      );
    if (input.intent === "kill" && !task.scope.allowKill)
      throw new E03RuntimeError("kill_not_permitted", "task scope denies kill");
    const priorId = this.idempotency.get(input.idempotencyKey);
    if (priorId) {
      const prior = this.require(priorId);
      if (
        prior.intent !== input.intent ||
        prior.body !== input.body.trim() ||
        prior.taskId !== task.identity.taskId
      )
        throw new E03RuntimeError(
          "steering_idempotency_conflict",
          "steering idempotency key changed content or recipient",
        );
      return structuredClone(prior);
    }
    const queued = [...this.records.values()].filter(
      (record) => record.phase === "queued" || record.phase === "claimed",
    );
    if (queued.length >= this.maximumQueued)
      throw new E03RuntimeError(
        "steering_backpressure",
        "steering queue is full",
        { pending: queued.length, limit: this.maximumQueued },
      );
    const body = input.body.trim();
    if (!body || body.length > 256_000)
      throw new E03RuntimeError(
        "invalid_steering_body",
        "steering body must contain 1..256000 characters",
      );
    const priority = input.priority ?? defaultPriority(input.intent);
    if (!Number.isSafeInteger(priority) || priority < 0 || priority > 1000)
      throw new E03RuntimeError(
        "invalid_steering_priority",
        "steering priority must be 0..1000",
      );
    const sequence =
      queued
        .filter((record) => record.taskId === task.identity.taskId)
        .reduce((maximum, record) => Math.max(maximum, record.sequence), 0) + 1;
    const record = sealSteering({
      steeringId: createId("team-steering"),
      taskId: task.identity.taskId,
      parentTaskId: task.identity.parentTaskId,
      runId: task.identity.runId,
      sessionId: task.identity.parentSessionId,
      expectedRevision: task.revision,
      leaseId: task.identity.leaseId,
      intent: input.intent,
      body,
      priority,
      sequence,
      phase: "queued",
      idempotencyKey: input.idempotencyKey,
      createdAt: this.clock.now(),
      claimedAt: null,
      appliedAt: null,
      error: "",
    });
    this.records.set(record.steeringId, record);
    this.idempotency.set(record.idempotencyKey, record.steeringId);
    return structuredClone(record);
  }

  claim(task: E03TaskState, maximum = 32): SteeringRecord[] {
    if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 10_000)
      throw new E03RuntimeError(
        "invalid_steering_claim",
        "steering claim maximum is out of range",
      );
    const candidates = this.snapshot()
      .filter(
        (record) =>
          record.taskId === task.identity.taskId && record.phase === "queued",
      )
      .sort(compareSteering)
      .slice(0, maximum);
    const claimed: SteeringRecord[] = [];
    for (const candidate of candidates) {
      if (
        candidate.runId !== task.identity.runId ||
        candidate.sessionId !== task.identity.parentSessionId
      ) {
        claimed.push(
          this.reject(candidate.steeringId, "steering_authority_mismatch"),
        );
        continue;
      }
      if (candidate.leaseId !== task.identity.leaseId) {
        claimed.push(this.reject(candidate.steeringId, "stale_lease"));
        continue;
      }
      if (candidate.expectedRevision > task.revision) {
        continue;
      }
      const next = sealSteering({
        ...candidate,
        phase: "claimed",
        claimedAt: this.clock.now(),
      });
      this.records.set(next.steeringId, next);
      claimed.push(structuredClone(next));
      if (next.intent === "cancel" || next.intent === "kill") break;
    }
    return claimed;
  }

  applied(steeringId: string, task: E03TaskState): SteeringRecord {
    const record = this.require(steeringId);
    if (record.phase === "applied") return structuredClone(record);
    if (record.phase !== "claimed")
      throw new E03RuntimeError(
        "steering_not_claimed",
        "steering must be claimed before application",
      );
    if (
      record.taskId !== task.identity.taskId ||
      record.runId !== task.identity.runId
    )
      throw new E03RuntimeError(
        "steering_authority_mismatch",
        "applied task differs from steering record",
      );
    if (record.leaseId !== task.identity.leaseId)
      throw new E03RuntimeError(
        "stale_lease",
        "steering application uses a stale task lease",
      );
    if (task.revision <= record.expectedRevision)
      throw new E03RuntimeError(
        "steering_state_unchanged",
        "steering application did not advance task revision",
      );
    const next = sealSteering({
      ...record,
      phase: "applied",
      appliedAt: this.clock.now(),
      error: "",
    });
    this.records.set(next.steeringId, next);
    return structuredClone(next);
  }

  reject(steeringId: string, error: string): SteeringRecord {
    const record = this.require(steeringId);
    if (record.phase === "applied")
      throw new E03RuntimeError(
        "steering_already_applied",
        "applied steering cannot be rejected",
      );
    if (record.phase === "rejected") return structuredClone(record);
    const next = sealSteering({
      ...record,
      phase: "rejected",
      appliedAt: this.clock.now(),
      error: error.trim() || "steering_rejected",
    });
    this.records.set(next.steeringId, next);
    return structuredClone(next);
  }

  restore(records: readonly SteeringRecord[]): void {
    const next = new Map<string, SteeringRecord>();
    const keys = new Map<string, string>();
    for (const record of records) {
      assertSteering(record);
      if (next.has(record.steeringId))
        throw new E03RuntimeError(
          "duplicate_steering_record",
          `duplicate steering id ${record.steeringId}`,
        );
      const prior = keys.get(record.idempotencyKey);
      if (prior && prior !== record.steeringId)
        throw new E03RuntimeError(
          "steering_idempotency_conflict",
          `idempotency key ${record.idempotencyKey} maps to multiple records`,
        );
      next.set(record.steeringId, structuredClone(record));
      keys.set(record.idempotencyKey, record.steeringId);
    }
    this.records.clear();
    this.idempotency.clear();
    for (const [key, value] of next) this.records.set(key, value);
    for (const [key, value] of keys) this.idempotency.set(key, value);
  }

  snapshot(): SteeringRecord[] {
    return [...this.records.values()]
      .sort(compareSteering)
      .map((record) => structuredClone(record));
  }

  pending(taskId?: string): SteeringRecord[] {
    return this.snapshot().filter(
      (record) =>
        (!taskId || record.taskId === taskId) &&
        (record.phase === "queued" || record.phase === "claimed"),
    );
  }

  private require(steeringId: string): SteeringRecord {
    const record = this.records.get(steeringId);
    if (!record)
      throw new E03RuntimeError(
        "unknown_steering_record",
        `unknown steering record ${steeringId}`,
      );
    assertSteering(record);
    return record;
  }
}

function sealSteering(
  value: Omit<SteeringRecord, "digest"> | SteeringRecord,
): SteeringRecord {
  const { digest: _digest, ...payload } = value as SteeringRecord;
  return { ...payload, digest: digest(payload) };
}

function assertSteering(record: SteeringRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "steering_digest_mismatch",
      `steering ${record.steeringId} digest is invalid`,
    );
  if (
    !record.steeringId ||
    !record.taskId ||
    !record.parentTaskId ||
    !record.runId ||
    !record.sessionId ||
    !record.leaseId ||
    !record.idempotencyKey
  )
    throw new E03RuntimeError(
      "invalid_steering_record",
      "steering custody identity is incomplete",
    );
  if (record.phase === "queued" && (record.claimedAt || record.appliedAt))
    throw new E03RuntimeError(
      "invalid_steering_phase",
      "queued steering has claim/application timestamps",
    );
  if (record.phase === "claimed" && !record.claimedAt)
    throw new E03RuntimeError(
      "invalid_steering_phase",
      "claimed steering has no claim timestamp",
    );
  if (
    (record.phase === "applied" || record.phase === "rejected") &&
    !record.appliedAt
  )
    throw new E03RuntimeError(
      "invalid_steering_phase",
      "terminal steering has no application timestamp",
    );
}

function compareSteering(left: SteeringRecord, right: SteeringRecord): number {
  return (
    right.priority - left.priority ||
    left.createdAt.localeCompare(right.createdAt) ||
    left.sequence - right.sequence ||
    left.steeringId.localeCompare(right.steeringId)
  );
}

function defaultPriority(intent: SteeringIntent): number {
  if (intent === "kill") return 1000;
  if (intent === "cancel") return 900;
  if (intent === "reprioritize") return 500;
  if (intent === "clarify") return 300;
  return 100;
}

export interface TeamMember {
  memberId: string;
  taskId: string;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  lineage: string[];
  leaseId: string;
  attempt: number;
  role: string;
  capabilities: string[];
  status: E03TaskState["status"];
  joinedAt: string;
  leftAt: string | null;
  digest: string;
}

export interface TeamOwnershipDecision {
  accepted: boolean;
  code: string;
  senderTaskId: string;
  recipientTaskId: string;
  commonParentTaskId: string;
  relationship:
    | "parent-child"
    | "child-parent"
    | "sibling"
    | "self"
    | "unrelated";
  requiredCapability: string;
  senderDigest: string;
  recipientDigest: string;
  digest: string;
}

export class TeamOwnershipRuntime {
  private readonly members = new Map<string, TeamMember>();

  join(task: E03TaskState, role = task.definition.name): TeamMember {
    assertTaskForTeam(task);
    const prior = this.members.get(task.identity.taskId);
    if (prior) {
      if (
        prior.leaseId !== task.identity.leaseId ||
        prior.runId !== task.identity.runId
      )
        throw new E03RuntimeError(
          "team_member_custody_conflict",
          `team member ${task.identity.taskId} changed run or lease`,
        );
      return this.update(task);
    }
    const payload = {
      memberId: `team-member-${digest({ taskId: task.identity.taskId, leaseId: task.identity.leaseId }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      runId: task.identity.runId,
      sessionId: task.identity.sessionId,
      parentTaskId: task.identity.parentTaskId,
      lineage: [...task.identity.lineage],
      leaseId: task.identity.leaseId,
      attempt: task.identity.attempt,
      role: role.trim() || task.definition.name,
      capabilities: memberCapabilities(task),
      status: task.status,
      joinedAt: task.createdAt,
      leftAt: isTerminal(task.status) ? task.terminalAt : null,
    };
    const member = { ...payload, digest: digest(payload) };
    this.members.set(member.taskId, member);
    return structuredClone(member);
  }

  update(task: E03TaskState): TeamMember {
    assertTaskForTeam(task);
    const prior = this.members.get(task.identity.taskId);
    if (!prior) return this.join(task);
    assertMember(prior);
    if (
      prior.runId !== task.identity.runId ||
      prior.parentTaskId !== task.identity.parentTaskId ||
      prior.sessionId !== task.identity.sessionId
    )
      throw new E03RuntimeError(
        "team_member_authority_changed",
        `team member ${task.identity.taskId} changed authority`,
      );
    if (task.identity.attempt < prior.attempt)
      throw new E03RuntimeError(
        "team_member_attempt_regression",
        `team member ${task.identity.taskId} attempt regressed`,
      );
    if (
      task.identity.attempt === prior.attempt &&
      task.identity.leaseId !== prior.leaseId
    )
      throw new E03RuntimeError(
        "team_member_lease_changed",
        `team member ${task.identity.taskId} changed lease without a new attempt`,
      );
    const payload = {
      ...prior,
      lineage: [...task.identity.lineage],
      leaseId: task.identity.leaseId,
      attempt: task.identity.attempt,
      capabilities: memberCapabilities(task),
      status: task.status,
      leftAt: isTerminal(task.status) ? task.terminalAt : null,
    };
    const member = sealMember(payload);
    this.members.set(member.taskId, member);
    return structuredClone(member);
  }

  leave(task: E03TaskState): TeamMember {
    if (!isTerminal(task.status))
      throw new E03RuntimeError(
        "team_member_not_terminal",
        `cannot remove active member ${task.identity.taskId}`,
      );
    const member = this.update(task);
    if (!member.leftAt)
      throw new E03RuntimeError(
        "team_member_leave_time_missing",
        "terminal team member has no leave timestamp",
      );
    return member;
  }

  decide(
    sender: E03TaskState,
    recipient: E03TaskState,
    capability: string,
  ): TeamOwnershipDecision {
    const senderMember =
      this.members.get(sender.identity.taskId) ?? this.join(sender);
    const recipientMember =
      this.members.get(recipient.identity.taskId) ?? this.join(recipient);
    assertMember(senderMember);
    assertMember(recipientMember);
    const relationship = relationshipOf(senderMember, recipientMember);
    let code = "team_ownership_accepted";
    if (senderMember.runId !== recipientMember.runId)
      code = "cross_run_team_access";
    else if (relationship === "unrelated") code = "unrelated_team_access";
    else if (!senderMember.capabilities.includes(capability))
      code = "team_capability_denied";
    else if (recipientMember.leftAt && capability !== "read")
      code = "terminal_team_recipient";
    const commonParentTaskId =
      relationship === "parent-child"
        ? senderMember.taskId
        : relationship === "child-parent"
          ? recipientMember.taskId
          : senderMember.parentTaskId === recipientMember.parentTaskId
            ? senderMember.parentTaskId
            : "";
    const payload = {
      accepted: code === "team_ownership_accepted",
      code,
      senderTaskId: senderMember.taskId,
      recipientTaskId: recipientMember.taskId,
      commonParentTaskId,
      relationship,
      requiredCapability: capability,
      senderDigest: senderMember.digest,
      recipientDigest: recipientMember.digest,
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(
    sender: E03TaskState,
    recipient: E03TaskState,
    capability: string,
  ): TeamOwnershipDecision {
    const decision = this.decide(sender, recipient, capability);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `${sender.identity.taskId} cannot ${capability} ${recipient.identity.taskId}`,
        {
          relationship: decision.relationship,
          commonParentTaskId: decision.commonParentTaskId,
        },
      );
    return decision;
  }

  restore(members: readonly TeamMember[]): void {
    const next = new Map<string, TeamMember>();
    for (const member of members) {
      assertMember(member);
      if (next.has(member.taskId))
        throw new E03RuntimeError(
          "duplicate_team_member",
          `duplicate member ${member.taskId}`,
        );
      next.set(member.taskId, structuredClone(member));
    }
    this.members.clear();
    for (const [taskId, member] of next) this.members.set(taskId, member);
  }

  snapshot(parentTaskId?: string): TeamMember[] {
    return [...this.members.values()]
      .filter(
        (member) =>
          !parentTaskId ||
          member.parentTaskId === parentTaskId ||
          member.taskId === parentTaskId,
      )
      .sort(
        (left, right) =>
          left.joinedAt.localeCompare(right.joinedAt) ||
          left.taskId.localeCompare(right.taskId),
      )
      .map((member) => structuredClone(member));
  }

  active(parentTaskId?: string): TeamMember[] {
    return this.snapshot(parentTaskId).filter((member) => !member.leftAt);
  }
}

export interface MessageCausalRecord {
  causalId: string;
  taskId: string;
  messageId: string;
  messageSequence: number;
  senderTaskId: string;
  recipientTaskId: string;
  action: "queued" | "delivered" | "acknowledged" | "rejected";
  taskRevision: number;
  leaseId: string;
  messageDigest: string;
  reason: string;
  recordedAt: string;
  previousDigest: string;
  digest: string;
}

export class MessageCausalLedger {
  private records: MessageCausalRecord[] = [];
  private readonly phases = new Map<string, MessageCausalRecord["action"]>();

  record(
    task: E03TaskState,
    message: E03Message,
    action: MessageCausalRecord["action"],
    reason: string = action,
  ): MessageCausalRecord {
    assertTaskForTeam(task);
    assertMessage(message);
    if (
      message.taskId !== task.identity.taskId ||
      message.recipientTaskId !== task.identity.taskId
    )
      throw new E03RuntimeError(
        "message_causal_custody",
        "message causal record uses another recipient task",
      );
    const priorPhase = this.phases.get(message.messageId);
    if (priorPhase === action) {
      const prior = this.records.find(
        (record) =>
          record.messageId === message.messageId && record.action === action,
      )!;
      return structuredClone(prior);
    }
    if (!validMessagePhase(priorPhase, action))
      throw new E03RuntimeError(
        "message_causal_phase",
        `message ${message.messageId} cannot move ${priorPhase ?? "none"}->${action}`,
      );
    if (action === "delivered" && !message.deliveredAt)
      throw new E03RuntimeError(
        "message_delivery_not_projected",
        "delivered causal record has no message deliveredAt",
      );
    if (action === "acknowledged" && !message.acknowledgedAt)
      throw new E03RuntimeError(
        "message_ack_not_projected",
        "acknowledged causal record has no message acknowledgedAt",
      );
    const previousDigest = this.records.at(-1)?.digest ?? "";
    const payload = {
      causalId: `message-causal-${digest({ messageId: message.messageId, action, previousDigest }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      messageId: message.messageId,
      messageSequence: message.sequence,
      senderTaskId: message.senderTaskId,
      recipientTaskId: message.recipientTaskId,
      action,
      taskRevision: task.revision,
      leaseId: task.identity.leaseId,
      messageDigest: message.digest,
      reason: reason.trim() || action,
      recordedAt: new Date().toISOString(),
      previousDigest,
    };
    const record = { ...payload, digest: digest(payload) };
    this.records.push(record);
    this.phases.set(message.messageId, action);
    return structuredClone(record);
  }

  restore(records: readonly MessageCausalRecord[]): void {
    let previous = "";
    const phases = new Map<string, MessageCausalRecord["action"]>();
    for (const record of records) {
      assertCausalRecord(record, previous);
      const phase = phases.get(record.messageId);
      if (!validMessagePhase(phase, record.action))
        throw new E03RuntimeError(
          "message_causal_phase",
          `restored message ${record.messageId} cannot move ${phase ?? "none"}->${record.action}`,
        );
      phases.set(record.messageId, record.action);
      previous = record.digest;
    }
    this.records = records.map((record) => structuredClone(record));
    this.phases.clear();
    for (const [messageId, phase] of phases) this.phases.set(messageId, phase);
  }

  snapshot(taskId?: string): MessageCausalRecord[] {
    return this.records
      .filter((record) => !taskId || record.taskId === taskId)
      .map((record) => structuredClone(record));
  }

  verify(): {
    valid: true;
    records: number;
    messages: number;
    headDigest: string;
    rejected: number;
  } {
    let previous = "";
    const phases = new Map<string, MessageCausalRecord["action"]>();
    for (const record of this.records) {
      assertCausalRecord(record, previous);
      if (!validMessagePhase(phases.get(record.messageId), record.action))
        throw new E03RuntimeError(
          "message_causal_phase",
          `message ${record.messageId} phase chain is invalid`,
        );
      phases.set(record.messageId, record.action);
      previous = record.digest;
    }
    return {
      valid: true,
      records: this.records.length,
      messages: phases.size,
      headDigest: previous,
      rejected: this.records.filter((record) => record.action === "rejected")
        .length,
    };
  }
}

export interface MailboxProcessResult {
  task: E03TaskState;
  delivered: E03Message[];
  acknowledged: E03Message[];
  rejected: E03Message[];
  commands: Array<{
    messageId: string;
    command: "cancel" | "kill" | "steer" | "prompt";
    body: string;
  }>;
  ledger: MessageCausalRecord[];
}

export class MailboxQueueProcessor {
  readonly ownership = new TeamOwnershipRuntime();
  readonly ledger = new MessageCausalLedger();

  process(
    task: E03TaskState,
    input: {
      maximum: number;
      autoAcknowledgeKinds?: readonly E03Message["kind"][];
      expectedLeaseId: string;
    },
  ): MailboxProcessResult {
    assertTaskForTeam(task);
    if (task.identity.leaseId !== input.expectedLeaseId)
      throw new E03RuntimeError(
        "stale_lease",
        "mailbox processor uses a stale task lease",
      );
    if (
      !Number.isSafeInteger(input.maximum) ||
      input.maximum < 1 ||
      input.maximum > 10_000
    )
      throw new E03RuntimeError(
        "invalid_mailbox_process_limit",
        "mailbox process maximum is invalid",
      );
    const auto = new Set(
      input.autoAcknowledgeKinds ?? [
        "prompt",
        "steer",
        "clarification",
        "response",
      ],
    );
    const pending = task.messages
      .filter((message) => !message.deliveredAt)
      .slice(0, input.maximum);
    let current = structuredClone(task);
    const delivered: E03Message[] = [];
    const acknowledged: E03Message[] = [];
    const rejected: E03Message[] = [];
    const commands: MailboxProcessResult["commands"] = [];
    for (const message of pending) {
      assertMessage(message);
      this.ledger.record(current, message, "queued");
      if (isTerminal(current.status) && message.kind !== "response") {
        this.ledger.record(
          current,
          message,
          "rejected",
          `recipient_${current.status}`,
        );
        rejected.push(structuredClone(message));
        continue;
      }
      const now = new Date().toISOString();
      current = resealTeamTask({
        ...current,
        messages: current.messages.map((value) =>
          value.messageId === message.messageId
            ? resealTeamMessage({ ...value, deliveredAt: now })
            : value,
        ),
        sequence: current.sequence + 1,
        updatedAt: now,
      });
      const projected = current.messages.find(
        (value) => value.messageId === message.messageId,
      )!;
      this.ledger.record(current, projected, "delivered");
      delivered.push(structuredClone(projected));
      commands.push({
        messageId: message.messageId,
        command:
          message.kind === "cancel"
            ? "cancel"
            : message.kind === "kill"
              ? "kill"
              : message.kind === "steer"
                ? "steer"
                : "prompt",
        body: message.body,
      });
      if (auto.has(message.kind)) {
        const acknowledgedAt = new Date().toISOString();
        current = resealTeamTask({
          ...current,
          messages: current.messages.map((value) =>
            value.messageId === message.messageId
              ? resealTeamMessage({ ...value, acknowledgedAt })
              : value,
          ),
          sequence: current.sequence + 1,
          updatedAt: acknowledgedAt,
        });
        const acknowledgedMessage = current.messages.find(
          (value) => value.messageId === message.messageId,
        )!;
        this.ledger.record(current, acknowledgedMessage, "acknowledged");
        acknowledged.push(structuredClone(acknowledgedMessage));
      }
    }
    return {
      task: current,
      delivered,
      acknowledged,
      rejected,
      commands,
      ledger: this.ledger.snapshot(task.identity.taskId),
    };
  }

  recover(task: E03TaskState): {
    redeliver: E03Message[];
    waitForAck: E03Message[];
    terminalReject: E03Message[];
  } {
    assertTaskForTeam(task);
    const redeliver = task.messages.filter((message) => !message.deliveredAt);
    const waitForAck = task.messages.filter(
      (message) => message.deliveredAt && !message.acknowledgedAt,
    );
    const terminalReject = isTerminal(task.status)
      ? [...redeliver, ...waitForAck].filter(
          (message) => message.kind !== "response",
        )
      : [];
    return {
      redeliver: redeliver.map((message) => structuredClone(message)),
      waitForAck: waitForAck.map((message) => structuredClone(message)),
      terminalReject: terminalReject.map((message) => structuredClone(message)),
    };
  }
}

export interface TeamNotification {
  notificationId: string;
  parentTaskId: string;
  sourceTaskId: string;
  sourceRevision: number;
  sourceStatus: E03TaskState["status"];
  kind:
    | "progress"
    | "waiting"
    | "completed"
    | "failed"
    | "cancelled"
    | "killed";
  summary: string;
  artifactIds: string[];
  deliveryIds: string[];
  visible: boolean;
  idempotencyKey: string;
  createdAt: string;
  digest: string;
}

export class TeamNotificationRuntime {
  private readonly notifications = new Map<string, TeamNotification>();

  fromTask(task: E03TaskState, summary?: string): TeamNotification {
    assertTaskForTeam(task);
    const kind: TeamNotification["kind"] =
      task.status === "completed"
        ? "completed"
        : task.status === "failed"
          ? "failed"
          : task.status === "cancelled"
            ? "cancelled"
            : task.status === "killed"
              ? "killed"
              : task.status === "waiting"
                ? "waiting"
                : "progress";
    const idempotencyKey = `team-notification:${task.identity.taskId}:${task.revision}:${kind}`;
    const prior = this.notifications.get(idempotencyKey);
    if (prior) return structuredClone(prior);
    const text =
      summary?.trim() ||
      task.deliveries.at(-1)?.summary ||
      task.error ||
      `${task.definition.name} is ${task.status}`;
    const payload = {
      notificationId: `team-notification-${digest(idempotencyKey).slice(0, 32)}`,
      parentTaskId: task.identity.parentTaskId,
      sourceTaskId: task.identity.taskId,
      sourceRevision: task.revision,
      sourceStatus: task.status,
      kind,
      summary: text,
      artifactIds: [
        ...new Set(task.artifacts.map((artifact) => artifact.artifact_id)),
      ].sort(),
      deliveryIds: task.deliveries.map((delivery) => delivery.deliveryId),
      visible: kind !== "progress" || Boolean(text),
      idempotencyKey,
      createdAt: new Date().toISOString(),
    };
    const notification = { ...payload, digest: digest(payload) };
    this.notifications.set(idempotencyKey, notification);
    return structuredClone(notification);
  }

  restore(values: readonly TeamNotification[]): void {
    const next = new Map<string, TeamNotification>();
    for (const value of values) {
      const { digest: checksum, ...payload } = value;
      if (digest(payload) !== checksum)
        throw new E03RuntimeError(
          "team_notification_digest",
          `notification ${value.notificationId} digest is invalid`,
        );
      const prior = next.get(value.idempotencyKey);
      if (prior && prior.digest !== value.digest)
        throw new E03RuntimeError(
          "team_notification_conflict",
          `notification key ${value.idempotencyKey} changed content`,
        );
      next.set(value.idempotencyKey, structuredClone(value));
    }
    this.notifications.clear();
    for (const [key, value] of next) this.notifications.set(key, value);
  }

  snapshot(parentTaskId?: string): TeamNotification[] {
    return [...this.notifications.values()]
      .filter((value) => !parentTaskId || value.parentTaskId === parentTaskId)
      .sort(
        (left, right) =>
          left.createdAt.localeCompare(right.createdAt) ||
          left.sourceTaskId.localeCompare(right.sourceTaskId) ||
          left.sourceRevision - right.sourceRevision,
      )
      .map((value) => structuredClone(value));
  }
}

function assertTaskForTeam(task: E03TaskState): void {
  const { checksum, ...payload } = task;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "task_checksum_mismatch",
      `team task ${task.identity.taskId} checksum is invalid`,
    );
  if (
    !task.identity.taskId ||
    !task.identity.runId ||
    !task.identity.sessionId ||
    !task.identity.parentTaskId ||
    !task.identity.leaseId
  )
    throw new E03RuntimeError(
      "invalid_team_task",
      "team task identity is incomplete",
    );
}

function assertMember(member: TeamMember): void {
  const { digest: checksum, ...payload } = member;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "team_member_digest",
      `team member ${member.memberId} digest is invalid`,
    );
  if (
    !member.memberId ||
    !member.taskId ||
    !member.runId ||
    !member.sessionId ||
    !member.parentTaskId ||
    !member.leaseId ||
    member.attempt < 1
  )
    throw new E03RuntimeError(
      "invalid_team_member",
      "team member identity is incomplete",
    );
  if (member.leftAt && !isTerminal(member.status))
    throw new E03RuntimeError(
      "active_team_member_left",
      "active team member has a leave timestamp",
    );
}

function memberCapabilities(task: E03TaskState): string[] {
  const capabilities = ["read"];
  if (task.scope.allowTeamMessaging) capabilities.push("send", "steer");
  if (task.scope.allowFanout) capabilities.push("fanout");
  if (task.scope.allowKill) capabilities.push("kill");
  return capabilities;
}

function relationshipOf(
  sender: TeamMember,
  recipient: TeamMember,
): TeamOwnershipDecision["relationship"] {
  if (sender.taskId === recipient.taskId) return "self";
  if (recipient.parentTaskId === sender.taskId) return "parent-child";
  if (sender.parentTaskId === recipient.taskId) return "child-parent";
  if (sender.parentTaskId === recipient.parentTaskId) return "sibling";
  return "unrelated";
}

function sealMember(
  value: Omit<TeamMember, "digest"> | TeamMember,
): TeamMember {
  const { digest: _digest, ...payload } = value as TeamMember;
  return { ...payload, digest: digest(payload) };
}

function assertMessage(message: E03Message): void {
  const { digest: checksum, ...payload } = message;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "message_digest_mismatch",
      `message ${message.messageId} digest is invalid`,
    );
  if (
    !message.messageId ||
    !message.taskId ||
    !message.senderTaskId ||
    !message.recipientTaskId ||
    !message.idempotencyKey ||
    !message.body ||
    message.sequence < 1
  )
    throw new E03RuntimeError(
      "invalid_message",
      "message identity or body is incomplete",
    );
}

function assertCausalRecord(
  record: MessageCausalRecord,
  previousDigest: string,
): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "message_causal_digest",
      `causal record ${record.causalId} digest is invalid`,
    );
  if (record.previousDigest !== previousDigest)
    throw new E03RuntimeError(
      "message_causal_chain",
      `causal record ${record.causalId} does not extend prior digest`,
    );
  if (
    !record.causalId ||
    !record.taskId ||
    !record.messageId ||
    !record.senderTaskId ||
    !record.recipientTaskId ||
    !record.leaseId ||
    !record.messageDigest ||
    !record.reason
  )
    throw new E03RuntimeError(
      "invalid_message_causal_record",
      "message causal record identity is incomplete",
    );
}

function validMessagePhase(
  previous: MessageCausalRecord["action"] | undefined,
  next: MessageCausalRecord["action"],
): boolean {
  if (!previous) return next === "queued";
  if (previous === "queued") return next === "delivered" || next === "rejected";
  if (previous === "delivered")
    return next === "acknowledged" || next === "rejected";
  return false;
}

function resealTeamMessage(message: E03Message): E03Message {
  const { digest: _digest, ...payload } = message;
  return { ...payload, digest: digest(payload) };
}

function resealTeamTask(task: E03TaskState): E03TaskState {
  const { checksum: _checksum, ...payload } = task;
  return { ...payload, checksum: digest(payload) };
}

export type MailboxConsumerState = "active" | "paused" | "revoked";

export interface MailboxConsumer {
  consumerId: string;
  taskId: string;
  sessionId: string;
  holderId: string;
  state: MailboxConsumerState;
  acceptedKinds: E03Message["kind"][];
  maximumInFlight: number;
  visibilityTimeoutMs: number;
  cursorSequence: number;
  inFlightMessageIds: string[];
  acknowledgedMessageIds: string[];
  rejectedMessageIds: string[];
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface MailboxDeliveryClaim {
  claimId: string;
  consumerId: string;
  messageId: string;
  messageSequence: number;
  state: "claimed" | "acknowledged" | "rejected" | "expired";
  claimedAt: string;
  visibleAgainAt: string;
  settledAt: string | null;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}

export interface MailboxDeadLetter {
  deadLetterId: string;
  taskId: string;
  message: E03Message;
  failureCount: number;
  failureCodes: string[];
  finalReason: string;
  createdAt: string;
  replayedAt: string | null;
  replayMessageId: string | null;
  digest: string;
}

function assertMailboxConsumer(consumer: MailboxConsumer): void {
  const { digest: checksum, ...payload } = consumer;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "mailbox_consumer_digest",
      `mailbox consumer ${consumer.consumerId} digest is invalid`,
    );
  if (
    !consumer.consumerId ||
    !consumer.taskId ||
    !consumer.sessionId ||
    !consumer.holderId
  )
    throw new E03RuntimeError(
      "mailbox_consumer_identity",
      "mailbox consumer identity is incomplete",
    );
  if (
    !Number.isSafeInteger(consumer.maximumInFlight) ||
    consumer.maximumInFlight < 1
  )
    throw new E03RuntimeError(
      "mailbox_consumer_inflight",
      "mailbox consumer in-flight limit is invalid",
    );
  if (
    !Number.isSafeInteger(consumer.visibilityTimeoutMs) ||
    consumer.visibilityTimeoutMs < 100
  )
    throw new E03RuntimeError(
      "mailbox_consumer_visibility",
      "mailbox consumer visibility timeout is invalid",
    );
  if (
    !Number.isSafeInteger(consumer.cursorSequence) ||
    consumer.cursorSequence < 0
  )
    throw new E03RuntimeError(
      "mailbox_consumer_cursor",
      "mailbox consumer cursor is invalid",
    );
  if (!Number.isSafeInteger(consumer.revision) || consumer.revision < 1)
    throw new E03RuntimeError(
      "mailbox_consumer_revision",
      "mailbox consumer revision is invalid",
    );
}

function assertMailboxDeliveryClaim(claim: MailboxDeliveryClaim): void {
  const { digest: checksum, ...payload } = claim;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "mailbox_claim_digest",
      `mailbox claim ${claim.claimId} digest is invalid`,
    );
  if (!claim.claimId || !claim.consumerId || !claim.messageId)
    throw new E03RuntimeError(
      "mailbox_claim_identity",
      "mailbox delivery claim identity is incomplete",
    );
  if (!Number.isSafeInteger(claim.messageSequence) || claim.messageSequence < 1)
    throw new E03RuntimeError(
      "mailbox_claim_sequence",
      "mailbox delivery claim sequence is invalid",
    );
  if (!Number.isSafeInteger(claim.revision) || claim.revision < 1)
    throw new E03RuntimeError(
      "mailbox_claim_revision",
      "mailbox delivery claim revision is invalid",
    );
  if (claim.state === "rejected" && !claim.rejectionReason)
    throw new E03RuntimeError(
      "mailbox_claim_rejection",
      "rejected mailbox claim requires a reason",
    );
}

function assertMailboxDeadLetter(letter: MailboxDeadLetter): void {
  const { digest: checksum, ...payload } = letter;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "mailbox_dead_letter_digest",
      `mailbox dead letter ${letter.deadLetterId} digest is invalid`,
    );
  if (!letter.deadLetterId || !letter.taskId || !letter.finalReason)
    throw new E03RuntimeError(
      "mailbox_dead_letter_identity",
      "mailbox dead letter identity is incomplete",
    );
  assertMessage(letter.message);
  if (!Number.isSafeInteger(letter.failureCount) || letter.failureCount < 1)
    throw new E03RuntimeError(
      "mailbox_dead_letter_failures",
      "mailbox dead letter failure count is invalid",
    );
}

export class MailboxConsumerRuntime {
  private consumers = new Map<string, MailboxConsumer>();
  private claims = new Map<string, MailboxDeliveryClaim>();
  private messages = new Map<string, E03Message>();
  private sequenceByTask = new Map<string, Map<string, number>>();
  private deadLetters = new Map<string, MailboxDeadLetter>();
  private failures = new Map<string, string[]>();

  constructor(
    private readonly clock: E03Clock = new SystemE03Clock(),
    private readonly maximumDeliveryFailures = 5,
  ) {
    if (
      !Number.isSafeInteger(maximumDeliveryFailures) ||
      maximumDeliveryFailures < 1
    )
      throw new E03RuntimeError(
        "mailbox_failure_limit",
        "mailbox delivery failure limit is invalid",
      );
  }

  register(input: {
    task: E03TaskState;
    holderId: string;
    acceptedKinds?: readonly E03Message["kind"][];
    maximumInFlight?: number;
    visibilityTimeoutMs?: number;
  }): MailboxConsumer {
    if (isTerminal(input.task.status))
      throw new E03RuntimeError(
        "mailbox_consumer_terminal",
        `cannot register mailbox consumer for ${input.task.status} task`,
      );
    if (!input.task.scope.allowTeamMessaging)
      throw new E03RuntimeError(
        "mailbox_consumer_scope",
        "task scope denies team messaging",
      );
    const now = this.clock.now();
    const payload = {
      consumerId: createId("mailbox-consumer"),
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      holderId: input.holderId.trim(),
      state: "active" as const,
      acceptedKinds: [...new Set(input.acceptedKinds ?? [])],
      maximumInFlight: input.maximumInFlight ?? 16,
      visibilityTimeoutMs: input.visibilityTimeoutMs ?? 30_000,
      cursorSequence: 0,
      inFlightMessageIds: [],
      acknowledgedMessageIds: [],
      rejectedMessageIds: [],
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const consumer = { ...payload, digest: digest(payload) };
    assertMailboxConsumer(consumer);
    if (
      [...this.consumers.values()].some(
        (candidate) =>
          candidate.taskId === consumer.taskId &&
          candidate.holderId === consumer.holderId &&
          candidate.state !== "revoked",
      )
    )
      throw new E03RuntimeError(
        "mailbox_consumer_duplicate",
        `holder ${consumer.holderId} already consumes task ${consumer.taskId}`,
      );
    this.consumers.set(consumer.consumerId, consumer);
    return structuredClone(consumer);
  }

  ingest(task: E03TaskState): number {
    const sequence = new Map<string, number>();
    const existing =
      this.sequenceByTask.get(task.identity.taskId) ??
      new Map<string, number>();
    let nextSequence = [...existing.values()].reduce(
      (maximum, value) => Math.max(maximum, value),
      0,
    );
    for (const message of task.messages) {
      assertMessage(message);
      this.messages.set(message.messageId, structuredClone(message));
      const priorSequence = existing.get(message.messageId);
      if (priorSequence) sequence.set(message.messageId, priorSequence);
      else {
        nextSequence += 1;
        sequence.set(message.messageId, nextSequence);
      }
    }
    this.sequenceByTask.set(task.identity.taskId, sequence);
    return sequence.size;
  }

  claim(
    consumerId: string,
    expectedRevision: number,
  ): {
    consumer: MailboxConsumer;
    claim: MailboxDeliveryClaim | null;
    message: E03Message | null;
  } {
    let consumer = this.requireConsumer(consumerId);
    this.assertConsumerRevision(consumer, expectedRevision);
    if (consumer.state !== "active")
      throw new E03RuntimeError(
        "mailbox_consumer_inactive",
        `mailbox consumer ${consumerId} is ${consumer.state}`,
      );
    this.expireClaims(this.clock.now());
    consumer = this.requireConsumer(consumerId);
    if (consumer.inFlightMessageIds.length >= consumer.maximumInFlight)
      throw new E03RuntimeError(
        "mailbox_consumer_backpressure",
        `mailbox consumer ${consumerId} reached its in-flight limit`,
      );
    const acknowledged = new Set(consumer.acknowledgedMessageIds);
    const inFlight = new Set(consumer.inFlightMessageIds);
    const rejected = new Set(consumer.rejectedMessageIds);
    const sequences =
      this.sequenceByTask.get(consumer.taskId) ?? new Map<string, number>();
    const candidate = [...sequences]
      .filter(([, sequence]) => sequence > consumer.cursorSequence)
      .sort((left, right) => left[1] - right[1])
      .map(([messageId, sequence]) => ({
        message: this.messages.get(messageId),
        sequence,
      }))
      .find(
        ({ message }) =>
          message &&
          !acknowledged.has(message.messageId) &&
          !inFlight.has(message.messageId) &&
          !rejected.has(message.messageId) &&
          (!consumer.acceptedKinds.length ||
            consumer.acceptedKinds.includes(message.kind)),
      );
    if (!candidate?.message)
      return {
        consumer: structuredClone(consumer),
        claim: null,
        message: null,
      };
    const now = this.clock.now();
    const claimPayload = {
      claimId: createId("mailbox-claim"),
      consumerId: consumer.consumerId,
      messageId: candidate.message.messageId,
      messageSequence: candidate.sequence,
      state: "claimed" as const,
      claimedAt: now,
      visibleAgainAt: new Date(
        Date.parse(now) + consumer.visibilityTimeoutMs,
      ).toISOString(),
      settledAt: null,
      rejectionReason: null,
      revision: 1,
    };
    const claim = { ...claimPayload, digest: digest(claimPayload) };
    assertMailboxDeliveryClaim(claim);
    this.claims.set(claim.claimId, claim);
    consumer = this.transitionConsumer(consumer, {
      inFlightMessageIds: [...consumer.inFlightMessageIds, claim.messageId],
    });
    return {
      consumer,
      claim: structuredClone(claim),
      message: structuredClone(candidate.message),
    };
  }

  acknowledge(input: {
    claimId: string;
    consumerId: string;
    expectedClaimRevision: number;
    expectedConsumerRevision: number;
  }): { consumer: MailboxConsumer; claim: MailboxDeliveryClaim } {
    const claim = this.requireClaim(input.claimId);
    const consumer = this.requireConsumer(input.consumerId);
    this.assertClaimCustody(claim, consumer);
    this.assertClaimRevision(claim, input.expectedClaimRevision);
    this.assertConsumerRevision(consumer, input.expectedConsumerRevision);
    if (claim.state !== "claimed")
      throw new E03RuntimeError(
        "mailbox_claim_settled",
        `mailbox claim ${claim.claimId} is already ${claim.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(claim.visibleAgainAt))
      throw new E03RuntimeError(
        "mailbox_claim_expired",
        `mailbox claim ${claim.claimId} visibility expired`,
      );
    const nextClaim = this.transitionClaim(claim, {
      state: "acknowledged",
      settledAt: now,
    });
    const nextConsumer = this.transitionConsumer(consumer, {
      cursorSequence: Math.max(consumer.cursorSequence, claim.messageSequence),
      inFlightMessageIds: consumer.inFlightMessageIds.filter(
        (messageId) => messageId !== claim.messageId,
      ),
      acknowledgedMessageIds: [
        ...new Set([...consumer.acknowledgedMessageIds, claim.messageId]),
      ],
    });
    return { consumer: nextConsumer, claim: nextClaim };
  }

  reject(input: {
    claimId: string;
    consumerId: string;
    expectedClaimRevision: number;
    expectedConsumerRevision: number;
    code: string;
    reason: string;
    retryable: boolean;
  }): {
    consumer: MailboxConsumer;
    claim: MailboxDeliveryClaim;
    deadLetter: MailboxDeadLetter | null;
  } {
    const claim = this.requireClaim(input.claimId);
    const consumer = this.requireConsumer(input.consumerId);
    this.assertClaimCustody(claim, consumer);
    this.assertClaimRevision(claim, input.expectedClaimRevision);
    this.assertConsumerRevision(consumer, input.expectedConsumerRevision);
    if (claim.state !== "claimed")
      throw new E03RuntimeError(
        "mailbox_claim_settled",
        `mailbox claim ${claim.claimId} is already ${claim.state}`,
      );
    if (!input.code.trim() || !input.reason.trim())
      throw new E03RuntimeError(
        "mailbox_claim_rejection_reason",
        "mailbox claim rejection requires code and reason",
      );
    const failures = [
      ...(this.failures.get(claim.messageId) ?? []),
      input.code.trim(),
    ];
    this.failures.set(claim.messageId, failures);
    const terminal =
      !input.retryable || failures.length >= this.maximumDeliveryFailures;
    const nextClaim = this.transitionClaim(claim, {
      state: "rejected",
      settledAt: this.clock.now(),
      rejectionReason: input.reason.trim(),
    });
    const nextConsumer = this.transitionConsumer(consumer, {
      cursorSequence: terminal
        ? Math.max(consumer.cursorSequence, claim.messageSequence)
        : consumer.cursorSequence,
      inFlightMessageIds: consumer.inFlightMessageIds.filter(
        (messageId) => messageId !== claim.messageId,
      ),
      rejectedMessageIds: terminal
        ? [...new Set([...consumer.rejectedMessageIds, claim.messageId])]
        : consumer.rejectedMessageIds,
    });
    let deadLetter: MailboxDeadLetter | null = null;
    if (terminal) {
      const message = this.messages.get(claim.messageId);
      if (!message)
        throw new E03RuntimeError(
          "mailbox_claim_message_missing",
          `mailbox message ${claim.messageId} does not exist`,
        );
      const payload = {
        deadLetterId: createId("mailbox-dead-letter"),
        taskId: consumer.taskId,
        message: structuredClone(message),
        failureCount: failures.length,
        failureCodes: [...failures],
        finalReason: input.reason.trim(),
        createdAt: this.clock.now(),
        replayedAt: null,
        replayMessageId: null,
      };
      deadLetter = { ...payload, digest: digest(payload) };
      assertMailboxDeadLetter(deadLetter);
      this.deadLetters.set(deadLetter.deadLetterId, deadLetter);
    }
    return {
      consumer: nextConsumer,
      claim: nextClaim,
      deadLetter: deadLetter ? structuredClone(deadLetter) : null,
    };
  }

  pause(consumerId: string, expectedRevision: number): MailboxConsumer {
    const consumer = this.requireConsumer(consumerId);
    this.assertConsumerRevision(consumer, expectedRevision);
    if (consumer.state !== "active")
      throw new E03RuntimeError(
        "mailbox_consumer_pause_state",
        `mailbox consumer ${consumerId} is ${consumer.state}`,
      );
    return this.transitionConsumer(consumer, { state: "paused" });
  }

  resume(consumerId: string, expectedRevision: number): MailboxConsumer {
    const consumer = this.requireConsumer(consumerId);
    this.assertConsumerRevision(consumer, expectedRevision);
    if (consumer.state !== "paused")
      throw new E03RuntimeError(
        "mailbox_consumer_resume_state",
        `mailbox consumer ${consumerId} is ${consumer.state}`,
      );
    return this.transitionConsumer(consumer, { state: "active" });
  }

  revoke(consumerId: string, expectedRevision: number): MailboxConsumer {
    const consumer = this.requireConsumer(consumerId);
    this.assertConsumerRevision(consumer, expectedRevision);
    if (consumer.state === "revoked") return structuredClone(consumer);
    for (const claim of this.claims.values())
      if (claim.consumerId === consumerId && claim.state === "claimed")
        this.transitionClaim(claim, {
          state: "expired",
          settledAt: this.clock.now(),
        });
    return this.transitionConsumer(consumer, {
      state: "revoked",
      inFlightMessageIds: [],
    });
  }

  expireClaims(now = this.clock.now()): MailboxDeliveryClaim[] {
    const expired: MailboxDeliveryClaim[] = [];
    for (const claim of this.claims.values()) {
      if (
        claim.state !== "claimed" ||
        Date.parse(now) < Date.parse(claim.visibleAgainAt)
      )
        continue;
      const consumer = this.requireConsumer(claim.consumerId);
      expired.push(
        this.transitionClaim(claim, {
          state: "expired",
          settledAt: now,
        }),
      );
      this.transitionConsumer(consumer, {
        inFlightMessageIds: consumer.inFlightMessageIds.filter(
          (messageId) => messageId !== claim.messageId,
        ),
      });
    }
    return expired;
  }

  snapshot(): {
    consumers: MailboxConsumer[];
    claims: MailboxDeliveryClaim[];
    messages: E03Message[];
    sequences: Array<[string, Array<[string, number]>]>;
    deadLetters: MailboxDeadLetter[];
    failures: Array<[string, string[]]>;
  } {
    return {
      consumers: [...this.consumers.values()].map((value) =>
        structuredClone(value),
      ),
      claims: [...this.claims.values()].map((value) => structuredClone(value)),
      messages: [...this.messages.values()].map((value) =>
        structuredClone(value),
      ),
      sequences: [...this.sequenceByTask].map(([taskId, values]) => [
        taskId,
        [...values],
      ]),
      deadLetters: [...this.deadLetters.values()].map((value) =>
        structuredClone(value),
      ),
      failures: [...this.failures].map(([messageId, values]) => [
        messageId,
        [...values],
      ]),
    };
  }

  restore(input: {
    consumers: readonly MailboxConsumer[];
    claims: readonly MailboxDeliveryClaim[];
    messages: readonly E03Message[];
    sequences: readonly (readonly [
      string,
      readonly (readonly [string, number])[],
    ])[];
    deadLetters: readonly MailboxDeadLetter[];
    failures: readonly (readonly [string, readonly string[]])[];
  }): void {
    const consumers = new Map<string, MailboxConsumer>();
    const claims = new Map<string, MailboxDeliveryClaim>();
    const messages = new Map<string, E03Message>();
    const sequences = new Map<string, Map<string, number>>();
    const deadLetters = new Map<string, MailboxDeadLetter>();
    const failures = new Map<string, string[]>();
    for (const raw of input.consumers) {
      const consumer = structuredClone(raw);
      assertMailboxConsumer(consumer);
      if (consumers.has(consumer.consumerId))
        throw new E03RuntimeError(
          "mailbox_consumer_restore_duplicate",
          `duplicate mailbox consumer ${consumer.consumerId}`,
        );
      consumers.set(consumer.consumerId, consumer);
    }
    for (const raw of input.messages) {
      const message = structuredClone(raw);
      assertMessage(message);
      if (messages.has(message.messageId))
        throw new E03RuntimeError(
          "mailbox_message_restore_duplicate",
          `duplicate mailbox message ${message.messageId}`,
        );
      messages.set(message.messageId, message);
    }
    for (const [taskId, rawEntries] of input.sequences) {
      if (sequences.has(taskId))
        throw new E03RuntimeError(
          "mailbox_sequence_restore_task_duplicate",
          `duplicate mailbox sequence task ${taskId}`,
        );
      const taskSequences = new Map<string, number>();
      const seenSequence = new Set<number>();
      for (const [messageId, sequence] of rawEntries) {
        const message = messages.get(messageId);
        if (!message)
          throw new E03RuntimeError(
            "mailbox_sequence_restore_message",
            `mailbox sequence references missing message ${messageId}`,
          );
        if (message.recipientTaskId !== taskId)
          throw new E03RuntimeError(
            "mailbox_sequence_restore_custody",
            `mailbox message ${messageId} belongs to another task`,
          );
        if (
          !Number.isSafeInteger(sequence) ||
          sequence < 1 ||
          seenSequence.has(sequence)
        )
          throw new E03RuntimeError(
            "mailbox_sequence_restore_invalid",
            `mailbox sequence ${sequence} is invalid or duplicated`,
          );
        seenSequence.add(sequence);
        taskSequences.set(messageId, sequence);
      }
      sequences.set(taskId, taskSequences);
    }
    for (const raw of input.claims) {
      const claim = structuredClone(raw);
      assertMailboxDeliveryClaim(claim);
      const consumer = consumers.get(claim.consumerId);
      if (!consumer)
        throw new E03RuntimeError(
          "mailbox_claim_restore_consumer",
          `mailbox claim ${claim.claimId} has no consumer`,
        );
      if (!messages.has(claim.messageId))
        throw new E03RuntimeError(
          "mailbox_claim_restore_message",
          `mailbox claim ${claim.claimId} has no message`,
        );
      if (claims.has(claim.claimId))
        throw new E03RuntimeError(
          "mailbox_claim_restore_duplicate",
          `duplicate mailbox claim ${claim.claimId}`,
        );
      claims.set(claim.claimId, claim);
    }
    for (const raw of input.deadLetters) {
      const letter = structuredClone(raw);
      assertMailboxDeadLetter(letter);
      if (!messages.has(letter.message.messageId))
        messages.set(letter.message.messageId, structuredClone(letter.message));
      if (deadLetters.has(letter.deadLetterId))
        throw new E03RuntimeError(
          "mailbox_dead_letter_restore_duplicate",
          `duplicate mailbox dead letter ${letter.deadLetterId}`,
        );
      deadLetters.set(letter.deadLetterId, letter);
    }
    for (const [messageId, codes] of input.failures) {
      if (!messages.has(messageId))
        throw new E03RuntimeError(
          "mailbox_failure_restore_message",
          `mailbox failure references missing message ${messageId}`,
        );
      if (failures.has(messageId))
        throw new E03RuntimeError(
          "mailbox_failure_restore_duplicate",
          `duplicate mailbox failure row ${messageId}`,
        );
      failures.set(messageId, [...codes]);
    }
    for (const consumer of consumers.values()) {
      const taskSequences =
        sequences.get(consumer.taskId) ?? new Map<string, number>();
      for (const messageId of [
        ...consumer.inFlightMessageIds,
        ...consumer.acknowledgedMessageIds,
        ...consumer.rejectedMessageIds,
      ])
        if (!taskSequences.has(messageId))
          throw new E03RuntimeError(
            "mailbox_consumer_restore_message",
            `mailbox consumer ${consumer.consumerId} references unknown message ${messageId}`,
          );
    }
    this.consumers = consumers;
    this.claims = claims;
    this.messages = messages;
    this.sequenceByTask = sequences;
    this.deadLetters = deadLetters;
    this.failures = failures;
  }

  private requireConsumer(consumerId: string): MailboxConsumer {
    const consumer = this.consumers.get(consumerId);
    if (!consumer)
      throw new E03RuntimeError(
        "mailbox_consumer_missing",
        `mailbox consumer ${consumerId} does not exist`,
      );
    assertMailboxConsumer(consumer);
    return consumer;
  }

  private requireClaim(claimId: string): MailboxDeliveryClaim {
    const claim = this.claims.get(claimId);
    if (!claim)
      throw new E03RuntimeError(
        "mailbox_claim_missing",
        `mailbox claim ${claimId} does not exist`,
      );
    assertMailboxDeliveryClaim(claim);
    return claim;
  }

  private assertClaimCustody(
    claim: MailboxDeliveryClaim,
    consumer: MailboxConsumer,
  ): void {
    if (claim.consumerId !== consumer.consumerId)
      throw new E03RuntimeError(
        "mailbox_claim_custody",
        `mailbox claim ${claim.claimId} belongs to another consumer`,
      );
  }

  private assertConsumerRevision(
    consumer: MailboxConsumer,
    expected: number,
  ): void {
    if (consumer.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_consumer_stale_revision",
        `mailbox consumer ${consumer.consumerId} revision is stale`,
      );
  }

  private assertClaimRevision(
    claim: MailboxDeliveryClaim,
    expected: number,
  ): void {
    if (claim.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_claim_stale_revision",
        `mailbox claim ${claim.claimId} revision is stale`,
      );
  }

  private transitionConsumer(
    consumer: MailboxConsumer,
    patch: Partial<Omit<MailboxConsumer, "consumerId" | "revision" | "digest">>,
  ): MailboxConsumer {
    const { digest: _, ...prior } = consumer;
    const payload = {
      ...prior,
      ...patch,
      consumerId: consumer.consumerId,
      updatedAt: this.clock.now(),
      revision: consumer.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxConsumer(next);
    this.consumers.set(next.consumerId, next);
    return structuredClone(next);
  }

  private transitionClaim(
    claim: MailboxDeliveryClaim,
    patch: Partial<
      Omit<MailboxDeliveryClaim, "claimId" | "revision" | "digest">
    >,
  ): MailboxDeliveryClaim {
    const { digest: _, ...prior } = claim;
    const payload = {
      ...prior,
      ...patch,
      claimId: claim.claimId,
      revision: claim.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxDeliveryClaim(next);
    this.claims.set(next.claimId, next);
    return structuredClone(next);
  }
}

export interface MailboxTopic {
  topicId: string;
  teamId: string;
  name: string;
  state: "active" | "paused" | "closed";
  retentionLimit: number;
  publishedSequence: number;
  createdAt: string;
  closedAt: string | null;
  revision: number;
  digest: string;
}
export interface MailboxTopicEvent {
  eventId: string;
  topicId: string;
  teamId: string;
  sequence: number;
  message: E03Message;
  publisherTaskId: string;
  publishedAt: string;
  previousDigest: string;
  digest: string;
}
export interface MailboxTopicSubscription {
  subscriptionId: string;
  topicId: string;
  taskId: string;
  state: "active" | "paused" | "revoked" | "closed";
  startSequence: number;
  deliveredSequence: number;
  acknowledgedSequence: number;
  maximumInFlight: number;
  leaseExpiresAt: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}
function assertMailboxTopic(value: MailboxTopic): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "mailbox_topic_digest",
      `mailbox topic ${value.topicId} is corrupt`,
    );
  if (
    !value.topicId ||
    !value.teamId ||
    !value.name ||
    !Number.isSafeInteger(value.retentionLimit) ||
    value.retentionLimit < 1 ||
    !Number.isSafeInteger(value.publishedSequence) ||
    value.publishedSequence < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "mailbox_topic",
      `mailbox topic ${value.topicId} is invalid`,
    );
}
function assertMailboxTopicEvent(value: MailboxTopicEvent): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "mailbox_topic_event_digest",
      `mailbox topic event ${value.eventId} is corrupt`,
    );
  if (
    !value.eventId ||
    !value.topicId ||
    !value.teamId ||
    !value.publisherTaskId ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1
  )
    throw new E03RuntimeError(
      "mailbox_topic_event",
      `mailbox topic event ${value.eventId} is invalid`,
    );
  const { digest: messageDigest, ...messagePayload } = value.message;
  if (digest(messagePayload) !== messageDigest)
    throw new E03RuntimeError(
      "mailbox_topic_message_digest",
      `mailbox topic event ${value.eventId} message is corrupt`,
    );
}
function assertMailboxSubscription(value: MailboxTopicSubscription): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "mailbox_subscription_digest",
      `mailbox subscription ${value.subscriptionId} is corrupt`,
    );
  if (
    !value.subscriptionId ||
    !value.topicId ||
    !value.taskId ||
    !Number.isSafeInteger(value.startSequence) ||
    value.startSequence < 0 ||
    !Number.isSafeInteger(value.deliveredSequence) ||
    value.deliveredSequence < value.startSequence ||
    !Number.isSafeInteger(value.acknowledgedSequence) ||
    value.acknowledgedSequence < value.startSequence ||
    value.acknowledgedSequence > value.deliveredSequence ||
    !Number.isSafeInteger(value.maximumInFlight) ||
    value.maximumInFlight < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.leaseExpiresAt))
  )
    throw new E03RuntimeError(
      "mailbox_subscription",
      `mailbox subscription ${value.subscriptionId} is invalid`,
    );
}
export interface MailboxQuarantineEntry {
  deadLetterId: string;
  messageId: string;
  recipientTaskId: string;
  consumerId: string;
  queue: string;
  originalDeliveryDigest: string;
  reasonCode: string;
  attempt: number;
  state: "quarantined" | "approved" | "redriving" | "recovered" | "discarded";
  quarantinedAt: string;
  updatedAt: string;
  terminalAt: string;
  revision: number;
  digest: string;
}

export interface MailboxRedriveAttempt {
  attemptId: string;
  deadLetterId: string;
  targetConsumerId: string;
  targetQueue: string;
  idempotencyKey: string;
  state: "prepared" | "offered" | "acknowledged" | "failed" | "cancelled";
  preparedAt: string;
  offeredAt: string;
  settledAt: string;
  receiptDigest: string;
  errorCode: string;
  previousDigest: string;
  revision: number;
  digest: string;
}

export interface MailboxQuarantinePolicy {
  policyId: string;
  queue: string;
  maximumAttempts: number;
  retryableReasonCodes: string[];
  maximumQuarantineEntries: number;
  redriveRequiresApproval: boolean;
  enabled: boolean;
  createdAt: string;
  revision: number;
  digest: string;
}

export interface MailboxRedriveSnapshot {
  policies: MailboxQuarantinePolicy[];
  deadLetters: MailboxQuarantineEntry[];
  attempts: MailboxRedriveAttempt[];
  activeDeadLetterByMessageConsumer: [string, string][];
  attemptByIdempotencyKey: [string, string][];
}

function assertMailboxQuarantinePolicy(value: MailboxQuarantinePolicy): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.policyId ||
    !value.queue ||
    value.maximumAttempts < 1 ||
    value.maximumQuarantineEntries < 1 ||
    new Set(value.retryableReasonCodes).size !==
      value.retryableReasonCodes.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "mailbox_quarantine_policy_corrupt",
      `mailbox quarantine policy ${value.policyId || "<empty>"} is corrupt`,
    );
}

function assertMailboxQuarantineEntry(value: MailboxQuarantineEntry): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.deadLetterId ||
    !value.messageId ||
    !value.recipientTaskId ||
    !value.consumerId ||
    !value.queue ||
    !value.originalDeliveryDigest ||
    !value.reasonCode ||
    value.attempt < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "mailbox_dead_letter_corrupt",
      `mailbox dead letter ${value.deadLetterId || "<empty>"} is corrupt`,
    );
}

function assertMailboxRedriveAttempt(value: MailboxRedriveAttempt): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.attemptId ||
    !value.deadLetterId ||
    !value.targetConsumerId ||
    !value.targetQueue ||
    !value.idempotencyKey ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "mailbox_redrive_attempt_corrupt",
      `mailbox redrive attempt ${value.attemptId || "<empty>"} is corrupt`,
    );
}

export class MailboxRedriveRuntime {
  private policies = new Map<string, MailboxQuarantinePolicy>();
  private policyByQueue = new Map<string, string>();
  private deadLetters = new Map<string, MailboxQuarantineEntry>();
  private attempts = new Map<string, MailboxRedriveAttempt[]>();
  private activeDeadLetterByMessageConsumer = new Map<string, string>();
  private attemptByIdempotencyKey = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registerPolicy(input: {
    policyId?: string;
    queue: string;
    maximumAttempts: number;
    retryableReasonCodes: readonly string[];
    maximumQuarantineEntries: number;
    redriveRequiresApproval: boolean;
  }): MailboxQuarantinePolicy {
    if (this.policyByQueue.has(input.queue))
      throw new E03RuntimeError(
        "mailbox_quarantine_policy_queue_duplicate",
        `queue ${input.queue} already has a quarantine policy`,
      );
    const policyId = input.policyId ?? createId("mailbox-quarantine-policy");
    const payload = {
      policyId,
      queue: input.queue,
      maximumAttempts: input.maximumAttempts,
      retryableReasonCodes: [...new Set(input.retryableReasonCodes)].sort(),
      maximumQuarantineEntries: input.maximumQuarantineEntries,
      redriveRequiresApproval: input.redriveRequiresApproval,
      enabled: true,
      createdAt: this.clock.now(),
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertMailboxQuarantinePolicy(policy);
    this.policies.set(policyId, policy);
    this.policyByQueue.set(input.queue, policyId);
    return structuredClone(policy);
  }

  quarantine(input: {
    deadLetterId?: string;
    messageId: string;
    recipientTaskId: string;
    consumerId: string;
    queue: string;
    originalDeliveryDigest: string;
    reasonCode: string;
    attempt: number;
  }): MailboxQuarantineEntry {
    const policy = this.requirePolicyForQueue(input.queue);
    if (!policy.enabled)
      throw new E03RuntimeError(
        "mailbox_quarantine_policy_disabled",
        `queue ${input.queue} quarantine is disabled`,
      );
    const key = this.deadLetterKey(input.messageId, input.consumerId);
    const existingId = this.activeDeadLetterByMessageConsumer.get(key);
    if (existingId) return structuredClone(this.requireDeadLetter(existingId));
    const queueEntries = [...this.deadLetters.values()].filter(
      (value) =>
        value.queue === input.queue &&
        !["recovered", "discarded"].includes(value.state),
    );
    if (queueEntries.length >= policy.maximumQuarantineEntries)
      throw new E03RuntimeError(
        "mailbox_quarantine_capacity",
        `queue ${input.queue} quarantine is full`,
      );
    if (input.attempt < policy.maximumAttempts)
      throw new E03RuntimeError(
        "mailbox_quarantine_attempts_remaining",
        `message ${input.messageId} still has delivery attempts`,
      );
    const deadLetterId = input.deadLetterId ?? createId("mailbox-dead-letter");
    const now = this.clock.now();
    const payload = {
      deadLetterId,
      messageId: input.messageId,
      recipientTaskId: input.recipientTaskId,
      consumerId: input.consumerId,
      queue: input.queue,
      originalDeliveryDigest: input.originalDeliveryDigest,
      reasonCode: input.reasonCode,
      attempt: input.attempt,
      state: "quarantined" as const,
      quarantinedAt: now,
      updatedAt: now,
      terminalAt: "",
      revision: 1,
    };
    const deadLetter = { ...payload, digest: digest(payload) };
    assertMailboxQuarantineEntry(deadLetter);
    this.deadLetters.set(deadLetterId, deadLetter);
    this.attempts.set(deadLetterId, []);
    this.activeDeadLetterByMessageConsumer.set(key, deadLetterId);
    return structuredClone(deadLetter);
  }

  approve(
    deadLetterId: string,
    expectedRevision: number,
  ): MailboxQuarantineEntry {
    const deadLetter = this.requireDeadLetter(deadLetterId);
    this.assertDeadLetterRevision(deadLetter, expectedRevision);
    if (deadLetter.state !== "quarantined")
      throw new E03RuntimeError(
        "mailbox_dead_letter_approve_state",
        `mailbox dead letter ${deadLetterId} is ${deadLetter.state}`,
      );
    return this.transitionDeadLetter(deadLetter, { state: "approved" });
  }

  prepare(input: {
    attemptId?: string;
    deadLetterId: string;
    expectedRevision: number;
    targetConsumerId: string;
    targetQueue: string;
    idempotencyKey: string;
  }): MailboxRedriveAttempt {
    const duplicateId = this.attemptByIdempotencyKey.get(input.idempotencyKey);
    if (duplicateId) return structuredClone(this.requireAttempt(duplicateId));
    const deadLetter = this.requireDeadLetter(input.deadLetterId);
    this.assertDeadLetterRevision(deadLetter, input.expectedRevision);
    const policy = this.requirePolicyForQueue(deadLetter.queue);
    const permitted = policy.redriveRequiresApproval
      ? deadLetter.state === "approved"
      : ["quarantined", "approved"].includes(deadLetter.state);
    if (!permitted)
      throw new E03RuntimeError(
        "mailbox_redrive_prepare_state",
        `mailbox dead letter ${deadLetter.deadLetterId} is ${deadLetter.state}`,
      );
    if (!policy.retryableReasonCodes.includes(deadLetter.reasonCode))
      throw new E03RuntimeError(
        "mailbox_redrive_reason_denied",
        `mailbox dead letter reason ${deadLetter.reasonCode} is not retryable`,
      );
    const entries = this.attemptEntries(deadLetter.deadLetterId);
    if (entries.some((value) => ["prepared", "offered"].includes(value.state)))
      throw new E03RuntimeError(
        "mailbox_redrive_attempt_active",
        `mailbox dead letter ${deadLetter.deadLetterId} already redrives`,
      );
    const attemptId = input.attemptId ?? createId("mailbox-redrive-attempt");
    const payload = {
      attemptId,
      deadLetterId: deadLetter.deadLetterId,
      targetConsumerId: input.targetConsumerId,
      targetQueue: input.targetQueue,
      idempotencyKey: input.idempotencyKey,
      state: "prepared" as const,
      preparedAt: this.clock.now(),
      offeredAt: "",
      settledAt: "",
      receiptDigest: "",
      errorCode: "",
      previousDigest: entries.at(-1)?.digest ?? "",
      revision: 1,
    };
    const attempt = { ...payload, digest: digest(payload) };
    assertMailboxRedriveAttempt(attempt);
    entries.push(attempt);
    this.attempts.set(deadLetter.deadLetterId, entries);
    this.attemptByIdempotencyKey.set(input.idempotencyKey, attemptId);
    this.transitionDeadLetter(deadLetter, { state: "redriving" });
    return structuredClone(attempt);
  }

  offer(
    attemptId: string,
    expectedRevision: number,
    receiptDigest: string,
  ): MailboxRedriveAttempt {
    const attempt = this.requireAttempt(attemptId);
    this.assertAttemptRevision(attempt, expectedRevision);
    if (attempt.state !== "prepared" || !receiptDigest)
      throw new E03RuntimeError(
        "mailbox_redrive_offer_state",
        `mailbox redrive attempt ${attemptId} cannot be offered`,
      );
    return this.transitionAttempt(attempt, {
      state: "offered",
      offeredAt: this.clock.now(),
      receiptDigest,
    });
  }

  settle(
    attemptId: string,
    expectedRevision: number,
    input: { accepted: boolean; receiptDigest: string; errorCode?: string },
  ): MailboxRedriveAttempt {
    const attempt = this.requireAttempt(attemptId);
    this.assertAttemptRevision(attempt, expectedRevision);
    if (
      attempt.state !== "offered" ||
      attempt.receiptDigest !== input.receiptDigest
    )
      throw new E03RuntimeError(
        "mailbox_redrive_settle_receipt",
        `mailbox redrive attempt ${attemptId} receipt is invalid`,
      );
    const deadLetter = this.requireDeadLetter(attempt.deadLetterId);
    const next = this.transitionAttempt(attempt, {
      state: input.accepted ? "acknowledged" : "failed",
      settledAt: this.clock.now(),
      errorCode: input.errorCode ?? "",
    });
    if (input.accepted) {
      this.transitionDeadLetter(deadLetter, {
        state: "recovered",
        terminalAt: this.clock.now(),
      });
      this.activeDeadLetterByMessageConsumer.delete(
        this.deadLetterKey(deadLetter.messageId, deadLetter.consumerId),
      );
    } else this.transitionDeadLetter(deadLetter, { state: "approved" });
    return next;
  }

  discard(
    deadLetterId: string,
    expectedRevision: number,
    reason: string,
  ): MailboxQuarantineEntry {
    const deadLetter = this.requireDeadLetter(deadLetterId);
    this.assertDeadLetterRevision(deadLetter, expectedRevision);
    if (["redriving", "recovered", "discarded"].includes(deadLetter.state))
      throw new E03RuntimeError(
        "mailbox_dead_letter_discard_state",
        `mailbox dead letter ${deadLetterId} cannot be discarded`,
      );
    const next = this.transitionDeadLetter(deadLetter, {
      state: "discarded",
      terminalAt: this.clock.now(),
      reasonCode: reason || deadLetter.reasonCode,
    });
    this.activeDeadLetterByMessageConsumer.delete(
      this.deadLetterKey(deadLetter.messageId, deadLetter.consumerId),
    );
    return next;
  }

  snapshot(): MailboxRedriveSnapshot {
    return {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      deadLetters: [...this.deadLetters.values()].map((value) =>
        structuredClone(value),
      ),
      attempts: [...this.attempts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeDeadLetterByMessageConsumer: [
        ...this.activeDeadLetterByMessageConsumer.entries(),
      ],
      attemptByIdempotencyKey: [...this.attemptByIdempotencyKey.entries()],
    };
  }

  restore(snapshot: MailboxRedriveSnapshot): void {
    const policies = new Map<string, MailboxQuarantinePolicy>();
    const policyByQueue = new Map<string, string>();
    for (const value of snapshot.policies) {
      assertMailboxQuarantinePolicy(value);
      if (policies.has(value.policyId) || policyByQueue.has(value.queue))
        throw new E03RuntimeError(
          "mailbox_quarantine_restore_duplicate",
          `policy ${value.policyId} duplicates`,
        );
      policies.set(value.policyId, structuredClone(value));
      policyByQueue.set(value.queue, value.policyId);
    }
    const deadLetters = new Map<string, MailboxQuarantineEntry>();
    const attempts = new Map<string, MailboxRedriveAttempt[]>();
    for (const value of snapshot.deadLetters) {
      assertMailboxQuarantineEntry(value);
      if (
        !policyByQueue.has(value.queue) ||
        deadLetters.has(value.deadLetterId)
      )
        throw new E03RuntimeError(
          "mailbox_dead_letter_restore",
          `dead letter ${value.deadLetterId} invalid`,
        );
      deadLetters.set(value.deadLetterId, structuredClone(value));
      attempts.set(value.deadLetterId, []);
    }
    for (const value of snapshot.attempts) {
      assertMailboxRedriveAttempt(value);
      const entries = attempts.get(value.deadLetterId);
      if (!entries || value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "mailbox_redrive_restore_chain",
          `attempt ${value.attemptId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    const activeDeadLetterByMessageConsumer = new Map(
      snapshot.activeDeadLetterByMessageConsumer,
    );
    const attemptByIdempotencyKey = new Map(snapshot.attemptByIdempotencyKey);
    if (
      activeDeadLetterByMessageConsumer.size !==
        snapshot.activeDeadLetterByMessageConsumer.length ||
      attemptByIdempotencyKey.size !== snapshot.attemptByIdempotencyKey.length
    )
      throw new E03RuntimeError(
        "mailbox_redrive_restore_index_duplicate",
        "redrive indexes duplicate",
      );
    for (const [key, deadLetterId] of activeDeadLetterByMessageConsumer) {
      const value = deadLetters.get(deadLetterId);
      if (
        !value ||
        key !== this.deadLetterKey(value.messageId, value.consumerId) ||
        ["recovered", "discarded"].includes(value.state)
      )
        throw new E03RuntimeError(
          "mailbox_dead_letter_restore_index",
          `dead letter index ${key} invalid`,
        );
    }
    for (const [key, attemptId] of attemptByIdempotencyKey) {
      const value = [...attempts.values()]
        .flat()
        .find((entry) => entry.attemptId === attemptId);
      if (!value || value.idempotencyKey !== key)
        throw new E03RuntimeError(
          "mailbox_redrive_restore_idempotency",
          `redrive index ${key} invalid`,
        );
    }
    this.policies = policies;
    this.policyByQueue = policyByQueue;
    this.deadLetters = deadLetters;
    this.attempts = attempts;
    this.activeDeadLetterByMessageConsumer = activeDeadLetterByMessageConsumer;
    this.attemptByIdempotencyKey = attemptByIdempotencyKey;
  }

  private deadLetterKey(messageId: string, consumerId: string): string {
    return `${messageId}\u0000${consumerId}`;
  }

  private requirePolicyForQueue(queue: string): MailboxQuarantinePolicy {
    const id = this.policyByQueue.get(queue);
    const value = id ? this.policies.get(id) : null;
    if (!value)
      throw new E03RuntimeError(
        "mailbox_quarantine_policy_missing",
        `queue ${queue} has no policy`,
      );
    assertMailboxQuarantinePolicy(value);
    return value;
  }

  private requireDeadLetter(id: string): MailboxQuarantineEntry {
    const value = this.deadLetters.get(id);
    if (!value)
      throw new E03RuntimeError(
        "mailbox_dead_letter_missing",
        `dead letter ${id} missing`,
      );
    assertMailboxQuarantineEntry(value);
    return value;
  }

  private requireAttempt(id: string): MailboxRedriveAttempt {
    const value = [...this.attempts.values()]
      .flat()
      .find((entry) => entry.attemptId === id);
    if (!value)
      throw new E03RuntimeError(
        "mailbox_redrive_attempt_missing",
        `redrive attempt ${id} missing`,
      );
    assertMailboxRedriveAttempt(value);
    return value;
  }

  private attemptEntries(deadLetterId: string): MailboxRedriveAttempt[] {
    return this.attempts.get(deadLetterId) ?? [];
  }

  private assertDeadLetterRevision(
    value: MailboxQuarantineEntry,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_dead_letter_stale_revision",
        `dead letter ${value.deadLetterId} stale`,
      );
  }

  private assertAttemptRevision(
    value: MailboxRedriveAttempt,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_redrive_attempt_stale_revision",
        `attempt ${value.attemptId} stale`,
      );
  }

  private transitionDeadLetter(
    value: MailboxQuarantineEntry,
    patch: Partial<
      Omit<MailboxQuarantineEntry, "deadLetterId" | "revision" | "digest">
    >,
  ): MailboxQuarantineEntry {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      deadLetterId: value.deadLetterId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxQuarantineEntry(next);
    this.deadLetters.set(next.deadLetterId, next);
    return structuredClone(next);
  }

  private transitionAttempt(
    value: MailboxRedriveAttempt,
    patch: Partial<
      Omit<MailboxRedriveAttempt, "attemptId" | "revision" | "digest">
    >,
  ): MailboxRedriveAttempt {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      attemptId: value.attemptId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxRedriveAttempt(next);
    const entries = this.attemptEntries(value.deadLetterId);
    const index = entries.findIndex(
      (entry) => entry.attemptId === value.attemptId,
    );
    entries[index] = next;
    this.attempts.set(value.deadLetterId, entries);
    return structuredClone(next);
  }
}

export class MailboxTopicRuntime {
  private topics = new Map<string, MailboxTopic>();
  private events = new Map<string, MailboxTopicEvent[]>();
  private subscriptions = new Map<string, MailboxTopicSubscription>();
  private subscriptionIndex = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  create(input: {
    teamId: string;
    name: string;
    retentionLimit?: number;
  }): MailboxTopic {
    if (!input.teamId.trim() || !input.name.trim())
      throw new E03RuntimeError(
        "mailbox_topic_input",
        "mailbox topic input is invalid",
      );
    const existing = [...this.topics.values()].find(
      (value) =>
        value.teamId === input.teamId &&
        value.name === input.name &&
        value.state !== "closed",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      topicId: createId("mailbox-topic"),
      teamId: input.teamId.trim(),
      name: input.name.trim(),
      state: "active" as const,
      retentionLimit: input.retentionLimit ?? 1024,
      publishedSequence: 0,
      createdAt: this.clock.now(),
      closedAt: null,
      revision: 1,
    };
    const topic = { ...payload, digest: digest(payload) };
    assertMailboxTopic(topic);
    this.topics.set(topic.topicId, topic);
    return structuredClone(topic);
  }
  publish(
    topicId: string,
    expectedRevision: number,
    message: E03Message,
  ): {
    topic: MailboxTopic;
    event: MailboxTopicEvent;
    trimmedEventIds: string[];
  } {
    const topic = this.requireTopic(topicId);
    this.assertTopicRevision(topic, expectedRevision);
    if (topic.state !== "active")
      throw new E03RuntimeError(
        "mailbox_topic_publish_state",
        `mailbox topic ${topicId} is ${topic.state}`,
      );
    const { digest: messageDigest, ...messagePayload } = message;
    if (
      digest(messagePayload) !== messageDigest ||
      !message.senderTaskId ||
      !message.recipientTaskId
    )
      throw new E03RuntimeError(
        "mailbox_topic_message_custody",
        `message ${message.messageId} is invalid for topic`,
      );
    const entries = this.events.get(topic.topicId) ?? [];
    const sequence = topic.publishedSequence + 1;
    const payload = {
      eventId: createId("mailbox-topic-event"),
      topicId: topic.topicId,
      teamId: topic.teamId,
      sequence,
      message: structuredClone(message),
      publisherTaskId: message.senderTaskId,
      publishedAt: this.clock.now(),
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
    };
    const event = { ...payload, digest: digest(payload) };
    assertMailboxTopicEvent(event);
    entries.push(event);
    const trimmedEventIds: string[] = [];
    while (entries.length > topic.retentionLimit) {
      const candidate = entries[0]!;
      const minimumAck = Math.min(
        sequence,
        ...[...this.subscriptions.values()]
          .filter(
            (value) =>
              value.topicId === topicId &&
              value.state !== "revoked" &&
              value.state !== "closed",
          )
          .map((value) => value.acknowledgedSequence),
      );
      if (candidate.sequence > minimumAck)
        throw new E03RuntimeError(
          "mailbox_topic_retention_blocked",
          `mailbox topic ${topicId} retention is blocked by subscriptions`,
        );
      trimmedEventIds.push(entries.shift()!.eventId);
    }
    this.events.set(topic.topicId, entries);
    const nextTopic = this.transitionTopic(topic, {
      publishedSequence: sequence,
    });
    return { topic: nextTopic, event: structuredClone(event), trimmedEventIds };
  }
  subscribe(input: {
    topicId: string;
    taskId: string;
    startSequence?: number;
    maximumInFlight?: number;
    ttlMs: number;
  }): MailboxTopicSubscription {
    const topic = this.requireTopic(input.topicId);
    if (topic.state === "closed")
      throw new E03RuntimeError(
        "mailbox_subscription_topic_closed",
        `mailbox topic ${topic.topicId} is closed`,
      );
    if (
      !input.taskId.trim() ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "mailbox_subscription_input",
        "mailbox subscription input is invalid",
      );
    const key = `${topic.topicId}:${input.taskId}`;
    const existingId = this.subscriptionIndex.get(key);
    if (existingId)
      return structuredClone(this.requireSubscription(existingId));
    const startSequence = input.startSequence ?? topic.publishedSequence;
    const earliest =
      this.events.get(topic.topicId)?.[0]?.sequence ??
      topic.publishedSequence + 1;
    if (
      !Number.isSafeInteger(startSequence) ||
      startSequence < 0 ||
      startSequence > topic.publishedSequence ||
      startSequence + 1 < earliest
    )
      throw new E03RuntimeError(
        "mailbox_subscription_cursor",
        `mailbox subscription start sequence ${startSequence} is unavailable`,
      );
    const payload = {
      subscriptionId: createId("mailbox-subscription"),
      topicId: topic.topicId,
      taskId: input.taskId.trim(),
      state: "active" as const,
      startSequence,
      deliveredSequence: startSequence,
      acknowledgedSequence: startSequence,
      maximumInFlight: input.maximumInFlight ?? 32,
      leaseExpiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      createdAt: this.clock.now(),
      updatedAt: this.clock.now(),
      revision: 1,
    };
    const subscription = { ...payload, digest: digest(payload) };
    assertMailboxSubscription(subscription);
    this.subscriptions.set(subscription.subscriptionId, subscription);
    this.subscriptionIndex.set(key, subscription.subscriptionId);
    return structuredClone(subscription);
  }
  poll(
    subscriptionId: string,
    expectedRevision: number,
    maximum?: number,
  ): { subscription: MailboxTopicSubscription; events: MailboxTopicEvent[] } {
    const subscription = this.requireSubscription(subscriptionId);
    this.assertSubscriptionRevision(subscription, expectedRevision);
    if (subscription.state !== "active")
      throw new E03RuntimeError(
        "mailbox_subscription_poll_state",
        `mailbox subscription ${subscriptionId} is ${subscription.state}`,
      );
    if (Date.parse(subscription.leaseExpiresAt) <= Date.parse(this.clock.now()))
      return {
        subscription: this.transitionSubscription(subscription, {
          state: "revoked",
        }),
        events: [],
      };
    const availableCapacity =
      subscription.maximumInFlight -
      (subscription.deliveredSequence - subscription.acknowledgedSequence);
    if (availableCapacity <= 0)
      throw new E03RuntimeError(
        "mailbox_subscription_backpressure",
        `mailbox subscription ${subscriptionId} reached in-flight limit`,
      );
    const limit = Math.min(maximum ?? availableCapacity, availableCapacity);
    if (!Number.isSafeInteger(limit) || limit < 1)
      throw new E03RuntimeError(
        "mailbox_subscription_poll_limit",
        "mailbox subscription poll limit is invalid",
      );
    const events = (this.events.get(subscription.topicId) ?? [])
      .filter((value) => value.sequence > subscription.deliveredSequence)
      .slice(0, limit)
      .map((value) => structuredClone(value));
    const deliveredSequence =
      events[events.length - 1]?.sequence ?? subscription.deliveredSequence;
    const next = events.length
      ? this.transitionSubscription(subscription, { deliveredSequence })
      : structuredClone(subscription);
    return { subscription: next, events };
  }
  acknowledge(
    subscriptionId: string,
    expectedRevision: number,
    sequence: number,
  ): MailboxTopicSubscription {
    const subscription = this.requireSubscription(subscriptionId);
    this.assertSubscriptionRevision(subscription, expectedRevision);
    if (subscription.state !== "active" && subscription.state !== "paused")
      throw new E03RuntimeError(
        "mailbox_subscription_ack_state",
        `mailbox subscription ${subscriptionId} is ${subscription.state}`,
      );
    if (
      !Number.isSafeInteger(sequence) ||
      sequence <= subscription.acknowledgedSequence ||
      sequence > subscription.deliveredSequence
    )
      throw new E03RuntimeError(
        "mailbox_subscription_ack_cursor",
        `mailbox subscription ACK ${sequence} is invalid`,
      );
    return this.transitionSubscription(subscription, {
      acknowledgedSequence: sequence,
    });
  }
  pause(
    subscriptionId: string,
    expectedRevision: number,
  ): MailboxTopicSubscription {
    const subscription = this.requireSubscription(subscriptionId);
    this.assertSubscriptionRevision(subscription, expectedRevision);
    if (subscription.state !== "active")
      throw new E03RuntimeError(
        "mailbox_subscription_pause_state",
        `mailbox subscription ${subscriptionId} is ${subscription.state}`,
      );
    return this.transitionSubscription(subscription, { state: "paused" });
  }
  resume(
    subscriptionId: string,
    expectedRevision: number,
    ttlMs: number,
  ): MailboxTopicSubscription {
    const subscription = this.requireSubscription(subscriptionId);
    this.assertSubscriptionRevision(subscription, expectedRevision);
    if (subscription.state !== "paused")
      throw new E03RuntimeError(
        "mailbox_subscription_resume_state",
        `mailbox subscription ${subscriptionId} is ${subscription.state}`,
      );
    if (!Number.isSafeInteger(ttlMs) || ttlMs < 1)
      throw new E03RuntimeError(
        "mailbox_subscription_ttl",
        "mailbox subscription TTL is invalid",
      );
    return this.transitionSubscription(subscription, {
      state: "active",
      leaseExpiresAt: new Date(
        Date.parse(this.clock.now()) + ttlMs,
      ).toISOString(),
    });
  }
  closeSubscription(
    subscriptionId: string,
    expectedRevision: number,
  ): MailboxTopicSubscription {
    const subscription = this.requireSubscription(subscriptionId);
    this.assertSubscriptionRevision(subscription, expectedRevision);
    if (subscription.state === "closed") return structuredClone(subscription);
    const next = this.transitionSubscription(subscription, { state: "closed" });
    this.subscriptionIndex.delete(`${next.topicId}:${next.taskId}`);
    return next;
  }
  closeTopic(topicId: string, expectedRevision: number): MailboxTopic {
    const topic = this.requireTopic(topicId);
    this.assertTopicRevision(topic, expectedRevision);
    if (
      [...this.subscriptions.values()].some(
        (value) =>
          value.topicId === topicId &&
          value.state !== "closed" &&
          value.state !== "revoked",
      )
    )
      throw new E03RuntimeError(
        "mailbox_topic_live_subscription",
        `mailbox topic ${topicId} has live subscriptions`,
      );
    if (topic.state === "closed") return structuredClone(topic);
    return this.transitionTopic(topic, {
      state: "closed",
      closedAt: this.clock.now(),
    });
  }
  verifyEvents(topicId?: string): void {
    const groups = topicId
      ? [[topicId, this.events.get(topicId) ?? []] as const]
      : [...this.events.entries()];
    for (const [id, entries] of groups) {
      let prior: MailboxTopicEvent | null = null;
      for (const event of entries) {
        assertMailboxTopicEvent(event);
        if (
          event.topicId !== id ||
          (prior && event.sequence !== prior.sequence + 1) ||
          event.previousDigest !== (prior?.digest ?? "root")
        )
          throw new E03RuntimeError(
            "mailbox_topic_event_chain",
            `mailbox topic event ${event.eventId} breaks chain`,
          );
        prior = event;
      }
    }
  }
  snapshot(): {
    topics: MailboxTopic[];
    events: MailboxTopicEvent[];
    subscriptions: MailboxTopicSubscription[];
  } {
    this.verifyEvents();
    return {
      topics: [...this.topics.values()].map((value) => structuredClone(value)),
      events: [...this.events.values()]
        .flat()
        .map((value) => structuredClone(value)),
      subscriptions: [...this.subscriptions.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    topics: readonly MailboxTopic[];
    events: readonly MailboxTopicEvent[];
    subscriptions: readonly MailboxTopicSubscription[];
  }): void {
    const topics = new Map<string, MailboxTopic>();
    const events = new Map<string, MailboxTopicEvent[]>();
    const subscriptions = new Map<string, MailboxTopicSubscription>();
    const subscriptionIndex = new Map<string, string>();
    for (const value of snapshot.topics) {
      assertMailboxTopic(value);
      if (topics.has(value.topicId))
        throw new E03RuntimeError(
          "mailbox_topic_restore_duplicate",
          `duplicate mailbox topic ${value.topicId}`,
        );
      topics.set(value.topicId, structuredClone(value));
    }
    for (const value of snapshot.events) {
      assertMailboxTopicEvent(value);
      if (!topics.has(value.topicId))
        throw new E03RuntimeError(
          "mailbox_topic_event_restore",
          `mailbox topic event ${value.eventId} has no topic`,
        );
      const entries = events.get(value.topicId) ?? [];
      if (entries.some((entry) => entry.eventId === value.eventId))
        throw new E03RuntimeError(
          "mailbox_topic_event_restore_duplicate",
          `duplicate mailbox topic event ${value.eventId}`,
        );
      entries.push(structuredClone(value));
      events.set(value.topicId, entries);
    }
    for (const entries of events.values())
      entries.sort((left, right) => left.sequence - right.sequence);
    for (const value of snapshot.subscriptions) {
      assertMailboxSubscription(value);
      const key = `${value.topicId}:${value.taskId}`;
      if (
        !topics.has(value.topicId) ||
        subscriptions.has(value.subscriptionId) ||
        (value.state !== "closed" &&
          value.state !== "revoked" &&
          subscriptionIndex.has(key))
      )
        throw new E03RuntimeError(
          "mailbox_subscription_restore",
          `mailbox subscription ${value.subscriptionId} is invalid`,
        );
      subscriptions.set(value.subscriptionId, structuredClone(value));
      if (value.state !== "closed" && value.state !== "revoked")
        subscriptionIndex.set(key, value.subscriptionId);
    }
    this.topics = topics;
    this.events = events;
    this.subscriptions = subscriptions;
    this.subscriptionIndex = subscriptionIndex;
    this.verifyEvents();
  }
  private requireTopic(id: string): MailboxTopic {
    const value = this.topics.get(id);
    if (!value)
      throw new E03RuntimeError(
        "mailbox_topic_missing",
        `mailbox topic ${id} does not exist`,
      );
    assertMailboxTopic(value);
    return value;
  }
  private requireSubscription(id: string): MailboxTopicSubscription {
    const value = this.subscriptions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "mailbox_subscription_missing",
        `mailbox subscription ${id} does not exist`,
      );
    assertMailboxSubscription(value);
    return value;
  }
  private assertTopicRevision(value: MailboxTopic, expected: number): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_topic_stale_revision",
        `mailbox topic ${value.topicId} revision is stale`,
      );
  }
  private assertSubscriptionRevision(
    value: MailboxTopicSubscription,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_subscription_stale_revision",
        `mailbox subscription ${value.subscriptionId} revision is stale`,
      );
  }
  private transitionTopic(
    value: MailboxTopic,
    patch: Partial<Omit<MailboxTopic, "topicId" | "revision" | "digest">>,
  ): MailboxTopic {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      topicId: value.topicId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxTopic(next);
    this.topics.set(next.topicId, next);
    return structuredClone(next);
  }
  private transitionSubscription(
    value: MailboxTopicSubscription,
    patch: Partial<
      Omit<MailboxTopicSubscription, "subscriptionId" | "revision" | "digest">
    >,
  ): MailboxTopicSubscription {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      subscriptionId: value.subscriptionId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxSubscription(next);
    this.subscriptions.set(next.subscriptionId, next);
    return structuredClone(next);
  }
}

export type MailboxRetentionSegmentState =
  | "open"
  | "sealed"
  | "compacting"
  | "compacted"
  | "expired";

export interface MailboxRetentionPolicy {
  policyId: string;
  topicId: string;
  retentionMs: number;
  maxEventsPerSegment: number;
  minSegmentsToRetain: number;
  compactAcknowledgedOnly: boolean;
  legalHold: boolean;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface MailboxRetentionEvent {
  retentionEventId: string;
  topicId: string;
  messageId: string;
  senderTaskId: string;
  recipientTaskId: string;
  messageDigest: string;
  acknowledged: boolean;
  occurredAt: string;
  sequence: number;
  digest: string;
}

export interface MailboxRetentionSegment {
  segmentId: string;
  policyId: string;
  topicId: string;
  state: MailboxRetentionSegmentState;
  firstSequence: number;
  lastSequence: number;
  eventCount: number;
  acknowledgedCount: number;
  eventDigests: string[];
  openedAt: string;
  sealedAt: string;
  expiresAt: string;
  replacedBySegmentId: string;
  revision: number;
  digest: string;
}

export interface MailboxCompactionReceipt {
  receiptId: string;
  policyId: string;
  sourceSegmentIds: string[];
  targetSegmentId: string;
  retainedEventIds: string[];
  removedEventIds: string[];
  sourceDigest: string;
  targetDigest: string;
  completedAt: string;
  revision: number;
  digest: string;
}

export interface MailboxRetentionSnapshot {
  policies: MailboxRetentionPolicy[];
  events: MailboxRetentionEvent[];
  segments: MailboxRetentionSegment[];
  receipts: MailboxCompactionReceipt[];
  nextSequenceByTopic: Array<[string, number]>;
  digest: string;
}

function assertMailboxRetentionPolicy(value: MailboxRetentionPolicy): void {
  if (!value.policyId || !value.topicId)
    throw new E03RuntimeError(
      "mailbox_retention_policy_invalid",
      "mailbox retention policy requires identity and topic",
    );
  if (!Number.isInteger(value.retentionMs) || value.retentionMs < 1)
    throw new E03RuntimeError(
      "mailbox_retention_window_invalid",
      "mailbox retention window must be a positive integer",
    );
  if (
    !Number.isInteger(value.maxEventsPerSegment) ||
    value.maxEventsPerSegment < 1
  )
    throw new E03RuntimeError(
      "mailbox_retention_segment_limit_invalid",
      "mailbox retention segment limit must be positive",
    );
  if (!Number.isInteger(value.revision) || value.revision < 1)
    throw new E03RuntimeError(
      "mailbox_retention_policy_revision_invalid",
      "mailbox retention policy revision must be positive",
    );
}

function assertMailboxRetentionEvent(value: MailboxRetentionEvent): void {
  if (
    !value.retentionEventId ||
    !value.topicId ||
    !value.messageId ||
    !value.messageDigest
  )
    throw new E03RuntimeError(
      "mailbox_retention_event_invalid",
      "mailbox retention event requires message custody",
    );
  if (!Number.isInteger(value.sequence) || value.sequence < 1)
    throw new E03RuntimeError(
      "mailbox_retention_sequence_invalid",
      "mailbox retention sequence must be positive",
    );
}

function assertMailboxRetentionSegment(value: MailboxRetentionSegment): void {
  if (!value.segmentId || !value.policyId || !value.topicId)
    throw new E03RuntimeError(
      "mailbox_retention_segment_invalid",
      "mailbox retention segment requires policy custody",
    );
  if (
    !Number.isInteger(value.firstSequence) ||
    !Number.isInteger(value.lastSequence) ||
    value.firstSequence < 1 ||
    value.lastSequence < value.firstSequence - 1
  )
    throw new E03RuntimeError(
      "mailbox_retention_segment_range_invalid",
      "mailbox retention segment range is invalid",
    );
  if (value.eventCount !== value.eventDigests.length)
    throw new E03RuntimeError(
      "mailbox_retention_segment_count_mismatch",
      "mailbox retention event count does not match digests",
    );
}

function assertMailboxCompactionReceipt(value: MailboxCompactionReceipt): void {
  if (!value.receiptId || !value.policyId || !value.targetSegmentId)
    throw new E03RuntimeError(
      "mailbox_compaction_receipt_invalid",
      "mailbox compaction receipt requires target custody",
    );
  if (new Set(value.sourceSegmentIds).size !== value.sourceSegmentIds.length)
    throw new E03RuntimeError(
      "mailbox_compaction_source_duplicate",
      "mailbox compaction sources must be unique",
    );
}

export class MailboxRetentionRuntime {
  private policies = new Map<string, MailboxRetentionPolicy>();
  private events = new Map<string, MailboxRetentionEvent>();
  private segments = new Map<string, MailboxRetentionSegment>();
  private receipts = new Map<string, MailboxCompactionReceipt>();
  private nextSequenceByTopic = new Map<string, number>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registerPolicy(input: {
    policyId?: string;
    topicId: string;
    retentionMs: number;
    maxEventsPerSegment: number;
    minSegmentsToRetain?: number;
    compactAcknowledgedOnly?: boolean;
  }): MailboxRetentionPolicy {
    if (!input.topicId)
      throw new E03RuntimeError(
        "mailbox_retention_topic_required",
        "mailbox retention policy requires topic",
      );
    const policyId = input.policyId ?? createId("mailbox-retention-policy");
    if (this.policies.has(policyId))
      throw new E03RuntimeError(
        "mailbox_retention_policy_duplicate",
        `mailbox retention policy ${policyId} already exists`,
      );
    const now = this.clock.now();
    const payload = {
      policyId,
      topicId: input.topicId,
      retentionMs: input.retentionMs,
      maxEventsPerSegment: input.maxEventsPerSegment,
      minSegmentsToRetain: input.minSegmentsToRetain ?? 1,
      compactAcknowledgedOnly: input.compactAcknowledgedOnly ?? true,
      legalHold: false,
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertMailboxRetentionPolicy(policy);
    this.policies.set(policyId, policy);
    return structuredClone(policy);
  }

  setLegalHold(input: {
    policyId: string;
    expectedRevision: number;
    enabled: boolean;
  }): MailboxRetentionPolicy {
    const policy = this.requirePolicy(input.policyId);
    this.assertPolicyRevision(policy, input.expectedRevision);
    return this.transitionPolicy(policy, {
      legalHold: input.enabled,
      updatedAt: this.clock.now(),
    });
  }

  append(input: {
    policyId: string;
    message: E03Message;
    acknowledged?: boolean;
  }): { event: MailboxRetentionEvent; segment: MailboxRetentionSegment } {
    const policy = this.requirePolicy(input.policyId);
    const existing = [...this.events.values()].find(
      (event) =>
        event.topicId === policy.topicId &&
        event.messageId === input.message.messageId,
    );
    if (existing) {
      const segment = [...this.segments.values()].find(
        (candidate) =>
          candidate.topicId === policy.topicId &&
          candidate.eventDigests.includes(existing.digest),
      );
      if (!segment)
        throw new E03RuntimeError(
          "mailbox_retention_segment_missing",
          `retained message ${existing.messageId} has no segment`,
        );
      return {
        event: structuredClone(existing),
        segment: structuredClone(segment),
      };
    }
    let segment = this.openSegment(policy);
    if (segment.eventCount >= policy.maxEventsPerSegment) {
      segment = this.transitionSegment(segment, {
        state: "sealed",
        sealedAt: this.clock.now(),
      });
      segment = this.createSegment(policy, segment.lastSequence + 1);
    }
    const sequence = this.nextSequenceByTopic.get(policy.topicId) ?? 1;
    const eventPayload = {
      retentionEventId: createId("mailbox-retention-event"),
      topicId: policy.topicId,
      messageId: input.message.messageId,
      senderTaskId: input.message.senderTaskId,
      recipientTaskId: input.message.recipientTaskId,
      messageDigest: input.message.digest,
      acknowledged: input.acknowledged ?? false,
      occurredAt: this.clock.now(),
      sequence,
    };
    const event = { ...eventPayload, digest: digest(eventPayload) };
    assertMailboxRetentionEvent(event);
    this.events.set(event.retentionEventId, event);
    this.nextSequenceByTopic.set(policy.topicId, sequence + 1);
    segment = this.transitionSegment(segment, {
      lastSequence: sequence,
      eventCount: segment.eventCount + 1,
      acknowledgedCount:
        segment.acknowledgedCount + (event.acknowledged ? 1 : 0),
      eventDigests: [...segment.eventDigests, event.digest],
    });
    return { event: structuredClone(event), segment };
  }

  acknowledge(input: {
    retentionEventId: string;
    expectedMessageDigest: string;
  }): MailboxRetentionEvent {
    const event = this.requireEvent(input.retentionEventId);
    if (event.messageDigest !== input.expectedMessageDigest)
      throw new E03RuntimeError(
        "mailbox_retention_message_digest_mismatch",
        "mailbox retention acknowledgement does not match message",
      );
    if (event.acknowledged) return structuredClone(event);
    const nextPayload = { ...event, acknowledged: true };
    const { digest: _, ...unsigned } = nextPayload;
    const next = { ...unsigned, digest: digest(unsigned) };
    assertMailboxRetentionEvent(next);
    this.events.set(next.retentionEventId, next);
    const segment = [...this.segments.values()].find((candidate) =>
      candidate.eventDigests.includes(event.digest),
    );
    if (!segment)
      throw new E03RuntimeError(
        "mailbox_retention_segment_missing",
        `retained message ${event.messageId} has no segment`,
      );
    this.transitionSegment(segment, {
      acknowledgedCount: segment.acknowledgedCount + 1,
      eventDigests: segment.eventDigests.map((value) =>
        value === event.digest ? next.digest : value,
      ),
    });
    return structuredClone(next);
  }

  compact(input: {
    policyId: string;
    sourceSegmentIds: string[];
    expectedRevisions: Record<string, number>;
  }): MailboxCompactionReceipt {
    const policy = this.requirePolicy(input.policyId);
    if (policy.legalHold)
      throw new E03RuntimeError(
        "mailbox_retention_legal_hold",
        "mailbox retention policy is under legal hold",
      );
    if (input.sourceSegmentIds.length < 1)
      throw new E03RuntimeError(
        "mailbox_compaction_sources_required",
        "mailbox compaction requires source segments",
      );
    const sources = input.sourceSegmentIds.map((id) => {
      const segment = this.requireSegment(id);
      if (segment.policyId !== policy.policyId)
        throw new E03RuntimeError(
          "mailbox_compaction_policy_mismatch",
          "mailbox compaction source belongs to another policy",
        );
      if (segment.state !== "sealed")
        throw new E03RuntimeError(
          "mailbox_compaction_source_not_sealed",
          `mailbox retention segment ${id} is not sealed`,
        );
      this.assertSegmentRevision(segment, input.expectedRevisions[id] ?? -1);
      return segment;
    });
    const sourceEvents = [...this.events.values()]
      .filter((event) =>
        sources.some((segment) => segment.eventDigests.includes(event.digest)),
      )
      .sort((left, right) => left.sequence - right.sequence);
    const retained = policy.compactAcknowledgedOnly
      ? sourceEvents.filter((event) => !event.acknowledged)
      : sourceEvents;
    const removed = sourceEvents.filter(
      (event) =>
        !retained.some(
          (candidate) => candidate.retentionEventId === event.retentionEventId,
        ),
    );
    const target = this.createCompactedSegment(policy, retained);
    for (const source of sources)
      this.transitionSegment(source, {
        state: "compacted",
        replacedBySegmentId: target.segmentId,
      });
    for (const event of removed) this.events.delete(event.retentionEventId);
    const receiptPayload = {
      receiptId: createId("mailbox-compaction-receipt"),
      policyId: policy.policyId,
      sourceSegmentIds: sources.map((value) => value.segmentId).sort(),
      targetSegmentId: target.segmentId,
      retainedEventIds: retained.map((value) => value.retentionEventId),
      removedEventIds: removed.map((value) => value.retentionEventId),
      sourceDigest: digest(sources.map((value) => value.digest).sort()),
      targetDigest: target.digest,
      completedAt: this.clock.now(),
      revision: 1,
    };
    const receipt = { ...receiptPayload, digest: digest(receiptPayload) };
    assertMailboxCompactionReceipt(receipt);
    this.receipts.set(receipt.receiptId, receipt);
    return structuredClone(receipt);
  }

  expire(now = this.clock.now()): MailboxRetentionSegment[] {
    const nowMs = Date.parse(now);
    const expired: MailboxRetentionSegment[] = [];
    for (const policy of this.policies.values()) {
      if (policy.legalHold) continue;
      const candidates = [...this.segments.values()]
        .filter(
          (segment) =>
            segment.policyId === policy.policyId &&
            (segment.state === "sealed" || segment.state === "compacted"),
        )
        .sort((left, right) => right.lastSequence - left.lastSequence);
      for (const segment of candidates.slice(policy.minSegmentsToRetain)) {
        if (Date.parse(segment.expiresAt) > nowMs) continue;
        const next = this.transitionSegment(segment, { state: "expired" });
        for (const event of [...this.events.values()])
          if (segment.eventDigests.includes(event.digest))
            this.events.delete(event.retentionEventId);
        expired.push(next);
      }
    }
    return expired;
  }

  snapshot(): MailboxRetentionSnapshot {
    const payload = {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      events: [...this.events.values()].map((value) => structuredClone(value)),
      segments: [...this.segments.values()].map((value) =>
        structuredClone(value),
      ),
      receipts: [...this.receipts.values()].map((value) =>
        structuredClone(value),
      ),
      nextSequenceByTopic: [...this.nextSequenceByTopic.entries()],
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(snapshot: MailboxRetentionSnapshot): void {
    const { digest: expected, ...payload } = snapshot;
    if (digest(payload) !== expected)
      throw new E03RuntimeError(
        "mailbox_retention_snapshot_corrupt",
        "mailbox retention snapshot digest mismatch",
      );
    const policies = new Map<string, MailboxRetentionPolicy>();
    const events = new Map<string, MailboxRetentionEvent>();
    const segments = new Map<string, MailboxRetentionSegment>();
    const receipts = new Map<string, MailboxCompactionReceipt>();
    for (const value of payload.policies) {
      assertMailboxRetentionPolicy(value);
      if (policies.has(value.policyId))
        throw new E03RuntimeError(
          "mailbox_retention_snapshot_policy_duplicate",
          `duplicate mailbox retention policy ${value.policyId}`,
        );
      policies.set(value.policyId, structuredClone(value));
    }
    for (const value of payload.events) {
      assertMailboxRetentionEvent(value);
      if (events.has(value.retentionEventId))
        throw new E03RuntimeError(
          "mailbox_retention_snapshot_event_duplicate",
          `duplicate mailbox retention event ${value.retentionEventId}`,
        );
      events.set(value.retentionEventId, structuredClone(value));
    }
    for (const value of payload.segments) {
      assertMailboxRetentionSegment(value);
      if (!policies.has(value.policyId))
        throw new E03RuntimeError(
          "mailbox_retention_snapshot_policy_missing",
          `segment ${value.segmentId} references missing policy`,
        );
      segments.set(value.segmentId, structuredClone(value));
    }
    for (const value of payload.receipts) {
      assertMailboxCompactionReceipt(value);
      receipts.set(value.receiptId, structuredClone(value));
    }
    this.policies = policies;
    this.events = events;
    this.segments = segments;
    this.receipts = receipts;
    this.nextSequenceByTopic = new Map(payload.nextSequenceByTopic);
  }

  private openSegment(policy: MailboxRetentionPolicy): MailboxRetentionSegment {
    const existing = [...this.segments.values()].find(
      (segment) =>
        segment.policyId === policy.policyId && segment.state === "open",
    );
    if (existing) return existing;
    return this.createSegment(
      policy,
      this.nextSequenceByTopic.get(policy.topicId) ?? 1,
    );
  }

  private createSegment(
    policy: MailboxRetentionPolicy,
    firstSequence: number,
  ): MailboxRetentionSegment {
    const now = this.clock.now();
    const payload = {
      segmentId: createId("mailbox-retention-segment"),
      policyId: policy.policyId,
      topicId: policy.topicId,
      state: "open" as const,
      firstSequence,
      lastSequence: firstSequence - 1,
      eventCount: 0,
      acknowledgedCount: 0,
      eventDigests: [] as string[],
      openedAt: now,
      sealedAt: "",
      expiresAt: new Date(Date.parse(now) + policy.retentionMs).toISOString(),
      replacedBySegmentId: "",
      revision: 1,
    };
    const segment = { ...payload, digest: digest(payload) };
    assertMailboxRetentionSegment(segment);
    this.segments.set(segment.segmentId, segment);
    return segment;
  }

  private createCompactedSegment(
    policy: MailboxRetentionPolicy,
    events: MailboxRetentionEvent[],
  ): MailboxRetentionSegment {
    const now = this.clock.now();
    const firstSequence = events.at(0)?.sequence ?? 1;
    const lastSequence = events.at(-1)?.sequence ?? firstSequence - 1;
    const payload = {
      segmentId: createId("mailbox-retention-compacted"),
      policyId: policy.policyId,
      topicId: policy.topicId,
      state: "sealed" as const,
      firstSequence,
      lastSequence,
      eventCount: events.length,
      acknowledgedCount: events.filter((event) => event.acknowledged).length,
      eventDigests: events.map((event) => event.digest),
      openedAt: now,
      sealedAt: now,
      expiresAt: new Date(Date.parse(now) + policy.retentionMs).toISOString(),
      replacedBySegmentId: "",
      revision: 1,
    };
    const segment = { ...payload, digest: digest(payload) };
    assertMailboxRetentionSegment(segment);
    this.segments.set(segment.segmentId, segment);
    return segment;
  }

  private requirePolicy(id: string): MailboxRetentionPolicy {
    const value = this.policies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "mailbox_retention_policy_missing",
        `mailbox retention policy ${id} does not exist`,
      );
    assertMailboxRetentionPolicy(value);
    return value;
  }

  private requireEvent(id: string): MailboxRetentionEvent {
    const value = this.events.get(id);
    if (!value)
      throw new E03RuntimeError(
        "mailbox_retention_event_missing",
        `mailbox retention event ${id} does not exist`,
      );
    assertMailboxRetentionEvent(value);
    return value;
  }

  private requireSegment(id: string): MailboxRetentionSegment {
    const value = this.segments.get(id);
    if (!value)
      throw new E03RuntimeError(
        "mailbox_retention_segment_missing",
        `mailbox retention segment ${id} does not exist`,
      );
    assertMailboxRetentionSegment(value);
    return value;
  }

  private assertPolicyRevision(
    value: MailboxRetentionPolicy,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_retention_policy_stale_revision",
        `mailbox retention policy ${value.policyId} revision is stale`,
      );
  }

  private assertSegmentRevision(
    value: MailboxRetentionSegment,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "mailbox_retention_segment_stale_revision",
        `mailbox retention segment ${value.segmentId} revision is stale`,
      );
  }

  private transitionPolicy(
    value: MailboxRetentionPolicy,
    patch: Partial<
      Omit<MailboxRetentionPolicy, "policyId" | "revision" | "digest">
    >,
  ): MailboxRetentionPolicy {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      policyId: value.policyId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxRetentionPolicy(next);
    this.policies.set(next.policyId, next);
    return structuredClone(next);
  }

  private transitionSegment(
    value: MailboxRetentionSegment,
    patch: Partial<
      Omit<MailboxRetentionSegment, "segmentId" | "revision" | "digest">
    >,
  ): MailboxRetentionSegment {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      segmentId: value.segmentId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMailboxRetentionSegment(next);
    this.segments.set(next.segmentId, next);
    return structuredClone(next);
  }
}
