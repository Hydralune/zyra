import {
  cloneJson,
  contractError,
  digest,
  nowIso,
  toolScopeIsSubset,
  type CompactPolicy,
  type CompactRestoreProjection,
  type JsonObject,
} from "./contracts.ts";
import { SkillContextProjector, type SkillContextProjectionInput, type SkillContextProjectionResult } from "./context-projector.ts";

export interface CompactRestoreBridgeReceipt {
  receiptId: string;
  projectionId: string;
  boundaryId: string;
  operation: "prepare" | "apply" | "reject" | "expire" | "replay";
  stateBefore: CompactRestoreProjection["state"] | null;
  stateAfter: CompactRestoreProjection["state"];
  contextEpochBefore: number;
  contextEpochAfter: number;
  providerMessageDigest: string | null;
  reason: string;
  occurredAt: string;
  receiptDigest: string;
  metadata: JsonObject;
}

export interface CompactRestoreBridgeSnapshot {
  version: "zyra.compact-restore-memory-bridge/v1";
  revision: number;
  contextEpoch: number;
  projections: CompactRestoreProjection[];
  providerMessages: Array<[string, JsonObject]>;
  receipts: CompactRestoreBridgeReceipt[];
  appliedBoundaryIds: string[];
  capturedAt: string;
  checksum: string;
}

export class CompactRestoreMemoryBridge {
  private readonly projector: SkillContextProjector;
  private readonly policy: CompactPolicy;
  private readonly projections = new Map<string, CompactRestoreProjection>();
  private readonly boundaryBindings = new Map<string, string>();
  private readonly providerMessages = new Map<string, JsonObject>();
  private readonly receipts: CompactRestoreBridgeReceipt[] = [];
  private readonly appliedBoundaryIds = new Set<string>();
  private readonly now: () => Date;
  private revision = 0;
  private contextEpoch = 0;

  constructor(options: {
    projector: SkillContextProjector;
    policy: CompactPolicy;
    initialContextEpoch?: number;
    now?: () => Date;
    snapshot?: CompactRestoreBridgeSnapshot | null;
  }) {
    this.projector = options.projector;
    this.policy = cloneJson(options.policy);
    this.now = options.now ?? (() => new Date());
    this.contextEpoch = Math.max(0, Math.floor(options.initialContextEpoch ?? 0));
    if (options.snapshot) this.restore(options.snapshot);
  }

  prepare(inputValue: Omit<SkillContextProjectionInput, "contextEpochBefore"> & { contextEpochBefore?: number }): SkillContextProjectionResult {
    this.assertEnabled();
    const contextEpochBefore = inputValue.contextEpochBefore ?? this.contextEpoch;
    if (contextEpochBefore !== this.contextEpoch) {
      throw contractError("compact_restore_epoch_conflict", "compact restore projection must start at the current context epoch", {
        supplied_epoch: contextEpochBefore,
        current_epoch: this.contextEpoch,
      });
    }
    const existingProjectionId = this.boundaryBindings.get(inputValue.boundaryId);
    if (existingProjectionId) {
      const existing = this.require(existingProjectionId);
      const providerMessage = this.providerMessages.get(existing.projectionId);
      if (!providerMessage) throw contractError("compact_restore_provider_message_missing", "prepared projection is missing its provider message");
      this.receipts.push(this.makeReceipt(existing, "replay", existing.state, existing.state, "boundary_already_prepared"));
      return {
        projection: cloneJson(existing),
        providerMessage: cloneJson(providerMessage),
        contextDigest: String(existing.metadata.context_digest ?? ""),
        selectedMemoryIds: [...existing.skillMemoryIds],
        selectedProcedureIds: [...existing.procedureIds],
        droppedMemoryIds: stringArray(existing.metadata.dropped_memory_ids),
        droppedProcedureIds: stringArray(existing.metadata.dropped_procedure_ids),
        totalTokens: Number(existing.metadata.selected_tokens ?? 0),
      };
    }
    const result = this.projector.project({ ...inputValue, contextEpochBefore });
    if (result.projection.contextEpochAfter !== contextEpochBefore + 1) {
      throw contractError("compact_restore_epoch_advance", "compact restore projection must advance exactly one context epoch");
    }
    if (!toolScopeIsSubset(result.projection.allowedToolsBefore, result.projection.allowedToolsAfter, result.projection.deniedTools)) {
      throw contractError("compact_restore_scope_widened", "prepared compact restore projection widened tool scope");
    }
    this.projections.set(result.projection.projectionId, cloneJson(result.projection));
    this.boundaryBindings.set(result.projection.boundaryId, result.projection.projectionId);
    this.providerMessages.set(result.projection.projectionId, cloneJson(result.providerMessage));
    this.receipts.push(this.makeReceipt(result.projection, "prepare", null, "prepared", "projection_prepared"));
    this.revision += 1;
    return cloneJson(result);
  }

  apply(projectionId: string): { projection: CompactRestoreProjection; providerMessage: JsonObject; receipt: CompactRestoreBridgeReceipt } {
    this.assertEnabled();
    const projection = this.require(projectionId);
    const message = this.providerMessages.get(projection.projectionId);
    if (!message) throw contractError("compact_restore_provider_message_missing", "compact restore provider message is missing");
    if (projection.state === "applied") {
      const receipt = this.makeReceipt(projection, "replay", "applied", "applied", "projection_already_applied");
      this.receipts.push(receipt);
      return { projection: cloneJson(projection), providerMessage: cloneJson(message), receipt: cloneJson(receipt) };
    }
    if (projection.state !== "prepared") throw contractError("compact_restore_apply_state", `compact restore projection cannot apply from ${projection.state}`);
    if (projection.expiresAt && Date.parse(projection.expiresAt) <= this.now().getTime()) {
      const expired = this.expire(projection.projectionId, "projection_ttl_elapsed");
      throw contractError("compact_restore_projection_expired", `compact restore projection expired: ${expired.projectionId}`);
    }
    if (projection.contextEpochBefore !== this.contextEpoch) {
      throw contractError("compact_restore_apply_epoch_conflict", "compact restore projection was prepared from a stale context epoch", {
        projection_epoch: projection.contextEpochBefore,
        current_epoch: this.contextEpoch,
      });
    }
    if (this.appliedBoundaryIds.has(projection.boundaryId)) throw contractError("compact_restore_boundary_already_applied", "compact boundary was already applied by another projection");
    const before = projection.state;
    projection.state = "applied";
    projection.appliedAt = nowIso(this.now);
    projection.projectionDigest = projectionDigest(projection);
    this.contextEpoch = projection.contextEpochAfter;
    this.appliedBoundaryIds.add(projection.boundaryId);
    this.revision += 1;
    const receipt = this.makeReceipt(projection, "apply", before, "applied", "projection_applied_to_next_worker_context");
    this.receipts.push(receipt);
    return { projection: cloneJson(projection), providerMessage: cloneJson(message), receipt: cloneJson(receipt) };
  }

  reject(projectionId: string, reason: string): CompactRestoreProjection {
    const projection = this.require(projectionId);
    if (projection.state === "applied") throw contractError("compact_restore_reject_applied", "applied compact restore projection cannot be rejected");
    if (projection.state === "rejected") return cloneJson(projection);
    const before = projection.state;
    projection.state = "rejected";
    projection.rejectionReason = reason.trim().slice(0, 4_096) || "projection_rejected";
    projection.projectionDigest = projectionDigest(projection);
    this.receipts.push(this.makeReceipt(projection, "reject", before, "rejected", projection.rejectionReason));
    this.revision += 1;
    return cloneJson(projection);
  }

  expire(projectionId: string, reason = "projection_expired"): CompactRestoreProjection {
    const projection = this.require(projectionId);
    if (projection.state === "applied") throw contractError("compact_restore_expire_applied", "applied compact restore projection cannot expire");
    if (projection.state === "expired") return cloneJson(projection);
    const before = projection.state;
    projection.state = "expired";
    projection.rejectionReason = reason;
    projection.projectionDigest = projectionDigest(projection);
    this.receipts.push(this.makeReceipt(projection, "expire", before, "expired", reason));
    this.revision += 1;
    return cloneJson(projection);
  }

  expireDue(at = nowIso(this.now)): string[] {
    const timestamp = Date.parse(at);
    const expired: string[] = [];
    for (const projection of this.projections.values()) {
      if (projection.state !== "prepared" || !projection.expiresAt || Date.parse(projection.expiresAt) > timestamp) continue;
      this.expire(projection.projectionId);
      expired.push(projection.projectionId);
    }
    return expired;
  }

  get(projectionId: string): CompactRestoreProjection | null {
    const projection = this.projections.get(projectionId.trim());
    return projection ? cloneJson(projection) : null;
  }

  getByBoundary(boundaryId: string): CompactRestoreProjection | null {
    const projectionId = this.boundaryBindings.get(boundaryId.trim());
    return projectionId ? this.get(projectionId) : null;
  }

  pending(): CompactRestoreProjection[] {
    return [...this.projections.values()].filter((item) => item.state === "prepared").sort(projectionOrder).map(cloneJson);
  }

  list(): CompactRestoreProjection[] {
    return [...this.projections.values()].sort(projectionOrder).map(cloneJson);
  }

  currentContextEpoch(): number {
    return this.contextEpoch;
  }

  health(): JsonObject {
    const values = [...this.projections.values()];
    return {
      canonical_owner: "CompactRestoreMemoryBridge",
      revision: this.revision,
      context_epoch: this.contextEpoch,
      projection_count: values.length,
      prepared_count: values.filter((item) => item.state === "prepared").length,
      applied_count: values.filter((item) => item.state === "applied").length,
      rejected_count: values.filter((item) => item.state === "rejected").length,
      expired_count: values.filter((item) => item.state === "expired").length,
      applied_boundary_count: this.appliedBoundaryIds.size,
      consumes_02d_restore: true,
      writes_through_02b_context: true,
      tool_scope_can_widen: false,
    };
  }

  snapshot(): CompactRestoreBridgeSnapshot {
    const unsigned: Omit<CompactRestoreBridgeSnapshot, "checksum"> = {
      version: "zyra.compact-restore-memory-bridge/v1",
      revision: this.revision,
      contextEpoch: this.contextEpoch,
      projections: this.list(),
      providerMessages: [...this.providerMessages.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([id, message]) => [id, cloneJson(message)]),
      receipts: this.receipts.map(cloneJson),
      appliedBoundaryIds: [...this.appliedBoundaryIds].sort(),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: CompactRestoreBridgeSnapshot): void {
    if (snapshotValue.version !== "zyra.compact-restore-memory-bridge/v1") throw contractError("compact_restore_bridge_snapshot_version", "unsupported compact restore bridge snapshot version");
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) throw contractError("compact_restore_bridge_snapshot_checksum", "compact restore bridge snapshot checksum mismatch");
    this.projections.clear();
    this.boundaryBindings.clear();
    this.providerMessages.clear();
    this.receipts.splice(0, this.receipts.length);
    this.appliedBoundaryIds.clear();
    for (const projection of snapshotValue.projections) {
      if (projectionDigest(projection) !== projection.projectionDigest) throw contractError("compact_restore_projection_digest", `compact restore projection digest mismatch: ${projection.projectionId}`);
      if (!toolScopeIsSubset(projection.allowedToolsBefore, projection.allowedToolsAfter, projection.deniedTools)) throw contractError("compact_restore_snapshot_scope_widened", "restored projection widens tool scope");
      if (this.projections.has(projection.projectionId) || this.boundaryBindings.has(projection.boundaryId)) throw contractError("compact_restore_snapshot_duplicate", "restored compact projection identity is duplicated");
      this.projections.set(projection.projectionId, cloneJson(projection));
      this.boundaryBindings.set(projection.boundaryId, projection.projectionId);
    }
    for (const [projectionId, message] of snapshotValue.providerMessages) {
      if (!this.projections.has(projectionId)) throw contractError("compact_restore_snapshot_orphan_message", "restored provider message has no projection");
      this.providerMessages.set(projectionId, cloneJson(message));
    }
    for (const receipt of snapshotValue.receipts) {
      const { receiptDigest, ...receiptUnsigned } = receipt;
      if (digest(receiptUnsigned) !== receiptDigest) throw contractError("compact_restore_receipt_digest", `restore receipt digest mismatch: ${receipt.receiptId}`);
      this.receipts.push(cloneJson(receipt));
    }
    for (const boundaryId of snapshotValue.appliedBoundaryIds) this.appliedBoundaryIds.add(boundaryId);
    this.revision = snapshotValue.revision;
    this.contextEpoch = snapshotValue.contextEpoch;
  }

  private require(projectionId: string): CompactRestoreProjection {
    const projection = this.projections.get(projectionId.trim());
    if (!projection) throw contractError("compact_restore_projection_not_found", `compact restore projection not found: ${projectionId}`);
    return projection;
  }

  private makeReceipt(
    projection: CompactRestoreProjection,
    operation: CompactRestoreBridgeReceipt["operation"],
    stateBefore: CompactRestoreProjection["state"] | null,
    stateAfter: CompactRestoreProjection["state"],
    reason: string,
  ): CompactRestoreBridgeReceipt {
    const occurredAt = nowIso(this.now);
    const unsigned = {
      receiptId: "",
      projectionId: projection.projectionId,
      boundaryId: projection.boundaryId,
      operation,
      stateBefore,
      stateAfter,
      contextEpochBefore: projection.contextEpochBefore,
      contextEpochAfter: projection.contextEpochAfter,
      providerMessageDigest: projection.providerMessageDigest,
      reason,
      occurredAt,
      metadata: {
        worker_kind: projection.workerKind,
        skill_memory_count: projection.skillMemoryIds.length,
        procedure_count: projection.procedureIds.length,
        context_section_count: projection.contextSectionIds.length,
      },
    };
    unsigned.receiptId = `restore-receipt-${digest(unsigned).slice(0, 32)}`;
    return { ...unsigned, receiptDigest: digest(unsigned) };
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_COMPACT_RESTORE_MEMORY_BRIDGE === "1") {
      throw contractError("compact_restore_memory_bridge_disabled", "06C compact restore memory bridge is disabled");
    }
  }
}

function projectionDigest(value: CompactRestoreProjection): string {
  const { projectionDigest: _ignored, ...unsigned } = value;
  return digest(unsigned);
}

function projectionOrder(left: CompactRestoreProjection, right: CompactRestoreProjection): number {
  const epoch = left.contextEpochAfter - right.contextEpochAfter;
  return epoch !== 0 ? epoch : left.projectionId.localeCompare(right.projectionId);
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}
