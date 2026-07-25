import type { JsonObject, JsonRpcMessage, JsonRpcResponse } from "../contracts.ts";
import type {
  McpHttpTransportConfig,
  McpServerConfigRecord,
  McpStdioTransportConfig,
} from "../config/config-store.ts";
import type { McpPolicyDecision, McpServerPolicy } from "../config/policy.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  hashChain,
  monotonicNow,
  sha256,
  verifyHashChain,
} from "../core/canonical.ts";
import {
  McpRuntimeError,
  normalizeFailure,
  type McpFailureRecord,
} from "../core/failure.ts";
import {
  MCP_PROTOCOL_VERSION,
  McpProtocolCodec,
  parseImplementation,
  type McpInitializeResult,
} from "../core/protocol.ts";
import type {
  McpConnectionPhase,
  McpConnectionRecord,
  McpConnectionResult,
  McpConnectionSnapshot,
  McpConnectionTransition,
  McpFetch,
  McpProcessFactory,
  McpTransportAdapter,
  McpTransportRequest,
  McpTransportSnapshot,
} from "./contracts.ts";
import { HttpMcpTransport } from "./http-transport.ts";
import { StdioMcpTransport } from "./stdio-transport.ts";

export interface McpConnectionRuntimeOptions {
  policy: McpServerPolicy;
  sessionId: string;
  workspaceRoot: string;
  interactive: boolean;
  sealedAutonomous: boolean;
  clientName?: string;
  clientVersion?: string;
  processFactory?: McpProcessFactory;
  fetch?: McpFetch;
  credentialResolver?: (serverId: string, handle: string) => Promise<string | null>;
  authorizationProvider?: (serverId: string) => Promise<string | null>;
  authenticationChallenge?: (serverId: string, challenge: string, response: Response) => Promise<string | null>;
  inProcessFactory?: (config: McpServerConfigRecord) => McpTransportAdapter;
  networkEvaluator?: (config: McpServerConfigRecord) => boolean;
  now?: () => Date;
  snapshot?: McpConnectionSnapshot | null;
}

export interface McpConnectedServer {
  record: McpConnectionRecord;
  initialize: McpInitializeResult;
  policyDecision: McpPolicyDecision;
  transport: McpTransportAdapter;
}

interface ConnectionOwner {
  config: McpServerConfigRecord;
  record: McpConnectionRecord;
  transport: McpTransportAdapter;
  initialize: McpInitializeResult | null;
  connectPromise: Promise<McpConnectedServer> | null;
  disconnectPromise: Promise<McpConnectionResult> | null;
  policyDecision: McpPolicyDecision | null;
}

const transitionHead = "sha256:zyra-mcp-connection-genesis";

export function assertMcpSourceRuntimeEnabled(): void {
  if (process.env.ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME === "1") {
    throw connectionError(
      "",
      "mcp_source_runtime_disabled",
      "The migrated TypeScript MCP source runtime is disabled; no logical client fallback is permitted",
    );
  }
}

export class McpConnectionRuntime {
  private readonly policy: McpServerPolicy;
  private readonly sessionId: string;
  private readonly workspaceRoot: string;
  private readonly interactive: boolean;
  private readonly sealedAutonomous: boolean;
  private readonly clientName: string;
  private readonly clientVersion: string;
  private readonly processFactory: McpProcessFactory | undefined;
  private readonly fetchValue: McpFetch | undefined;
  private readonly credentialResolver: (serverId: string, handle: string) => Promise<string | null>;
  private readonly authorizationProvider: (serverId: string) => Promise<string | null>;
  private readonly authenticationChallenge: ((serverId: string, challenge: string, response: Response) => Promise<string | null>) | null;
  private readonly inProcessFactory: ((config: McpServerConfigRecord) => McpTransportAdapter) | null;
  private readonly networkEvaluator: (config: McpServerConfigRecord) => boolean;
  private readonly now: () => Date;
  private readonly codec = new McpProtocolCodec();
  private readonly owners = new Map<string, ConnectionOwner>();
  private readonly transitions: McpConnectionTransition[] = [];
  private readonly transitionListeners = new Set<(transition: McpConnectionTransition) => void | Promise<void>>();
  private revision = 0;
  private sequence = 0;
  private restoredTransportSnapshots = new Map<string, McpTransportSnapshot>();

  constructor(options: McpConnectionRuntimeOptions) {
    this.policy = options.policy;
    this.sessionId = options.sessionId;
    this.workspaceRoot = options.workspaceRoot;
    this.interactive = options.interactive;
    this.sealedAutonomous = options.sealedAutonomous;
    this.clientName = options.clientName ?? "zyra-code-worker";
    this.clientVersion = options.clientVersion ?? "0.1.0";
    this.processFactory = options.processFactory;
    this.fetchValue = options.fetch;
    this.credentialResolver = options.credentialResolver ?? (async () => null);
    this.authorizationProvider = options.authorizationProvider ?? (async () => null);
    this.authenticationChallenge = options.authenticationChallenge ?? null;
    this.inProcessFactory = options.inProcessFactory ?? null;
    this.networkEvaluator = options.networkEvaluator ?? defaultNetworkEvaluator;
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  async connect(configValue: McpServerConfigRecord, signal?: AbortSignal): Promise<McpConnectedServer> {
    assertMcpSourceRuntimeEnabled();
    const config = cloneJson(configValue);
    let owner = this.owners.get(config.serverId);
    if (owner?.record.phase === "ready" && owner.initialize && owner.record.configDigest === config.configDigest) {
      return {
        record: cloneJson(owner.record),
        initialize: cloneJson(owner.initialize),
        policyDecision: cloneJson(owner.policyDecision!),
        transport: owner.transport,
      };
    }
    if (owner?.connectPromise) return owner.connectPromise;
    if (owner && owner.record.configDigest !== config.configDigest) {
      await this.disconnect(config.serverId, "config_changed");
      owner = undefined;
    }
    if (!owner) {
      const transport = await this.createTransport(config);
      owner = {
        config,
        record: initialRecord(config, this.policy.digest),
        transport,
        initialize: null,
        connectPromise: null,
        disconnectPromise: null,
        policyDecision: null,
      };
      this.owners.set(config.serverId, owner);
      this.subscribeTransport(owner);
    } else if (owner.record.phase === "closed" || owner.record.phase === "failed") {
      await this.rebuildTransport(owner, `connect_after_${owner.record.phase}`);
    }
    const promise = this.performConnect(owner, signal, owner.record.reconnectAttempt > 0);
    owner.connectPromise = promise;
    try {
      return await promise;
    } finally {
      owner.connectPromise = null;
    }
  }

  async reconnect(serverId: string, reason = "requested", signal?: AbortSignal): Promise<McpConnectedServer> {
    assertMcpSourceRuntimeEnabled();
    const owner = this.requireOwner(serverId);
    if (owner.connectPromise) return owner.connectPromise;
    await this.rebuildTransport(owner, reason);
    const promise = this.performConnect(owner, signal, true);
    owner.connectPromise = promise;
    try {
      return await promise;
    } finally {
      owner.connectPromise = null;
    }
  }

  async disconnect(serverId: string, reason = "requested"): Promise<McpConnectionResult> {
    const owner = this.owners.get(serverId);
    if (!owner) {
      return {
        serverId,
        connectionId: deterministicMcpId("mcp-connection", { server_id: serverId }),
        phase: "closed",
        epoch: 0,
        initialized: false,
        reconnected: false,
        catalogRevision: 0,
        transitionId: "",
        metadata: { already_absent: true },
      };
    }
    if (owner.record.phase === "closed") {
      return {
        serverId,
        connectionId: owner.record.connectionId,
        phase: "closed",
        epoch: owner.record.epoch,
        initialized: false,
        reconnected: owner.record.reconnectAttempt > 0,
        catalogRevision: owner.record.catalogRevision,
        transitionId: "",
        metadata: { already_closed: true, reason },
      };
    }
    if (owner.disconnectPromise) return owner.disconnectPromise;
    const promise = this.performDisconnect(owner, reason);
    owner.disconnectPromise = promise;
    try {
      return await promise;
    } finally {
      owner.disconnectPromise = null;
    }
  }

  async disconnectAll(reason = "runtime_shutdown"): Promise<McpConnectionResult[]> {
    const output: McpConnectionResult[] = [];
    for (const serverId of [...this.owners.keys()].sort()) output.push(await this.disconnect(serverId, reason));
    return output;
  }

  get(serverId: string): McpConnectionRecord | null {
    const owner = this.owners.get(serverId);
    return owner ? cloneJson(owner.record) : null;
  }

  getConnected(serverId: string): McpConnectedServer | null {
    const owner = this.owners.get(serverId);
    if (!owner || owner.record.phase !== "ready" || !owner.initialize || !owner.policyDecision) return null;
    return {
      record: cloneJson(owner.record),
      initialize: cloneJson(owner.initialize),
      policyDecision: cloneJson(owner.policyDecision),
      transport: owner.transport,
    };
  }

  requireConnected(serverId: string): McpConnectedServer {
    assertMcpSourceRuntimeEnabled();
    const connected = this.getConnected(serverId);
    if (!connected) throw connectionError(serverId, "server_not_connected", `MCP server ${serverId} is not connected`);
    return connected;
  }

  list(): McpConnectionRecord[] {
    return [...this.owners.values()]
      .map((owner) => cloneJson(owner.record))
      .sort((left, right) => left.serverId.localeCompare(right.serverId));
  }

  transport(serverId: string): McpTransportAdapter {
    return this.requireOwner(serverId).transport;
  }

  updateCatalogRevision(serverId: string, revision: number): void {
    const owner = this.requireOwner(serverId);
    if (!Number.isSafeInteger(revision) || revision < owner.record.catalogRevision) {
      throw connectionError(serverId, "invalid_catalog_revision", `catalog revision ${revision} is older than ${owner.record.catalogRevision}`);
    }
    owner.record.catalogRevision = revision;
    owner.record.lastActivityAt = this.timestamp(owner.record.lastActivityAt);
    this.revision += 1;
  }

  updateAuthRevision(serverId: string, revision: number): void {
    const owner = this.requireOwner(serverId);
    if (!Number.isSafeInteger(revision) || revision < owner.record.authRevision) {
      throw connectionError(serverId, "invalid_auth_revision", `auth revision ${revision} is older than ${owner.record.authRevision}`);
    }
    owner.record.authRevision = revision;
    owner.record.lastActivityAt = this.timestamp(owner.record.lastActivityAt);
    this.revision += 1;
  }

  onTransition(listener: (transition: McpConnectionTransition) => void | Promise<void>): () => void {
    this.transitionListeners.add(listener);
    return () => this.transitionListeners.delete(listener);
  }

  snapshot(): McpConnectionSnapshot {
    const transports: Record<string, McpTransportSnapshot> = {};
    for (const [serverId, owner] of this.owners) transports[serverId] = owner.transport.snapshot();
    const snapshotWithoutDigest = {
      version: "zyra.mcp-connection-runtime/v1" as const,
      revision: this.revision,
      sequence: this.sequence,
      connections: this.list(),
      transitions: this.transitions.map(cloneJson),
      transports,
      metadata: {
        session_id: this.sessionId,
        workspace_root: this.workspaceRoot,
        interactive: this.interactive,
        sealed_autonomous: this.sealedAutonomous,
        policy_digest: this.policy.digest,
      },
    };
    return {
      ...snapshotWithoutDigest,
      digest: sha256(snapshotWithoutDigest),
    };
  }

  restore(snapshot: McpConnectionSnapshot): void {
    if (snapshot.version !== "zyra.mcp-connection-runtime/v1") throw connectionError("", "unsupported_connection_snapshot", "unsupported MCP connection snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw connectionError("", "connection_snapshot_digest_mismatch", "MCP connection snapshot digest mismatch");
    verifyHashChain(
      snapshot.transitions,
      transitionHead,
      (transition) => transition.previousHash,
      (transition) => transition.transitionHash,
      transitionPayload,
    );
    this.owners.clear();
    this.transitions.splice(0);
    this.restoredTransportSnapshots.clear();
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    for (const transition of snapshot.transitions) this.transitions.push(cloneJson(transition));
    for (const [serverId, transport] of Object.entries(snapshot.transports)) {
      this.restoredTransportSnapshots.set(serverId, cloneJson(transport));
    }
    for (const record of snapshot.connections) {
      const safeRecord = cloneJson(record);
      safeRecord.phase = safeRecord.phase === "closed" ? "closed" : "idle";
      safeRecord.initialized = false;
      safeRecord.readyAt = null;
      safeRecord.metadata = {
        ...safeRecord.metadata,
        restored_phase: record.phase,
        restore_requires_reconnect: record.phase !== "closed",
      };
      // Config is deliberately re-supplied by the config owner before a transport can be rebuilt.
    }
  }

  private async performConnect(
    owner: ConnectionOwner,
    signal?: AbortSignal,
    reconnected = false,
  ): Promise<McpConnectedServer> {
    const config = owner.config;
    const networkReachable = this.networkEvaluator(config);
    const decision = this.policy.evaluate({
      serverId: config.serverId,
      transport: config.transport.kind,
      operation: "connect",
      capabilityName: "",
      resourceUri: "",
      workspaceRoot: this.workspaceRoot,
      source: config.source,
      interactive: this.interactive,
      sealedAutonomous: this.sealedAutonomous,
      networkReachable,
      authenticated: config.authProviderId === null,
      config,
      metadata: { session_id: this.sessionId },
    });
    owner.policyDecision = decision;
    if (decision.effect !== "allow") {
      this.transition(owner, "failed", decision.reason, null, null);
      throw new McpRuntimeError({
        failureId: decision.decisionId,
        category: "policy",
        code: decision.reasonCode,
        message: decision.reason,
        serverId: config.serverId,
        operation: "connect",
        retryable: false,
        disposition: "replan",
        details: { recovery_input: decision.recoveryInput },
      });
    }
    try {
      this.transition(owner, "connecting", reconnected ? "reconnect_start" : "connect_start");
      await owner.transport.start(signal);
      this.transition(owner, "initializing", "transport_ready");
      const initialize = await this.initialize(owner, signal);
      owner.initialize = initialize;
      owner.record.protocolVersion = initialize.protocolVersion;
      owner.record.initialized = true;
      owner.record.connectedAt ??= this.timestamp(owner.record.connectedAt);
      owner.record.readyAt = this.timestamp(owner.record.readyAt);
      owner.record.reconnectNotBefore = null;
      owner.record.lastFailure = null;
      const transition = this.transition(owner, "ready", "initialize_committed");
      this.revision += 1;
      return {
        record: cloneJson(owner.record),
        initialize: cloneJson(initialize),
        policyDecision: cloneJson(decision),
        transport: owner.transport,
      };
    } catch (error) {
      const failure = normalizeFailure(error, {
        failureId: deterministicMcpId("mcp-connect", { server_id: config.serverId, epoch: owner.record.epoch }),
        category: "transport",
        code: "connection_failed",
        message: `MCP connection failed for ${config.serverId}: ${error instanceof Error ? error.message : String(error)}`,
        serverId: config.serverId,
        operation: "connect",
        retryable: true,
        disposition: "reconnect_then_retry",
      });
      owner.record.lastFailure = failure;
      owner.record.reconnectNotBefore = this.nextReconnectTime(owner);
      this.transition(owner, "failed", failure.code, null, failure);
      this.revision += 1;
      throw new McpRuntimeError(failureToInput(failure), { cause: error });
    }
  }

  private async initialize(owner: ConnectionOwner, signal?: AbortSignal): Promise<McpInitializeResult> {
    const requestId = deterministicMcpId("mcp-initialize", {
      server_id: owner.config.serverId,
      connection_id: owner.record.connectionId,
      epoch: owner.record.epoch,
    });
    const message = this.codec.request(requestId, "initialize", {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: {
        roots: { listChanged: true },
        sampling: { context: true, tools: true },
        elicitation: { form: true, url: true },
        tasks: { list: true, cancel: true },
      },
      clientInfo: {
        name: this.clientName,
        title: "Zyra Code Worker",
        version: this.clientVersion,
      },
    });
    const response = await owner.transport.request({
      requestId,
      method: "initialize",
      message,
      timeoutMs: owner.config.timeouts.initializeMs,
      idempotent: true,
      idempotencyKey: requestId,
      authorization: null,
      headers: {},
      signal,
      metadata: {
        connection_id: owner.record.connectionId,
        connection_epoch: owner.record.epoch,
      },
    });
    if (!("result" in response.message)) {
      const error = "error" in response.message ? response.message.error : null;
      throw connectionError(
        owner.config.serverId,
        "initialize_rejected",
        error?.message ?? "MCP initialize response did not include result",
        false,
        { json_rpc_error: error ?? null },
      );
    }
    const initialize = this.codec.parseInitializeResult(response.message.result);
    if (!owner.config.protocolVersions.includes(initialize.protocolVersion)) {
      throw connectionError(
        owner.config.serverId,
        "unsupported_protocol_version",
        `MCP server selected unsupported protocol ${initialize.protocolVersion}`,
        false,
        { allowed_protocol_versions: owner.config.protocolVersions },
      );
    }
    await owner.transport.notify(this.codec.notification("notifications/initialized", {}), signal);
    return initialize;
  }

  private async performDisconnect(owner: ConnectionOwner, reason: string): Promise<McpConnectionResult> {
    const transition = this.transition(owner, "closing", reason);
    try {
      await owner.transport.close(reason);
    } finally {
      owner.record.initialized = false;
      owner.record.readyAt = null;
      const closed = this.transition(owner, "closed", reason);
      this.revision += 1;
      return {
        serverId: owner.config.serverId,
        connectionId: owner.record.connectionId,
        phase: owner.record.phase,
        epoch: owner.record.epoch,
        initialized: false,
        reconnected: owner.record.reconnectAttempt > 0,
        catalogRevision: owner.record.catalogRevision,
        transitionId: closed.transitionId,
        metadata: { closing_transition_id: transition.transitionId, reason },
      };
    }
  }

  private async createTransport(config: McpServerConfigRecord): Promise<McpTransportAdapter> {
    const snapshot = this.restoredTransportSnapshots.get(config.serverId) ?? null;
    if (config.transport.kind === "stdio") {
      const credentials: Record<string, string> = {};
      for (const handle of Object.values(config.transport.environmentHandles)) {
        const value = await this.credentialResolver(config.serverId, handle);
        if (value !== null) credentials[handle] = value;
      }
      return new StdioMcpTransport({
        serverId: config.serverId,
        config: config.transport,
        connectTimeoutMs: config.timeouts.connectMs,
        requestTimeoutMs: config.timeouts.requestMs,
        shutdownTimeoutMs: config.timeouts.shutdownMs,
        credentialEnvironment: credentials,
        processFactory: this.processFactory,
        snapshot,
      });
    }
    if (config.transport.kind === "streamable_http" || config.transport.kind === "sse") {
      const credentialHeaders: Record<string, string> = {};
      for (const [name, handle] of Object.entries(config.transport.credentialHandles)) {
        const value = await this.credentialResolver(config.serverId, handle);
        if (value !== null) credentialHeaders[name] = value;
      }
      return new HttpMcpTransport({
        serverId: config.serverId,
        config: config.transport,
        connectTimeoutMs: config.timeouts.connectMs,
        requestTimeoutMs: config.timeouts.requestMs,
        idleTimeoutMs: config.timeouts.idleMs,
        credentialHeaders,
        fetch: this.fetchValue,
        authorizationProvider: () => this.authorizationProvider(config.serverId),
        onAuthenticationChallenge: this.authenticationChallenge
          ? (challenge, response) => this.authenticationChallenge!(config.serverId, challenge, response)
          : undefined,
        snapshot,
      });
    }
    if (!this.inProcessFactory) throw connectionError(config.serverId, "in_process_factory_missing", "in-process MCP transport factory is not configured");
    return this.inProcessFactory(config);
  }

  private async rebuildTransport(owner: ConnectionOwner, reason: string): Promise<void> {
    const serverId = owner.config.serverId;
    const nonIdempotentPending = owner.transport.snapshot().pending.filter((request) => !request.idempotent);
    if (nonIdempotentPending.length) {
      throw connectionError(
        serverId,
        "non_idempotent_request_inflight",
        `cannot reconnect ${serverId} with ${nonIdempotentPending.length} non-idempotent requests in flight`,
        false,
        { request_ids: nonIdempotentPending.map((request) => request.requestId) },
      );
    }
    this.transition(owner, "reconnecting", reason);
    await owner.transport.close(`reconnect:${reason}`);
    const snapshot = owner.transport.snapshot();
    this.restoredTransportSnapshots.set(serverId, snapshot);
    owner.transport = await this.createTransport(owner.config);
    owner.record.epoch += 1;
    owner.record.initialized = false;
    owner.record.protocolVersion = null;
    owner.record.reconnectAttempt += 1;
    owner.initialize = null;
    this.subscribeTransport(owner);
  }

  private subscribeTransport(owner: ConnectionOwner): void {
    const serverId = owner.config.serverId;
    owner.transport.onMessage((message) => this.handleUnsolicited(serverId, message));
    owner.transport.onEvent((event) => {
      const current = this.owners.get(serverId);
      if (current) current.record.lastActivityAt = event.occurredAt;
    });
  }

  private handleUnsolicited(serverId: string, message: JsonRpcMessage): void {
    const owner = this.owners.get(serverId);
    if (!owner) return;
    owner.record.lastActivityAt = this.timestamp(owner.record.lastActivityAt);
    owner.record.metadata = {
      ...owner.record.metadata,
      last_unsolicited_message_digest: sha256(message),
      last_unsolicited_message_at: owner.record.lastActivityAt,
    };
  }

  private transition(
    owner: ConnectionOwner,
    toPhase: McpConnectionPhase,
    reason: string,
    requestId: string | null = null,
    failure: McpFailureRecord | null = null,
  ): McpConnectionTransition {
    const fromPhase = owner.record.phase;
    if (!allowedTransition(fromPhase, toPhase)) {
      throw connectionError(owner.config.serverId, "invalid_connection_transition", `cannot transition MCP connection from ${fromPhase} to ${toPhase}`);
    }
    this.sequence += 1;
    const previousHash = this.transitions.at(-1)?.transitionHash ?? transitionHead;
    const occurredAt = this.timestamp(owner.record.lastActivityAt);
    const base = {
      transitionId: deterministicMcpId("mcp-connection-transition", {
        connection_id: owner.record.connectionId,
        sequence: this.sequence,
        from_phase: fromPhase,
        to_phase: toPhase,
        reason,
      }),
      serverId: owner.config.serverId,
      connectionId: owner.record.connectionId,
      sequence: this.sequence,
      fromPhase,
      toPhase,
      reason,
      epoch: owner.record.epoch,
      requestId,
      failure: failure ? cloneJson(failure) : null,
      metadata: {
        config_digest: owner.record.configDigest,
        policy_digest: owner.record.policyDigest,
      },
      occurredAt,
      previousHash,
    };
    const transition: McpConnectionTransition = {
      ...base,
      transitionHash: hashChain(previousHash, transitionPayload(base)),
    };
    owner.record.phase = toPhase;
    owner.record.lastActivityAt = occurredAt;
    if (failure) owner.record.lastFailure = cloneJson(failure);
    this.transitions.push(transition);
    for (const listener of this.transitionListeners) void Promise.resolve(listener(cloneJson(transition)));
    return transition;
  }

  private nextReconnectTime(owner: ConnectionOwner): string {
    const retry = owner.config.retry;
    const attempt = Math.max(0, owner.record.reconnectAttempt);
    const raw = Math.min(retry.maximumDelayMs, retry.initialDelayMs * retry.multiplier ** attempt);
    const deterministicJitter = (Number.parseInt(sha256({ server_id: owner.config.serverId, epoch: owner.record.epoch }).slice(0, 8), 16) / 0xffffffff * 2 - 1) * retry.jitterRatio;
    return new Date(this.now().getTime() + Math.max(0, Math.round(raw * (1 + deterministicJitter)))).toISOString();
  }

  private requireOwner(serverId: string): ConnectionOwner {
    const owner = this.owners.get(serverId);
    if (!owner) throw connectionError(serverId, "connection_not_found", `MCP connection ${serverId} was not found`);
    return owner;
  }

  private timestamp(previous: string | null): string {
    return monotonicNow(previous, this.now);
  }
}

function initialRecord(config: McpServerConfigRecord, policyDigest: string): McpConnectionRecord {
  return {
    serverId: config.serverId,
    connectionId: deterministicMcpId("mcp-connection", {
      server_id: config.serverId,
      config_digest: config.configDigest,
    }),
    phase: "idle",
    epoch: 1,
    configDigest: config.configDigest,
    policyDigest,
    protocolVersion: null,
    initialized: false,
    connectedAt: null,
    readyAt: null,
    lastActivityAt: null,
    lastFailure: null,
    reconnectAttempt: 0,
    reconnectNotBefore: null,
    catalogRevision: 0,
    authRevision: 0,
    metadata: {
      config_source: config.source,
      source_custody: "claude-code-best:connectToServer",
      transport_initialize_order: ["policy", "transport", "initialize", "initialized_notification", "ready"],
    },
  };
}

function allowedTransition(from: McpConnectionPhase, to: McpConnectionPhase): boolean {
  if (from === to && (to === "ready" || to === "degraded")) return true;
  const allowed: Record<McpConnectionPhase, McpConnectionPhase[]> = {
    idle: ["connecting", "closing", "closed", "failed"],
    connecting: ["authenticating", "initializing", "failed", "closing"],
    authenticating: ["connecting", "initializing", "failed", "closing"],
    initializing: ["ready", "failed", "closing"],
    ready: ["degraded", "reconnecting", "closing", "failed"],
    degraded: ["ready", "reconnecting", "closing", "failed"],
    reconnecting: ["connecting", "initializing", "ready", "failed", "closing"],
    closing: ["closed", "failed"],
    closed: ["connecting", "reconnecting"],
    failed: ["reconnecting", "connecting", "closing", "closed"],
  };
  return allowed[from].includes(to);
}

function transitionPayload(value: Omit<McpConnectionTransition, "transitionHash"> | McpConnectionTransition): JsonObject {
  const { transitionHash: _ignored, ...payload } = value as McpConnectionTransition;
  return canonicalJson(payload) as JsonObject;
}

function defaultNetworkEvaluator(config: McpServerConfigRecord): boolean {
  if (config.transport.kind === "stdio" || config.transport.kind === "in_process") return true;
  if (config.networkPolicy === "offline") return false;
  const url = new URL(config.transport.url);
  const loopback = url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "::1";
  if (config.networkPolicy === "loopback") return loopback;
  return true;
}

function connectionError(
  serverId: string,
  code: string,
  message: string,
  retryable = false,
  details: JsonObject = {},
): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-connection", { server_id: serverId, code, message }),
    category: code.includes("policy") ? "policy" : code.includes("protocol") ? "protocol" : "transport",
    code,
    message,
    serverId,
    retryable,
    disposition: retryable ? "reconnect_then_retry" : "terminal",
    details,
  });
}

function failureToInput(failure: McpFailureRecord) {
  return {
    failureId: failure.failure_id,
    category: failure.category,
    code: failure.code,
    message: failure.message,
    serverId: failure.server_id,
    operation: failure.operation,
    requestId: failure.request_id,
    sessionId: failure.session_id,
    retryable: failure.retryable,
    disposition: failure.disposition,
    retryAfterMs: failure.retry_after_ms,
    statusCode: failure.status_code,
    jsonRpcCode: failure.json_rpc_code,
    details: failure.details,
    causeChain: failure.cause_chain,
    occurredAt: failure.occurred_at,
  };
}
