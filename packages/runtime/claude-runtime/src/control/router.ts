import type { RuntimeRunInput } from "../contracts.ts";
import {
  createId,
  digest,
  E03RuntimeError,
  response,
  type E03Clock,
  type E03ControlEnvelope,
  type E03ControlResponse,
  SystemE03Clock,
} from "../e03/contracts.ts";
import { AgentControlHandler } from "./session-handler.ts";

export interface ControlDelegate {
  execute(envelope: E03ControlEnvelope): Promise<E03ControlResponse>;
}

export interface E03CommandHandler {
  execute(
    envelope: E03ControlEnvelope,
    parentInput?: RuntimeRunInput,
  ): Promise<E03ControlResponse>;
}

export class StructuredControlRouter {
  constructor(
    private readonly agent: AgentControlHandler,
    private readonly e03: E03CommandHandler,
    private readonly e01?: ControlDelegate,
    private readonly e02?: ControlDelegate,
  ) {}

  async dispatch(
    envelope: E03ControlEnvelope,
    parentInput?: RuntimeRunInput,
  ): Promise<E03ControlResponse> {
    if (isE01(envelope.command)) return this.delegateE01(envelope);
    if (isE02(envelope.command)) return this.delegateE02(envelope);
    if (
      envelope.command === "agent.create" ||
      envelope.command === "team.fanout" ||
      envelope.command.startsWith("worktree.")
    )
      return this.e03.execute(envelope, parentInput);
    return this.agent.execute(envelope, parentInput);
  }

  async delegateE01(envelope: E03ControlEnvelope): Promise<E03ControlResponse> {
    if (!isE01(envelope.command))
      throw new E03RuntimeError(
        "control_owner_mismatch",
        `${envelope.command} is not owned by E01`,
      );
    if (!this.e01)
      return response({
        ok: false,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "rejected",
        error: "e01_control_delegate_unavailable",
      });
    const delegated = await this.e01.execute(envelope);
    this.assertDelegated(delegated, envelope, "E01");
    return delegated;
  }

  async delegateE02(envelope: E03ControlEnvelope): Promise<E03ControlResponse> {
    if (!isE02(envelope.command))
      throw new E03RuntimeError(
        "control_owner_mismatch",
        `${envelope.command} is not owned by E02`,
      );
    if (!this.e02)
      return response({
        ok: false,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "rejected",
        error: "e02_control_delegate_unavailable",
      });
    const delegated = await this.e02.execute(envelope);
    this.assertDelegated(delegated, envelope, "E02");
    return delegated;
  }

  recoverLostAck(envelope: E03ControlEnvelope): E03ControlResponse | null {
    if (isE01(envelope.command) || isE02(envelope.command)) return null;
    return this.agent.recoverLostAck(envelope.idempotency_key);
  }

  private assertDelegated(
    responseValue: E03ControlResponse,
    envelope: E03ControlEnvelope,
    owner: "E01" | "E02",
  ): void {
    if (
      responseValue.request_id !== envelope.request_id ||
      responseValue.command !== envelope.command
    )
      throw new E03RuntimeError(
        "delegated_control_identity",
        `${owner} delegate response identity differs from request`,
      );
    if (
      responseValue.runtime_origin ===
        "typescript.E03AgentControlCoordinator" &&
      responseValue.ok &&
      responseValue.state?.canonical_owner === "E03"
    )
      throw new E03RuntimeError(
        "delegated_control_owner",
        `${owner} command was falsely claimed by E03`,
      );
  }
}

function isE01(command: E03ControlEnvelope["command"]): boolean {
  return (
    command === "context.compact" ||
    command === "context.clear" ||
    command === "session.model"
  );
}

function isE02(command: E03ControlEnvelope["command"]): boolean {
  return (
    command === "permission.inspect" ||
    command === "mcp.inspect" ||
    command === "skills.inspect"
  );
}

export type ControlRouteOwner = "E01" | "E02" | "E03";
export type ControlRouteState = "active" | "draining" | "disabled" | "failed";

export interface ControlRouteDescriptor {
  routeId: string;
  command: E03ControlEnvelope["command"];
  owner: ControlRouteOwner;
  handlerId: string;
  priority: number;
  state: ControlRouteState;
  mutation: boolean;
  physicalEffect: boolean;
  supportsReplay: boolean;
  supportsLostAck: boolean;
  maximumInFlight: number;
  timeoutMs: number;
  registeredAt: string;
  revision: number;
  digest: string;
}

export interface ControlRouteLease {
  routeLeaseId: string;
  routeId: string;
  command: E03ControlEnvelope["command"];
  requestId: string;
  idempotencyKey: string;
  handlerId: string;
  owner: ControlRouteOwner;
  expectedRouteRevision: number;
  acquiredAt: string;
  expiresAt: string;
  releasedAt: string | null;
  outcome: "active" | "succeeded" | "failed" | "timed-out" | "cancelled";
  errorCode: string;
  revision: number;
  digest: string;
}

export interface ControlRouteDecision {
  accepted: boolean;
  code: string;
  requestId: string;
  command: E03ControlEnvelope["command"];
  routeId: string | null;
  owner: ControlRouteOwner | null;
  handlerId: string | null;
  routeState: ControlRouteState | null;
  inFlight: number;
  maximumInFlight: number;
  selectedAt: string;
  digest: string;
}

function assertRouteDescriptor(route: ControlRouteDescriptor): void {
  const { digest: checksum, ...payload } = route;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_route_checksum",
      `control route ${route.routeId} checksum mismatch`,
    );
  if (
    !route.routeId ||
    !route.handlerId ||
    route.priority < 0 ||
    route.maximumInFlight < 1 ||
    route.timeoutMs < 1 ||
    route.revision < 1
  )
    throw new E03RuntimeError(
      "control_route_invalid",
      `control route ${route.routeId} is invalid`,
    );
}

function assertRouteLease(lease: ControlRouteLease): void {
  const { digest: checksum, ...payload } = lease;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_route_lease_checksum",
      `control route lease ${lease.routeLeaseId} checksum mismatch`,
    );
  if (
    !lease.requestId ||
    !lease.idempotencyKey ||
    lease.expectedRouteRevision < 1 ||
    lease.revision < 1 ||
    Date.parse(lease.expiresAt) <= Date.parse(lease.acquiredAt)
  )
    throw new E03RuntimeError(
      "control_route_lease_invalid",
      `control route lease ${lease.routeLeaseId} is invalid`,
    );
  if (lease.outcome !== "active" && !lease.releasedAt)
    throw new E03RuntimeError(
      "control_route_lease_release_time",
      `terminal route lease ${lease.routeLeaseId} has no release time`,
    );
}

function resealRoute(
  route: ControlRouteDescriptor,
  patch: Partial<
    Omit<ControlRouteDescriptor, "routeId" | "command" | "digest">
  >,
): ControlRouteDescriptor {
  const { digest: _, ...prior } = route;
  const payload = {
    ...prior,
    ...patch,
    routeId: route.routeId,
    command: route.command,
  };
  const next = { ...payload, digest: digest(payload) };
  assertRouteDescriptor(next);
  return next;
}

function resealRouteLease(
  lease: ControlRouteLease,
  patch: Partial<
    Omit<ControlRouteLease, "routeLeaseId" | "routeId" | "digest">
  >,
): ControlRouteLease {
  const { digest: _, ...prior } = lease;
  const payload = {
    ...prior,
    ...patch,
    routeLeaseId: lease.routeLeaseId,
    routeId: lease.routeId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertRouteLease(next);
  return next;
}

export class ControlRouteRegistry {
  private routes = new Map<string, ControlRouteDescriptor>();
  private byCommand = new Map<E03ControlEnvelope["command"], string[]>();
  private leases = new Map<string, ControlRouteLease>();
  private activeByRoute = new Map<string, Set<string>>();
  private idempotency = new Map<string, string>();

  register(
    input: Omit<
      ControlRouteDescriptor,
      "routeId" | "registeredAt" | "revision" | "digest"
    > & {
      routeId?: string;
      registeredAt?: string;
    },
  ): ControlRouteDescriptor {
    const routeId =
      input.routeId?.trim() ||
      `control-route-${digest({ command: input.command, owner: input.owner, handlerId: input.handlerId }).slice(0, 32)}`;
    const existing = this.routes.get(routeId);
    const payload = {
      ...structuredClone(input),
      routeId,
      handlerId: input.handlerId.trim(),
      registeredAt:
        existing?.registeredAt ??
        input.registeredAt ??
        new Date().toISOString(),
      revision: (existing?.revision ?? 0) + 1,
    };
    const route = { ...payload, digest: digest(payload) };
    assertRouteDescriptor(route);
    if (existing && existing.command !== route.command)
      throw new E03RuntimeError(
        "control_route_command_changed",
        `control route ${routeId} cannot change command`,
      );
    this.routes.set(routeId, route);
    const ids = this.byCommand.get(route.command) ?? [];
    if (!ids.includes(routeId)) ids.push(routeId);
    this.byCommand.set(route.command, ids);
    if (!this.activeByRoute.has(routeId))
      this.activeByRoute.set(routeId, new Set<string>());
    return structuredClone(route);
  }

  setState(routeId: string, state: ControlRouteState): ControlRouteDescriptor {
    const current = this.requireRoute(routeId);
    if (
      state === "disabled" &&
      (this.activeByRoute.get(routeId)?.size ?? 0) > 0
    )
      throw new E03RuntimeError(
        "control_route_active_disable",
        `control route ${routeId} has active leases`,
      );
    const next = resealRoute(current, {
      state,
      revision: current.revision + 1,
    });
    this.routes.set(routeId, next);
    return structuredClone(next);
  }

  decide(
    envelope: E03ControlEnvelope,
    now = new Date().toISOString(),
  ): ControlRouteDecision {
    const candidates = (this.byCommand.get(envelope.command) ?? [])
      .map((routeId) => this.routes.get(routeId)!)
      .sort(
        (left, right) =>
          left.priority - right.priority ||
          left.routeId.localeCompare(right.routeId),
      );
    const selected = candidates.find((route) => {
      if (route.state !== "active") return false;
      const inFlight = this.activeByRoute.get(route.routeId)?.size ?? 0;
      return inFlight < route.maximumInFlight;
    });
    const fallback = candidates[0] ?? null;
    let code = "control_route_accepted";
    if (!candidates.length) code = "control_route_missing";
    else if (!selected && candidates.every((route) => route.state !== "active"))
      code = "control_route_unavailable";
    else if (!selected) code = "control_route_saturated";
    const route = selected ?? fallback;
    const payload = {
      accepted: code === "control_route_accepted",
      code,
      requestId: envelope.request_id,
      command: envelope.command,
      routeId: route?.routeId ?? null,
      owner: route?.owner ?? null,
      handlerId: route?.handlerId ?? null,
      routeState: route?.state ?? null,
      inFlight: route ? (this.activeByRoute.get(route.routeId)?.size ?? 0) : 0,
      maximumInFlight: route?.maximumInFlight ?? 0,
      selectedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  acquire(
    envelope: E03ControlEnvelope,
    now = new Date().toISOString(),
  ): ControlRouteLease {
    const bound = this.idempotency.get(envelope.idempotency_key);
    if (bound) return structuredClone(this.leases.get(bound)!);
    const decision = this.decide(envelope, now);
    if (!decision.accepted || !decision.routeId)
      throw new E03RuntimeError(
        decision.code,
        `control command ${envelope.command} has no available route`,
      );
    const route = this.requireRoute(decision.routeId);
    const payload = {
      routeLeaseId: `control-route-lease-${digest({
        routeId: route.routeId,
        requestId: envelope.request_id,
        idempotencyKey: envelope.idempotency_key,
      }).slice(0, 32)}`,
      routeId: route.routeId,
      command: envelope.command,
      requestId: envelope.request_id,
      idempotencyKey: envelope.idempotency_key,
      handlerId: route.handlerId,
      owner: route.owner,
      expectedRouteRevision: route.revision,
      acquiredAt: now,
      expiresAt: new Date(Date.parse(now) + route.timeoutMs).toISOString(),
      releasedAt: null,
      outcome: "active" as const,
      errorCode: "",
      revision: 1,
    };
    const lease = { ...payload, digest: digest(payload) };
    assertRouteLease(lease);
    this.leases.set(lease.routeLeaseId, lease);
    this.idempotency.set(lease.idempotencyKey, lease.routeLeaseId);
    const active = this.activeByRoute.get(route.routeId) ?? new Set<string>();
    active.add(lease.routeLeaseId);
    this.activeByRoute.set(route.routeId, active);
    return structuredClone(lease);
  }

  release(input: {
    routeLeaseId: string;
    outcome: "succeeded" | "failed" | "timed-out" | "cancelled";
    errorCode?: string;
    now?: string;
  }): ControlRouteLease {
    const current = this.requireLease(input.routeLeaseId);
    if (current.outcome !== "active") return structuredClone(current);
    const now = input.now ?? new Date().toISOString();
    const route = this.requireRoute(current.routeId);
    if (
      route.revision !== current.expectedRouteRevision &&
      route.state !== "draining"
    )
      throw new E03RuntimeError(
        "control_route_stale_revision",
        `control route ${route.routeId} changed during dispatch`,
      );
    const next = resealRouteLease(current, {
      outcome: input.outcome,
      releasedAt: now,
      errorCode: input.errorCode?.trim() || "",
      revision: current.revision + 1,
    });
    this.leases.set(next.routeLeaseId, next);
    this.activeByRoute.get(next.routeId)?.delete(next.routeLeaseId);
    return structuredClone(next);
  }

  expired(now = new Date().toISOString()): ControlRouteLease[] {
    return [...this.leases.values()]
      .filter(
        (lease) =>
          lease.outcome === "active" &&
          Date.parse(now) >= Date.parse(lease.expiresAt),
      )
      .map((lease) => structuredClone(lease));
  }

  restore(input: {
    routes: readonly ControlRouteDescriptor[];
    leases: readonly ControlRouteLease[];
  }): void {
    const routes = new Map<string, ControlRouteDescriptor>();
    const byCommand = new Map<E03ControlEnvelope["command"], string[]>();
    const activeByRoute = new Map<string, Set<string>>();
    for (const raw of input.routes) {
      const route = structuredClone(raw);
      assertRouteDescriptor(route);
      if (routes.has(route.routeId))
        throw new E03RuntimeError(
          "duplicate_control_route",
          `control route ${route.routeId} repeats`,
        );
      routes.set(route.routeId, route);
      const ids = byCommand.get(route.command) ?? [];
      ids.push(route.routeId);
      byCommand.set(route.command, ids);
      activeByRoute.set(route.routeId, new Set<string>());
    }
    const leases = new Map<string, ControlRouteLease>();
    const idempotency = new Map<string, string>();
    for (const raw of input.leases) {
      const lease = structuredClone(raw);
      assertRouteLease(lease);
      if (!routes.has(lease.routeId))
        throw new E03RuntimeError(
          "control_route_lease_route_missing",
          `control route lease ${lease.routeLeaseId} route is missing`,
        );
      if (
        leases.has(lease.routeLeaseId) ||
        idempotency.has(lease.idempotencyKey)
      )
        throw new E03RuntimeError(
          "duplicate_control_route_lease",
          `control route lease ${lease.routeLeaseId} repeats`,
        );
      leases.set(lease.routeLeaseId, lease);
      idempotency.set(lease.idempotencyKey, lease.routeLeaseId);
      if (lease.outcome === "active")
        activeByRoute.get(lease.routeId)!.add(lease.routeLeaseId);
    }
    this.routes = routes;
    this.byCommand = byCommand;
    this.leases = leases;
    this.activeByRoute = activeByRoute;
    this.idempotency = idempotency;
  }

  snapshot(): {
    routes: ControlRouteDescriptor[];
    leases: ControlRouteLease[];
  } {
    return {
      routes: [...this.routes.values()]
        .sort((left, right) => left.routeId.localeCompare(right.routeId))
        .map((route) => structuredClone(route)),
      leases: [...this.leases.values()]
        .sort((left, right) =>
          left.routeLeaseId.localeCompare(right.routeLeaseId),
        )
        .map((lease) => structuredClone(lease)),
    };
  }

  private requireRoute(routeId: string): ControlRouteDescriptor {
    const route = this.routes.get(routeId);
    if (!route)
      throw new E03RuntimeError(
        "control_route_missing",
        `control route ${routeId} is missing`,
      );
    assertRouteDescriptor(route);
    return route;
  }

  private requireLease(routeLeaseId: string): ControlRouteLease {
    const lease = this.leases.get(routeLeaseId);
    if (!lease)
      throw new E03RuntimeError(
        "control_route_lease_missing",
        `control route lease ${routeLeaseId} is missing`,
      );
    assertRouteLease(lease);
    return lease;
  }
}

export type ControlMiddlewareStage =
  | "parse"
  | "authenticate"
  | "authorize"
  | "validate"
  | "route"
  | "execute"
  | "settle"
  | "project";

export interface ControlMiddlewareDescriptor {
  middlewareId: string;
  stage: ControlMiddlewareStage;
  priority: number;
  commands: E03ControlEnvelope["command"][];
  mutationOnly: boolean;
  enabled: boolean;
  failOpen: boolean;
  timeoutMs: number;
  revision: number;
  registeredAt: string;
  digest: string;
}

export interface ControlMiddlewareResult {
  resultId: string;
  middlewareId: string;
  requestId: string;
  command: E03ControlEnvelope["command"];
  stage: ControlMiddlewareStage;
  accepted: boolean;
  code: string;
  inputDigest: string;
  output: Record<string, unknown>;
  outputDigest: string;
  durationMs: number;
  startedAt: string;
  completedAt: string;
  digest: string;
}

export interface ControlPipelineTrace {
  traceId: string;
  requestId: string;
  command: E03ControlEnvelope["command"];
  stages: ControlMiddlewareStage[];
  results: ControlMiddlewareResult[];
  accepted: boolean;
  code: string;
  failedMiddlewareId: string | null;
  startedAt: string;
  completedAt: string;
  digest: string;
}

export type ControlMiddleware = (input: {
  envelope: E03ControlEnvelope;
  stage: ControlMiddlewareStage;
  context: Record<string, unknown>;
  signal: AbortSignal;
}) => Promise<{
  accepted: boolean;
  code?: string;
  output?: Record<string, unknown>;
}>;

function assertMiddlewareDescriptor(value: ControlMiddlewareDescriptor): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_middleware_checksum",
      `control middleware ${value.middlewareId} checksum mismatch`,
    );
  if (
    !value.middlewareId ||
    value.priority < 0 ||
    value.timeoutMs < 1 ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_middleware_invalid",
      `control middleware ${value.middlewareId} is invalid`,
    );
}

export class ControlMiddlewarePipeline {
  private descriptors = new Map<string, ControlMiddlewareDescriptor>();
  private implementations = new Map<string, ControlMiddleware>();
  private traces = new Map<string, ControlPipelineTrace>();

  register(input: {
    middlewareId: string;
    stage: ControlMiddlewareStage;
    priority?: number;
    commands?: readonly E03ControlEnvelope["command"][];
    mutationOnly?: boolean;
    enabled?: boolean;
    failOpen?: boolean;
    timeoutMs?: number;
    implementation: ControlMiddleware;
    now?: string;
  }): ControlMiddlewareDescriptor {
    const middlewareId = input.middlewareId.trim();
    if (!middlewareId)
      throw new E03RuntimeError(
        "control_middleware_id_missing",
        "control middleware id is required",
      );
    const existing = this.descriptors.get(middlewareId);
    const payload = {
      middlewareId,
      stage: input.stage,
      priority: input.priority ?? 100,
      commands: [...new Set(input.commands ?? [])].sort(),
      mutationOnly: input.mutationOnly ?? false,
      enabled: input.enabled ?? true,
      failOpen: input.failOpen ?? false,
      timeoutMs: input.timeoutMs ?? 30_000,
      revision: (existing?.revision ?? 0) + 1,
      registeredAt:
        existing?.registeredAt ?? input.now ?? new Date().toISOString(),
    };
    const descriptor = { ...payload, digest: digest(payload) };
    assertMiddlewareDescriptor(descriptor);
    this.descriptors.set(middlewareId, descriptor);
    this.implementations.set(middlewareId, input.implementation);
    return structuredClone(descriptor);
  }

  setEnabled(
    middlewareId: string,
    enabled: boolean,
  ): ControlMiddlewareDescriptor {
    const current = this.requireDescriptor(middlewareId);
    const { digest: _, ...prior } = current;
    const payload = {
      ...prior,
      enabled,
      revision: current.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMiddlewareDescriptor(next);
    this.descriptors.set(middlewareId, next);
    return structuredClone(next);
  }

  async execute(input: {
    envelope: E03ControlEnvelope;
    mutation: boolean;
    context?: Record<string, unknown>;
    stages?: readonly ControlMiddlewareStage[];
    now?: string;
  }): Promise<ControlPipelineTrace> {
    const stages = input.stages ?? [
      "parse",
      "authenticate",
      "authorize",
      "validate",
      "route",
      "execute",
      "settle",
      "project",
    ];
    const startedAt = input.now ?? new Date().toISOString();
    const context: Record<string, unknown> = structuredClone(
      input.context ?? {},
    );
    const results: ControlMiddlewareResult[] = [];
    let code = "control_pipeline_accepted";
    let failedMiddlewareId: string | null = null;
    stageLoop: for (const stage of stages) {
      const descriptors = [...this.descriptors.values()]
        .filter(
          (value) =>
            value.enabled &&
            value.stage === stage &&
            (!value.commands.length ||
              value.commands.includes(input.envelope.command)) &&
            (!value.mutationOnly || input.mutation),
        )
        .sort(
          (left, right) =>
            left.priority - right.priority ||
            left.middlewareId.localeCompare(right.middlewareId),
        );
      for (const descriptor of descriptors) {
        const implementation = this.implementations.get(
          descriptor.middlewareId,
        );
        if (!implementation)
          throw new E03RuntimeError(
            "control_middleware_implementation_missing",
            `control middleware ${descriptor.middlewareId} implementation is missing`,
          );
        const middlewareStartedAt = new Date().toISOString();
        const controller = new AbortController();
        const timer = setTimeout(
          () => controller.abort(),
          descriptor.timeoutMs,
        );
        let result: {
          accepted: boolean;
          code?: string;
          output?: Record<string, unknown>;
        };
        try {
          result = await implementation({
            envelope: input.envelope,
            stage,
            context: structuredClone(context),
            signal: controller.signal,
          });
        } catch (error) {
          result = {
            accepted: descriptor.failOpen,
            code:
              error instanceof E03RuntimeError
                ? error.code
                : controller.signal.aborted
                  ? "control_middleware_timeout"
                  : "control_middleware_failed",
            output: {
              error: error instanceof Error ? error.message : String(error),
            },
          };
        } finally {
          clearTimeout(timer);
        }
        const output = structuredClone(result.output ?? {});
        Object.assign(context, output);
        const completedAt = new Date().toISOString();
        const payload = {
          resultId: `control-middleware-result-${digest({
            requestId: input.envelope.request_id,
            middlewareId: descriptor.middlewareId,
            descriptorRevision: descriptor.revision,
            inputDigest: digest(context),
          }).slice(0, 32)}`,
          middlewareId: descriptor.middlewareId,
          requestId: input.envelope.request_id,
          command: input.envelope.command,
          stage,
          accepted: result.accepted,
          code:
            result.code?.trim() || (result.accepted ? "accepted" : "rejected"),
          inputDigest: digest(input.context ?? {}),
          output,
          outputDigest: digest(output),
          durationMs: Math.max(
            0,
            Date.parse(completedAt) - Date.parse(middlewareStartedAt),
          ),
          startedAt: middlewareStartedAt,
          completedAt,
        };
        const record = { ...payload, digest: digest(payload) };
        results.push(record);
        if (!record.accepted && !descriptor.failOpen) {
          code = record.code;
          failedMiddlewareId = descriptor.middlewareId;
          break stageLoop;
        }
      }
    }
    const completedAt = new Date().toISOString();
    const payload = {
      traceId: `control-pipeline-trace-${digest({
        requestId: input.envelope.request_id,
        resultDigests: results.map((result) => result.digest),
      }).slice(0, 32)}`,
      requestId: input.envelope.request_id,
      command: input.envelope.command,
      stages: [...stages],
      results,
      accepted: failedMiddlewareId === null,
      code,
      failedMiddlewareId,
      startedAt,
      completedAt,
    };
    const trace = { ...payload, digest: digest(payload) };
    this.traces.set(trace.traceId, trace);
    return structuredClone(trace);
  }

  restore(input: {
    descriptors: readonly ControlMiddlewareDescriptor[];
    traces?: readonly ControlPipelineTrace[];
  }): void {
    const descriptors = new Map<string, ControlMiddlewareDescriptor>();
    for (const raw of input.descriptors) {
      const descriptor = structuredClone(raw);
      assertMiddlewareDescriptor(descriptor);
      if (descriptors.has(descriptor.middlewareId))
        throw new E03RuntimeError(
          "duplicate_control_middleware",
          `control middleware ${descriptor.middlewareId} repeats`,
        );
      descriptors.set(descriptor.middlewareId, descriptor);
    }
    const traces = new Map<string, ControlPipelineTrace>();
    for (const raw of input.traces ?? []) {
      const trace = structuredClone(raw);
      const { digest: checksum, ...payload } = trace;
      if (digest(payload) !== checksum)
        throw new E03RuntimeError(
          "control_pipeline_trace_checksum",
          `control pipeline trace ${trace.traceId} checksum mismatch`,
        );
      if (traces.has(trace.traceId))
        throw new E03RuntimeError(
          "duplicate_control_pipeline_trace",
          `control pipeline trace ${trace.traceId} repeats`,
        );
      traces.set(trace.traceId, trace);
    }
    this.descriptors = descriptors;
    this.traces = traces;
  }

  snapshot(): {
    descriptors: ControlMiddlewareDescriptor[];
    traces: ControlPipelineTrace[];
  } {
    return {
      descriptors: [...this.descriptors.values()]
        .sort((left, right) =>
          left.middlewareId.localeCompare(right.middlewareId),
        )
        .map((descriptor) => structuredClone(descriptor)),
      traces: [...this.traces.values()]
        .sort((left, right) => left.traceId.localeCompare(right.traceId))
        .map((trace) => structuredClone(trace)),
    };
  }

  private requireDescriptor(middlewareId: string): ControlMiddlewareDescriptor {
    const descriptor = this.descriptors.get(middlewareId);
    if (!descriptor)
      throw new E03RuntimeError(
        "control_middleware_missing",
        `control middleware ${middlewareId} is missing`,
      );
    assertMiddlewareDescriptor(descriptor);
    return descriptor;
  }
}

export type ControlCircuitState = "closed" | "open" | "half-open" | "disabled";

export interface ControlCircuitPolicy {
  policyId: string;
  routeId: string;
  failureThreshold: number;
  successThreshold: number;
  samplingWindowMs: number;
  openDurationMs: number;
  halfOpenMaximumRequests: number;
  retryableErrors: string[];
  ignoredErrors: string[];
  digest: string;
}

export interface ControlCircuit {
  circuitId: string;
  policyId: string;
  routeId: string;
  state: ControlCircuitState;
  consecutiveFailures: number;
  consecutiveSuccesses: number;
  halfOpenInFlight: number;
  openedAt: string | null;
  halfOpenedAt: string | null;
  closedAt: string | null;
  lastFailureAt: string | null;
  lastSuccessAt: string | null;
  revision: number;
  digest: string;
}

export interface ControlCircuitObservation {
  observationId: string;
  circuitId: string;
  routeId: string;
  requestId: string;
  outcome: "success" | "failure" | "ignored" | "rejected";
  errorCode: string | null;
  durationMs: number;
  circuitRevision: number;
  observedAt: string;
  digest: string;
}

function assertControlCircuitPolicy(policy: ControlCircuitPolicy): void {
  const { digest: checksum, ...payload } = policy;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_circuit_policy_digest",
      `control circuit policy ${policy.policyId} digest is invalid`,
    );
  if (!policy.policyId || !policy.routeId)
    throw new E03RuntimeError(
      "control_circuit_policy_identity",
      "control circuit policy identity is incomplete",
    );
  for (const [name, value] of [
    ["failure threshold", policy.failureThreshold],
    ["success threshold", policy.successThreshold],
    ["sampling window", policy.samplingWindowMs],
    ["open duration", policy.openDurationMs],
    ["half-open maximum", policy.halfOpenMaximumRequests],
  ] as const)
    if (!Number.isSafeInteger(value) || value < 1)
      throw new E03RuntimeError(
        "control_circuit_policy_limit",
        `control circuit ${name} is invalid`,
      );
}

function assertControlCircuit(circuit: ControlCircuit): void {
  const { digest: checksum, ...payload } = circuit;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_circuit_digest",
      `control circuit ${circuit.circuitId} digest is invalid`,
    );
  if (!circuit.circuitId || !circuit.policyId || !circuit.routeId)
    throw new E03RuntimeError(
      "control_circuit_identity",
      "control circuit identity is incomplete",
    );
  for (const value of [
    circuit.consecutiveFailures,
    circuit.consecutiveSuccesses,
    circuit.halfOpenInFlight,
  ])
    if (!Number.isSafeInteger(value) || value < 0)
      throw new E03RuntimeError(
        "control_circuit_counter",
        "control circuit counter is invalid",
      );
  if (!Number.isSafeInteger(circuit.revision) || circuit.revision < 1)
    throw new E03RuntimeError(
      "control_circuit_revision",
      "control circuit revision is invalid",
    );
  if (circuit.state === "open" && circuit.openedAt === null)
    throw new E03RuntimeError(
      "control_circuit_state",
      "open control circuit requires openedAt",
    );
}

export class ControlCircuitRuntime {
  private policies = new Map<string, ControlCircuitPolicy>();
  private circuits = new Map<string, ControlCircuit>();
  private observations = new Map<string, ControlCircuitObservation>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  register(input: {
    routeId: string;
    failureThreshold?: number;
    successThreshold?: number;
    samplingWindowMs?: number;
    openDurationMs?: number;
    halfOpenMaximumRequests?: number;
    retryableErrors?: readonly string[];
    ignoredErrors?: readonly string[];
  }): { policy: ControlCircuitPolicy; circuit: ControlCircuit } {
    const policyPayload = {
      policyId: createId("control-circuit-policy"),
      routeId: input.routeId.trim(),
      failureThreshold: input.failureThreshold ?? 5,
      successThreshold: input.successThreshold ?? 2,
      samplingWindowMs: input.samplingWindowMs ?? 60_000,
      openDurationMs: input.openDurationMs ?? 30_000,
      halfOpenMaximumRequests: input.halfOpenMaximumRequests ?? 1,
      retryableErrors: [...new Set(input.retryableErrors ?? [])],
      ignoredErrors: [...new Set(input.ignoredErrors ?? [])],
    };
    const overlap = policyPayload.retryableErrors.filter((error) =>
      policyPayload.ignoredErrors.includes(error),
    );
    if (overlap.length)
      throw new E03RuntimeError(
        "control_circuit_policy_overlap",
        `control circuit errors are both retryable and ignored: ${overlap.join(", ")}`,
      );
    const policy = { ...policyPayload, digest: digest(policyPayload) };
    assertControlCircuitPolicy(policy);
    const circuitPayload = {
      circuitId: createId("control-circuit"),
      policyId: policy.policyId,
      routeId: policy.routeId,
      state: "closed" as const,
      consecutiveFailures: 0,
      consecutiveSuccesses: 0,
      halfOpenInFlight: 0,
      openedAt: null,
      halfOpenedAt: null,
      closedAt: this.clock.now(),
      lastFailureAt: null,
      lastSuccessAt: null,
      revision: 1,
    };
    const circuit = { ...circuitPayload, digest: digest(circuitPayload) };
    assertControlCircuit(circuit);
    this.policies.set(policy.policyId, policy);
    this.circuits.set(circuit.circuitId, circuit);
    return {
      policy: structuredClone(policy),
      circuit: structuredClone(circuit),
    };
  }

  acquire(circuitId: string, requestId: string): ControlCircuit {
    let circuit = this.requireCircuit(circuitId);
    const policy = this.requirePolicy(circuit.policyId);
    const now = this.clock.now();
    if (
      circuit.state === "open" &&
      circuit.openedAt !== null &&
      Date.parse(now) - Date.parse(circuit.openedAt) >= policy.openDurationMs
    )
      circuit = this.transition(circuit, {
        state: "half-open",
        halfOpenedAt: now,
        halfOpenInFlight: 0,
        consecutiveSuccesses: 0,
      });
    if (circuit.state === "open" || circuit.state === "disabled") {
      this.recordObservation(circuit, {
        requestId,
        outcome: "rejected",
        errorCode: "circuit_open",
        durationMs: 0,
      });
      throw new E03RuntimeError(
        "control_circuit_open",
        `control circuit ${circuitId} is ${circuit.state}`,
      );
    }
    if (
      circuit.state === "half-open" &&
      circuit.halfOpenInFlight >= policy.halfOpenMaximumRequests
    ) {
      this.recordObservation(circuit, {
        requestId,
        outcome: "rejected",
        errorCode: "half_open_capacity",
        durationMs: 0,
      });
      throw new E03RuntimeError(
        "control_circuit_half_open_capacity",
        `control circuit ${circuitId} reached half-open capacity`,
      );
    }
    if (circuit.state === "half-open")
      circuit = this.transition(circuit, {
        halfOpenInFlight: circuit.halfOpenInFlight + 1,
      });
    return structuredClone(circuit);
  }

  success(input: {
    circuitId: string;
    requestId: string;
    durationMs: number;
    expectedRevision: number;
  }): ControlCircuit {
    const circuit = this.requireCircuit(input.circuitId);
    this.assertRevision(circuit, input.expectedRevision);
    const policy = this.requirePolicy(circuit.policyId);
    let next = this.transition(circuit, {
      consecutiveFailures: 0,
      consecutiveSuccesses: circuit.consecutiveSuccesses + 1,
      halfOpenInFlight: Math.max(0, circuit.halfOpenInFlight - 1),
      lastSuccessAt: this.clock.now(),
    });
    if (
      next.state === "half-open" &&
      next.consecutiveSuccesses >= policy.successThreshold
    )
      next = this.transition(next, {
        state: "closed",
        consecutiveSuccesses: 0,
        closedAt: this.clock.now(),
        openedAt: null,
        halfOpenedAt: null,
      });
    this.recordObservation(next, {
      requestId: input.requestId,
      outcome: "success",
      errorCode: null,
      durationMs: input.durationMs,
    });
    return structuredClone(next);
  }

  failure(input: {
    circuitId: string;
    requestId: string;
    errorCode: string;
    durationMs: number;
    expectedRevision: number;
  }): ControlCircuit {
    const circuit = this.requireCircuit(input.circuitId);
    this.assertRevision(circuit, input.expectedRevision);
    const policy = this.requirePolicy(circuit.policyId);
    if (policy.ignoredErrors.includes(input.errorCode)) {
      this.recordObservation(circuit, {
        requestId: input.requestId,
        outcome: "ignored",
        errorCode: input.errorCode,
        durationMs: input.durationMs,
      });
      return structuredClone(circuit);
    }
    const countFailure =
      !policy.retryableErrors.length ||
      policy.retryableErrors.includes(input.errorCode);
    let next = this.transition(circuit, {
      consecutiveFailures: countFailure ? circuit.consecutiveFailures + 1 : 0,
      consecutiveSuccesses: 0,
      halfOpenInFlight: Math.max(0, circuit.halfOpenInFlight - 1),
      lastFailureAt: this.clock.now(),
    });
    if (
      next.state === "half-open" ||
      (countFailure && next.consecutiveFailures >= policy.failureThreshold)
    )
      next = this.transition(next, {
        state: "open",
        openedAt: this.clock.now(),
        halfOpenedAt: null,
        halfOpenInFlight: 0,
      });
    this.recordObservation(next, {
      requestId: input.requestId,
      outcome: "failure",
      errorCode: input.errorCode,
      durationMs: input.durationMs,
    });
    return structuredClone(next);
  }

  disable(circuitId: string): ControlCircuit {
    const circuit = this.requireCircuit(circuitId);
    return this.transition(circuit, {
      state: "disabled",
      halfOpenInFlight: 0,
    });
  }

  enable(circuitId: string): ControlCircuit {
    const circuit = this.requireCircuit(circuitId);
    if (circuit.state !== "disabled")
      throw new E03RuntimeError(
        "control_circuit_enable_state",
        `control circuit ${circuitId} is not disabled`,
      );
    return this.transition(circuit, {
      state: "closed",
      consecutiveFailures: 0,
      consecutiveSuccesses: 0,
      closedAt: this.clock.now(),
    });
  }

  observationsFor(
    circuitId: string,
    since?: string,
  ): ControlCircuitObservation[] {
    this.requireCircuit(circuitId);
    return [...this.observations.values()]
      .filter((observation) => observation.circuitId === circuitId)
      .filter((observation) => !since || observation.observedAt >= since)
      .sort((left, right) => left.observedAt.localeCompare(right.observedAt))
      .map((observation) => structuredClone(observation));
  }

  snapshot(): {
    policies: ControlCircuitPolicy[];
    circuits: ControlCircuit[];
    observations: ControlCircuitObservation[];
  } {
    return {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      circuits: [...this.circuits.values()].map((value) =>
        structuredClone(value),
      ),
      observations: [...this.observations.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    policies: readonly ControlCircuitPolicy[];
    circuits: readonly ControlCircuit[];
    observations: readonly ControlCircuitObservation[];
  }): void {
    const policies = new Map<string, ControlCircuitPolicy>();
    const circuits = new Map<string, ControlCircuit>();
    const observations = new Map<string, ControlCircuitObservation>();
    for (const policy of input.policies) {
      assertControlCircuitPolicy(policy);
      if (policies.has(policy.policyId))
        throw new E03RuntimeError(
          "control_circuit_policy_restore_duplicate",
          `duplicate control circuit policy ${policy.policyId}`,
        );
      policies.set(policy.policyId, structuredClone(policy));
    }
    for (const circuit of input.circuits) {
      assertControlCircuit(circuit);
      const policy = policies.get(circuit.policyId);
      if (!policy || policy.routeId !== circuit.routeId)
        throw new E03RuntimeError(
          "control_circuit_restore_policy",
          `control circuit ${circuit.circuitId} has invalid policy custody`,
        );
      if (circuits.has(circuit.circuitId))
        throw new E03RuntimeError(
          "control_circuit_restore_duplicate",
          `duplicate control circuit ${circuit.circuitId}`,
        );
      circuits.set(circuit.circuitId, structuredClone(circuit));
    }
    for (const observation of input.observations) {
      const { digest: checksum, ...payload } = observation;
      if (digest(payload) !== checksum)
        throw new E03RuntimeError(
          "control_circuit_observation_restore_digest",
          `control circuit observation ${observation.observationId} digest is invalid`,
        );
      if (!circuits.has(observation.circuitId))
        throw new E03RuntimeError(
          "control_circuit_observation_restore_circuit",
          `control circuit observation ${observation.observationId} has no circuit`,
        );
      if (observations.has(observation.observationId))
        throw new E03RuntimeError(
          "control_circuit_observation_restore_duplicate",
          `duplicate control circuit observation ${observation.observationId}`,
        );
      observations.set(observation.observationId, structuredClone(observation));
    }
    this.policies = policies;
    this.circuits = circuits;
    this.observations = observations;
  }

  private recordObservation(
    circuit: ControlCircuit,
    input: {
      requestId: string;
      outcome: ControlCircuitObservation["outcome"];
      errorCode: string | null;
      durationMs: number;
    },
  ): ControlCircuitObservation {
    if (!Number.isFinite(input.durationMs) || input.durationMs < 0)
      throw new E03RuntimeError(
        "control_circuit_observation_duration",
        "control circuit observation duration is invalid",
      );
    const payload = {
      observationId: createId("control-circuit-observation"),
      circuitId: circuit.circuitId,
      routeId: circuit.routeId,
      requestId: input.requestId,
      outcome: input.outcome,
      errorCode: input.errorCode,
      durationMs: input.durationMs,
      circuitRevision: circuit.revision,
      observedAt: this.clock.now(),
    };
    const observation = { ...payload, digest: digest(payload) };
    this.observations.set(observation.observationId, observation);
    return observation;
  }

  private requirePolicy(policyId: string): ControlCircuitPolicy {
    const policy = this.policies.get(policyId);
    if (!policy)
      throw new E03RuntimeError(
        "control_circuit_policy_missing",
        `control circuit policy ${policyId} does not exist`,
      );
    assertControlCircuitPolicy(policy);
    return policy;
  }

  private requireCircuit(circuitId: string): ControlCircuit {
    const circuit = this.circuits.get(circuitId);
    if (!circuit)
      throw new E03RuntimeError(
        "control_circuit_missing",
        `control circuit ${circuitId} does not exist`,
      );
    assertControlCircuit(circuit);
    return circuit;
  }

  private assertRevision(
    circuit: ControlCircuit,
    expectedRevision: number,
  ): void {
    if (circuit.revision !== expectedRevision)
      throw new E03RuntimeError(
        "control_circuit_stale_revision",
        `control circuit ${circuit.circuitId} revision is stale`,
      );
  }

  private transition(
    circuit: ControlCircuit,
    patch: Partial<Omit<ControlCircuit, "circuitId" | "revision" | "digest">>,
  ): ControlCircuit {
    const { digest: _, ...prior } = circuit;
    const payload = {
      ...prior,
      ...patch,
      circuitId: circuit.circuitId,
      revision: circuit.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlCircuit(next);
    this.circuits.set(next.circuitId, next);
    return structuredClone(next);
  }
}
