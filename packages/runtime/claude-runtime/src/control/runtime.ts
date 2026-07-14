import { asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";
import type { ControlReceipt, ControlRuntimeState } from "./contracts.ts";
import { canonicalDigest } from "../agents/memory.ts";

export class TypeScriptControlRuntime {
  private readonly state: ControlRuntimeState;
  private readonly idempotency = new Map<string, ControlReceipt>();

  constructor(modelName: string, restored?: JsonObject) {
    const snapshot = asObject(restored);
    const expectedChecksum = asString(snapshot.checksum);
    if (expectedChecksum) {
      const payload = { ...snapshot };
      delete payload.checksum;
      if (canonicalDigest(payload) !== expectedChecksum) {
        throw new Error("TypeScript control snapshot checksum mismatch");
      }
    }
    this.state = {
      revision: integer(snapshot.revision),
      contextEpoch: integer(snapshot.context_epoch),
      compactRequested: snapshot.compact_requested === true,
      clearRequested: snapshot.clear_requested === true,
      cancelled: snapshot.cancelled === true,
      modelName: asString(snapshot.model_name, modelName),
      receipts: [],
    };
    for (const raw of Array.isArray(snapshot.receipts) ? snapshot.receipts : []) {
      const value = asObject(raw);
      const receipt: ControlReceipt = {
        requestId: asString(value.request_id),
        name: asString(value.name),
        action: asString(value.action),
        accepted: value.accepted === true,
        changed: value.changed === true,
        revisionBefore: integer(value.revision_before),
        revisionAfter: integer(value.revision_after),
        status: asString(value.status, "rejected") as ControlReceipt["status"],
        effect: asObject(value.effect),
        error: asString(value.error),
      };
      if (receipt.requestId) {
        this.idempotency.set(receipt.requestId, receipt);
      }
    }
  }

  apply(raw: JsonValue): ControlReceipt {
    const command = asObject(raw);
    const name = asString(command.name).trim().replace(/^\//, "");
    const action = asString(command.action, inferAction(name, command));
    const requestId = asString(command.request_id)
      || "control_" + canonicalDigest([name, action, command]).slice(7, 31);
    const replay = this.idempotency.get(requestId);
    if (replay) {
      return replay;
    }
    const before = this.state.revision;
    const expected = command.expected_revision;
    if (typeof expected === "number" && expected !== before) {
      return this.record({
        requestId,
        name,
        action,
        accepted: false,
        changed: false,
        revisionBefore: before,
        revisionAfter: before,
        status: "conflict",
        effect: {},
        error: "control_revision_conflict",
      });
    }
    let changed = false;
    let status: ControlReceipt["status"] = "ok";
    let error = "";
    const effect: JsonObject = {
      canonical_control_owner: "typescript",
      command_name: name,
      action,
    };
    if (["context", "tools", "doctor", "permissions", "mcp", "skills", "agents", "task", "session"].includes(name)) {
      effect.read_only = true;
    } else if (name === "resume") {
      changed = true;
      effect.resume_requested = true;
    } else if (name === "compact") {
      changed = true;
      this.state.compactRequested = true;
      this.state.contextEpoch += 1;
      effect.compact_requested = true;
      effect.context_epoch = this.state.contextEpoch;
    } else if (name === "clear") {
      changed = true;
      this.state.clearRequested = true;
      this.state.compactRequested = true;
      this.state.contextEpoch += 1;
      effect.clear_requested = true;
      effect.context_epoch = this.state.contextEpoch;
    } else if (name === "model") {
      const model = asString(command.model ?? asObject(command.arguments).model).trim();
      if (!model) {
        status = "rejected";
        error = "model_control_requires_model";
      } else {
        changed = model !== this.state.modelName;
        this.state.modelName = model;
        effect.model_name = model;
      }
    } else if (name === "cancel") {
      changed = !this.state.cancelled;
      this.state.cancelled = true;
      effect.cancelled = true;
    } else {
      status = "unsupported";
      error = "unsupported_control_command";
    }
    if (changed && status === "ok") {
      this.state.revision += 1;
    }
    return this.record({
      requestId,
      name,
      action,
      accepted: status === "ok",
      changed,
      revisionBefore: before,
      revisionAfter: this.state.revision,
      status,
      effect,
      error,
    });
  }

  snapshot(): JsonObject {
    const payload: JsonObject = {
      version: "zyra.typescript-control-runtime.v1",
      canonical_owner: "typescript",
      revision: this.state.revision,
      context_epoch: this.state.contextEpoch,
      compact_requested: this.state.compactRequested,
      clear_requested: this.state.clearRequested,
      cancelled: this.state.cancelled,
      model_name: this.state.modelName,
      receipts: this.state.receipts.map(receiptPayload),
      python_control_fallback: false,
    };
    return { ...payload, checksum: canonicalDigest(payload) };
  }

  get revision(): number {
    return this.state.revision;
  }

  get modelName(): string {
    return this.state.modelName;
  }

  get compactRequested(): boolean {
    return this.state.compactRequested;
  }

  get cancelled(): boolean {
    return this.state.cancelled;
  }

  private record(receipt: ControlReceipt): ControlReceipt {
    this.state.receipts.push(receipt);
    this.idempotency.set(receipt.requestId, receipt);
    return receipt;
  }
}

function receiptPayload(value: ControlReceipt): JsonObject {
  return {
    request_id: value.requestId,
    name: value.name,
    action: value.action,
    accepted: value.accepted,
    changed: value.changed,
    revision_before: value.revisionBefore,
    revision_after: value.revisionAfter,
    status: value.status,
    effect: value.effect,
    error: value.error,
  };
}

function inferAction(name: string, command: JsonObject): string {
  if (["compact", "clear", "cancel", "resume"].includes(name)) {
    return name;
  }
  if (name === "model" && (command.model || asObject(command.arguments).model)) {
    return "set";
  }
  return "inspect";
}

function integer(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}
