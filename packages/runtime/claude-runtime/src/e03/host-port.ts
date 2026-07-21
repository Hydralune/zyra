import type {
  AgentMutationReceipt,
  JsonObject,
  RuntimeHost,
} from "../contracts.ts";
import {
  effectRequestDigest,
  E03RuntimeError,
  type E03EffectReceipt,
  type E03EffectRequest,
  type E03PhysicalPort,
  type E03RegistrySnapshot,
} from "./contracts.ts";

export class HostE03PhysicalPort implements E03PhysicalPort {
  constructor(
    private readonly host: RuntimeHost,
    private readonly authority: {
      runId: string;
      sessionId: string;
      parentTaskId: string;
    },
  ) {}

  async restore(
    runId: string,
    sessionId: string,
  ): Promise<E03RegistrySnapshot | null> {
    this.assertAuthority(runId, sessionId);
    const receipt = await this.call(
      "e03.restore",
      this.authority.parentTaskId,
      {
        run_id: runId,
        parent_session_id: sessionId,
        parent_task_id: this.authority.parentTaskId,
      },
    );
    if (!receipt.accepted) {
      if (
        receipt.error === "e03_snapshot_not_found" ||
        receipt.error_code === "e03_snapshot_not_found"
      )
        return null;
      throw new E03RuntimeError(
        "e03_restore_rejected",
        String(receipt.error || "E03 restore rejected"),
      );
    }
    const snapshot = receipt.snapshot;
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot))
      return null;
    return structuredClone(snapshot) as unknown as E03RegistrySnapshot;
  }

  async effect(request: E03EffectRequest): Promise<E03EffectReceipt> {
    const receipt = await this.call("e03.effect", request.taskId, {
      run_id: this.authority.runId,
      parent_session_id: this.authority.sessionId,
      parent_task_id: this.authority.parentTaskId,
      effect_request: request as unknown as JsonObject,
    });
    const value = receipt.effect_receipt;
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new E03RuntimeError(
        "invalid_e03_effect_receipt",
        `Python physical port returned no typed effect receipt: ${String(receipt.error || receipt.error_code || "unknown rejection")}`,
        {
          physicalPortError: String(receipt.error || ""),
          physicalPortErrorCode: String(receipt.error_code || ""),
        },
      );
    const typed = structuredClone(value) as unknown as E03EffectReceipt;
    if (
      typed.idempotencyKey !== request.idempotencyKey ||
      typed.requestDigest !== effectRequestDigest(request)
    )
      throw new E03RuntimeError(
        "invalid_e03_effect_receipt",
        "Python physical port returned a receipt for different semantic content",
      );
    return typed;
  }

  async compareAndSwap(
    expectedRevision: number,
    snapshot: E03RegistrySnapshot,
  ): Promise<{
    accepted: boolean;
    revision: number;
    replayed: boolean;
    error: string;
  }> {
    const receipt = await this.call("e03.cas", this.authority.parentTaskId, {
      run_id: this.authority.runId,
      parent_session_id: this.authority.sessionId,
      parent_task_id: this.authority.parentTaskId,
      expected_registry_revision: expectedRevision,
      registry_snapshot: snapshot as unknown as JsonObject,
    });
    return {
      accepted: receipt.accepted,
      revision:
        typeof receipt.revision === "number"
          ? receipt.revision
          : expectedRevision,
      replayed: receipt.replayed === true,
      error: String(receipt.error ?? ""),
    };
  }

  private async call(
    action: string,
    taskId: string,
    payload: JsonObject,
  ): Promise<AgentMutationReceipt> {
    if (!this.host.mutateAgent)
      throw new E03RuntimeError(
        "agent_physical_port_unavailable",
        "agent durable/physical host port is unavailable",
      );
    return this.host.mutateAgent({ action, task_id: taskId, ...payload });
  }

  private assertAuthority(runId: string, sessionId: string): void {
    if (
      runId !== this.authority.runId ||
      sessionId !== this.authority.sessionId
    )
      throw new E03RuntimeError(
        "host_port_authority",
        "restore authority differs from HostE03PhysicalPort",
      );
  }
}
