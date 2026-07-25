import {
  asBoolean,
  asObject,
  asString,
  type JsonObject,
  type RuntimeRunInput,
  type ToolSpecContract,
} from "../contracts.ts";
import type {
  AgentCapabilityResult,
  AgentExecutionContext,
  AgentToolSurface,
} from "./contracts.ts";
import { E03AgentControlCoordinator } from "../e03/coordinator.ts";
import {
  digest,
  E03RuntimeError,
  type E03ControlEnvelope,
} from "../e03/contracts.ts";
import { HostE03PhysicalPort } from "../e03/host-port.ts";
import { taskProjection } from "../tasks/registry.ts";
import { AgentBackgroundSupervisor } from "./execution-runtime.ts";
import { mapWithConcurrencyLimit } from "../omp-worker-control/semaphore.ts";

const AGENT_TOOLS = new Set([
  "Agent",
  "Task",
  "agent_status",
  "agent_cancel",
  "agent_kill",
  "agent_wait",
  "agent_result",
  "agent_resume",
  "agent_message",
  "agent_list",
]);

export class TypeScriptAgentRuntime implements AgentToolSurface {
  private coordinatorValue: E03AgentControlCoordinator | null = null;
  private coordinatorHost: AgentExecutionContext["host"] | null = null;
  private readonly backgroundSupervisor = new AgentBackgroundSupervisor();

  constructor(private readonly input: RuntimeRunInput) {}

  toolSpecs(): ToolSpecContract[] {
    const invocationSchema: JsonObject = {
      type: "object",
      required: ["prompt"],
      properties: {
        prompt: { type: "string", minLength: 1 },
        agent_type: { type: "string" },
        task_id: { type: "string" },
        idempotency_key: { type: "string" },
        background: { type: "boolean" },
        context_mode: { type: "string", enum: ["isolated", "fork", "resume"] },
        tools: { type: "array", items: { type: "string" } },
        skills: { type: "array", items: { type: "string" } },
        mcp_servers: { type: "array", items: { type: "string" } },
        permission_mode: { type: "string" },
        isolation: {
          type: "string",
          enum: ["none", "workspace", "worktree", "sandbox", "remote"],
        },
        workspace_root: { type: "string" },
        base_revision: { type: "string" },
        turns: { type: "array" },
        messages: { type: "array" },
        requests: { type: "array" },
        physical_dispatch: { type: "object" },
      },
      additionalProperties: true,
    };
    const controlSchema: JsonObject = {
      type: "object",
      required: ["task_id"],
      properties: {
        task_id: { type: "string" },
        expected_revision: { type: "integer", minimum: 0 },
        idempotency_key: { type: "string" },
        control_request_id: { type: "string" },
        control_nonce: { type: "string" },
        owner_idempotency_key: { type: "string" },
        expected_attempt: { type: "integer", minimum: 1 },
        expected_physical_lease_id: { type: "string" },
        message: { type: "string" },
        reason: { type: "string" },
        timeout_ms: { type: "integer", minimum: 1 },
      },
      additionalProperties: true,
    };
    return [
      spec(
        "Agent",
        "Create or fan out a canonical TypeScript child CodeWorker.",
        invocationSchema,
      ),
      spec(
        "Task",
        "Alias for Agent with identical E03 ownership and lifecycle.",
        invocationSchema,
      ),
      spec("agent_status", "Read canonical child status.", controlSchema, true),
      spec(
        "agent_cancel",
        "Cancel a child and fence late results.",
        controlSchema,
      ),
      spec(
        "agent_kill",
        "Kill a child through the physical process port.",
        controlSchema,
      ),
      spec(
        "agent_wait",
        "Wait for a child to reach a terminal state.",
        controlSchema,
        true,
      ),
      spec(
        "agent_result",
        "Read a terminal child result.",
        controlSchema,
        true,
      ),
      spec(
        "agent_resume",
        "Resume a child from exact correlated state.",
        controlSchema,
      ),
      spec(
        "agent_message",
        "Queue an idempotent steering message for a child.",
        controlSchema,
      ),
      spec(
        "agent_list",
        "List children owned by this parent session.",
        { type: "object", properties: {}, additionalProperties: true },
        true,
      ),
    ];
  }

  owns(toolName: string): boolean {
    return AGENT_TOOLS.has(toolName);
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    if (!this.owns(toolName))
      throw new E03RuntimeError(
        "unsupported_agent_tool",
        `unsupported agent tool ${toolName}`,
      );
    if (toolName === "Agent" || toolName === "Task") {
      const requests = Array.isArray(argumentsValue.requests)
        ? argumentsValue.requests.map(asObject)
        : [];
      return requests.length
        ? this.fanout(requests, argumentsValue, context)
        : this.spawn(argumentsValue, context);
    }
    return this.control(toolName, argumentsValue, context);
  }

  async drainBackground(context: AgentExecutionContext): Promise<void> {
    const coordinator = await this.coordinator(context);
    const tasks = coordinator.registry.list(context.parentInput.taskId);
    await this.backgroundSupervisor.drain(tasks, {
      workerId: `typescript-agent-runtime:${context.parentInput.sessionId}`,
      maximumConcurrency: Math.max(
        1,
        ...tasks.map((task) => task.definition.budget.maxConcurrency),
      ),
      run: async (task) => {
        const resumeInput = this.backgroundSupervisor.resumeInput(task);
        const settled = await coordinator.execution.run(
          task.identity.taskId,
          context.parentInput,
          resumeInput,
          `background-run:${task.identity.taskId}:${task.identity.attempt}`,
          `background-run:${task.identity.taskId}:${task.identity.leaseId}`,
        );
        return settled;
      },
    });
  }

  snapshot(): JsonObject {
    const snapshot = this.coordinatorValue?.registry.snapshot();
    return {
      version: "zyra.e03-agent-runtime/v1",
      canonical_logical_owner: "typescript.E03AgentControlCoordinator",
      durable_physical_port: this.coordinatorHost?.mutateAgent
        ? "python-cas-effect-port"
        : "uninitialized",
      task_registry_revision: snapshot?.revision ?? 0,
      task_ids: snapshot ? Object.keys(snapshot.tasks).sort() : [],
      background_queue_owner: "DurableTaskRegistry",
      queued_background_task_ids: snapshot
        ? Object.values(snapshot.tasks)
            .filter(
              (task) =>
                task.executionMode === "background" &&
                !["completed", "failed", "cancelled", "killed"].includes(
                  task.status,
                ),
            )
            .map((task) => task.identity.taskId)
            .sort()
        : [],
      background_claims:
        this.backgroundSupervisor.snapshot() as unknown as JsonObject[],
      background_claims_are_projection_only: true,
      omp_worker_dispatch:
        this.coordinatorValue?.execution.physicalDispatch.snapshot() ?? {
          version: "zyra.omp-worker-dispatch/v1",
          canonical_state_owner: "python.WorkerPoolStore",
          projection_only: true,
          jobs: [],
          semaphores: [],
        },
      python_agent_fallback: false,
      commit_protocol: ["prepare", "effect", "receipt", "commit", "ack"],
    };
  }

  private async spawn(
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const coordinator = await this.coordinator(context);
    const prompt = asString(argumentsValue.prompt).trim();
    if (!prompt)
      throw new E03RuntimeError(
        "empty_agent_prompt",
        "Agent requires a non-empty prompt",
      );
    const agent = normalizeAgentName(
      asString(argumentsValue.agent_type, "general"),
    );
    const idempotencyKey =
      asString(argumentsValue.idempotency_key) ||
      `agent:${digest({ parentTaskId: context.parentInput.taskId, parentSessionId: context.parentInput.sessionId, agent, prompt }).slice(0, 40)}`;
    const taskId =
      asString(argumentsValue.task_id) ||
      `agent-${digest(idempotencyKey).slice(0, 24)}`;
    const background = asBoolean(argumentsValue.background);
    if (
      background &&
      ((Array.isArray(argumentsValue.messages) &&
        argumentsValue.messages.length > 0) ||
        (Array.isArray(argumentsValue.turns) &&
          argumentsValue.turns.length > 0))
    )
      throw new E03RuntimeError(
        "background_input_not_durable",
        "background Agent accepts prompt/context references only; inline messages or turns cannot enter shadow state",
      );
    const requestId = `agent-create:${taskId}`;
    const envelope = this.envelope(
      context.parentInput,
      "agent.create",
      requestId,
      idempotencyKey,
      0,
      {
        task_id: taskId,
        agent,
        prompt,
        execution_mode: background ? "background" : "foreground",
        tools: list(argumentsValue.tools),
        skills: list(argumentsValue.skills),
        mcp_servers: list(argumentsValue.mcp_servers),
        isolation: asString(argumentsValue.isolation, "workspace"),
        workspace_root: asString(
          argumentsValue.workspace_root,
          workspaceRoot(context.parentInput),
        ),
        base_revision: asString(argumentsValue.base_revision, "HEAD"),
        start_immediately: !background,
        physical_dispatch: argumentsValue.physical_dispatch ?? null,
        messages: argumentsValue.messages ?? [],
        turns: argumentsValue.turns ?? [],
      },
    );
    const result = await coordinator.execute(envelope, context.parentInput);
    if (!result.ok)
      throw new E03RuntimeError(
        result.error || "agent_create_failed",
        String(result.state?.message ?? "Agent creation failed"),
      );
    if (background) {
      this.backgroundSupervisor.discover([
        coordinator.registry.require(taskId),
      ]);
    }
    return {
      summary: background
        ? `Agent ${taskId} queued in TypeScript E03 runtime`
        : `Agent ${taskId} completed through TypeScript QueryEngine`,
      output: {
        ...result.state,
        result: result.result,
        background,
        runtime_origin: result.runtime_origin,
      },
      metadata: {
        canonical_owner: "typescript",
        e03_task_id: taskId,
        e03_revision: String(result.revision),
        background: String(background),
        python_logical_owner: "false",
      },
    };
  }

  private async fanout(
    requests: JsonObject[],
    common: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const maximum = positive(
      common.maximum_concurrency,
      Math.min(requests.length, 4),
    );
    const output = await mapWithConcurrencyLimit(
      requests,
      maximum,
      async (_request, index) => {
        const { requests: _requests, ...requestValues } = {
          ...common,
          ...requests[index],
        };
        const request = {
          ...requestValues,
          idempotency_key:
            requests[index]!.idempotency_key ??
            `${asString(common.idempotency_key, "agent-fanout")}:${index}`,
          background: requests[index]!.background ?? common.background ?? true,
        } as JsonObject;
        try {
          return await this.spawn(request, context);
        } catch (error) {
          const failure = error instanceof Error ? error : new Error(String(error));
          if (common.failure_mode !== "collect") throw failure;
          const collected: AgentCapabilityResult = {
            summary: failure.message,
            output: { ok: false, error: failure.message },
            metadata: { canonical_owner: "typescript", failed: "true" },
          };
          return collected;
        }
      },
      { failureMode: common.failure_mode === "collect" ? "collect" : "fail-fast" },
    );
    return {
      summary: `Fanout created ${output.length} TypeScript-owned children`,
      output: {
        tasks: output.map((item) => item.output),
        count: output.length,
        maximum_concurrency: maximum,
      },
      metadata: {
        canonical_owner: "typescript",
        e03_fanout_count: String(output.length),
        background: "true",
      },
    };
  }

  private async control(
    toolName: string,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult> {
    const coordinator = await this.coordinator(context);
    const taskId = asString(argumentsValue.task_id);
    if (!taskId && toolName !== "agent_list")
      throw new E03RuntimeError(
        "missing_task_id",
        `${toolName} requires task_id`,
      );
    const task = taskId ? coordinator.registry.get(taskId) : null;
    const expectedRevision =
      typeof argumentsValue.expected_revision === "number"
        ? Math.floor(argumentsValue.expected_revision)
        : (task?.revision ?? 0);
    const idempotencyKey =
      asString(argumentsValue.idempotency_key) ||
      `${toolName}:${taskId || context.parentInput.taskId}:${expectedRevision}:${digest(argumentsValue).slice(0, 24)}`;
    const command = toolCommand(toolName);
    const body: JsonObject =
      toolName === "agent_list"
        ? {}
        : {
            task_id: taskId,
            message: argumentsValue.message,
            reason: argumentsValue.reason,
            timeout_ms: argumentsValue.timeout_ms,
            prompt: argumentsValue.prompt,
            control_nonce: argumentsValue.control_nonce,
            owner_idempotency_key: argumentsValue.owner_idempotency_key,
            expected_attempt: argumentsValue.expected_attempt,
            expected_physical_lease_id:
              argumentsValue.expected_physical_lease_id,
          };
    for (const key of Object.keys(body))
      if (body[key] === undefined || body[key] === "") delete body[key];
    const envelope = this.envelope(
      context.parentInput,
      command,
      asString(argumentsValue.control_request_id) ||
        `${command}:${taskId || "list"}`,
      idempotencyKey,
      expectedRevision,
      body,
    );
    const result = await coordinator.execute(envelope, context.parentInput);
    if (
      !result.ok &&
      !["agent.status", "agent.wait", "agent.result"].includes(command)
    )
      throw new E03RuntimeError(
        result.error || "agent_control_failed",
        String(result.state?.message ?? "agent control failed"),
      );
    return {
      summary: result.ok
        ? `${command} accepted at revision ${result.revision}`
        : `${command} rejected: ${result.error}`,
      output: {
        ...result.state,
        result: result.result,
        error: result.error,
        replayed: result.replayed,
        restored: result.restored,
      },
      metadata: {
        canonical_owner: "typescript",
        e03_command: command,
        e03_revision: String(result.revision),
        accepted: String(result.ok),
      },
    };
  }

  private async coordinator(
    context: AgentExecutionContext,
  ): Promise<E03AgentControlCoordinator> {
    if (this.coordinatorValue) {
      if (this.coordinatorHost !== context.host)
        throw new E03RuntimeError(
          "agent_host_changed",
          "TypeScriptAgentRuntime cannot change physical host within one parent session",
        );
      return this.coordinatorValue;
    }
    const port = new HostE03PhysicalPort(context.host, {
      runId: context.parentInput.runId,
      sessionId: context.parentInput.sessionId,
      parentTaskId: context.parentInput.taskId,
    });
    this.coordinatorValue = new E03AgentControlCoordinator({
      runId: context.parentInput.runId,
      sessionId: context.parentInput.sessionId,
      parentTaskId: context.parentInput.taskId,
      physicalPort: port,
      parentInput: context.parentInput,
      runChild: context.runChild,
      disabled:
        context.parentInput.config.runtimeConstraints
          ?.disable_e03_agent_control === true,
    });
    this.coordinatorHost = context.host;
    await this.coordinatorValue.restore();
    const configured =
      context.parentInput.config.runtimeConstraints
        ?.typescriptAgentDefinitions ??
      context.parentInput.config.runtimeConstraints
        ?.typescript_agent_definitions;
    const values = Array.isArray(configured)
      ? configured.map(asObject)
      : Object.entries(asObject(configured)).map(([name, value]) => ({
          name,
          ...asObject(value),
        }));
    for (const value of values)
      this.coordinatorValue.definitions.register({
        source: "project",
        ...value,
      } as any);
    return this.coordinatorValue;
  }

  private envelope(
    parent: RuntimeRunInput,
    command: E03ControlEnvelope["command"],
    requestId: string,
    idempotencyKey: string,
    expectedRevision: number,
    body: JsonObject,
  ): E03ControlEnvelope {
    return {
      schema_version: "3.0",
      request_id: requestId,
      idempotency_key: idempotencyKey,
      run_id: parent.runId,
      session_id: parent.sessionId,
      parent_task_id: parent.taskId,
      expected_revision: expectedRevision,
      command,
      body,
    };
  }
}

function spec(
  name: string,
  description: string,
  inputSchema: JsonObject,
  readOnly = false,
): ToolSpecContract {
  return {
    name,
    purpose: description,
    source: "typescript-e03-agent-control",
    input_schema: inputSchema,
    output_schema: { type: "object" },
    metadata: {
      owner: "typescript.E03AgentControlCoordinator",
      source: "claude-agenttool-adapted",
      commit_protocol: "prepare/effect/receipt/commit/ack",
      read_only: String(readOnly),
    },
  };
}

function toolCommand(name: string): E03ControlEnvelope["command"] {
  const commands: Record<string, E03ControlEnvelope["command"]> = {
    agent_status: "agent.status",
    agent_cancel: "agent.cancel",
    agent_kill: "agent.kill",
    agent_wait: "agent.wait",
    agent_result: "agent.result",
    agent_resume: "agent.resume",
    agent_message: "agent.steer",
    agent_list: "agent.list",
  };
  const selected = commands[name];
  if (!selected)
    throw new E03RuntimeError(
      "unsupported_agent_tool",
      `unsupported agent tool ${name}`,
    );
  return selected;
}

function normalizeAgentName(value: string): string {
  return value === "general-purpose" ? "general" : value;
}

function list(value: unknown): string[] {
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

function positive(value: unknown, fallback: number): number {
  return Number.isSafeInteger(value) && (value as number) > 0
    ? Math.min(value as number, 128)
    : Math.max(1, fallback);
}

function workspaceRoot(input: RuntimeRunInput): string {
  const roots =
    input.metadata && Array.isArray(input.metadata.workspace_roots)
      ? input.metadata.workspace_roots
      : [];
  return typeof roots[0] === "string" ? roots[0] : process.cwd();
}
