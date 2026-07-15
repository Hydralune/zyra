import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  type JsonValue,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  canonicalJson,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
  uniqueSorted,
  withTimeout,
} from "../core/runtime-primitives.js";

export const HOOK_PHASES = [
  "query.accept",
  "context.before_build",
  "context.after_build",
  "model.before_request",
  "model.after_response",
  "tool.before_permission",
  "tool.after_permission",
  "tool.before_execute",
  "tool.after_execute",
  "compact.before",
  "compact.after",
  "query.before_stop",
  "query.after_stop",
] as const;

export type HookPhase = (typeof HOOK_PHASES)[number];
export type HookFailureMode = "fail_open" | "fail_closed" | "quarantine";
export type HookDecisionKind = "continue" | "block" | "retry" | "stop";
export type PatchOperationKind = "add" | "replace" | "remove" | "test";

export interface RuntimeHookDescriptor {
  hookId: string;
  phase: HookPhase;
  priority: number;
  timeoutMilliseconds: number;
  failureMode: HookFailureMode;
  enabled: boolean;
  before: string[];
  after: string[];
  owner: string;
  revision: number;
  metadata: JsonRecord;
}

export interface HookInvocationContext {
  invocationId: string;
  sessionId: string;
  runId: string;
  queryId: string;
  turnId: string | null;
  phase: HookPhase;
  attempt: number;
  startedAt: number;
  deadlineAt: number;
  correlationId: string;
  payload: JsonRecord;
  accumulated: JsonRecord;
}

export interface HookPatchOperation {
  operation: PatchOperationKind;
  path: string;
  value?: JsonValue;
}

export interface HookDecision {
  kind: HookDecisionKind;
  reason: string;
  patches?: HookPatchOperation[];
  retryAfterMilliseconds?: number;
  metadata?: JsonRecord;
}

export type RuntimeHookHandler = (
  context: Readonly<HookInvocationContext>,
) => Promise<HookDecision> | HookDecision;

export interface HookBinding {
  descriptor: RuntimeHookDescriptor;
  handler: RuntimeHookHandler;
}

export interface HookAuditEntry {
  auditId: string;
  invocationId: string;
  hookId: string;
  phase: HookPhase;
  sequence: number;
  startedAt: number;
  finishedAt: number;
  durationMilliseconds: number;
  inputDigest: string;
  outputDigest: string | null;
  decision: HookDecisionKind | "error" | "skipped";
  reason: string;
  errorCode: string | null;
  patchCount: number;
}

export interface HookPhaseResult {
  invocationId: string;
  phase: HookPhase;
  decision: HookDecisionKind;
  reason: string;
  value: JsonRecord;
  appliedHooks: string[];
  skippedHooks: string[];
  retryAfterMilliseconds: number | null;
  auditIds: string[];
}

export interface HookRuntimeSnapshot {
  version: "zyra.query-hooks/v1";
  revision: number;
  sequence: number;
  descriptors: RuntimeHookDescriptor[];
  quarantine: Array<{
    hookId: string;
    reason: string;
    quarantinedAt: number;
  }>;
  audits: HookAuditEntry[];
  checksum: string;
}

interface HookQuarantine {
  hookId: string;
  reason: string;
  quarantinedAt: number;
}

export interface HookRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  maximumAuditEntries?: number;
  maximumPatchesPerHook?: number;
  maximumRetryDelayMilliseconds?: number;
}

export class QueryHookRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumAuditEntries: number;
  private readonly maximumPatchesPerHook: number;
  private readonly maximumRetryDelayMilliseconds: number;
  private readonly bindings = new Map<string, HookBinding>();
  private readonly persistedDescriptors = new Map<string, RuntimeHookDescriptor>();
  private readonly quarantine = new Map<string, HookQuarantine>();
  private readonly audits: HookAuditEntry[] = [];
  private revision = 0;
  private sequence = 0;

  constructor(options: HookRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumAuditEntries = options.maximumAuditEntries ?? 2_000;
    this.maximumPatchesPerHook = options.maximumPatchesPerHook ?? 64;
    this.maximumRetryDelayMilliseconds =
      options.maximumRetryDelayMilliseconds ?? 60_000;
    assertNonNegativeInteger(this.maximumAuditEntries, "maximumAuditEntries");
    assertNonNegativeInteger(this.maximumPatchesPerHook, "maximumPatchesPerHook");
    assertNonNegativeInteger(
      this.maximumRetryDelayMilliseconds,
      "maximumRetryDelayMilliseconds",
    );
  }

  register(binding: HookBinding): RuntimeHookDescriptor {
    const descriptor = normalizeDescriptor(binding.descriptor);
    if (this.bindings.has(descriptor.hookId)) {
      throw new RuntimeInvariantError("hook_already_registered", {
        hookId: descriptor.hookId,
      });
    }
    this.assertDependenciesDoNotReferenceSelf(descriptor);
    this.bindings.set(descriptor.hookId, {
      descriptor,
      handler: binding.handler,
    });
    this.persistedDescriptors.set(descriptor.hookId, deepClone(descriptor));
    this.assertPhaseAcyclic(descriptor.phase);
    this.bumpRevision();
    return deepClone(descriptor);
  }

  bindHandler(hookId: string, handler: RuntimeHookHandler): void {
    assertNonEmpty(hookId, "hookId");
    const descriptor = this.persistedDescriptors.get(hookId);
    if (descriptor === undefined) {
      throw new RuntimeInvariantError("unknown_hook", { hookId });
    }
    this.bindings.set(hookId, {
      descriptor: deepClone(descriptor),
      handler,
    });
  }

  unregister(hookId: string): RuntimeHookDescriptor {
    const descriptor = this.requireDescriptor(hookId);
    this.bindings.delete(hookId);
    this.persistedDescriptors.delete(hookId);
    this.quarantine.delete(hookId);
    this.bumpRevision();
    return descriptor;
  }

  configure(
    hookId: string,
    update: Partial<
      Pick<
        RuntimeHookDescriptor,
        | "priority"
        | "timeoutMilliseconds"
        | "failureMode"
        | "enabled"
        | "before"
        | "after"
        | "metadata"
      >
    >,
  ): RuntimeHookDescriptor {
    const previous = this.requireDescriptor(hookId);
    const next = normalizeDescriptor({
      ...previous,
      ...deepClone(update),
      revision: previous.revision + 1,
    });
    this.assertDependenciesDoNotReferenceSelf(next);
    this.persistedDescriptors.set(hookId, next);
    const binding = this.bindings.get(hookId);
    if (binding !== undefined) {
      this.bindings.set(hookId, { descriptor: next, handler: binding.handler });
    }
    try {
      this.assertPhaseAcyclic(next.phase);
    } catch (error) {
      this.persistedDescriptors.set(hookId, previous);
      if (binding !== undefined) {
        this.bindings.set(hookId, {
          descriptor: previous,
          handler: binding.handler,
        });
      }
      throw error;
    }
    this.bumpRevision();
    return deepClone(next);
  }

  enable(hookId: string): RuntimeHookDescriptor {
    this.quarantine.delete(hookId);
    return this.configure(hookId, { enabled: true });
  }

  disable(hookId: string): RuntimeHookDescriptor {
    return this.configure(hookId, { enabled: false });
  }

  list(phase?: HookPhase): RuntimeHookDescriptor[] {
    const values = [...this.persistedDescriptors.values()].filter(
      (descriptor) => phase === undefined || descriptor.phase === phase,
    );
    values.sort(descriptorComparator);
    return values.map((value) => deepClone(value));
  }

  orderedHookIds(phase: HookPhase): string[] {
    return this.resolveOrder(phase).map((descriptor) => descriptor.hookId);
  }

  async run(
    input: Omit<
      HookInvocationContext,
      "invocationId" | "phase" | "attempt" | "startedAt" | "deadlineAt" | "accumulated"
    > & {
      phase: HookPhase;
      attempt?: number;
      accumulated?: JsonRecord;
    },
  ): Promise<HookPhaseResult> {
    const invocationId = this.ids.next("hook-invocation");
    const startedAt = this.clock.now();
    const attempt = input.attempt ?? 1;
    assertNonNegativeInteger(attempt, "attempt");
    let value = deepClone(input.accumulated ?? input.payload);
    const appliedHooks: string[] = [];
    const skippedHooks: string[] = [];
    const auditIds: string[] = [];
    let finalDecision: HookDecisionKind = "continue";
    let finalReason = "all_hooks_continued";
    let retryAfterMilliseconds: number | null = null;

    for (const descriptor of this.resolveOrder(input.phase)) {
      const binding = this.bindings.get(descriptor.hookId);
      const quarantine = this.quarantine.get(descriptor.hookId);
      if (!descriptor.enabled || quarantine !== undefined || binding === undefined) {
        skippedHooks.push(descriptor.hookId);
        const audit = this.appendAudit({
          invocationId,
          hookId: descriptor.hookId,
          phase: input.phase,
          startedAt: this.clock.now(),
          finishedAt: this.clock.now(),
          inputDigest: digestJson(value),
          outputDigest: null,
          decision: "skipped",
          reason:
            quarantine !== undefined
              ? `quarantined:${quarantine.reason}`
              : binding === undefined
                ? "handler_not_bound"
                : "disabled",
          errorCode: null,
          patchCount: 0,
        });
        auditIds.push(audit.auditId);
        continue;
      }

      const hookStartedAt = this.clock.now();
      const context: HookInvocationContext = {
        invocationId,
        sessionId: input.sessionId,
        runId: input.runId,
        queryId: input.queryId,
        turnId: input.turnId,
        phase: input.phase,
        attempt,
        startedAt,
        deadlineAt: hookStartedAt + descriptor.timeoutMilliseconds,
        correlationId: input.correlationId,
        payload: deepClone(input.payload),
        accumulated: deepClone(value),
      };
      const inputDigest = digestJson(value);
      try {
        const decision = normalizeDecision(
          await withTimeout(
            Promise.resolve(binding.handler(Object.freeze(context))),
            descriptor.timeoutMilliseconds,
            `hook:${descriptor.hookId}`,
          ),
          this.maximumPatchesPerHook,
          this.maximumRetryDelayMilliseconds,
        );
        const next = applyPatches(value, decision.patches ?? []);
        value = next;
        appliedHooks.push(descriptor.hookId);
        const audit = this.appendAudit({
          invocationId,
          hookId: descriptor.hookId,
          phase: input.phase,
          startedAt: hookStartedAt,
          finishedAt: this.clock.now(),
          inputDigest,
          outputDigest: digestJson(value),
          decision: decision.kind,
          reason: decision.reason,
          errorCode: null,
          patchCount: decision.patches?.length ?? 0,
        });
        auditIds.push(audit.auditId);
        if (decision.kind !== "continue") {
          finalDecision = decision.kind;
          finalReason = decision.reason;
          retryAfterMilliseconds = decision.retryAfterMilliseconds ?? null;
          break;
        }
      } catch (error) {
        const code =
          error instanceof RuntimeInvariantError
            ? error.code
            : error instanceof Error
              ? error.name
              : "unknown_hook_error";
        const audit = this.appendAudit({
          invocationId,
          hookId: descriptor.hookId,
          phase: input.phase,
          startedAt: hookStartedAt,
          finishedAt: this.clock.now(),
          inputDigest,
          outputDigest: null,
          decision: "error",
          reason: error instanceof Error ? error.message : String(error),
          errorCode: code,
          patchCount: 0,
        });
        auditIds.push(audit.auditId);
        if (descriptor.failureMode === "quarantine") {
          this.quarantine.set(descriptor.hookId, {
            hookId: descriptor.hookId,
            reason: code,
            quarantinedAt: this.clock.now(),
          });
          this.bumpRevision();
          continue;
        }
        if (descriptor.failureMode === "fail_open") {
          continue;
        }
        finalDecision = "block";
        finalReason = `hook_failure:${descriptor.hookId}:${code}`;
        break;
      }
    }

    return {
      invocationId,
      phase: input.phase,
      decision: finalDecision,
      reason: finalReason,
      value,
      appliedHooks,
      skippedHooks,
      retryAfterMilliseconds,
      auditIds,
    };
  }

  getAudit(auditId: string): HookAuditEntry {
    const entry = this.audits.find((value) => value.auditId === auditId);
    if (entry === undefined) {
      throw new RuntimeInvariantError("unknown_hook_audit", { auditId });
    }
    return deepClone(entry);
  }

  queryAudits(filter: {
    invocationId?: string;
    hookId?: string;
    phase?: HookPhase;
    decision?: HookAuditEntry["decision"];
    sinceSequence?: number;
  }): HookAuditEntry[] {
    return this.audits
      .filter(
        (entry) =>
          (filter.invocationId === undefined ||
            entry.invocationId === filter.invocationId) &&
          (filter.hookId === undefined || entry.hookId === filter.hookId) &&
          (filter.phase === undefined || entry.phase === filter.phase) &&
          (filter.decision === undefined || entry.decision === filter.decision) &&
          (filter.sinceSequence === undefined ||
            entry.sequence > filter.sinceSequence),
      )
      .map((entry) => deepClone(entry));
  }

  snapshot(): HookRuntimeSnapshot {
    const body = {
      version: "zyra.query-hooks/v1" as const,
      revision: this.revision,
      sequence: this.sequence,
      descriptors: this.list(),
      quarantine: [...this.quarantine.values()]
        .sort((left, right) => compareStrings(left.hookId, right.hookId))
        .map((value) => deepClone(value)),
      audits: this.audits.map((entry) => deepClone(entry)),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: HookRuntimeSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.query-hooks/v1") {
      throw new RuntimeInvariantError("unsupported_hook_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("hook_snapshot_checksum_mismatch");
    }
    this.persistedDescriptors.clear();
    this.quarantine.clear();
    this.audits.length = 0;
    for (const descriptor of snapshot.descriptors) {
      const normalized = normalizeDescriptor(descriptor);
      if (this.persistedDescriptors.has(normalized.hookId)) {
        throw new RuntimeInvariantError("duplicate_hook_in_snapshot", {
          hookId: normalized.hookId,
        });
      }
      this.persistedDescriptors.set(normalized.hookId, normalized);
      const existing = this.bindings.get(normalized.hookId);
      if (existing !== undefined) {
        this.bindings.set(normalized.hookId, {
          descriptor: normalized,
          handler: existing.handler,
        });
      }
    }
    for (const item of snapshot.quarantine) {
      this.quarantine.set(item.hookId, deepClone(item));
    }
    for (const entry of snapshot.audits.slice(-this.maximumAuditEntries)) {
      this.audits.push(deepClone(entry));
    }
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    for (const phase of HOOK_PHASES) {
      this.assertPhaseAcyclic(phase);
    }
  }

  private requireDescriptor(hookId: string): RuntimeHookDescriptor {
    const descriptor = this.persistedDescriptors.get(hookId);
    if (descriptor === undefined) {
      throw new RuntimeInvariantError("unknown_hook", { hookId });
    }
    return deepClone(descriptor);
  }

  private resolveOrder(phase: HookPhase): RuntimeHookDescriptor[] {
    const descriptors = [...this.persistedDescriptors.values()].filter(
      (descriptor) => descriptor.phase === phase,
    );
    const byId = new Map(descriptors.map((value) => [value.hookId, value]));
    const outgoing = new Map<string, Set<string>>();
    const incoming = new Map<string, number>();
    for (const descriptor of descriptors) {
      outgoing.set(descriptor.hookId, new Set());
      incoming.set(descriptor.hookId, 0);
    }
    const addEdge = (from: string, to: string): void => {
      if (!byId.has(from) || !byId.has(to)) {
        return;
      }
      const edges = outgoing.get(from);
      if (edges === undefined || edges.has(to)) {
        return;
      }
      edges.add(to);
      incoming.set(to, (incoming.get(to) ?? 0) + 1);
    };
    for (const descriptor of descriptors) {
      for (const before of descriptor.before) {
        addEdge(descriptor.hookId, before);
      }
      for (const after of descriptor.after) {
        addEdge(after, descriptor.hookId);
      }
    }
    const ready = descriptors
      .filter((value) => incoming.get(value.hookId) === 0)
      .sort(descriptorComparator);
    const result: RuntimeHookDescriptor[] = [];
    while (ready.length > 0) {
      const current = ready.shift();
      if (current === undefined) {
        break;
      }
      result.push(current);
      for (const target of outgoing.get(current.hookId) ?? []) {
        const count = (incoming.get(target) ?? 1) - 1;
        incoming.set(target, count);
        if (count === 0) {
          const descriptor = byId.get(target);
          if (descriptor !== undefined) {
            ready.push(descriptor);
            ready.sort(descriptorComparator);
          }
        }
      }
    }
    if (result.length !== descriptors.length) {
      const unresolved = descriptors
        .filter((value) => !result.some((item) => item.hookId === value.hookId))
        .map((value) => value.hookId)
        .sort(compareStrings);
      throw new RuntimeInvariantError("hook_dependency_cycle", {
        phase,
        unresolved,
      });
    }
    return result.map((value) => deepClone(value));
  }

  private assertDependenciesDoNotReferenceSelf(
    descriptor: RuntimeHookDescriptor,
  ): void {
    if (
      descriptor.before.includes(descriptor.hookId) ||
      descriptor.after.includes(descriptor.hookId)
    ) {
      throw new RuntimeInvariantError("hook_self_dependency", {
        hookId: descriptor.hookId,
      });
    }
  }

  private assertPhaseAcyclic(phase: HookPhase): void {
    this.resolveOrder(phase);
  }

  private appendAudit(
    input: Omit<
      HookAuditEntry,
      "auditId" | "sequence" | "durationMilliseconds"
    >,
  ): HookAuditEntry {
    this.sequence += 1;
    const entry: HookAuditEntry = {
      ...input,
      auditId: this.ids.next("hook-audit"),
      sequence: this.sequence,
      durationMilliseconds: Math.max(0, input.finishedAt - input.startedAt),
    };
    this.audits.push(entry);
    if (this.audits.length > this.maximumAuditEntries) {
      this.audits.splice(0, this.audits.length - this.maximumAuditEntries);
    }
    return deepClone(entry);
  }

  private bumpRevision(): void {
    this.revision += 1;
  }
}

function normalizeDescriptor(
  descriptor: RuntimeHookDescriptor,
): RuntimeHookDescriptor {
  assertNonEmpty(descriptor.hookId, "hookId");
  assertNonEmpty(descriptor.owner, "owner");
  if (!HOOK_PHASES.includes(descriptor.phase)) {
    throw new RuntimeInvariantError("unknown_hook_phase", {
      phase: descriptor.phase,
    });
  }
  if (!Number.isSafeInteger(descriptor.priority)) {
    throw new RuntimeInvariantError("invalid_hook_priority", {
      priority: descriptor.priority,
    });
  }
  assertNonNegativeInteger(
    descriptor.timeoutMilliseconds,
    "timeoutMilliseconds",
  );
  if (descriptor.timeoutMilliseconds === 0) {
    throw new RuntimeInvariantError("zero_hook_timeout", {
      hookId: descriptor.hookId,
    });
  }
  assertNonNegativeInteger(descriptor.revision, "revision");
  return {
    ...deepClone(descriptor),
    before: uniqueSorted(descriptor.before),
    after: uniqueSorted(descriptor.after),
  };
}

function descriptorComparator(
  left: RuntimeHookDescriptor,
  right: RuntimeHookDescriptor,
): number {
  return (
    compareNumbers(left.priority, right.priority) ||
    compareStrings(left.hookId, right.hookId)
  );
}

function normalizeDecision(
  decision: HookDecision,
  maximumPatches: number,
  maximumRetryDelayMilliseconds: number,
): HookDecision {
  const kinds: HookDecisionKind[] = ["continue", "block", "retry", "stop"];
  if (!kinds.includes(decision.kind)) {
    throw new RuntimeInvariantError("unknown_hook_decision", {
      kind: decision.kind,
    });
  }
  assertNonEmpty(decision.reason, "decision.reason");
  const patches = decision.patches ?? [];
  if (patches.length > maximumPatches) {
    throw new RuntimeInvariantError("hook_patch_limit_exceeded", {
      actual: patches.length,
      maximum: maximumPatches,
    });
  }
  for (const patch of patches) {
    validatePatch(patch);
  }
  if (decision.kind === "retry") {
    const delay = decision.retryAfterMilliseconds ?? 0;
    assertNonNegativeInteger(delay, "retryAfterMilliseconds");
    if (delay > maximumRetryDelayMilliseconds) {
      throw new RuntimeInvariantError("hook_retry_delay_exceeded", {
        actual: delay,
        maximum: maximumRetryDelayMilliseconds,
      });
    }
  }
  return deepClone(decision);
}

function validatePatch(patch: HookPatchOperation): void {
  if (!["add", "replace", "remove", "test"].includes(patch.operation)) {
    throw new RuntimeInvariantError("unknown_patch_operation", {
      operation: patch.operation,
    });
  }
  parsePointer(patch.path);
  if (
    (patch.operation === "add" ||
      patch.operation === "replace" ||
      patch.operation === "test") &&
    patch.value === undefined
  ) {
    throw new RuntimeInvariantError("patch_value_required", {
      operation: patch.operation,
      path: patch.path,
    });
  }
}

function applyPatches(
  input: JsonRecord,
  patches: readonly HookPatchOperation[],
): JsonRecord {
  let value = deepClone(input);
  for (const patch of patches) {
    value = applyPatch(value, patch);
  }
  return value;
}

function applyPatch(
  input: JsonRecord,
  patch: HookPatchOperation,
): JsonRecord {
  const result = deepClone(input);
  const segments = parsePointer(patch.path);
  if (segments.length === 0) {
    if (patch.operation === "test") {
      if (canonicalJson(result) !== canonicalJson(patch.value ?? null)) {
        throw new RuntimeInvariantError("patch_test_failed", {
          path: patch.path,
        });
      }
      return result;
    }
    if (
      patch.operation === "remove" ||
      patch.value === null ||
      Array.isArray(patch.value) ||
      typeof patch.value !== "object"
    ) {
      throw new RuntimeInvariantError("root_patch_must_be_record", {
        operation: patch.operation,
      });
    }
    return deepClone(patch.value);
  }
  let parent: JsonValue = result;
  for (const segment of segments.slice(0, -1)) {
    parent = readChild(parent, segment, patch.path);
  }
  const key = segments.at(-1);
  if (key === undefined) {
    return result;
  }
  if (patch.operation === "test") {
    const actual = readChild(parent, key, patch.path);
    if (canonicalJson(actual) !== canonicalJson(patch.value ?? null)) {
      throw new RuntimeInvariantError("patch_test_failed", {
        path: patch.path,
      });
    }
    return result;
  }
  if (Array.isArray(parent)) {
    const index = key === "-" ? parent.length : parseArrayIndex(key, parent.length);
    if (patch.operation === "remove") {
      if (index >= parent.length) {
        throw new RuntimeInvariantError("patch_path_missing", {
          path: patch.path,
        });
      }
      parent.splice(index, 1);
    } else if (patch.operation === "add") {
      parent.splice(index, 0, deepClone(patch.value ?? null));
    } else {
      if (index >= parent.length) {
        throw new RuntimeInvariantError("patch_path_missing", {
          path: patch.path,
        });
      }
      parent[index] = deepClone(patch.value ?? null);
    }
    return result;
  }
  if (parent === null || typeof parent !== "object") {
    throw new RuntimeInvariantError("patch_parent_not_container", {
      path: patch.path,
    });
  }
  if (patch.operation === "remove") {
    if (!(key in parent)) {
      throw new RuntimeInvariantError("patch_path_missing", {
        path: patch.path,
      });
    }
    delete parent[key];
  } else if (patch.operation === "replace") {
    if (!(key in parent)) {
      throw new RuntimeInvariantError("patch_path_missing", {
        path: patch.path,
      });
    }
    parent[key] = deepClone(patch.value ?? null);
  } else {
    parent[key] = deepClone(patch.value ?? null);
  }
  return result;
}

function parsePointer(path: string): string[] {
  if (path === "") {
    return [];
  }
  if (!path.startsWith("/")) {
    throw new RuntimeInvariantError("invalid_json_pointer", { path });
  }
  return path
    .slice(1)
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
}

function readChild(parent: JsonValue, key: string, path: string): JsonValue {
  if (Array.isArray(parent)) {
    const index = parseArrayIndex(key, parent.length);
    const value = parent[index];
    if (value === undefined) {
      throw new RuntimeInvariantError("patch_path_missing", { path });
    }
    return value;
  }
  if (parent === null || typeof parent !== "object" || !(key in parent)) {
    throw new RuntimeInvariantError("patch_path_missing", { path });
  }
  return parent[key] ?? null;
}

function parseArrayIndex(value: string, length: number): number {
  if (!/^(0|[1-9][0-9]*)$/.test(value)) {
    throw new RuntimeInvariantError("invalid_array_index", { value });
  }
  const index = Number(value);
  if (!Number.isSafeInteger(index) || index > length) {
    throw new RuntimeInvariantError("array_index_out_of_range", {
      value,
      length,
    });
  }
  return index;
}
