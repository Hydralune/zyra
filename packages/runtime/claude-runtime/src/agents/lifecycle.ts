import type { AgentTask, AgentTaskStatus, AgentToolResult } from "./contracts.ts";

const TRANSITIONS: Record<AgentTaskStatus, AgentTaskStatus[]> = {
  created: ["validating", "cancelled", "failed"],
  validating: ["ready", "cancelled", "failed"],
  ready: ["dispatched", "cancelled", "failed"],
  dispatched: ["running", "cancelled", "failed"],
  running: ["waiting", "completed", "failed", "cancelled"],
  waiting: ["resuming", "completed", "failed", "cancelled"],
  resuming: ["dispatched", "running", "failed", "cancelled"],
  completed: ["resuming"],
  failed: ["resuming"],
  cancelled: [],
};

export class AgentLifecycle {
  private readonly tasks = new Map<string, AgentTask>();
  private readonly idempotency = new Map<string, string>();

  create(task: AgentTask): AgentTask {
    const replayId = this.idempotency.get(task.idempotencyKey);
    if (replayId) {
      const replay = this.require(replayId);
      if (replay.promptDigest !== task.promptDigest || replay.definition.digest !== task.definition.digest) {
        throw new Error("agent idempotency key was reused with different input");
      }
      return replay;
    }
    if (this.tasks.has(task.taskId)) {
      throw new Error("agent task already exists: " + task.taskId);
    }
    this.tasks.set(task.taskId, task);
    this.idempotency.set(task.idempotencyKey, task.taskId);
    return task;
  }

  require(taskId: string): AgentTask {
    const task = this.tasks.get(taskId);
    if (!task) {
      throw new Error("agent task not found: " + taskId);
    }
    return task;
  }

  get(taskId: string): AgentTask | null {
    return this.tasks.get(taskId) ?? null;
  }

  hydrate(task: AgentTask): AgentTask {
    const existing = this.tasks.get(task.taskId);
    if (existing) {
      if (existing.idempotencyKey !== task.idempotencyKey || existing.promptDigest !== task.promptDigest) {
        throw new Error("durable agent task identity conflict");
      }
      return existing;
    }
    const replayId = this.idempotency.get(task.idempotencyKey);
    if (replayId && replayId !== task.taskId) {
      throw new Error("durable agent idempotency key belongs to another task");
    }
    this.tasks.set(task.taskId, task);
    this.idempotency.set(task.idempotencyKey, task.taskId);
    return task;
  }

  transition(taskId: string, status: AgentTaskStatus, expectedRevision: number): AgentTask {
    const task = this.require(taskId);
    if (task.revision !== expectedRevision) {
      throw new Error("agent revision conflict");
    }
    if (status !== task.status && !TRANSITIONS[task.status].includes(status)) {
      throw new Error("invalid agent transition " + task.status + " -> " + status);
    }
    task.status = status;
    task.revision += 1;
    task.updatedAt = new Date().toISOString();
    return task;
  }

  acceptDurable(taskId: string, status: AgentTaskStatus, revision: number): AgentTask {
    const task = this.require(taskId);
    if (revision < task.revision) {
      throw new Error("durable agent revision regressed");
    }
    task.status = status;
    task.revision = revision;
    task.updatedAt = new Date().toISOString();
    return task;
  }

  commitResult(taskId: string, result: AgentToolResult, digest: string): AgentTask {
    const task = this.require(taskId);
    if (task.status === "cancelled") {
      throw new Error("late agent result cannot commit after cancellation");
    }
    task.result = result;
    task.resultDigest = digest;
    task.error = result.error;
    return task;
  }

  list(parentTaskId?: string): AgentTask[] {
    return [...this.tasks.values()]
      .filter((task) => !parentTaskId || task.parentTaskId === parentTaskId)
      .sort((left, right) => left.taskId.localeCompare(right.taskId));
  }

  snapshot(): unknown[] {
    return this.list().map((task) => ({
      task_id: task.taskId,
      parent_task_id: task.parentTaskId,
      parent_session_id: task.parentSessionId,
      child_session_id: task.childSessionId,
      run_id: task.runId,
      definition_name: task.definition.name,
      definition_digest: task.definition.digest,
      prompt_digest: task.promptDigest,
      execution_mode: task.executionMode,
      status: task.status,
      revision: task.revision,
      attempt: task.attempt,
      sequence: task.sequence,
      scope_digest: task.scope.digest,
      context_digest: task.context.digest,
      idempotency_key: task.idempotencyKey,
      result_digest: task.resultDigest,
      error: task.error,
      created_at: task.createdAt,
      updated_at: task.updatedAt,
    }));
  }
}
