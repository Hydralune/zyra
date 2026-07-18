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

export interface ControlQuotaPolicy {
  policyId: string;
  principalId: string;
  commandPrefix: string;
  capacity: number;
  refillTokens: number;
  refillIntervalMs: number;
  maximumInFlight: number;
  queueLimit: number;
  enabled: boolean;
  revision: number;
  digest: string;
}
export interface ControlQuotaBucket {
  bucketId: string;
  policyId: string;
  principalId: string;
  commandPrefix: string;
  tokens: number;
  inFlight: number;
  queued: number;
  lastRefillAt: string;
  revision: number;
  digest: string;
}
export interface ControlQuotaPermit {
  permitId: string;
  bucketId: string;
  requestId: string;
  command: string;
  state: "queued" | "granted" | "settled" | "rejected" | "expired";
  tokenCost: number;
  queuedAt: string | null;
  grantedAt: string | null;
  expiresAt: string;
  settledAt: string | null;
  outcome: string | null;
  revision: number;
  digest: string;
}
function assertQuotaPolicy(value: ControlQuotaPolicy): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_quota_policy_digest",
      `control quota policy ${value.policyId} is corrupt`,
    );
  const numbers = [
    value.capacity,
    value.refillTokens,
    value.refillIntervalMs,
    value.maximumInFlight,
    value.queueLimit,
  ];
  if (
    !value.policyId ||
    !value.principalId ||
    !value.commandPrefix ||
    numbers.some((number) => !Number.isSafeInteger(number) || number < 1) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_quota_policy",
      `control quota policy ${value.policyId} is invalid`,
    );
}
function assertQuotaBucket(value: ControlQuotaBucket): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_quota_bucket_digest",
      `control quota bucket ${value.bucketId} is corrupt`,
    );
  if (
    !value.bucketId ||
    !value.policyId ||
    !value.principalId ||
    !Number.isFinite(value.tokens) ||
    value.tokens < 0 ||
    !Number.isSafeInteger(value.inFlight) ||
    value.inFlight < 0 ||
    !Number.isSafeInteger(value.queued) ||
    value.queued < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.lastRefillAt))
  )
    throw new E03RuntimeError(
      "control_quota_bucket",
      `control quota bucket ${value.bucketId} is invalid`,
    );
}
function assertQuotaPermit(value: ControlQuotaPermit): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_quota_permit_digest",
      `control quota permit ${value.permitId} is corrupt`,
    );
  if (
    !value.permitId ||
    !value.bucketId ||
    !value.requestId ||
    !value.command ||
    !Number.isSafeInteger(value.tokenCost) ||
    value.tokenCost < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "control_quota_permit",
      `control quota permit ${value.permitId} is invalid`,
    );
}
export class ControlTrafficQuotaRuntime {
  private policies = new Map<string, ControlQuotaPolicy>();
  private buckets = new Map<string, ControlQuotaBucket>();
  private permits = new Map<string, ControlQuotaPermit>();
  private requestIndex = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  register(
    input: Omit<ControlQuotaPolicy, "policyId" | "revision" | "digest">,
  ): ControlQuotaPolicy {
    const existing = [...this.policies.values()].find(
      (value) =>
        value.principalId === input.principalId &&
        value.commandPrefix === input.commandPrefix,
    );
    if (existing)
      throw new E03RuntimeError(
        "control_quota_policy_duplicate",
        `control quota policy for ${input.principalId}:${input.commandPrefix} already exists`,
      );
    const payload = {
      ...structuredClone(input),
      policyId: createId("control-quota-policy"),
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertQuotaPolicy(policy);
    this.policies.set(policy.policyId, policy);
    const bucketPayload = {
      bucketId: createId("control-quota-bucket"),
      policyId: policy.policyId,
      principalId: policy.principalId,
      commandPrefix: policy.commandPrefix,
      tokens: policy.capacity,
      inFlight: 0,
      queued: 0,
      lastRefillAt: this.clock.now(),
      revision: 1,
    };
    const bucket = { ...bucketPayload, digest: digest(bucketPayload) };
    assertQuotaBucket(bucket);
    this.buckets.set(bucket.bucketId, bucket);
    return structuredClone(policy);
  }
  update(
    policyId: string,
    expectedRevision: number,
    patch: Partial<
      Pick<
        ControlQuotaPolicy,
        | "capacity"
        | "refillTokens"
        | "refillIntervalMs"
        | "maximumInFlight"
        | "queueLimit"
        | "enabled"
      >
    >,
  ): ControlQuotaPolicy {
    const policy = this.requirePolicy(policyId);
    if (policy.revision !== expectedRevision)
      throw new E03RuntimeError(
        "control_quota_policy_stale_revision",
        `control quota policy ${policyId} revision is stale`,
      );
    const { digest: _, ...prior } = policy;
    const payload = {
      ...prior,
      ...patch,
      policyId: policy.policyId,
      revision: policy.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertQuotaPolicy(next);
    this.policies.set(next.policyId, next);
    const bucket = this.bucketForPolicy(next.policyId);
    if (bucket.tokens > next.capacity)
      this.transitionBucket(bucket, { tokens: next.capacity });
    return structuredClone(next);
  }
  acquire(input: {
    principalId: string;
    requestId: string;
    command: string;
    tokenCost?: number;
    ttlMs?: number;
    allowQueue?: boolean;
  }): { permit: ControlQuotaPermit; retryAfterMs: number } {
    const existingId = this.requestIndex.get(input.requestId);
    if (existingId) {
      const existing = this.requirePermit(existingId);
      if (existing.command !== input.command)
        throw new E03RuntimeError(
          "control_quota_request_conflict",
          `request ${input.requestId} reused for another command`,
        );
      return { permit: structuredClone(existing), retryAfterMs: 0 };
    }
    const match = [...this.policies.values()]
      .filter(
        (value) =>
          value.enabled &&
          value.principalId === input.principalId &&
          input.command.startsWith(value.commandPrefix),
      )
      .sort(
        (left, right) => right.commandPrefix.length - left.commandPrefix.length,
      )[0];
    if (!match)
      throw new E03RuntimeError(
        "control_quota_policy_missing",
        `no control quota policy for ${input.principalId}:${input.command}`,
      );
    let bucket = this.refill(this.bucketForPolicy(match.policyId), match);
    const tokenCost = input.tokenCost ?? 1;
    const ttlMs = input.ttlMs ?? match.refillIntervalMs;
    if (
      !Number.isSafeInteger(tokenCost) ||
      tokenCost < 1 ||
      tokenCost > match.capacity ||
      !Number.isSafeInteger(ttlMs) ||
      ttlMs < 1
    )
      throw new E03RuntimeError(
        "control_quota_request",
        "control quota request is invalid",
      );
    let state: ControlQuotaPermit["state"] = "granted";
    let queuedAt: string | null = null;
    let grantedAt: string | null = this.clock.now();
    let retryAfterMs = 0;
    if (bucket.tokens < tokenCost || bucket.inFlight >= match.maximumInFlight) {
      if (!input.allowQueue || bucket.queued >= match.queueLimit) {
        state = "rejected";
        grantedAt = null;
        retryAfterMs = Math.max(
          1,
          match.refillIntervalMs -
            (Date.parse(this.clock.now()) - Date.parse(bucket.lastRefillAt)),
        );
      } else {
        state = "queued";
        queuedAt = this.clock.now();
        grantedAt = null;
        retryAfterMs = Math.max(
          1,
          match.refillIntervalMs -
            (Date.parse(this.clock.now()) - Date.parse(bucket.lastRefillAt)),
        );
        bucket = this.transitionBucket(bucket, { queued: bucket.queued + 1 });
      }
    } else
      bucket = this.transitionBucket(bucket, {
        tokens: bucket.tokens - tokenCost,
        inFlight: bucket.inFlight + 1,
      });
    const payload = {
      permitId: createId("control-quota-permit"),
      bucketId: bucket.bucketId,
      requestId: input.requestId,
      command: input.command,
      state,
      tokenCost,
      queuedAt,
      grantedAt,
      expiresAt: new Date(Date.parse(this.clock.now()) + ttlMs).toISOString(),
      settledAt: state === "rejected" ? this.clock.now() : null,
      outcome: state === "rejected" ? "quota unavailable" : null,
      revision: 1,
    };
    const permit = { ...payload, digest: digest(payload) };
    assertQuotaPermit(permit);
    this.permits.set(permit.permitId, permit);
    this.requestIndex.set(permit.requestId, permit.permitId);
    return { permit: structuredClone(permit), retryAfterMs };
  }
  promote(permitId: string, expectedRevision: number): ControlQuotaPermit {
    const permit = this.requirePermit(permitId);
    this.assertPermitRevision(permit, expectedRevision);
    if (permit.state !== "queued")
      throw new E03RuntimeError(
        "control_quota_promote_state",
        `control quota permit ${permitId} is ${permit.state}`,
      );
    if (Date.parse(permit.expiresAt) <= Date.parse(this.clock.now()))
      return this.expire(permitId, expectedRevision);
    const bucket = this.bucketForId(permit.bucketId);
    const policy = this.requirePolicy(bucket.policyId);
    const refilled = this.refill(bucket, policy);
    if (
      refilled.tokens < permit.tokenCost ||
      refilled.inFlight >= policy.maximumInFlight
    )
      throw new E03RuntimeError(
        "control_quota_promote_unavailable",
        `control quota permit ${permitId} cannot be promoted`,
      );
    this.transitionBucket(refilled, {
      tokens: refilled.tokens - permit.tokenCost,
      inFlight: refilled.inFlight + 1,
      queued: Math.max(0, refilled.queued - 1),
    });
    return this.transitionPermit(permit, {
      state: "granted",
      grantedAt: this.clock.now(),
    });
  }
  settle(
    permitId: string,
    expectedRevision: number,
    outcome: string,
  ): ControlQuotaPermit {
    const permit = this.requirePermit(permitId);
    this.assertPermitRevision(permit, expectedRevision);
    if (
      permit.state === "settled" ||
      permit.state === "rejected" ||
      permit.state === "expired"
    )
      return structuredClone(permit);
    if (permit.state !== "granted")
      throw new E03RuntimeError(
        "control_quota_settle_state",
        `control quota permit ${permitId} is ${permit.state}`,
      );
    if (!outcome.trim())
      throw new E03RuntimeError(
        "control_quota_outcome",
        "control quota outcome is required",
      );
    const bucket = this.bucketForId(permit.bucketId);
    this.transitionBucket(bucket, {
      inFlight: Math.max(0, bucket.inFlight - 1),
    });
    return this.transitionPermit(permit, {
      state: "settled",
      settledAt: this.clock.now(),
      outcome: outcome.trim(),
    });
  }
  expire(permitId: string, expectedRevision: number): ControlQuotaPermit {
    const permit = this.requirePermit(permitId);
    this.assertPermitRevision(permit, expectedRevision);
    if (permit.state !== "queued" && permit.state !== "granted")
      return structuredClone(permit);
    const bucket = this.bucketForId(permit.bucketId);
    this.transitionBucket(
      bucket,
      permit.state === "queued"
        ? { queued: Math.max(0, bucket.queued - 1) }
        : { inFlight: Math.max(0, bucket.inFlight - 1) },
    );
    return this.transitionPermit(permit, {
      state: "expired",
      settledAt: this.clock.now(),
      outcome: "permit expired",
    });
  }
  sweep(now = this.clock.now()): ControlQuotaPermit[] {
    const timestamp = Date.parse(now);
    if (Number.isNaN(timestamp))
      throw new E03RuntimeError(
        "control_quota_sweep_time",
        "control quota sweep time is invalid",
      );
    const values: ControlQuotaPermit[] = [];
    for (const permit of this.permits.values())
      if (
        (permit.state === "queued" || permit.state === "granted") &&
        Date.parse(permit.expiresAt) <= timestamp
      )
        values.push(this.expire(permit.permitId, permit.revision));
    return values;
  }
  snapshot(): {
    policies: ControlQuotaPolicy[];
    buckets: ControlQuotaBucket[];
    permits: ControlQuotaPermit[];
  } {
    return {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      buckets: [...this.buckets.values()].map((value) =>
        structuredClone(value),
      ),
      permits: [...this.permits.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    policies: readonly ControlQuotaPolicy[];
    buckets: readonly ControlQuotaBucket[];
    permits: readonly ControlQuotaPermit[];
  }): void {
    const policies = new Map<string, ControlQuotaPolicy>();
    const buckets = new Map<string, ControlQuotaBucket>();
    const permits = new Map<string, ControlQuotaPermit>();
    const requestIndex = new Map<string, string>();
    for (const value of snapshot.policies) {
      assertQuotaPolicy(value);
      if (policies.has(value.policyId))
        throw new E03RuntimeError(
          "control_quota_policy_restore_duplicate",
          `duplicate control quota policy ${value.policyId}`,
        );
      policies.set(value.policyId, structuredClone(value));
    }
    for (const value of snapshot.buckets) {
      assertQuotaBucket(value);
      if (buckets.has(value.bucketId) || !policies.has(value.policyId))
        throw new E03RuntimeError(
          "control_quota_bucket_restore",
          `control quota bucket ${value.bucketId} is invalid`,
        );
      buckets.set(value.bucketId, structuredClone(value));
    }
    for (const value of snapshot.permits) {
      assertQuotaPermit(value);
      if (
        permits.has(value.permitId) ||
        requestIndex.has(value.requestId) ||
        !buckets.has(value.bucketId)
      )
        throw new E03RuntimeError(
          "control_quota_permit_restore",
          `control quota permit ${value.permitId} is invalid`,
        );
      permits.set(value.permitId, structuredClone(value));
      requestIndex.set(value.requestId, value.permitId);
    }
    this.policies = policies;
    this.buckets = buckets;
    this.permits = permits;
    this.requestIndex = requestIndex;
  }
  private refill(
    bucket: ControlQuotaBucket,
    policy: ControlQuotaPolicy,
  ): ControlQuotaBucket {
    const elapsed =
      Date.parse(this.clock.now()) - Date.parse(bucket.lastRefillAt);
    const intervals = Math.floor(elapsed / policy.refillIntervalMs);
    if (intervals <= 0) return bucket;
    return this.transitionBucket(bucket, {
      tokens: Math.min(
        policy.capacity,
        bucket.tokens + intervals * policy.refillTokens,
      ),
      lastRefillAt: new Date(
        Date.parse(bucket.lastRefillAt) + intervals * policy.refillIntervalMs,
      ).toISOString(),
    });
  }
  private requirePolicy(id: string): ControlQuotaPolicy {
    const value = this.policies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_quota_policy_missing",
        `control quota policy ${id} does not exist`,
      );
    assertQuotaPolicy(value);
    return value;
  }
  private bucketForPolicy(policyId: string): ControlQuotaBucket {
    const value = [...this.buckets.values()].find(
      (candidate) => candidate.policyId === policyId,
    );
    if (!value)
      throw new E03RuntimeError(
        "control_quota_bucket_missing",
        `control quota policy ${policyId} has no bucket`,
      );
    assertQuotaBucket(value);
    return value;
  }
  private bucketForId(id: string): ControlQuotaBucket {
    const value = this.buckets.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_quota_bucket_missing",
        `control quota bucket ${id} does not exist`,
      );
    assertQuotaBucket(value);
    return value;
  }
  private requirePermit(id: string): ControlQuotaPermit {
    const value = this.permits.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_quota_permit_missing",
        `control quota permit ${id} does not exist`,
      );
    assertQuotaPermit(value);
    return value;
  }
  private assertPermitRevision(
    value: ControlQuotaPermit,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_quota_permit_stale_revision",
        `control quota permit ${value.permitId} revision is stale`,
      );
  }
  private transitionBucket(
    value: ControlQuotaBucket,
    patch: Partial<
      Omit<ControlQuotaBucket, "bucketId" | "revision" | "digest">
    >,
  ): ControlQuotaBucket {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      bucketId: value.bucketId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertQuotaBucket(next);
    this.buckets.set(next.bucketId, next);
    return structuredClone(next);
  }
  private transitionPermit(
    value: ControlQuotaPermit,
    patch: Partial<
      Omit<ControlQuotaPermit, "permitId" | "revision" | "digest">
    >,
  ): ControlQuotaPermit {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      permitId: value.permitId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertQuotaPermit(next);
    this.permits.set(next.permitId, next);
    return structuredClone(next);
  }
}

export interface ControlRouteAffinity {
  affinityId: string;
  key: string;
  owner: ControlRouteOwner;
  routeId: string;
  state: "bound" | "draining" | "released";
  leaseCount: number;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}
function assertRouteAffinity(value: ControlRouteAffinity): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_route_affinity_digest",
      `control route affinity ${value.affinityId} is corrupt`,
    );
  if (
    !value.affinityId ||
    !value.key ||
    !value.routeId ||
    !Number.isSafeInteger(value.leaseCount) ||
    value.leaseCount < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_route_affinity",
      `control route affinity ${value.affinityId} is invalid`,
    );
}
export class ControlRouteAffinityRuntime {
  private affinities = new Map<string, ControlRouteAffinity>();
  private byKey = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  bind(input: {
    key: string;
    owner: ControlRouteOwner;
    routeId: string;
  }): ControlRouteAffinity {
    const existingId = this.byKey.get(input.key);
    if (existingId) {
      const existing = this.require(existingId);
      if (existing.owner !== input.owner || existing.routeId !== input.routeId)
        throw new E03RuntimeError(
          "control_route_affinity_conflict",
          `control route affinity ${input.key} already targets another route`,
        );
      return structuredClone(existing);
    }
    const payload = {
      affinityId: createId("control-route-affinity"),
      key: input.key.trim(),
      owner: input.owner,
      routeId: input.routeId.trim(),
      state: "bound" as const,
      leaseCount: 0,
      createdAt: this.clock.now(),
      updatedAt: this.clock.now(),
      revision: 1,
    };
    const affinity = { ...payload, digest: digest(payload) };
    assertRouteAffinity(affinity);
    this.affinities.set(affinity.affinityId, affinity);
    this.byKey.set(affinity.key, affinity.affinityId);
    return structuredClone(affinity);
  }
  acquire(key: string): ControlRouteAffinity {
    const id = this.byKey.get(key);
    if (!id)
      throw new E03RuntimeError(
        "control_route_affinity_missing",
        `control route affinity ${key} does not exist`,
      );
    const affinity = this.require(id);
    if (affinity.state !== "bound")
      throw new E03RuntimeError(
        "control_route_affinity_acquire_state",
        `control route affinity ${key} is ${affinity.state}`,
      );
    return this.transition(affinity, { leaseCount: affinity.leaseCount + 1 });
  }
  releaseLease(
    affinityId: string,
    expectedRevision: number,
  ): ControlRouteAffinity {
    const affinity = this.require(affinityId);
    this.assertRevision(affinity, expectedRevision);
    if (affinity.leaseCount < 1)
      throw new E03RuntimeError(
        "control_route_affinity_lease_underflow",
        `control route affinity ${affinityId} has no lease`,
      );
    return this.transition(affinity, { leaseCount: affinity.leaseCount - 1 });
  }
  drain(affinityId: string, expectedRevision: number): ControlRouteAffinity {
    const affinity = this.require(affinityId);
    this.assertRevision(affinity, expectedRevision);
    if (affinity.state !== "bound")
      throw new E03RuntimeError(
        "control_route_affinity_drain_state",
        `control route affinity ${affinityId} is ${affinity.state}`,
      );
    return this.transition(affinity, { state: "draining" });
  }
  release(affinityId: string, expectedRevision: number): ControlRouteAffinity {
    const affinity = this.require(affinityId);
    this.assertRevision(affinity, expectedRevision);
    if (affinity.state !== "draining" || affinity.leaseCount > 0)
      throw new E03RuntimeError(
        "control_route_affinity_release_state",
        `control route affinity ${affinityId} cannot release`,
      );
    const next = this.transition(affinity, { state: "released" });
    this.byKey.delete(next.key);
    return next;
  }
  resolve(key: string): ControlRouteAffinity | null {
    const id = this.byKey.get(key);
    if (!id) return null;
    const affinity = this.require(id);
    return affinity.state === "released" ? null : structuredClone(affinity);
  }
  snapshot(): ControlRouteAffinity[] {
    return [...this.affinities.values()].map((value) => structuredClone(value));
  }
  restore(values: readonly ControlRouteAffinity[]): void {
    const affinities = new Map<string, ControlRouteAffinity>();
    const byKey = new Map<string, string>();
    for (const value of values) {
      assertRouteAffinity(value);
      if (
        affinities.has(value.affinityId) ||
        (value.state !== "released" && byKey.has(value.key))
      )
        throw new E03RuntimeError(
          "control_route_affinity_restore_duplicate",
          `duplicate control route affinity ${value.affinityId}`,
        );
      affinities.set(value.affinityId, structuredClone(value));
      if (value.state !== "released") byKey.set(value.key, value.affinityId);
    }
    this.affinities = affinities;
    this.byKey = byKey;
  }
  private require(id: string): ControlRouteAffinity {
    const value = this.affinities.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_route_affinity_missing",
        `control route affinity ${id} does not exist`,
      );
    assertRouteAffinity(value);
    return value;
  }
  private assertRevision(value: ControlRouteAffinity, expected: number): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_route_affinity_stale_revision",
        `control route affinity ${value.affinityId} revision is stale`,
      );
  }
  private transition(
    value: ControlRouteAffinity,
    patch: Partial<
      Omit<ControlRouteAffinity, "affinityId" | "revision" | "digest">
    >,
  ): ControlRouteAffinity {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      affinityId: value.affinityId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRouteAffinity(next);
    this.affinities.set(next.affinityId, next);
    return structuredClone(next);
  }
}

export type ControlRouteRolloutState =
  | "draft"
  | "probing"
  | "canary"
  | "promoted"
  | "draining"
  | "rolled_back";

export interface ControlRouteEndpoint {
  endpointId: string;
  routeId: string;
  generation: number;
  address: string;
  capabilities: string[];
  weight: number;
  maxInflight: number;
  inflight: number;
  healthy: boolean;
  draining: boolean;
  consecutiveFailures: number;
  lastProbeAt: string;
  lastFailure: string;
  revision: number;
  digest: string;
}

export interface ControlRouteProbe {
  probeId: string;
  endpointId: string;
  generation: number;
  startedAt: string;
  completedAt: string;
  latencyMs: number;
  accepted: boolean;
  capabilityDigest: string;
  errorCode: string;
  previousDigest: string;
  digest: string;
}

export interface ControlRouteRollout {
  rolloutId: string;
  routeId: string;
  fromGeneration: number;
  toGeneration: number;
  state: ControlRouteRolloutState;
  canaryPercent: number;
  minimumHealthyEndpoints: number;
  maximumFailureRatio: number;
  requiredProbeCount: number;
  observedProbeCount: number;
  observedFailureCount: number;
  createdAt: string;
  updatedAt: string;
  promotedAt: string;
  rollbackReason: string;
  revision: number;
  digest: string;
}

export interface ControlRouteAssignment {
  assignmentId: string;
  rolloutId: string;
  requestId: string;
  endpointId: string;
  generation: number;
  capability: string;
  acquiredAt: string;
  releasedAt: string;
  outcome: "pending" | "succeeded" | "failed" | "cancelled";
  latencyMs: number;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface ControlRouteRolloutSnapshot {
  endpoints: ControlRouteEndpoint[];
  probes: ControlRouteProbe[];
  rollouts: ControlRouteRollout[];
  assignments: ControlRouteAssignment[];
  activeRolloutByRoute: [string, string][];
  requestAssignments: [string, string][];
}

function assertRouteEndpoint(value: ControlRouteEndpoint): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.endpointId ||
    !value.routeId ||
    !value.address ||
    !Number.isSafeInteger(value.generation) ||
    value.generation < 1 ||
    !Number.isFinite(value.weight) ||
    value.weight < 0 ||
    !Number.isSafeInteger(value.maxInflight) ||
    value.maxInflight < 1 ||
    !Number.isSafeInteger(value.inflight) ||
    value.inflight < 0 ||
    value.inflight > value.maxInflight ||
    !Number.isSafeInteger(value.consecutiveFailures) ||
    value.consecutiveFailures < 0 ||
    value.revision < 1 ||
    digest(payload) !== expected
  ) {
    throw new E03RuntimeError(
      "control_route_endpoint_corrupt",
      `control route endpoint ${value.endpointId || "<empty>"} is corrupt`,
    );
  }
  if (new Set(value.capabilities).size !== value.capabilities.length)
    throw new E03RuntimeError(
      "control_route_endpoint_capability_duplicate",
      `control route endpoint ${value.endpointId} has duplicate capabilities`,
    );
}

function assertRouteProbe(value: ControlRouteProbe): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.probeId ||
    !value.endpointId ||
    value.generation < 1 ||
    value.latencyMs < 0 ||
    !Number.isFinite(value.latencyMs) ||
    !value.startedAt ||
    !value.completedAt ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_route_probe_corrupt",
      `control route probe ${value.probeId || "<empty>"} is corrupt`,
    );
}

function assertRouteRollout(value: ControlRouteRollout): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.rolloutId ||
    !value.routeId ||
    value.fromGeneration < 0 ||
    value.toGeneration < 1 ||
    value.toGeneration <= value.fromGeneration ||
    value.canaryPercent < 0 ||
    value.canaryPercent > 100 ||
    value.minimumHealthyEndpoints < 1 ||
    value.maximumFailureRatio < 0 ||
    value.maximumFailureRatio > 1 ||
    value.requiredProbeCount < 1 ||
    value.observedProbeCount < value.observedFailureCount ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_route_rollout_corrupt",
      `control route rollout ${value.rolloutId || "<empty>"} is corrupt`,
    );
}

function assertRouteAssignment(value: ControlRouteAssignment): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.assignmentId ||
    !value.rolloutId ||
    !value.requestId ||
    !value.endpointId ||
    value.generation < 1 ||
    value.latencyMs < 0 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_route_assignment_corrupt",
      `control route assignment ${value.assignmentId || "<empty>"} is corrupt`,
    );
}

export class ControlRouteRolloutRuntime {
  private endpoints = new Map<string, ControlRouteEndpoint>();
  private probes = new Map<string, ControlRouteProbe[]>();
  private rollouts = new Map<string, ControlRouteRollout>();
  private assignments = new Map<string, ControlRouteAssignment>();
  private activeRolloutByRoute = new Map<string, string>();
  private requestAssignments = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registerEndpoint(input: {
    endpointId?: string;
    routeId: string;
    generation: number;
    address: string;
    capabilities: readonly string[];
    weight: number;
    maxInflight: number;
  }): ControlRouteEndpoint {
    if (!input.routeId || !input.address)
      throw new E03RuntimeError(
        "control_route_endpoint_identity",
        "control route endpoint requires route and address",
      );
    if (input.generation < 1 || !Number.isSafeInteger(input.generation))
      throw new E03RuntimeError(
        "control_route_endpoint_generation",
        "control route endpoint generation must be positive",
      );
    if (input.weight < 0 || !Number.isFinite(input.weight))
      throw new E03RuntimeError(
        "control_route_endpoint_weight",
        "control route endpoint weight must be finite and non-negative",
      );
    if (input.maxInflight < 1 || !Number.isSafeInteger(input.maxInflight))
      throw new E03RuntimeError(
        "control_route_endpoint_capacity",
        "control route endpoint capacity must be positive",
      );
    const endpointId = input.endpointId ?? createId("control-route-endpoint");
    if (this.endpoints.has(endpointId))
      throw new E03RuntimeError(
        "control_route_endpoint_duplicate",
        `control route endpoint ${endpointId} already exists`,
      );
    if (
      [...this.endpoints.values()].some(
        (value) =>
          value.routeId === input.routeId &&
          value.generation === input.generation &&
          value.address === input.address,
      )
    )
      throw new E03RuntimeError(
        "control_route_endpoint_address_duplicate",
        `control route endpoint address ${input.address} already exists`,
      );
    const capabilities = [...new Set(input.capabilities)].sort();
    if (!capabilities.length)
      throw new E03RuntimeError(
        "control_route_endpoint_capability_empty",
        "control route endpoint requires a capability",
      );
    const payload = {
      endpointId,
      routeId: input.routeId,
      generation: input.generation,
      address: input.address,
      capabilities,
      weight: input.weight,
      maxInflight: input.maxInflight,
      inflight: 0,
      healthy: false,
      draining: false,
      consecutiveFailures: 0,
      lastProbeAt: "",
      lastFailure: "",
      revision: 1,
    };
    const endpoint = { ...payload, digest: digest(payload) };
    assertRouteEndpoint(endpoint);
    this.endpoints.set(endpointId, endpoint);
    return structuredClone(endpoint);
  }

  beginRollout(input: {
    rolloutId?: string;
    routeId: string;
    fromGeneration: number;
    toGeneration: number;
    canaryPercent: number;
    minimumHealthyEndpoints: number;
    maximumFailureRatio: number;
    requiredProbeCount: number;
  }): ControlRouteRollout {
    if (this.activeRolloutByRoute.has(input.routeId))
      throw new E03RuntimeError(
        "control_route_rollout_active",
        `route ${input.routeId} already has an active rollout`,
      );
    if (input.toGeneration <= input.fromGeneration)
      throw new E03RuntimeError(
        "control_route_rollout_generation",
        "control route rollout must advance generation",
      );
    if (input.canaryPercent < 1 || input.canaryPercent > 100)
      throw new E03RuntimeError(
        "control_route_rollout_canary",
        "control route rollout canary percentage is invalid",
      );
    const candidates = [...this.endpoints.values()].filter(
      (value) =>
        value.routeId === input.routeId &&
        value.generation === input.toGeneration &&
        !value.draining,
    );
    if (candidates.length < input.minimumHealthyEndpoints)
      throw new E03RuntimeError(
        "control_route_rollout_endpoint_shortage",
        `route ${input.routeId} has too few candidate endpoints`,
      );
    const rolloutId = input.rolloutId ?? createId("control-route-rollout");
    if (this.rollouts.has(rolloutId))
      throw new E03RuntimeError(
        "control_route_rollout_duplicate",
        `control route rollout ${rolloutId} already exists`,
      );
    const now = this.clock.now();
    const payload = {
      rolloutId,
      routeId: input.routeId,
      fromGeneration: input.fromGeneration,
      toGeneration: input.toGeneration,
      state: "probing" as const,
      canaryPercent: input.canaryPercent,
      minimumHealthyEndpoints: input.minimumHealthyEndpoints,
      maximumFailureRatio: input.maximumFailureRatio,
      requiredProbeCount: input.requiredProbeCount,
      observedProbeCount: 0,
      observedFailureCount: 0,
      createdAt: now,
      updatedAt: now,
      promotedAt: "",
      rollbackReason: "",
      revision: 1,
    };
    const rollout = { ...payload, digest: digest(payload) };
    assertRouteRollout(rollout);
    this.rollouts.set(rolloutId, rollout);
    this.activeRolloutByRoute.set(input.routeId, rolloutId);
    return structuredClone(rollout);
  }

  recordProbe(input: {
    probeId?: string;
    endpointId: string;
    startedAt: string;
    latencyMs: number;
    accepted: boolean;
    capabilityDigest: string;
    errorCode?: string;
  }): ControlRouteProbe {
    const endpoint = this.requireEndpoint(input.endpointId);
    if (input.latencyMs < 0 || !Number.isFinite(input.latencyMs))
      throw new E03RuntimeError(
        "control_route_probe_latency",
        "control route probe latency is invalid",
      );
    const probeId = input.probeId ?? createId("control-route-probe");
    if (
      [...this.probes.values()].some((entries) =>
        entries.some((entry) => entry.probeId === probeId),
      )
    )
      throw new E03RuntimeError(
        "control_route_probe_duplicate",
        `control route probe ${probeId} already exists`,
      );
    const entries = this.probes.get(endpoint.endpointId) ?? [];
    const previousDigest = entries.at(-1)?.digest ?? "";
    const payload = {
      probeId,
      endpointId: endpoint.endpointId,
      generation: endpoint.generation,
      startedAt: input.startedAt,
      completedAt: this.clock.now(),
      latencyMs: input.latencyMs,
      accepted: input.accepted,
      capabilityDigest: input.capabilityDigest,
      errorCode: input.errorCode ?? "",
      previousDigest,
    };
    const probe = { ...payload, digest: digest(payload) };
    assertRouteProbe(probe);
    entries.push(probe);
    this.probes.set(endpoint.endpointId, entries);
    this.transitionEndpoint(endpoint, {
      healthy: input.accepted,
      consecutiveFailures: input.accepted
        ? 0
        : endpoint.consecutiveFailures + 1,
      lastProbeAt: probe.completedAt,
      lastFailure: input.accepted ? "" : probe.errorCode || "probe_failed",
    });
    const rolloutId = this.activeRolloutByRoute.get(endpoint.routeId);
    if (rolloutId) {
      const rollout = this.requireRollout(rolloutId);
      if (
        rollout.state === "probing" &&
        rollout.toGeneration === endpoint.generation
      )
        this.transitionRollout(rollout, {
          observedProbeCount: rollout.observedProbeCount + 1,
          observedFailureCount:
            rollout.observedFailureCount + (input.accepted ? 0 : 1),
        });
    }
    return structuredClone(probe);
  }

  enterCanary(
    rolloutId: string,
    expectedRevision: number,
  ): ControlRouteRollout {
    const rollout = this.requireRollout(rolloutId);
    this.assertRolloutRevision(rollout, expectedRevision);
    if (rollout.state !== "probing")
      throw new E03RuntimeError(
        "control_route_rollout_canary_state",
        `control route rollout ${rolloutId} is ${rollout.state}`,
      );
    if (rollout.observedProbeCount < rollout.requiredProbeCount)
      throw new E03RuntimeError(
        "control_route_rollout_probe_shortage",
        `control route rollout ${rolloutId} lacks probes`,
      );
    const failureRatio =
      rollout.observedFailureCount / rollout.observedProbeCount;
    if (failureRatio > rollout.maximumFailureRatio)
      throw new E03RuntimeError(
        "control_route_rollout_probe_failure_ratio",
        `control route rollout ${rolloutId} exceeds failure ratio`,
      );
    const healthy = [...this.endpoints.values()].filter(
      (value) =>
        value.routeId === rollout.routeId &&
        value.generation === rollout.toGeneration &&
        value.healthy &&
        !value.draining,
    );
    if (healthy.length < rollout.minimumHealthyEndpoints)
      throw new E03RuntimeError(
        "control_route_rollout_healthy_shortage",
        `control route rollout ${rolloutId} lacks healthy endpoints`,
      );
    return this.transitionRollout(rollout, { state: "canary" });
  }

  promote(rolloutId: string, expectedRevision: number): ControlRouteRollout {
    const rollout = this.requireRollout(rolloutId);
    this.assertRolloutRevision(rollout, expectedRevision);
    if (rollout.state !== "canary")
      throw new E03RuntimeError(
        "control_route_rollout_promote_state",
        `control route rollout ${rolloutId} is ${rollout.state}`,
      );
    const assignments = [...this.assignments.values()].filter(
      (value) =>
        value.rolloutId === rolloutId &&
        value.generation === rollout.toGeneration,
    );
    const finished = assignments.filter((value) => value.outcome !== "pending");
    const failures = finished.filter((value) => value.outcome === "failed");
    if (!finished.length)
      throw new E03RuntimeError(
        "control_route_rollout_canary_empty",
        `control route rollout ${rolloutId} has no canary outcomes`,
      );
    if (failures.length / finished.length > rollout.maximumFailureRatio)
      throw new E03RuntimeError(
        "control_route_rollout_canary_failure_ratio",
        `control route rollout ${rolloutId} canary failed`,
      );
    const next = this.transitionRollout(rollout, {
      state: "promoted",
      canaryPercent: 100,
      promotedAt: this.clock.now(),
    });
    for (const endpoint of [...this.endpoints.values()])
      if (
        endpoint.routeId === rollout.routeId &&
        endpoint.generation === rollout.fromGeneration
      )
        this.transitionEndpoint(endpoint, { draining: true });
    return next;
  }

  rollback(
    rolloutId: string,
    expectedRevision: number,
    reason: string,
  ): ControlRouteRollout {
    const rollout = this.requireRollout(rolloutId);
    this.assertRolloutRevision(rollout, expectedRevision);
    if (!["probing", "canary", "promoted"].includes(rollout.state))
      throw new E03RuntimeError(
        "control_route_rollout_rollback_state",
        `control route rollout ${rolloutId} cannot roll back`,
      );
    if (!reason)
      throw new E03RuntimeError(
        "control_route_rollout_rollback_reason",
        "control route rollout rollback requires a reason",
      );
    for (const endpoint of [...this.endpoints.values()]) {
      if (
        endpoint.routeId === rollout.routeId &&
        endpoint.generation === rollout.fromGeneration
      )
        this.transitionEndpoint(endpoint, { draining: false });
      if (
        endpoint.routeId === rollout.routeId &&
        endpoint.generation === rollout.toGeneration
      )
        this.transitionEndpoint(endpoint, { draining: true });
    }
    const next = this.transitionRollout(rollout, {
      state: "rolled_back",
      rollbackReason: reason,
    });
    this.activeRolloutByRoute.delete(rollout.routeId);
    return next;
  }

  assign(input: {
    assignmentId?: string;
    routeId: string;
    requestId: string;
    capability: string;
  }): ControlRouteAssignment {
    const duplicateId = this.requestAssignments.get(input.requestId);
    if (duplicateId)
      return structuredClone(this.requireAssignment(duplicateId));
    const rolloutId = this.activeRolloutByRoute.get(input.routeId);
    if (!rolloutId)
      throw new E03RuntimeError(
        "control_route_rollout_missing_active",
        `route ${input.routeId} has no active rollout`,
      );
    const rollout = this.requireRollout(rolloutId);
    if (!["canary", "promoted"].includes(rollout.state))
      throw new E03RuntimeError(
        "control_route_rollout_assignment_state",
        `control route rollout ${rolloutId} cannot assign`,
      );
    const useCanary =
      rollout.state === "promoted" ||
      this.bucket(input.requestId) < rollout.canaryPercent;
    const generation = useCanary
      ? rollout.toGeneration
      : rollout.fromGeneration;
    const candidates = [...this.endpoints.values()]
      .filter(
        (value) =>
          value.routeId === input.routeId &&
          value.generation === generation &&
          value.healthy &&
          !value.draining &&
          value.inflight < value.maxInflight &&
          value.capabilities.includes(input.capability),
      )
      .sort((left, right) => {
        const leftLoad = left.inflight / left.maxInflight;
        const rightLoad = right.inflight / right.maxInflight;
        return (
          leftLoad - rightLoad ||
          right.weight - left.weight ||
          left.endpointId.localeCompare(right.endpointId)
        );
      });
    const endpoint = candidates[0];
    if (!endpoint)
      throw new E03RuntimeError(
        "control_route_rollout_capacity_unavailable",
        `route ${input.routeId} has no endpoint capacity`,
      );
    const assignmentId =
      input.assignmentId ?? createId("control-route-assignment");
    if (this.assignments.has(assignmentId))
      throw new E03RuntimeError(
        "control_route_assignment_duplicate",
        `control route assignment ${assignmentId} already exists`,
      );
    const payload = {
      assignmentId,
      rolloutId,
      requestId: input.requestId,
      endpointId: endpoint.endpointId,
      generation,
      capability: input.capability,
      acquiredAt: this.clock.now(),
      releasedAt: "",
      outcome: "pending" as const,
      latencyMs: 0,
      errorCode: "",
      revision: 1,
    };
    const assignment = { ...payload, digest: digest(payload) };
    assertRouteAssignment(assignment);
    this.assignments.set(assignmentId, assignment);
    this.requestAssignments.set(input.requestId, assignmentId);
    this.transitionEndpoint(endpoint, { inflight: endpoint.inflight + 1 });
    return structuredClone(assignment);
  }

  settle(
    assignmentId: string,
    expectedRevision: number,
    input: {
      outcome: Exclude<ControlRouteAssignment["outcome"], "pending">;
      latencyMs: number;
      errorCode?: string;
    },
  ): ControlRouteAssignment {
    const assignment = this.requireAssignment(assignmentId);
    this.assertAssignmentRevision(assignment, expectedRevision);
    if (assignment.outcome !== "pending")
      throw new E03RuntimeError(
        "control_route_assignment_terminal",
        `control route assignment ${assignmentId} is terminal`,
      );
    if (input.latencyMs < 0 || !Number.isFinite(input.latencyMs))
      throw new E03RuntimeError(
        "control_route_assignment_latency",
        "control route assignment latency is invalid",
      );
    const endpoint = this.requireEndpoint(assignment.endpointId);
    if (endpoint.inflight < 1)
      throw new E03RuntimeError(
        "control_route_assignment_inflight_underflow",
        `control route endpoint ${endpoint.endpointId} has no inflight request`,
      );
    const next = this.transitionAssignment(assignment, {
      outcome: input.outcome,
      latencyMs: input.latencyMs,
      errorCode: input.errorCode ?? "",
      releasedAt: this.clock.now(),
    });
    this.transitionEndpoint(endpoint, {
      inflight: endpoint.inflight - 1,
      healthy: input.outcome === "failed" ? endpoint.healthy : true,
      consecutiveFailures:
        input.outcome === "failed" ? endpoint.consecutiveFailures + 1 : 0,
      lastFailure:
        input.outcome === "failed" ? (input.errorCode ?? "request_failed") : "",
    });
    return next;
  }

  snapshot(): ControlRouteRolloutSnapshot {
    return {
      endpoints: [...this.endpoints.values()].map((value) =>
        structuredClone(value),
      ),
      probes: [...this.probes.values()]
        .flat()
        .map((value) => structuredClone(value)),
      rollouts: [...this.rollouts.values()].map((value) =>
        structuredClone(value),
      ),
      assignments: [...this.assignments.values()].map((value) =>
        structuredClone(value),
      ),
      activeRolloutByRoute: [...this.activeRolloutByRoute.entries()],
      requestAssignments: [...this.requestAssignments.entries()],
    };
  }

  restore(snapshot: ControlRouteRolloutSnapshot): void {
    const endpoints = new Map<string, ControlRouteEndpoint>();
    const probes = new Map<string, ControlRouteProbe[]>();
    const rollouts = new Map<string, ControlRouteRollout>();
    const assignments = new Map<string, ControlRouteAssignment>();
    const activeRolloutByRoute = new Map<string, string>();
    const requestAssignments = new Map<string, string>();
    for (const value of snapshot.endpoints) {
      assertRouteEndpoint(value);
      if (endpoints.has(value.endpointId))
        throw new E03RuntimeError(
          "control_route_endpoint_restore_duplicate",
          `duplicate control route endpoint ${value.endpointId}`,
        );
      endpoints.set(value.endpointId, structuredClone(value));
    }
    for (const value of snapshot.probes) {
      assertRouteProbe(value);
      const endpoint = endpoints.get(value.endpointId);
      if (!endpoint || endpoint.generation !== value.generation)
        throw new E03RuntimeError(
          "control_route_probe_restore_endpoint",
          `control route probe ${value.probeId} has no endpoint`,
        );
      const entries = probes.get(value.endpointId) ?? [];
      const expectedPrevious = entries.at(-1)?.digest ?? "";
      if (value.previousDigest !== expectedPrevious)
        throw new E03RuntimeError(
          "control_route_probe_restore_chain",
          `control route probe ${value.probeId} breaks chain`,
        );
      if (entries.some((entry) => entry.probeId === value.probeId))
        throw new E03RuntimeError(
          "control_route_probe_restore_duplicate",
          `duplicate control route probe ${value.probeId}`,
        );
      entries.push(structuredClone(value));
      probes.set(value.endpointId, entries);
    }
    for (const value of snapshot.rollouts) {
      assertRouteRollout(value);
      if (rollouts.has(value.rolloutId))
        throw new E03RuntimeError(
          "control_route_rollout_restore_duplicate",
          `duplicate control route rollout ${value.rolloutId}`,
        );
      rollouts.set(value.rolloutId, structuredClone(value));
    }
    for (const value of snapshot.assignments) {
      assertRouteAssignment(value);
      const rollout = rollouts.get(value.rolloutId);
      const endpoint = endpoints.get(value.endpointId);
      if (!rollout || !endpoint || endpoint.generation !== value.generation)
        throw new E03RuntimeError(
          "control_route_assignment_restore_custody",
          `control route assignment ${value.assignmentId} has invalid custody`,
        );
      if (assignments.has(value.assignmentId))
        throw new E03RuntimeError(
          "control_route_assignment_restore_duplicate",
          `duplicate control route assignment ${value.assignmentId}`,
        );
      assignments.set(value.assignmentId, structuredClone(value));
    }
    for (const [routeId, rolloutId] of snapshot.activeRolloutByRoute) {
      const rollout = rollouts.get(rolloutId);
      if (
        !rollout ||
        rollout.routeId !== routeId ||
        ["rolled_back", "draining"].includes(rollout.state) ||
        activeRolloutByRoute.has(routeId)
      )
        throw new E03RuntimeError(
          "control_route_rollout_restore_active",
          `active control route rollout ${rolloutId} is invalid`,
        );
      activeRolloutByRoute.set(routeId, rolloutId);
    }
    for (const [requestId, assignmentId] of snapshot.requestAssignments) {
      const assignment = assignments.get(assignmentId);
      if (
        !assignment ||
        assignment.requestId !== requestId ||
        requestAssignments.has(requestId)
      )
        throw new E03RuntimeError(
          "control_route_assignment_restore_index",
          `control route assignment request index ${requestId} is invalid`,
        );
      requestAssignments.set(requestId, assignmentId);
    }
    for (const endpoint of endpoints.values()) {
      const pending = [...assignments.values()].filter(
        (value) =>
          value.endpointId === endpoint.endpointId &&
          value.outcome === "pending",
      ).length;
      if (pending !== endpoint.inflight)
        throw new E03RuntimeError(
          "control_route_endpoint_restore_inflight",
          `control route endpoint ${endpoint.endpointId} inflight is inconsistent`,
        );
    }
    this.endpoints = endpoints;
    this.probes = probes;
    this.rollouts = rollouts;
    this.assignments = assignments;
    this.activeRolloutByRoute = activeRolloutByRoute;
    this.requestAssignments = requestAssignments;
  }

  private bucket(requestId: string): number {
    const hex = digest({ requestId, domain: "control-route-rollout" }).slice(
      0,
      8,
    );
    return Number.parseInt(hex, 16) % 100;
  }

  private requireEndpoint(id: string): ControlRouteEndpoint {
    const value = this.endpoints.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_route_endpoint_missing",
        `control route endpoint ${id} does not exist`,
      );
    assertRouteEndpoint(value);
    return value;
  }

  private requireRollout(id: string): ControlRouteRollout {
    const value = this.rollouts.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_route_rollout_missing",
        `control route rollout ${id} does not exist`,
      );
    assertRouteRollout(value);
    return value;
  }

  private requireAssignment(id: string): ControlRouteAssignment {
    const value = this.assignments.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_route_assignment_missing",
        `control route assignment ${id} does not exist`,
      );
    assertRouteAssignment(value);
    return value;
  }

  private assertRolloutRevision(
    value: ControlRouteRollout,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_route_rollout_stale_revision",
        `control route rollout ${value.rolloutId} revision is stale`,
      );
  }

  private assertAssignmentRevision(
    value: ControlRouteAssignment,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_route_assignment_stale_revision",
        `control route assignment ${value.assignmentId} revision is stale`,
      );
  }

  private transitionEndpoint(
    value: ControlRouteEndpoint,
    patch: Partial<
      Omit<ControlRouteEndpoint, "endpointId" | "revision" | "digest">
    >,
  ): ControlRouteEndpoint {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      endpointId: value.endpointId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRouteEndpoint(next);
    this.endpoints.set(next.endpointId, next);
    return structuredClone(next);
  }

  private transitionRollout(
    value: ControlRouteRollout,
    patch: Partial<
      Omit<ControlRouteRollout, "rolloutId" | "revision" | "digest">
    >,
  ): ControlRouteRollout {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      rolloutId: value.rolloutId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRouteRollout(next);
    this.rollouts.set(next.rolloutId, next);
    return structuredClone(next);
  }

  private transitionAssignment(
    value: ControlRouteAssignment,
    patch: Partial<
      Omit<ControlRouteAssignment, "assignmentId" | "revision" | "digest">
    >,
  ): ControlRouteAssignment {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      assignmentId: value.assignmentId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRouteAssignment(next);
    this.assignments.set(next.assignmentId, next);
    return structuredClone(next);
  }
}
