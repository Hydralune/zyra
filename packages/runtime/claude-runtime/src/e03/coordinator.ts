import type {
  JsonObject,
  RuntimeRunInput,
  RuntimeRunResult,
} from "../contracts.ts";
import {
  digest,
  E03RuntimeError,
  emptySnapshot,
  response,
  sealTask,
  type E03AgentDefinition,
  type E03ControlEnvelope,
  type E03ControlResponse,
  type E03IsolationReceipt,
  type E03PhysicalPort,
  type E03TaskState,
} from "./contracts.ts";
import {
  AgentDefinitionRegistry,
  builtinAgentDefinitions,
} from "../agents/definition-registry.ts";
import { AgentContextFork, rootContext } from "../agents/context-fork.ts";
import { AgentExecutionRuntime } from "../agents/execution-runtime.ts";
import {
  AgentScopeLattice,
  rootCapabilityScope,
} from "../agents/scope-lattice.ts";
import {
  IsolationMergeRuntime,
  type CleanupReceipt,
  type MergeReceipt,
} from "../isolation/merge-runtime.ts";
import { IsolationRequestRuntime } from "../isolation/request-runtime.ts";
import { WorktreeCustodyLedger } from "../isolation/merge-runtime.ts";
import { DurableTaskRegistry, taskProjection } from "../tasks/registry.ts";
import { TeamFanout, type FanoutTarget } from "../team/fanout.ts";
import { AgentControlHandler } from "../control/session-handler.ts";
import {
  StructuredControlRouter,
  type ControlDelegate,
  type E03CommandHandler,
} from "../control/router.ts";
import { ControlSchema } from "../control/schema.ts";
import { ControlCommandCatalog } from "../control/schema.ts";
import { ControlAuditLedger } from "../control/session-handler.ts";

export interface E03CoordinatorOptions {
  runId: string;
  sessionId: string;
  parentTaskId: string;
  physicalPort: E03PhysicalPort;
  parentInput?: RuntimeRunInput;
  e01?: ControlDelegate;
  e02?: ControlDelegate;
  disabled?: boolean;
  runChild?: (input: RuntimeRunInput) => Promise<RuntimeRunResult>;
}

export class E03AgentControlCoordinator implements E03CommandHandler {
  readonly definitions = new AgentDefinitionRegistry(builtinAgentDefinitions());
  readonly registry: DurableTaskRegistry;
  readonly execution: AgentExecutionRuntime;
  readonly controls: AgentControlHandler;
  readonly router: StructuredControlRouter;
  readonly schema = new ControlSchema();
  readonly commandCatalog = new ControlCommandCatalog();
  readonly audit = new ControlAuditLedger();
  private readonly scopes = new AgentScopeLattice();
  private readonly contexts = new AgentContextFork();
  private readonly isolation = new IsolationRequestRuntime();
  private readonly worktrees = new WorktreeCustodyLedger();
  private readonly mergeRuntime = new IsolationMergeRuntime();
  private readonly fanout = new TeamFanout();
  private restored = false;

  constructor(private readonly options: E03CoordinatorOptions) {
    this.registry = new DurableTaskRegistry(
      emptySnapshot(),
      options.physicalPort,
    );
    this.execution = new AgentExecutionRuntime(this.registry, {
      runChild: async (input) => {
        if (!options.runChild)
          throw new E03RuntimeError(
            "child_runtime_unavailable",
            "E03 coordinator has no QueryEngine child host",
          );
        return options.runChild(input);
      },
    });
    this.controls = new AgentControlHandler(this.registry, this.execution);
    this.router = new StructuredControlRouter(
      this.controls,
      this,
      options.e01,
      options.e02,
    );
  }

  async restore(): Promise<void> {
    if (this.restored) return;
    const snapshot = await this.registry.restore(
      this.options.runId,
      this.options.sessionId,
    );
    if (Object.keys(snapshot.definitions).length)
      this.definitions.restore(snapshot);
    this.restored = true;
  }

  async execute(
    value: E03ControlEnvelope | unknown,
    parentInput = this.options.parentInput,
  ): Promise<E03ControlResponse> {
    const envelope = isEnvelope(value) ? value : this.schema.parse(value);
    const descriptor = this.commandCatalog.validate(envelope);
    if (this.options.disabled || process.env.ZYRA_E03_DISABLED === "1") {
      const rejected = response({
        ok: false,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "rejected",
        error: "typescript_agent_control_disabled",
      });
      this.audit.record(envelope, descriptor, rejected);
      return rejected;
    }
    this.assertEnvelopeAuthority(envelope);
    await this.restore();
    if (envelope.command === "agent.kill" || envelope.command === "agent.steer")
      this.controls.assertExactControlBinding(envelope);
    const recovered = this.router.recoverLostAck(envelope);
    if (recovered) {
      const replay = { ...recovered, restored: true };
      this.audit.record(envelope, descriptor, replay);
      return replay;
    }
    try {
      let result: E03ControlResponse;
      if (envelope.command === "agent.create")
        result = await this.create(envelope, parentInput);
      else if (envelope.command === "team.fanout")
        result = await this.fanoutCommand(envelope, parentInput);
      else if (envelope.command === "worktree.prepare")
        result = await this.prepareWorktree(envelope);
      else if (envelope.command === "worktree.merge")
        result = await this.mergeWorktree(envelope);
      else if (envelope.command === "worktree.cleanup")
        result = await this.cleanupWorktree(envelope);
      else result = await this.router.dispatch(envelope, parentInput);
      if (envelope.simulate_lost_ack && result.ok) {
        const lost: E03ControlResponse = {
          ...result,
          ok: false,
          phase: "rejected",
          replayed: false,
          error: "simulated_lost_ack",
        };
        this.audit.record(envelope, descriptor, lost);
        return lost;
      }
      this.audit.record(envelope, descriptor, result);
      return result;
    } catch (error) {
      if (!(error instanceof E03RuntimeError)) throw error;
      const rejected = response({
        ok: false,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "rejected",
        revision: this.taskRevision(envelope.body.task_id),
        restored: this.restored,
        error: error.code,
        state: { message: error.message, details: error.details },
      });
      this.audit.record(envelope, descriptor, rejected);
      return rejected;
    }
  }

  private async create(
    envelope: E03ControlEnvelope,
    parentInput?: RuntimeRunInput,
  ): Promise<E03ControlResponse> {
    const definition = this.definitions.resolve(
      text(envelope.body.agent, "agent"),
    );
    const rootScope = this.rootScope(parentInput);
    const scope = this.scopes.derive({
      parent: rootScope,
      definition,
      requestedTools: stringList(envelope.body.tools),
      requestedSkills: stringList(envelope.body.skills),
      requestedMcpServers: stringList(envelope.body.mcp_servers),
      requestedWorkspaceRoots: stringList(
        envelope.body.workspace_root
          ? [envelope.body.workspace_root]
          : undefined,
      ),
      requestedIsolation:
        typeof envelope.body.isolation === "string"
          ? (envelope.body.isolation as any)
          : definition.isolation,
      requestedBackground: envelope.body.execution_mode === "background",
      depth: parentInput ? Number(parentInput.metadata?.e03_depth ?? 0) + 1 : 1,
    });
    const taskId = text(envelope.body.task_id, "task_id");
    const sessionId = `agent-session-${digest({ parent: envelope.session_id, taskId }).slice(0, 24)}`;
    const parentContext = rootContext({
      sessionId: envelope.session_id,
      taskId: envelope.parent_task_id,
      permissionDigest: rootScope.permissionCeilingDigest,
      toolCatalogDigest: digest(rootScope.tools),
    });
    const context = this.contexts.fork({
      parent: parentContext,
      childTaskId: taskId,
      childSessionId: sessionId,
      mode: "fork",
      scope,
    });
    let task = await this.execution.create({
      requestId: envelope.request_id,
      runId: envelope.run_id,
      sessionId,
      parentTaskId: envelope.parent_task_id,
      parentSessionId: envelope.session_id,
      taskId,
      idempotencyKey: envelope.idempotency_key,
      definition,
      scope,
      context,
      prompt: text(envelope.body.prompt, "prompt"),
      executionMode:
        envelope.body.execution_mode === "background"
          ? "background"
          : "foreground",
      physicalDispatch: objectOrNull(envelope.body.physical_dispatch),
      parentLineage: Array.isArray(parentInput?.metadata?.e03_lineage)
        ? (parentInput!.metadata!.e03_lineage as string[])
        : [],
    });
    if (envelope.body.isolation && envelope.body.isolation !== "none")
      task = await this.prepareIsolationForTask(task, envelope);
    const initial = response({
      ok: true,
      request_id: envelope.request_id,
      command: envelope.command,
      phase: "ack",
      revision: task.revision,
      state: taskProjection(task),
      dispatch_count: 1,
    });
    if (parentInput && envelope.body.start_immediately === true) {
      const completed = await this.execution.run(
        task.identity.taskId,
        parentInput,
        envelope.body,
        envelope.request_id,
        `${envelope.idempotency_key}:run`,
      );
      return this.registry.acknowledgeDurably(envelope.idempotency_key, {
        ...initial,
        revision: completed.revision,
        state: taskProjection(completed),
        result: completed.result,
      });
    }
    return this.registry.acknowledgeDurably(envelope.idempotency_key, initial);
  }

  private async fanoutCommand(
    envelope: E03ControlEnvelope,
    parentInput?: RuntimeRunInput,
  ): Promise<E03ControlResponse> {
    const parent = this.registry.require(
      text(envelope.body.parent_task_id, "parent_task_id"),
    );
    const targets = arrayObjects(envelope.body.targets).map(
      (value, index): FanoutTarget => ({
        key: text(value.key ?? String(index + 1), `targets[${index}].key`),
        agent: text(value.agent, `targets[${index}].agent`),
        prompt: text(value.prompt, `targets[${index}].prompt`),
        metadata: object(value.metadata),
      }),
    );
    const plan = this.fanout.plan(parent, targets, {
      maximumConcurrency:
        typeof envelope.body.maximum_concurrency === "number"
          ? envelope.body.maximum_concurrency
          : undefined,
      failureMode:
        envelope.body.failure_mode === "fail-fast" ? "fail-fast" : "collect",
      idempotencyKey: envelope.idempotency_key,
    });
    const result = await this.fanout.dispatch(plan, async (target, index) => {
      const childEnvelope: E03ControlEnvelope = {
        ...envelope,
        request_id: `${envelope.request_id}:${target.key}`,
        idempotency_key: `${envelope.idempotency_key}:${target.key}`,
        expected_revision: 0,
        command: "agent.create",
        body: {
          task_id: `${parent.identity.taskId}:${target.key}`,
          agent: target.agent,
          prompt: target.prompt,
          execution_mode: "background",
          start_immediately: Boolean(parentInput),
        },
      };
      const created = await this.create(childEnvelope, parentInput);
      if (!created.ok)
        throw new E03RuntimeError(
          created.error || "fanout_create_failed",
          `fanout target ${target.key} failed`,
        );
      return this.registry.require(String(created.state!.task_id));
    });
    return response({
      ok: result.ok,
      request_id: envelope.request_id,
      command: envelope.command,
      phase: "ack",
      revision: Math.max(
        parent.revision,
        ...result.completed.map((task) => task.revision),
        ...result.failed.map((task) => task.revision),
      ),
      state: {
        plan_id: plan.planId,
        summary: result.summary,
        completed: result.completed.map(taskProjection),
        failed: result.failed.map(taskProjection),
        cancelled: result.cancelled.map(taskProjection),
        pending: result.pending.map(taskProjection),
      },
      dispatch_count: targets.length,
      error: result.ok ? "" : "fanout_incomplete",
    });
  }

  private async prepareWorktree(
    envelope: E03ControlEnvelope,
  ): Promise<E03ControlResponse> {
    const task = this.registry.require(text(envelope.body.task_id, "task_id"));
    const state = await this.prepareIsolationForTask(task, envelope);
    return this.registry.acknowledgeDurably(
      envelope.idempotency_key,
      response({
        ok: true,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "ack",
        revision: state.revision,
        state: taskProjection(state),
        dispatch_count: 1,
      }),
    );
  }

  private async prepareIsolationForTask(
    task: E03TaskState,
    envelope: E03ControlEnvelope,
  ): Promise<E03TaskState> {
    const isolationKey = `${envelope.idempotency_key}:isolation`;
    const request = this.isolation.prepare(task, {
      mode:
        envelope.command === "worktree.prepare"
          ? "worktree"
          : (envelope.body.isolation as any),
      workspaceRoot: text(
        envelope.body.workspace_root ?? task.scope.workspaceRoots[0],
        "workspace_root",
      ),
      baseRevision: String(envelope.body.base_revision ?? "HEAD"),
      expectedArtifacts: stringList(envelope.body.expected_artifacts),
      allowDirtyBaseline: envelope.body.allow_dirty_baseline === true,
      allowNestedRepository: envelope.body.allow_nested_repository === true,
      idempotencyKey: isolationKey,
    });
    this.worktrees.prepare(request);
    const proposed = sealTask({
      ...task,
      isolation: request,
      revision: task.revision + 1,
      sequence: task.sequence + 1,
      checksum: "",
    });
    this.registry.prepare({
      requestId: `${envelope.request_id}:isolation`,
      idempotencyKey: isolationKey,
      writerId: "typescript.E03AgentControlCoordinator",
      taskId: task.identity.taskId,
      expectedRevision: task.revision,
      proposed,
      effectKind: "workspace",
      effectOperation: "prepare_workspace_isolation",
      effectPayload: request as unknown as JsonObject,
    });
    const effect = await this.registry.recordReceipt(isolationKey);
    if (!effect)
      throw new E03RuntimeError(
        "missing_isolation_receipt",
        "workspace effect did not produce a receipt",
      );
    const receiptPayload = {
      receiptId: `isolation-receipt-${digest(effect.receiptId).slice(0, 24)}`,
      requestId: request.requestId,
      taskId: request.taskId,
      leaseId: request.leaseId,
      accepted: effect.accepted,
      workspacePath: String(
        effect.result.workspace_path ?? request.workspaceRoot,
      ),
      observedBaseRevision: String(
        effect.result.observed_base_revision ?? request.baseRevision,
      ),
      resultingRevision: String(
        effect.result.resulting_revision ?? request.baseRevision,
      ),
      dirtyBaseline: effect.result.dirty_baseline === true,
      nestedRepository: effect.result.nested_repository === true,
      mergeConflict: false,
      cleanupFailed: false,
      workspaceDisposition: String(
        effect.result.workspace_disposition ??
          (request.mode === "worktree" ? "created" : "prepared"),
      ) as E03IsolationReceipt["workspaceDisposition"],
      physicalBackend: String(effect.result.backend ?? "workspace"),
      worktreeHead: String(effect.result.worktree_head ?? ""),
      worktreeBranch: String(effect.result.worktree_branch ?? request.branchName),
      artifacts: effect.artifacts,
      error: effect.error,
      completedAt: effect.completedAt,
    };
    const isolationReceipt: E03IsolationReceipt = {
      ...receiptPayload,
      digest: digest(receiptPayload),
    };
    this.worktrees.ready(request, isolationReceipt);
    const committedTask = this.isolation.commit(
      proposed,
      request,
      isolationReceipt,
    );
    this.registry.revisePrepared(
      isolationKey,
      sealTask({ ...committedTask, revision: proposed.revision, checksum: "" }),
    );
    return (await this.registry.commit(isolationKey, effect)).state;
  }

  private async mergeWorktree(
    envelope: E03ControlEnvelope,
  ): Promise<E03ControlResponse> {
    const task = this.registry.require(text(envelope.body.task_id, "task_id"));
    if (!task.isolation || !task.isolationReceipt)
      throw new E03RuntimeError(
        "missing_isolation",
        "task has no committed isolation request",
      );
    const merged = this.mergeRuntime.merge(
      task,
      task.isolation,
      task.isolationReceipt,
      envelope.body.merge_receipt as unknown as MergeReceipt,
    );
    this.worktrees.merge(
      task.isolation,
      envelope.body.merge_receipt as unknown as MergeReceipt,
    );
    return this.persistDerived(
      envelope,
      task,
      merged,
      "persist_worktree_merge",
    );
  }

  private async cleanupWorktree(
    envelope: E03ControlEnvelope,
  ): Promise<E03ControlResponse> {
    const task = this.registry.require(text(envelope.body.task_id, "task_id"));
    if (!task.isolation)
      throw new E03RuntimeError(
        "missing_isolation",
        "task has no committed isolation request",
      );
    const cleaned = this.mergeRuntime.cleanup(
      task,
      task.isolation,
      envelope.body.cleanup_receipt as unknown as CleanupReceipt,
    );
    this.worktrees.cleanup(
      task.isolation,
      envelope.body.cleanup_receipt as unknown as CleanupReceipt,
    );
    return this.persistDerived(
      envelope,
      task,
      cleaned,
      "persist_worktree_cleanup",
    );
  }

  private async persistDerived(
    envelope: E03ControlEnvelope,
    before: E03TaskState,
    derived: E03TaskState,
    operation: string,
  ): Promise<E03ControlResponse> {
    const proposed = sealTask({
      ...derived,
      revision: before.revision + 1,
      checksum: "",
    });
    const prepared = this.registry.prepare({
      requestId: envelope.request_id,
      idempotencyKey: envelope.idempotency_key,
      writerId: "typescript.E03AgentControlCoordinator",
      taskId: before.identity.taskId,
      expectedRevision: before.revision,
      proposed,
      effectKind: "persist",
      effectOperation: operation,
      effectPayload: {},
    });
    const receipt = await this.registry.recordReceipt(envelope.idempotency_key);
    const committed = await this.registry.commit(
      envelope.idempotency_key,
      receipt,
    );
    return this.registry.acknowledgeDurably(
      envelope.idempotency_key,
      response({
        ok: true,
        request_id: envelope.request_id,
        command: envelope.command,
        phase: "ack",
        revision: committed.state.revision,
        state: taskProjection(committed.state),
        dispatch_count: 1,
      }),
    );
  }

  private rootScope(parentInput?: RuntimeRunInput) {
    const tools = parentInput?.tools.map((tool) => tool.name) ?? [
      "read",
      "grep",
      "glob",
      "shell",
      "edit",
      "write",
      "agent",
    ];
    const roots =
      parentInput && Array.isArray(parentInput.metadata?.workspace_roots)
        ? (parentInput.metadata!.workspace_roots as string[])
        : [process.cwd()];
    return rootCapabilityScope({
      tools,
      skills: stringList(parentInput?.metadata?.skills),
      mcpServers: stringList(parentInput?.metadata?.mcp_servers),
      permissionMode: String(
        parentInput?.config.permissionPolicy?.mode ?? "inherit",
      ),
      permissionCeilingDigest: String(
        parentInput?.config.permissionPolicy?.digest ??
          digest(parentInput?.config.permissionPolicy ?? {}),
      ),
      workspaceRoots: roots,
    });
  }

  private assertEnvelopeAuthority(envelope: E03ControlEnvelope): void {
    if (
      envelope.run_id !== this.options.runId ||
      envelope.session_id !== this.options.sessionId ||
      envelope.parent_task_id !== this.options.parentTaskId
    )
      throw new E03RuntimeError(
        "control_authority_mismatch",
        "control envelope differs from coordinator run/session/parent authority",
      );
  }

  private taskRevision(value: unknown): number {
    if (typeof value !== "string") return 0;
    return this.registry.get(value)?.revision ?? 0;
  }
}

function objectOrNull(value: unknown): JsonObject | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return structuredClone(value as JsonObject);
}

function isEnvelope(value: unknown): value is E03ControlEnvelope {
  return Boolean(
    value &&
      typeof value === "object" &&
      (value as any).schema_version === "3.0" &&
      typeof (value as any).command === "string",
  );
}

function text(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim())
    throw new E03RuntimeError(
      "invalid_control_field",
      `${field} must be a non-empty string`,
    );
  return value.trim();
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? [
        ...new Set(
          value
            .map(String)
            .map((item) => item.trim())
            .filter(Boolean),
        ),
      ]
    : [];
}

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (structuredClone(value) as JsonObject)
    : {};
}

function arrayObjects(value: unknown): JsonObject[] {
  if (!Array.isArray(value))
    throw new E03RuntimeError(
      "invalid_control_field",
      "targets must be an array",
    );
  return value.map((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item))
      throw new E03RuntimeError(
        "invalid_control_field",
        `targets[${index}] must be an object`,
      );
    return item as JsonObject;
  });
}
