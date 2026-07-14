import {
  APPROVAL_SCHEMA,
  Effects,
  GatewayProtocolError,
  type ApprovalBinding,
  type ApprovalConsumption,
  type ApprovalGrant,
  type ApprovalRequest,
  type CommandEnvelope,
  type JsonValue,
} from "./contracts.ts";
import {
  assertExactCommand,
  commandDigest,
  digestJson,
  stableId,
} from "./canonical.ts";

export interface ApprovalBindingStore {
  load(bindingId: string): Promise<ApprovalBinding | null>;
  insert(binding: ApprovalBinding): Promise<ApprovalBinding>;
  compareAndSwap(
    bindingId: string,
    expectedRevision: number,
    next: ApprovalBinding,
  ): Promise<boolean>;
}

export interface ToolPermissionApprovalPort {
  evaluate(request: ApprovalRequest): Promise<ApprovalGrant>;
  validateAndConsume(
    request: ApprovalRequest,
    grant: ApprovalGrant,
    replay: CommandEnvelope,
  ): Promise<boolean>;
}

export class InMemoryApprovalBindingStore implements ApprovalBindingStore {
  private readonly values = new Map<string, ApprovalBinding>();
  private queue: Promise<void> = Promise.resolve();

  async load(bindingId: string): Promise<ApprovalBinding | null> {
    await this.queue;
    return this.values.get(bindingId) ?? null;
  }

  async insert(binding: ApprovalBinding): Promise<ApprovalBinding> {
    return this.exclusive(async () => {
      const existing = this.values.get(binding.bindingId);
      if (existing) {
        if (digestJson(existing as unknown as JsonValue) !==
            digestJson(binding as unknown as JsonValue)) {
          throw new GatewayProtocolError(
            "approval_binding_collision",
            "Approval binding identity collision",
          );
        }
        return existing;
      }
      this.values.set(binding.bindingId, Object.freeze({ ...binding }));
      return binding;
    });
  }

  async compareAndSwap(
    bindingId: string,
    expectedRevision: number,
    next: ApprovalBinding,
  ): Promise<boolean> {
    return this.exclusive(async () => {
      const current = this.values.get(bindingId);
      if (!current || current.revision !== expectedRevision) {
        return false;
      }
      this.values.set(bindingId, Object.freeze({ ...next }));
      return true;
    });
  }

  descriptor(): JsonValue {
    return {
      store: "InMemoryApprovalBindingStore",
      canonical: false,
      durable: false,
      purpose: "typed port and test execution",
      productionCanonicalStore: "GatewayStateStore.permission_bindings",
      entries: this.values.size,
    };
  }

  private async exclusive<T>(operation: () => Promise<T>): Promise<T> {
    const predecessor = this.queue;
    let release: () => void = () => undefined;
    this.queue = new Promise<void>((resolve) => {
      release = resolve;
    });
    await predecessor;
    try {
      return await operation();
    } finally {
      release();
    }
  }
}

export interface ApprovalTicket {
  readonly request: ApprovalRequest;
  readonly grant: ApprovalGrant;
  readonly binding: ApprovalBinding;
}

export class ApprovalLedger {
  readonly store: ApprovalBindingStore;
  readonly permissionPort: ToolPermissionApprovalPort;
  readonly clock: () => number;

  constructor(
    store: ApprovalBindingStore,
    permissionPort: ToolPermissionApprovalPort,
    options: { readonly clock?: () => number } = {},
  ) {
    this.store = store;
    this.permissionPort = permissionPort;
    this.clock = options.clock ?? Date.now;
  }

  async issue(request: ApprovalRequest): Promise<ApprovalTicket> {
    if (request.policy.effect === Effects.deny) {
      throw new GatewayProtocolError(
        "policy_denied",
        "Denied command cannot enter the approval ledger",
      );
    }
    let grant: ApprovalGrant;
    try {
      grant = await this.permissionPort.evaluate(request);
    } catch (error) {
      throw new GatewayProtocolError(
        "permission_unavailable",
        "ToolPermissionRuntime evaluation failed closed: " +
          errorName(error),
        { retryable: true },
      );
    }
    this.validateGrant(request, grant);
    const now = this.clock();
    if (grant.expiresAt <= now) {
      throw new GatewayProtocolError(
        "approval_expired",
        "ToolPermissionRuntime issued an expired grant",
      );
    }
    const bindingId = stableId("gateway-approval-binding", {
      sessionId: request.envelope.sessionId,
      commandId: request.envelope.commandId,
      requestFingerprint: request.requestFingerprint,
      grantDigest: grant.grantDigest,
    });
    const binding: ApprovalBinding = Object.freeze({
      schema: APPROVAL_SCHEMA,
      bindingId,
      sessionId: request.envelope.sessionId,
      commandId: request.envelope.commandId,
      toolUseId: request.envelope.toolUseId,
      requestFingerprint: request.requestFingerprint,
      commandDigest: commandDigest(request.envelope),
      grantDigest: grant.grantDigest,
      effect: grant.effect,
      issuedAt: grant.issuedAt,
      expiresAt: grant.expiresAt,
      consumedAt: null,
      consumptionId: "",
      revision: 1,
      metadata: Object.freeze({
        permissionOwner: "ToolPermissionRuntime",
        policyDigest: request.policy.policyDigest,
        interactive: request.interactive,
        sealed: request.sealed,
      }),
    });
    const persisted = await this.store.insert(binding);
    return Object.freeze({ request, grant, binding: persisted });
  }

  async consume(
    ticket: ApprovalTicket,
    replay: CommandEnvelope,
  ): Promise<ApprovalConsumption> {
    const current = await this.store.load(ticket.binding.bindingId);
    if (!current) {
      throw new GatewayProtocolError(
        "approval_missing",
        "Approval binding is absent",
      );
    }
    if (current.consumedAt !== null) {
      throw new GatewayProtocolError(
        "approval_replay",
        "Approval binding has already been consumed",
      );
    }
    if (this.clock() >= current.expiresAt) {
      throw new GatewayProtocolError(
        "approval_expired",
        "Approval binding expired before execution",
      );
    }
    assertExactCommand(ticket.request.envelope, replay);
    if (current.commandDigest !== commandDigest(replay)) {
      throw new GatewayProtocolError(
        "command_mutated",
        "Replay command does not match binding digest",
      );
    }
    if (
      current.requestFingerprint !== ticket.request.requestFingerprint ||
      current.grantDigest !== ticket.grant.grantDigest
    ) {
      throw new GatewayProtocolError(
        "approval_mismatch",
        "Request fingerprint or grant digest changed",
      );
    }
    let accepted: boolean;
    try {
      accepted = await this.permissionPort.validateAndConsume(
        ticket.request,
        ticket.grant,
        replay,
      );
    } catch (error) {
      throw new GatewayProtocolError(
        "approval_mismatch",
        "ToolPermissionRuntime validation failed: " + errorName(error),
      );
    }
    if (!accepted) {
      throw new GatewayProtocolError(
        "approval_mismatch",
        "ToolPermissionRuntime rejected identity or replay",
      );
    }
    const consumedAt = this.clock();
    const consumptionId = stableId("gateway-approval-consumption", {
      bindingId: current.bindingId,
      commandId: replay.commandId,
      commandDigest: current.commandDigest,
      consumedAt,
    });
    const consumed: ApprovalBinding = Object.freeze({
      ...current,
      consumedAt,
      consumptionId,
      revision: current.revision + 1,
    });
    const committed = await this.store.compareAndSwap(
      current.bindingId,
      current.revision,
      consumed,
    );
    if (!committed) {
      throw new GatewayProtocolError(
        "approval_replay",
        "Concurrent consumer already changed the approval binding",
      );
    }
    return Object.freeze({
      accepted: true,
      binding: consumed,
      reason: "Exact one-use ToolPermissionRuntime grant consumed",
    });
  }

  private validateGrant(
    request: ApprovalRequest,
    grant: ApprovalGrant,
  ): void {
    if (grant.permissionOwner !== "ToolPermissionRuntime") {
      throw new GatewayProtocolError(
        "permission_owner_mismatch",
        "Approval grant must be owned by ToolPermissionRuntime",
      );
    }
    if (grant.requestFingerprint !== request.requestFingerprint) {
      throw new GatewayProtocolError(
        "approval_mismatch",
        "Approval grant is bound to different request material",
      );
    }
    if (!grant.grantDigest) {
      throw new GatewayProtocolError(
        "approval_missing",
        "ToolPermissionRuntime did not issue grant material",
      );
    }
    if (grant.effect !== Effects.allow && grant.effect !== Effects.ask) {
      throw new GatewayProtocolError(
        "approval_invalid",
        "Approval grant cannot carry deny",
      );
    }
  }
}

export class CallbackToolPermissionApprovalPort
  implements ToolPermissionApprovalPort
{
  readonly evaluateCallback: (
    request: ApprovalRequest,
  ) => Promise<ApprovalGrant> | ApprovalGrant;
  readonly consumeCallback: (
    request: ApprovalRequest,
    grant: ApprovalGrant,
    replay: CommandEnvelope,
  ) => Promise<boolean> | boolean;

  constructor(callbacks: {
    readonly evaluate: (
      request: ApprovalRequest,
    ) => Promise<ApprovalGrant> | ApprovalGrant;
    readonly validateAndConsume: (
      request: ApprovalRequest,
      grant: ApprovalGrant,
      replay: CommandEnvelope,
    ) => Promise<boolean> | boolean;
  }) {
    this.evaluateCallback = callbacks.evaluate;
    this.consumeCallback = callbacks.validateAndConsume;
  }

  async evaluate(request: ApprovalRequest): Promise<ApprovalGrant> {
    return await this.evaluateCallback(request);
  }

  async validateAndConsume(
    request: ApprovalRequest,
    grant: ApprovalGrant,
    replay: CommandEnvelope,
  ): Promise<boolean> {
    return Boolean(
      await this.consumeCallback(request, grant, replay),
    );
  }

  descriptor(): JsonValue {
    return {
      port: "CallbackToolPermissionApprovalPort",
      finalAuthority: "ToolPermissionRuntime",
      decidesWithoutCallback: false,
    };
  }
}

function errorName(value: unknown): string {
  return value instanceof Error ? value.name : typeof value;
}
