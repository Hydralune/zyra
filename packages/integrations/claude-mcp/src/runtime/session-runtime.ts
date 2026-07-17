import type { JsonObject } from "../contracts.ts";
import type { McpCapabilityCatalogSnapshot } from "../catalog/capability-catalog.ts";
import type {
  McpConnectionRecord,
  McpConnectionSnapshot,
} from "../connection/contracts.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  monotonicNow,
  sha256,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type {
  McpPreparedRequest,
  McpRequestJournalSnapshot,
  McpRestoreClassification,
} from "./request-journal.ts";

export type McpSessionServerPhase =
  | "configured"
  | "connecting"
  | "ready"
  | "degraded"
  | "reconciling"
  | "disconnected"
  | "disabled"
  | "failed";

export interface McpSessionIdentity {
  sessionId: string;
  workspaceRoot: string;
  epoch: number;
}

export interface McpSessionServerState {
  serverId: string;
  phase: McpSessionServerPhase;
  connectionId: string | null;
  connectionEpoch: number;
  configDigest: string;
  policyDigest: string;
  protocolVersion: string | null;
  catalogRevision: number;
  catalogDigest: string;
  authRevision: number;
  reconnectCount: number;
  lastReadyAt: string | null;
  lastDisconnectedAt: string | null;
  lastFailureCode: string | null;
  lastFailureDigest: string | null;
  pendingRequestCount: number;
  indeterminateRequestCount: number;
  reconciliationRequired: boolean;
  generation: number;
  updatedAt: string;
  metadata: JsonObject;
}

export interface McpSessionInvocationLease {
  leaseId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  sessionId: string;
  taskId: string;
  workerRequestId: string;
  toolCallId: string;
  operation: string;
  argumentsDigest: string;
  idempotent: boolean;
  status: "acquired" | "completed" | "failed" | "cancelled" | "indeterminate";
  acquiredAt: string;
  completedAt: string | null;
  resultDigest: string | null;
  failureCode: string | null;
  metadata: JsonObject;
}

export interface McpRecoveryAction {
  actionId: string;
  serverId: string;
  journalId: string;
  transitionId: string;
  action:
    | "replay_idempotent"
    | "reconcile_effect"
    | "await_remote_task"
    | "cancel_stale"
    | "operator_attention";
  replayAllowed: boolean;
  effectKnown: boolean;
  requiresReconciliation: boolean;
  reason: string;
  connectionEpoch: number;
  requestId: string;
  method: string;
  createdAt: string;
  metadata: JsonObject;
}

export interface McpSessionTransition {
  transitionId: string;
  sequence: number;
  serverId: string;
  fromPhase: McpSessionServerPhase | null;
  toPhase: McpSessionServerPhase;
  reason: string;
  connectionId: string | null;
  connectionEpoch: number;
  generation: number;
  occurredAt: string;
  previousHash: string;
  transitionHash: string;
  metadata: JsonObject;
}

export interface McpSessionSnapshot {
  version: "zyra.mcp-session-runtime/v1";
  identity: McpSessionIdentity;
  revision: number;
  sequence: number;
  headHash: string;
  servers: McpSessionServerState[];
  leases: McpSessionInvocationLease[];
  recoveryActions: McpRecoveryAction[];
  transitions: McpSessionTransition[];
  restoredBeforeBootstrap: boolean;
  digest: string;
  capturedAt: string;
}

const transitionGenesis = "sha256:zyra-mcp-session-genesis";

const connectionPhaseMap: Record<McpConnectionRecord["phase"], McpSessionServerPhase> = {
  idle: "configured",
  connecting: "connecting",
  authenticating: "connecting",
  initializing: "connecting",
  ready: "ready",
  degraded: "degraded",
  reconnecting: "degraded",
  closing: "disconnected",
  closed: "disconnected",
  failed: "failed",
};

export class McpSessionRuntime {
  private readonly identity: McpSessionIdentity;
  private readonly servers = new Map<string, McpSessionServerState>();
  private readonly leases = new Map<string, McpSessionInvocationLease>();
  private readonly recoveryActions = new Map<string, McpRecoveryAction>();
  private readonly transitions: McpSessionTransition[] = [];
  private readonly now: () => Date;
  private readonly maximumLeases: number;
  private readonly maximumTransitions: number;
  private revision = 0;
  private sequence = 0;
  private headHash = transitionGenesis;
  private restoredBeforeBootstrap = false;
  private lastTimestamp: string | null = null;

  constructor(options: {
    identity: McpSessionIdentity;
    now?: () => Date;
    maximumLeases?: number;
    maximumTransitions?: number;
    snapshot?: McpSessionSnapshot | null;
  }) {
    validateIdentity(options.identity);
    this.identity = cloneJson(options.identity);
    this.now = options.now ?? (() => new Date());
    this.maximumLeases = options.maximumLeases ?? 100_000;
    this.maximumTransitions = options.maximumTransitions ?? 100_000;
    if (options.snapshot) {
      this.restore(options.snapshot);
      this.restoredBeforeBootstrap = true;
    }
  }

  synchronize(
    connections: McpConnectionSnapshot,
    catalog: McpCapabilityCatalogSnapshot,
    journal: McpRequestJournalSnapshot,
    metadata: JsonObject = {},
  ): McpSessionServerState[] {
    const catalogByServer = new Map(
      catalog.servers.map((server) => [server.serverId, server]),
    );
    const journalByServer = groupRequests(journal.records);
    const seen = new Set<string>();
    for (const connection of connections.connections) {
      seen.add(connection.serverId);
      const existing = this.servers.get(connection.serverId);
      const catalogState = catalogByServer.get(connection.serverId);
      const requestState = journalByServer.get(connection.serverId) ?? [];
      const phase = connectionPhaseMap[connection.phase];
      const connectionChanged = existing?.connectionId
        && existing.connectionId !== connection.connectionId;
      const epochAdvanced = existing
        && connection.epoch > existing.connectionEpoch;
      const generation = existing
        ? existing.generation + (connectionChanged || epochAdvanced ? 1 : 0)
        : 1;
      const pendingRequestCount = requestState.filter(isPendingRequest).length;
      const indeterminateRequestCount = requestState.filter((record) => record.status === "indeterminate").length;
      const state: McpSessionServerState = {
        serverId: connection.serverId,
        phase: indeterminateRequestCount && phase === "ready"
          ? "reconciling"
          : phase,
        connectionId: connection.connectionId,
        connectionEpoch: connection.epoch,
        configDigest: connection.configDigest,
        policyDigest: connection.policyDigest,
        protocolVersion: connection.protocolVersion,
        catalogRevision: catalogState?.revision ?? connection.catalogRevision,
        catalogDigest: catalogState?.digest ?? "",
        authRevision: connection.authRevision,
        reconnectCount: Math.max(
          existing?.reconnectCount ?? 0,
          connection.reconnectAttempt,
        ) + (connectionChanged || epochAdvanced ? 1 : 0),
        lastReadyAt: connection.readyAt ?? existing?.lastReadyAt ?? null,
        lastDisconnectedAt: phase === "disconnected"
          ? this.timestamp()
          : existing?.lastDisconnectedAt ?? null,
        lastFailureCode: connection.lastFailure?.code ?? null,
        lastFailureDigest: connection.lastFailure
          ? sha256(connection.lastFailure)
          : null,
        pendingRequestCount,
        indeterminateRequestCount,
        reconciliationRequired: indeterminateRequestCount > 0,
        generation,
        updatedAt: this.timestamp(),
        metadata: canonicalJson({
          ...metadata,
          connection_phase: connection.phase,
          connection_revision: connections.revision,
          catalog_global_revision: catalog.revision,
          journal_revision: journal.revision,
        }) as JsonObject,
      };
      this.putServer(state, existing ? "connection_synchronized" : "server_discovered");
    }
    for (const [serverId, existing] of this.servers) {
      if (seen.has(serverId)) {
        continue;
      }
      const requestState = journalByServer.get(serverId) ?? [];
      const next: McpSessionServerState = {
        ...existing,
        phase: existing.phase === "disabled" ? "disabled" : "disconnected",
        connectionId: null,
        pendingRequestCount: requestState.filter(isPendingRequest).length,
        indeterminateRequestCount: requestState.filter((record) => record.status === "indeterminate").length,
        reconciliationRequired: requestState.some((record) => record.status === "indeterminate"),
        lastDisconnectedAt: this.timestamp(),
        updatedAt: this.timestamp(),
        metadata: canonicalJson({
          ...existing.metadata,
          ...metadata,
          missing_from_connection_snapshot: true,
        }) as JsonObject,
      };
      this.putServer(next, "connection_missing");
    }
    this.revision += 1;
    return this.listServers();
  }

  planRecovery(
    classifications: McpRestoreClassification[],
    journal: McpRequestJournalSnapshot,
    metadata: JsonObject = {},
  ): McpRecoveryAction[] {
    const byJournal = new Map(
      journal.records.map((record) => [record.journalId, record]),
    );
    const output: McpRecoveryAction[] = [];
    for (const classification of classifications) {
      const record = byJournal.get(classification.journalId);
      if (!record) {
        throw sessionError(
          "",
          "recovery_journal_record_missing",
          `recovery classification ${classification.journalId} has no journal record`,
        );
      }
      const action = recoveryAction(classification, record);
      const actionId = deterministicMcpId("mcp-recovery-action", {
        journal_id: classification.journalId,
        transition_id: classification.transitionId,
        restored_status: classification.restoredStatus,
        action,
      }, 40);
      const existing = this.recoveryActions.get(actionId);
      if (existing) {
        output.push(cloneJson(existing));
        continue;
      }
      const value: McpRecoveryAction = {
        actionId,
        serverId: record.identity.serverId,
        journalId: record.journalId,
        transitionId: record.transitionId,
        action,
        replayAllowed: classification.replayAllowed,
        effectKnown: classification.effectKnown,
        requiresReconciliation: classification.requiresReconciliation,
        reason: classification.reason,
        connectionEpoch: record.identity.connectionEpoch,
        requestId: record.identity.requestId,
        method: record.identity.method,
        createdAt: this.timestamp(),
        metadata: canonicalJson({
          ...metadata,
          prior_status: classification.priorStatus,
          restored_status: classification.restoredStatus,
          idempotent: record.idempotent,
          effect_receipt_ids: record.effectReceiptIds,
        }) as JsonObject,
      };
      this.recoveryActions.set(actionId, value);
      output.push(cloneJson(value));
      const server = this.servers.get(value.serverId);
      if (server && value.requiresReconciliation) {
        this.putServer({
          ...server,
          phase: "reconciling",
          reconciliationRequired: true,
          indeterminateRequestCount: Math.max(1, server.indeterminateRequestCount),
          updatedAt: this.timestamp(),
        }, "request_reconciliation_required");
      }
    }
    this.revision += output.length ? 1 : 0;
    return output.sort((left, right) => left.actionId.localeCompare(right.actionId));
  }

  acquire(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    taskId: string;
    workerRequestId: string;
    toolCallId: string;
    operation: string;
    arguments: JsonObject;
    idempotent: boolean;
    metadata?: JsonObject;
  }): McpSessionInvocationLease {
    const server = this.servers.get(input.serverId);
    if (!server) {
      throw sessionError(
        input.serverId,
        "session_server_not_registered",
        `MCP server ${input.serverId} is not registered in the session`,
      );
    }
    if (server.phase !== "ready") {
      throw sessionError(
        input.serverId,
        "session_server_not_ready",
        `MCP server ${input.serverId} is ${server.phase}`,
      );
    }
    if (
      server.connectionId !== input.connectionId
      || server.connectionEpoch !== input.connectionEpoch
    ) {
      throw sessionError(
        input.serverId,
        "session_connection_binding_mismatch",
        `MCP server ${input.serverId} invocation targets a stale connection binding`,
      );
    }
    const leaseId = deterministicMcpId("mcp-session-lease", {
      session_id: this.identity.sessionId,
      epoch: this.identity.epoch,
      server_id: input.serverId,
      connection_id: input.connectionId,
      connection_epoch: input.connectionEpoch,
      task_id: input.taskId,
      worker_request_id: input.workerRequestId,
      tool_call_id: input.toolCallId,
      operation: input.operation,
      arguments_digest: sha256(input.arguments),
    }, 40);
    const existing = this.leases.get(leaseId);
    if (existing) {
      if (existing.status === "acquired") {
        return cloneJson(existing);
      }
      throw sessionError(
        input.serverId,
        "session_lease_terminal",
        `MCP invocation lease ${leaseId} is already ${existing.status}`,
      );
    }
    const lease: McpSessionInvocationLease = {
      leaseId,
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      sessionId: this.identity.sessionId,
      taskId: input.taskId,
      workerRequestId: input.workerRequestId,
      toolCallId: input.toolCallId,
      operation: input.operation,
      argumentsDigest: sha256(input.arguments),
      idempotent: input.idempotent,
      status: "acquired",
      acquiredAt: this.timestamp(),
      completedAt: null,
      resultDigest: null,
      failureCode: null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.leases.set(leaseId, lease);
    this.revision += 1;
    this.trimLeases();
    return cloneJson(lease);
  }

  settle(
    leaseId: string,
    input: {
      status: Exclude<McpSessionInvocationLease["status"], "acquired">;
      result?: JsonObject | null;
      failureCode?: string | null;
      metadata?: JsonObject;
    },
  ): McpSessionInvocationLease {
    const lease = this.leases.get(leaseId);
    if (!lease) {
      throw sessionError(
        "",
        "session_lease_not_found",
        `MCP invocation lease ${leaseId} was not found`,
      );
    }
    if (lease.status !== "acquired") {
      const resultDigest = input.result ? sha256(input.result) : null;
      if (
        lease.status === input.status
        && lease.resultDigest === resultDigest
        && lease.failureCode === (input.failureCode ?? null)
      ) {
        return cloneJson(lease);
      }
      throw sessionError(
        lease.serverId,
        "session_lease_settlement_conflict",
        `MCP invocation lease ${leaseId} is already ${lease.status}`,
      );
    }
    lease.status = input.status;
    lease.completedAt = this.timestamp();
    lease.resultDigest = input.result ? sha256(input.result) : null;
    lease.failureCode = input.failureCode ?? null;
    lease.metadata = canonicalJson({
      ...lease.metadata,
      ...(input.metadata ?? {}),
    }) as JsonObject;
    this.revision += 1;
    return cloneJson(lease);
  }

  requireReplayAllowed(journalId: string): McpRecoveryAction {
    const action = [...this.recoveryActions.values()].find((item) => item.journalId === journalId);
    if (!action) {
      throw sessionError(
        "",
        "recovery_action_not_found",
        `MCP recovery action for ${journalId} was not found`,
      );
    }
    if (!action.replayAllowed || action.action !== "replay_idempotent") {
      throw sessionError(
        action.serverId,
        "non_idempotent_replay_blocked",
        `MCP request ${journalId} cannot be replayed without reconciliation`,
      );
    }
    return cloneJson(action);
  }

  resolveRecovery(
    actionId: string,
    resolution: {
      outcome: "committed" | "failed" | "cancelled";
      receiptDigest: string;
      metadata?: JsonObject;
    },
  ): McpRecoveryAction {
    const action = this.recoveryActions.get(actionId);
    if (!action) {
      throw sessionError(
        "",
        "recovery_action_not_found",
        `MCP recovery action ${actionId} was not found`,
      );
    }
    action.metadata = canonicalJson({
      ...action.metadata,
      resolved: true,
      resolution_outcome: resolution.outcome,
      resolution_receipt_digest: resolution.receiptDigest,
      resolution_metadata: resolution.metadata ?? {},
      resolved_at: this.timestamp(),
    }) as JsonObject;
    const unresolved = [...this.recoveryActions.values()].filter((item) => {
      return item.serverId === action.serverId
        && item.requiresReconciliation
        && item.metadata.resolved !== true;
    });
    const server = this.servers.get(action.serverId);
    if (server && !unresolved.length) {
      this.putServer({
        ...server,
        phase: server.connectionId ? "ready" : "disconnected",
        reconciliationRequired: false,
        indeterminateRequestCount: 0,
        updatedAt: this.timestamp(),
      }, "request_reconciliation_resolved");
    }
    this.revision += 1;
    return cloneJson(action);
  }

  disableServer(serverId: string, reason: string, metadata: JsonObject = {}): McpSessionServerState {
    const server = this.servers.get(serverId);
    if (!server) {
      throw sessionError(
        serverId,
        "session_server_not_registered",
        `MCP server ${serverId} is not registered in the session`,
      );
    }
    const active = [...this.leases.values()].filter((lease) => {
      return lease.serverId === serverId && lease.status === "acquired";
    });
    for (const lease of active) {
      lease.status = lease.idempotent ? "cancelled" : "indeterminate";
      lease.completedAt = this.timestamp();
      lease.failureCode = "server_disabled_during_invocation";
      lease.metadata = canonicalJson({
        ...lease.metadata,
        disable_reason: reason,
      }) as JsonObject;
    }
    const next: McpSessionServerState = {
      ...server,
      phase: "disabled",
      lastDisconnectedAt: this.timestamp(),
      reconciliationRequired: active.some((lease) => !lease.idempotent),
      indeterminateRequestCount: server.indeterminateRequestCount
        + active.filter((lease) => !lease.idempotent).length,
      updatedAt: this.timestamp(),
      metadata: canonicalJson({
        ...server.metadata,
        ...metadata,
        disable_reason: reason,
      }) as JsonObject,
    };
    this.putServer(next, reason || "server_disabled");
    this.revision += 1;
    return cloneJson(next);
  }

  server(serverId: string): McpSessionServerState | null {
    const value = this.servers.get(serverId);
    return value ? cloneJson(value) : null;
  }

  listServers(): McpSessionServerState[] {
    return [...this.servers.values()]
      .sort((left, right) => left.serverId.localeCompare(right.serverId))
      .map(cloneJson);
  }

  listLeases(options: {
    serverId?: string;
    status?: McpSessionInvocationLease["status"];
    taskId?: string;
  } = {}): McpSessionInvocationLease[] {
    return [...this.leases.values()]
      .filter((lease) => !options.serverId || lease.serverId === options.serverId)
      .filter((lease) => !options.status || lease.status === options.status)
      .filter((lease) => !options.taskId || lease.taskId === options.taskId)
      .sort((left, right) => left.acquiredAt.localeCompare(right.acquiredAt))
      .map(cloneJson);
  }

  listRecovery(options: {
    serverId?: string;
    unresolvedOnly?: boolean;
  } = {}): McpRecoveryAction[] {
    return [...this.recoveryActions.values()]
      .filter((action) => !options.serverId || action.serverId === options.serverId)
      .filter((action) => !options.unresolvedOnly || action.metadata.resolved !== true)
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
      .map(cloneJson);
  }

  snapshot(): McpSessionSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-session-runtime/v1" as const,
      identity: cloneJson(this.identity),
      revision: this.revision,
      sequence: this.sequence,
      headHash: this.headHash,
      servers: this.listServers(),
      leases: this.listLeases(),
      recoveryActions: this.listRecovery(),
      transitions: this.transitions.map(cloneJson),
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      capturedAt: this.timestamp(),
    };
    return {
      ...withoutDigest,
      digest: sha256(withoutDigest),
    };
  }

  restore(snapshot: McpSessionSnapshot): void {
    if (snapshot.version !== "zyra.mcp-session-runtime/v1") {
      throw sessionError(
        "",
        "unsupported_session_snapshot",
        `unsupported MCP session snapshot ${snapshot.version}`,
      );
    }
    const { digest: expectedDigest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== expectedDigest) {
      throw sessionError(
        "",
        "session_snapshot_digest_mismatch",
        "MCP session snapshot digest does not match its payload",
      );
    }
    if (
      snapshot.identity.sessionId !== this.identity.sessionId
      || snapshot.identity.workspaceRoot !== this.identity.workspaceRoot
    ) {
      throw sessionError(
        "",
        "session_snapshot_binding_mismatch",
        "MCP session snapshot belongs to another session or workspace",
      );
    }
    if (snapshot.identity.epoch > this.identity.epoch) {
      throw sessionError(
        "",
        "session_snapshot_epoch_regression",
        "MCP session cannot restore a future epoch",
      );
    }
    verifyTransitions(snapshot.transitions, snapshot.headHash);
    this.servers.clear();
    this.leases.clear();
    this.recoveryActions.clear();
    this.transitions.length = 0;
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    this.headHash = snapshot.headHash;
    for (const server of snapshot.servers) {
      this.servers.set(server.serverId, cloneJson(server));
    }
    for (const lease of snapshot.leases) {
      const restored = cloneJson(lease);
      if (restored.status === "acquired") {
        restored.status = restored.idempotent ? "cancelled" : "indeterminate";
        restored.completedAt = this.timestamp();
        restored.failureCode = "runtime_restarted_during_invocation";
        restored.metadata = canonicalJson({
          ...restored.metadata,
          restored_from_active_lease: true,
          replay_allowed: restored.idempotent,
        }) as JsonObject;
      }
      this.leases.set(restored.leaseId, restored);
    }
    for (const action of snapshot.recoveryActions) {
      this.recoveryActions.set(action.actionId, cloneJson(action));
    }
    this.transitions.push(...snapshot.transitions.map(cloneJson));
  }

  private putServer(nextValue: McpSessionServerState, reason: string): void {
    const next = cloneJson(nextValue);
    const current = this.servers.get(next.serverId);
    const changed = !current
      || current.phase !== next.phase
      || current.connectionId !== next.connectionId
      || current.connectionEpoch !== next.connectionEpoch
      || current.catalogRevision !== next.catalogRevision
      || current.authRevision !== next.authRevision
      || current.reconciliationRequired !== next.reconciliationRequired;
    this.servers.set(next.serverId, next);
    if (!changed) {
      return;
    }
    this.sequence += 1;
    const base = {
      transitionId: deterministicMcpId("mcp-session-transition", {
        session_id: this.identity.sessionId,
        epoch: this.identity.epoch,
        sequence: this.sequence,
        server_id: next.serverId,
        from_phase: current?.phase ?? null,
        to_phase: next.phase,
        connection_id: next.connectionId,
        connection_epoch: next.connectionEpoch,
        generation: next.generation,
        reason,
      }, 40),
      sequence: this.sequence,
      serverId: next.serverId,
      fromPhase: current?.phase ?? null,
      toPhase: next.phase,
      reason,
      connectionId: next.connectionId,
      connectionEpoch: next.connectionEpoch,
      generation: next.generation,
      occurredAt: this.timestamp(),
      previousHash: this.headHash,
      metadata: cloneJson(next.metadata),
    };
    const transition: McpSessionTransition = {
      ...base,
      transitionHash: sha256({
        previous_hash: this.headHash,
        transition: base,
      }),
    };
    this.headHash = transition.transitionHash;
    this.transitions.push(transition);
    while (this.transitions.length > this.maximumTransitions) {
      this.transitions.shift();
    }
  }

  private trimLeases(): void {
    if (this.leases.size <= this.maximumLeases) {
      return;
    }
    const candidates = [...this.leases.values()]
      .filter((lease) => lease.status !== "acquired" && lease.status !== "indeterminate")
      .sort((left, right) => left.acquiredAt.localeCompare(right.acquiredAt));
    for (const candidate of candidates) {
      if (this.leases.size <= this.maximumLeases) {
        break;
      }
      this.leases.delete(candidate.leaseId);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function validateIdentity(identity: McpSessionIdentity): void {
  if (!identity.sessionId || !identity.workspaceRoot) {
    throw sessionError(
      "",
      "session_identity_incomplete",
      "MCP session identity requires session and workspace",
    );
  }
  if (!Number.isSafeInteger(identity.epoch) || identity.epoch < 0) {
    throw sessionError(
      "",
      "session_epoch_invalid",
      "MCP session epoch must be a non-negative safe integer",
    );
  }
}

function groupRequests(records: McpPreparedRequest[]): Map<string, McpPreparedRequest[]> {
  const output = new Map<string, McpPreparedRequest[]>();
  for (const record of records) {
    const group = output.get(record.identity.serverId) ?? [];
    group.push(record);
    output.set(record.identity.serverId, group);
  }
  return output;
}

function isPendingRequest(record: McpPreparedRequest): boolean {
  return record.status === "prepared"
    || record.status === "sent"
    || record.status === "effect_observed"
    || record.status === "indeterminate";
}

function recoveryAction(
  classification: McpRestoreClassification,
  record: McpPreparedRequest,
): McpRecoveryAction["action"] {
  if (classification.replayAllowed) {
    return "replay_idempotent";
  }
  if (record.identity.method.startsWith("tasks/") || record.metadata.remote_task_id) {
    return "await_remote_task";
  }
  if (classification.effectKnown) {
    return "reconcile_effect";
  }
  if (record.status === "prepared") {
    return "cancel_stale";
  }
  return "operator_attention";
}

function verifyTransitions(
  transitions: McpSessionTransition[],
  expectedHead: string,
): void {
  let head = transitionGenesis;
  for (const [index, transition] of transitions.entries()) {
    if (transition.previousHash !== head) {
      throw sessionError(
        transition.serverId,
        "session_transition_previous_hash_mismatch",
        `MCP session transition ${index} has an invalid previous hash`,
      );
    }
    const {
      transitionHash: _ignored,
      ...base
    } = transition;
    const expected = sha256({
      previous_hash: head,
      transition: base,
    });
    if (transition.transitionHash !== expected) {
      throw sessionError(
        transition.serverId,
        "session_transition_hash_mismatch",
        `MCP session transition ${index} has an invalid hash`,
      );
    }
    head = transition.transitionHash;
  }
  if (head !== expectedHead) {
    throw sessionError(
      "",
      "session_transition_head_mismatch",
      "MCP session transition head does not match the snapshot",
    );
  }
}

function sessionError(
  serverId: string,
  code: string,
  message: string,
): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-session-runtime", {
      server_id: serverId,
      code,
      message,
    }),
    category: code.includes("binding") || code.includes("conflict")
      ? "conflict"
      : code.includes("replay") || code.includes("restore")
        ? "restore"
        : "protocol",
    code,
    message,
    serverId,
    retryable: code.includes("not_ready"),
    disposition: code.includes("not_ready")
      ? "reconnect_then_retry"
      : "terminal",
  });
}
