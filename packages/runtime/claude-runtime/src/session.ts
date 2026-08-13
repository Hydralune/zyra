import { createHash } from "node:crypto";

import {
  asObject,
  asString,
  cloneJson,
  jsonChars,
  runtimeId,
  type JsonObject,
  type JsonValue,
  type ToolExecutionResponse,
} from "./contracts.ts";
import { projectToolOutputForRuntime } from "./tools/model-result-projection.ts";

export const SESSION_SNAPSHOT_VERSION = "zyra.typescript-query-session.v1";

export interface RuntimeMessage {
  message_id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  turn_index: number | null;
  tool_call_id: string | null;
  created_at: string;
  metadata: JsonObject;
}

export interface RuntimeTurn {
  turn_id: string;
  turn_index: number;
  status: "active" | "completed" | "failed";
  started_at: string;
  completed_at: string | null;
  tool_call_ids: string[];
  error: string | null;
}

export interface SessionSnapshot extends JsonObject {
  version: typeof SESSION_SNAPSHOT_VERSION;
  canonical_owner: "typescript";
  runtime_id: "zyra-typescript-claude-runtime";
  session_id: string;
  run_id: string;
  task_id: string;
  worker_request_id: string;
  phase: string;
  revision: number;
  turn_count: number;
  tool_call_count: number;
  compaction_count: number;
  messages: JsonValue[];
  turns: JsonValue[];
  context: JsonObject;
  lineage: JsonObject;
  checksum: string;
}

export class RuntimeSession {
  readonly sessionId: string;
  readonly runId: string;
  readonly taskId: string;
  readonly workerRequestId: string;
  readonly messages: RuntimeMessage[];
  readonly turns: RuntimeTurn[];
  revision: number;
  compactionCount: number;
  phase: string;
  restored: boolean;
  parentChecksum: string;
  private activeTurn: RuntimeTurn | null;

  private constructor(
    sessionId: string,
    runId: string,
    taskId: string,
    workerRequestId: string,
    messages: RuntimeMessage[] = [],
    turns: RuntimeTurn[] = [],
  ) {
    this.sessionId = sessionId;
    this.runId = runId;
    this.taskId = taskId;
    this.workerRequestId = workerRequestId;
    this.messages = messages;
    this.turns = turns;
    this.revision = 0;
    this.compactionCount = 0;
    this.phase = "created";
    this.restored = false;
    this.parentChecksum = "";
    this.activeTurn = null;
  }

  static create(
    sessionId: string,
    runId: string,
    taskId: string,
    workerRequestId: string,
    inputMessages: JsonObject[],
  ): RuntimeSession {
    const session = new RuntimeSession(sessionId, runId, taskId, workerRequestId);
    for (const value of inputMessages) {
      const roleValue = asString(value.role, "user");
      const role = roleValue === "system" || roleValue === "assistant" || roleValue === "tool"
        ? roleValue
        : "user";
      const contentValue = value.content;
      const content = typeof contentValue === "string"
        ? contentValue
        : JSON.stringify(contentValue ?? "");
      session.messages.push({
        message_id: asString(value.message_id) || runtimeId("message"),
        role,
        content,
        turn_index: null,
        tool_call_id: asString(value.tool_call_id) || null,
        created_at: new Date().toISOString(),
        metadata: asObject(value.metadata),
      });
    }
    session.bump("session_started");
    return session;
  }

  static restore(
    snapshotValue: JsonObject,
    expected: {
      sessionId: string;
      runId: string;
      taskId: string;
      workerRequestId: string;
    },
  ): RuntimeSession {
    const snapshot = asObject(snapshotValue);
    if (snapshot.version !== SESSION_SNAPSHOT_VERSION) {
      throw new Error("unsupported_typescript_session_snapshot");
    }
    if (snapshot.canonical_owner !== "typescript") {
      throw new Error("typescript_session_owner_mismatch");
    }
    if (asString(snapshot.session_id) !== expected.sessionId) {
      throw new Error("typescript_session_id_mismatch");
    }
    if (asString(snapshot.task_id) !== expected.taskId) {
      throw new Error("typescript_session_task_mismatch");
    }
    const suppliedChecksum = asString(snapshot.checksum);
    const unsigned = { ...snapshot };
    delete unsigned.checksum;
    if (!suppliedChecksum || checksum(unsigned) !== suppliedChecksum) {
      throw new Error("typescript_session_checksum_mismatch");
    }
    const messages = Array.isArray(snapshot.messages)
      ? snapshot.messages.map((item) => normalizeMessage(asObject(item)))
      : [];
    const turns = Array.isArray(snapshot.turns)
      ? snapshot.turns.map((item) => normalizeTurn(asObject(item)))
      : [];
    const session = new RuntimeSession(
      expected.sessionId,
      expected.runId,
      expected.taskId,
      expected.workerRequestId,
      messages,
      turns,
    );
    session.revision = Number(snapshot.revision) || 0;
    session.compactionCount = Number(snapshot.compaction_count) || 0;
    session.phase = "context_restored";
    session.restored = true;
    session.parentChecksum = suppliedChecksum;
    const activeTurns = turns.filter((turn) => turn.status === "active");
    if (activeTurns.length > 1) {
      throw new Error("typescript_session_multiple_active_turns");
    }
    if (activeTurns[0] && activeTurns[0].completed_at !== null) {
      throw new Error("typescript_session_active_turn_completed");
    }
    session.activeTurn = activeTurns[0] ?? null;
    session.bump("context_restored");
    return session;
  }

  beginTurn(turnIndex: number, prompt: string): RuntimeTurn {
    if (this.activeTurn) {
      throw new Error("query_turn_already_active");
    }
    const turn: RuntimeTurn = {
      turn_id: runtimeId("turn"),
      turn_index: turnIndex,
      status: "active",
      started_at: new Date().toISOString(),
      completed_at: null,
      tool_call_ids: [],
      error: null,
    };
    this.turns.push(turn);
    this.activeTurn = turn;
    if (prompt) {
      this.messages.push({
        message_id: runtimeId("message"),
        role: "user",
        content: prompt,
        turn_index: turnIndex,
        tool_call_id: null,
        created_at: new Date().toISOString(),
        metadata: {},
      });
    }
    this.bump("turn_started");
    return turn;
  }

  activeTurnSnapshot(): RuntimeTurn | null {
    return this.activeTurn ? structuredClone(this.activeTurn) : null;
  }

  recordToolCall(toolCallId: string, toolName: string): void {
    if (!this.activeTurn) {
      throw new Error("tool_call_without_active_turn");
    }
    this.activeTurn.tool_call_ids.push(toolCallId);
    this.messages.push({
      message_id: runtimeId("message"),
      role: "assistant",
      content: "tool_use:" + toolName,
      turn_index: this.activeTurn.turn_index,
      tool_call_id: toolCallId,
      created_at: new Date().toISOString(),
      metadata: { tool_name: toolName },
    });
    this.bump("tool_call_started");
  }

  recordToolResult(toolName: string, result: ToolExecutionResponse): void {
    const turnIndex = this.activeTurn?.turn_index ?? null;
    this.messages.push({
      message_id: runtimeId("message"),
      role: "tool",
      content: JSON.stringify({
        ok: result.ok,
        summary: result.summary,
        output: projectToolOutputForRuntime(result.output),
        error: result.error ?? null,
      }),
      turn_index: turnIndex,
      tool_call_id: result.tool_call_id,
      created_at: new Date().toISOString(),
      metadata: {
        tool_name: toolName,
        artifact_ids: result.artifacts.map((item) => item.artifact_id),
      },
    });
    this.bump("tool_call_completed");
  }

  completeTurn(ok: boolean, error: string | null = null): void {
    if (!this.activeTurn) {
      return;
    }
    this.activeTurn.status = ok ? "completed" : "failed";
    this.activeTurn.error = error;
    this.activeTurn.completed_at = new Date().toISOString();
    this.activeTurn = null;
    this.bump("turn_completed");
  }

  compact(
    summary: string,
    artifactId: string,
    preservedMessages: RuntimeMessage[],
    replacementMessages: readonly RuntimeMessage[] | null = null,
  ): void {
    const fallbackMessages: RuntimeMessage[] = [{
      message_id: runtimeId("message"),
      role: "system",
      content: summary,
      turn_index: null,
      tool_call_id: null,
      created_at: new Date().toISOString(),
      metadata: {
        compact_artifact_id: artifactId,
        compacted: true,
      },
    }, ...preservedMessages.map((item) => structuredClone(item))];
    const selectedMessages = replacementMessages === null
      ? fallbackMessages
      : replacementMessages.map((item, index) => ({
        ...structuredClone(item),
        metadata: index === 0
          ? {
            ...structuredClone(item.metadata),
            compact_artifact_id: artifactId,
            compacted: true,
          }
          : structuredClone(item.metadata),
      }));
    if (selectedMessages.length === 0) throw new Error("post_compact_messages_required");
    this.messages.splice(0, this.messages.length, ...selectedMessages);
    this.compactionCount += 1;
    this.bump("context_compacted");
  }

  compactCandidates(): {
    content: string;
    summary: string;
    preserved: RuntimeMessage[];
    removedCount: number;
  } {
    const preserveCount = Math.min(4, this.messages.length);
    const removed = this.messages.slice(0, Math.max(0, this.messages.length - preserveCount));
    const preserved = this.messages.slice(this.messages.length - preserveCount);
    const content = JSON.stringify({
      session_id: this.sessionId,
      removed_messages: removed,
      parent_checksum: this.parentChecksum,
    });
    const summary = "Compacted " + String(removed.length) + " earlier runtime messages.";
    return {
      content,
      summary,
      preserved,
      removedCount: removed.length,
    };
  }

  contextChars(): number {
    return jsonChars(this.messages);
  }

  finish(ok: boolean): void {
    if (this.activeTurn) {
      this.completeTurn(ok, ok ? null : "session_terminated");
    }
    this.bump(ok ? "session_completed" : "session_failed");
  }

  snapshot(): SessionSnapshot {
    const unsigned: Omit<SessionSnapshot, "checksum"> = {
      version: SESSION_SNAPSHOT_VERSION,
      canonical_owner: "typescript",
      runtime_id: "zyra-typescript-claude-runtime",
      session_id: this.sessionId,
      run_id: this.runId,
      task_id: this.taskId,
      worker_request_id: this.workerRequestId,
      phase: this.phase,
      revision: this.revision,
      turn_count: this.turns.length,
      tool_call_count: this.turns.reduce((total, turn) => total + turn.tool_call_ids.length, 0),
      compaction_count: this.compactionCount,
      messages: cloneJson(this.messages as unknown as JsonValue[]),
      turns: cloneJson(this.turns as unknown as JsonValue[]),
      context: {
        chars: this.contextChars(),
        message_count: this.messages.length,
        compacted: this.compactionCount > 0,
      },
      lineage: {
        restored: this.restored,
        parent_checksum: this.parentChecksum,
      },
    };
    return {
      ...unsigned,
      checksum: checksum(unsigned),
    } as SessionSnapshot;
  }

  private bump(phase: string): void {
    this.phase = phase;
    this.revision += 1;
  }
}

function checksum(value: object): string {
  return "sha256:" + createHash("sha256").update(stableJson(value)).digest("hex");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) {
    return "[" + value.map(stableJson).join(",") + "]";
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return "{" + Object.keys(record).sort().map((key) => {
      return JSON.stringify(key) + ":" + stableJson(record[key]);
    }).join(",") + "}";
  }
  return JSON.stringify(value);
}

function normalizeMessage(value: JsonObject): RuntimeMessage {
  const roleValue = asString(value.role);
  const role = roleValue === "system" || roleValue === "assistant" || roleValue === "tool"
    ? roleValue
    : "user";
  return {
    message_id: asString(value.message_id) || runtimeId("message"),
    role,
    content: asString(value.content),
    turn_index: typeof value.turn_index === "number" ? value.turn_index : null,
    tool_call_id: asString(value.tool_call_id) || null,
    created_at: asString(value.created_at) || new Date().toISOString(),
    metadata: asObject(value.metadata),
  };
}

function normalizeTurn(value: JsonObject): RuntimeTurn {
  const statusValue = asString(value.status);
  const status = statusValue === "failed" || statusValue === "active" ? statusValue : "completed";
  return {
    turn_id: asString(value.turn_id) || runtimeId("turn"),
    turn_index: typeof value.turn_index === "number" ? value.turn_index : 0,
    status,
    started_at: asString(value.started_at) || new Date().toISOString(),
    completed_at: asString(value.completed_at) || null,
    tool_call_ids: Array.isArray(value.tool_call_ids)
      ? value.tool_call_ids.map((item) => asString(item)).filter(Boolean)
      : [],
    error: asString(value.error) || null,
  };
}
