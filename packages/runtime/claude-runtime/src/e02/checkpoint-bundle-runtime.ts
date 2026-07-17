import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  hashChain,
} from "./canonical.ts";
import type { E02RuntimeIdentity } from "./contracts.ts";

export type E02CheckpointDomain =
  | "permission"
  | "mcp"
  | "skill"
  | "plugin"
  | "command"
  | "agent"
  | "route"
  | "transition"
  | "event"
  | "execution"
  | "host-port"
  | "custody";

export type E02CheckpointPhase =
  | "open"
  | "sealed"
  | "committed"
  | "delivery_indeterminate"
  | "delivered"
  | "aborted";

export interface E02CheckpointDomainRecord extends JsonObject {
  domain: E02CheckpointDomain;
  owner: string;
  revision: number;
  payloadDigest: string;
  priorPayloadDigest: string;
  changed: boolean;
  stagedAt: string;
  metadata: JsonObject;
  recordDigest: string;
}

export interface E02CheckpointBundle extends JsonObject {
  bundleId: string;
  sequence: number;
  runtimeId: string;
  runtimeEpoch: number;
  reason: string;
  phase: E02CheckpointPhase;
  expectedPriorBundleHash: string;
  expectedPriorSequence: number;
  records: E02CheckpointDomainRecord[];
  requiredDomains: E02CheckpointDomain[];
  missingDomains: E02CheckpointDomain[];
  openedAt: string;
  sealedAt: string | null;
  committedAt: string | null;
  deliveredAt: string | null;
  abortedAt: string | null;
  abortReason: string | null;
  deliveryAttempts: number;
  deliveryReceiptId: string | null;
  deliveryFailure: JsonObject | null;
  payloadRootDigest: string;
  bundleHash: string;
  previousBundleHash: string;
  metadata: JsonObject;
}

export interface E02CheckpointDeliveryReceipt extends JsonObject {
  receiptId: string;
  bundleId: string;
  bundleHash: string;
  attempt: number;
  ok: boolean;
  providerReceiptId: string | null;
  deliveredSnapshotHash: string | null;
  recordedAt: string;
  error: JsonObject | null;
  metadata: JsonObject;
  receiptDigest: string;
}

export interface E02CheckpointBundleSnapshot {
  version: "zyra.e02-checkpoint-bundles/v1";
  runtime: E02RuntimeIdentity;
  requiredDomains: E02CheckpointDomain[];
  sequence: number;
  previousBundleHash: string;
  lastDomainDigests: Array<[E02CheckpointDomain, string]>;
  bundles: E02CheckpointBundle[];
  deliveryReceipts: E02CheckpointDeliveryReceipt[];
  snapshotHash: string;
}

const DEFAULT_REQUIRED_DOMAINS: readonly E02CheckpointDomain[] = Object.freeze([
  "permission",
  "mcp",
  "skill",
  "plugin",
  "command",
  "agent",
  "route",
  "transition",
  "event",
  "execution",
  "host-port",
  "custody",
]);

const DOMAIN_OWNERS: Readonly<Record<E02CheckpointDomain, string>> = Object.freeze({
  permission: "PermissionCoordinator",
  mcp: "McpRuntimeCoordinator",
  skill: "SkillCoordinator",
  plugin: "PluginCoordinator",
  command: "CommandCoordinator",
  agent: "TypeScriptAgentRuntime",
  route: "E02RouteRuntime",
  transition: "DurableTransitionJournal",
  event: "E02EventLog",
  execution: "CapabilityExecutionLedger",
  "host-port": "E02HostPortRuntime",
  custody: "E02CustodyRuntime",
});

export class E02CheckpointBundleRuntime {
  readonly runtime: E02RuntimeIdentity;
  private readonly now: () => Date;
  private readonly requiredDomains: E02CheckpointDomain[];
  private sequence = 0;
  private previousBundleHash = digest({ genesis: "zyra.e02-checkpoint-bundles/v1" });
  private readonly lastDomainDigests = new Map<E02CheckpointDomain, string>();
  private readonly bundles = new Map<string, E02CheckpointBundle>();
  private readonly deliveryReceipts = new Map<string, E02CheckpointDeliveryReceipt>();

  constructor(options: {
    runtime: E02RuntimeIdentity;
    requiredDomains?: E02CheckpointDomain[];
    now?: () => Date;
    snapshot?: E02CheckpointBundleSnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.now = options.now ?? (() => new Date());
    this.requiredDomains = this.normalizeRequiredDomains(
      options.requiredDomains ?? [...DEFAULT_REQUIRED_DOMAINS],
    );
    if (options.snapshot) this.restore(options.snapshot);
  }

  begin(reasonValue: string, metadataValue: JsonObject = {}): E02CheckpointBundle {
    const reason = reasonValue.trim();
    if (!reason) {
      throw checkpointError(
        "e02_checkpoint_reason_missing",
        "checkpoint bundle requires a non-empty reason",
      );
    }
    const existingOpen = this.openBundles();
    if (existingOpen.length > 0) {
      throw checkpointError(
        "e02_checkpoint_bundle_already_open",
        `checkpoint bundle ${existingOpen[0]!.bundleId} is still open`,
        { open_bundle_ids: existingOpen.map((bundle) => bundle.bundleId) },
      );
    }
    const sequence = this.sequence + 1;
    const openedAt = this.timestamp();
    const identity = {
      runtime_id: this.runtime.runtimeId,
      runtime_epoch: this.runtime.epoch,
      sequence,
      reason,
      previous_bundle_hash: this.previousBundleHash,
    };
    const bundleId = deterministicId("e02-checkpoint-bundle", identity, 48);
    if (this.bundles.has(bundleId)) {
      throw checkpointError(
        "e02_checkpoint_bundle_collision",
        `checkpoint bundle identity ${bundleId} already exists`,
      );
    }
    const bundle: E02CheckpointBundle = {
      bundleId,
      sequence,
      runtimeId: this.runtime.runtimeId,
      runtimeEpoch: this.runtime.epoch,
      reason,
      phase: "open",
      expectedPriorBundleHash: this.previousBundleHash,
      expectedPriorSequence: this.sequence,
      records: [],
      requiredDomains: [...this.requiredDomains],
      missingDomains: [...this.requiredDomains],
      openedAt,
      sealedAt: null,
      committedAt: null,
      deliveredAt: null,
      abortedAt: null,
      abortReason: null,
      deliveryAttempts: 0,
      deliveryReceiptId: null,
      deliveryFailure: null,
      payloadRootDigest: digest({ empty: true }),
      bundleHash: "",
      previousBundleHash: this.previousBundleHash,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    bundle.bundleHash = checkpointBundleDigest(bundle);
    this.bundles.set(bundle.bundleId, bundle);
    return cloneJson(bundle);
  }

  stage(
    bundleId: string,
    domain: E02CheckpointDomain,
    payloadValue: JsonValue,
    revisionValue: number,
    metadataValue: JsonObject = {},
  ): E02CheckpointDomainRecord {
    const bundle = this.requireBundle(bundleId);
    this.assertPhase(bundle, ["open"], "stage domain state");
    this.assertDomain(domain);
    const revision = this.normalizeRevision(revisionValue);
    if (bundle.records.some((record) => record.domain === domain)) {
      throw checkpointError(
        "e02_checkpoint_domain_already_staged",
        `domain ${domain} was already staged in ${bundle.bundleId}`,
      );
    }
    const payload = canonicalize(payloadValue);
    const payloadDigest = digest(payload);
    const priorPayloadDigest = this.lastDomainDigests.get(domain)
      ?? digest({ domain, genesis: true });
    const base = {
      domain,
      owner: DOMAIN_OWNERS[domain],
      revision,
      payloadDigest,
      priorPayloadDigest,
      changed: !constantTimeDigestEquals(payloadDigest, priorPayloadDigest),
      stagedAt: this.timestamp(),
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const record: E02CheckpointDomainRecord = {
      ...base,
      recordDigest: digest(base),
    };
    bundle.records.push(record);
    bundle.records.sort((left, right) => left.domain.localeCompare(right.domain));
    bundle.missingDomains = bundle.requiredDomains
      .filter((required) => !bundle.records.some((candidate) => candidate.domain === required));
    bundle.payloadRootDigest = this.payloadRoot(bundle.records);
    bundle.bundleHash = checkpointBundleDigest(bundle);
    return cloneJson(record);
  }

  stageAll(
    bundleId: string,
    payloads: Readonly<Record<E02CheckpointDomain, {
      value: JsonValue;
      revision: number;
      metadata?: JsonObject;
    }>>,
  ): E02CheckpointBundle {
    for (const domain of this.requiredDomains) {
      const value = payloads[domain];
      if (!value) {
        throw checkpointError(
          "e02_checkpoint_required_payload_missing",
          `required domain ${domain} has no checkpoint payload`,
        );
      }
      this.stage(bundleId, domain, value.value, value.revision, value.metadata ?? {});
    }
    return this.requireBundleClone(bundleId);
  }

  replaceOpenDomain(
    bundleId: string,
    domain: E02CheckpointDomain,
    payloadValue: JsonValue,
    revisionValue: number,
    reason: string,
  ): E02CheckpointDomainRecord {
    const bundle = this.requireBundle(bundleId);
    this.assertPhase(bundle, ["open"], "replace staged domain state");
    if (!reason.trim()) {
      throw checkpointError(
        "e02_checkpoint_replace_reason_missing",
        "replacing staged state requires a reason",
      );
    }
    const index = bundle.records.findIndex((record) => record.domain === domain);
    if (index < 0) {
      return this.stage(bundleId, domain, payloadValue, revisionValue, {
        replacement_reason: reason,
        replaced_existing: false,
      });
    }
    bundle.records.splice(index, 1);
    bundle.bundleHash = checkpointBundleDigest(bundle);
    return this.stage(bundleId, domain, payloadValue, revisionValue, {
      replacement_reason: reason,
      replaced_existing: true,
    });
  }

  seal(bundleId: string): E02CheckpointBundle {
    const bundle = this.requireBundle(bundleId);
    this.assertPhase(bundle, ["open"], "seal checkpoint bundle");
    if (bundle.missingDomains.length > 0) {
      throw checkpointError(
        "e02_checkpoint_bundle_incomplete",
        `checkpoint bundle ${bundle.bundleId} is missing required domains`,
        { missing_domains: [...bundle.missingDomains] },
      );
    }
    this.validateRecords(bundle.records);
    bundle.phase = "sealed";
    bundle.sealedAt = this.timestamp();
    bundle.payloadRootDigest = this.payloadRoot(bundle.records);
    bundle.bundleHash = checkpointBundleDigest(bundle);
    return cloneJson(bundle);
  }

  commit(bundleId: string): E02CheckpointBundle {
    const bundle = this.requireBundle(bundleId);
    if (bundle.phase === "committed" || bundle.phase === "delivery_indeterminate" || bundle.phase === "delivered") {
      return cloneJson(bundle);
    }
    this.assertPhase(bundle, ["sealed"], "commit checkpoint bundle");
    if (
      bundle.expectedPriorSequence !== this.sequence
      || !constantTimeDigestEquals(bundle.expectedPriorBundleHash, this.previousBundleHash)
    ) {
      throw checkpointError(
        "e02_checkpoint_compare_and_swap_failed",
        `checkpoint bundle ${bundle.bundleId} was prepared against stale state`,
        {
          expected_prior_sequence: bundle.expectedPriorSequence,
          actual_prior_sequence: this.sequence,
          expected_prior_hash: bundle.expectedPriorBundleHash,
          actual_prior_hash: this.previousBundleHash,
        },
      );
    }
    this.validateRecords(bundle.records);
    const committedAt = this.timestamp();
    const commitBase = {
      bundle_id: bundle.bundleId,
      sequence: bundle.sequence,
      payload_root_digest: bundle.payloadRootDigest,
      previous_bundle_hash: this.previousBundleHash,
      committed_at: committedAt,
      runtime_epoch: this.runtime.epoch,
    };
    bundle.phase = "committed";
    bundle.committedAt = committedAt;
    bundle.previousBundleHash = this.previousBundleHash;
    bundle.bundleHash = hashChain(this.previousBundleHash, commitBase);
    this.sequence = bundle.sequence;
    this.previousBundleHash = bundle.bundleHash;
    for (const record of bundle.records) {
      this.lastDomainDigests.set(record.domain, record.payloadDigest);
    }
    return cloneJson(bundle);
  }

  recordDeliveryAttempt(bundleId: string): E02CheckpointBundle {
    const bundle = this.requireBundle(bundleId);
    this.assertPhase(
      bundle,
      ["committed", "delivery_indeterminate"],
      "record checkpoint delivery attempt",
    );
    bundle.deliveryAttempts += 1;
    bundle.deliveryFailure = null;
    return cloneJson(bundle);
  }

  markDelivered(
    bundleId: string,
    input: {
      providerReceiptId?: string | null;
      deliveredSnapshotHash: string;
      metadata?: JsonObject;
    },
  ): E02CheckpointDeliveryReceipt {
    const bundle = this.requireBundle(bundleId);
    if (bundle.phase === "delivered" && bundle.deliveryReceiptId) {
      const replay = this.deliveryReceipts.get(bundle.deliveryReceiptId);
      if (!replay) {
        throw checkpointError(
          "e02_checkpoint_delivery_receipt_missing",
          `delivered bundle ${bundle.bundleId} has no receipt`,
        );
      }
      if (!constantTimeDigestEquals(replay.deliveredSnapshotHash ?? "", input.deliveredSnapshotHash)) {
        throw checkpointError(
          "e02_checkpoint_delivery_replay_mismatch",
          `delivered bundle ${bundle.bundleId} was replayed with another snapshot`,
        );
      }
      return cloneJson(replay);
    }
    this.assertPhase(
      bundle,
      ["committed", "delivery_indeterminate"],
      "mark checkpoint delivered",
    );
    if (bundle.deliveryAttempts < 1) {
      throw checkpointError(
        "e02_checkpoint_delivery_attempt_missing",
        "checkpoint delivery must be attempted before acknowledgement",
      );
    }
    const recordedAt = this.timestamp();
    const base = {
      bundleId: bundle.bundleId,
      bundleHash: bundle.bundleHash,
      attempt: bundle.deliveryAttempts,
      ok: true,
      providerReceiptId: input.providerReceiptId ?? null,
      deliveredSnapshotHash: input.deliveredSnapshotHash,
      recordedAt,
      error: null,
      metadata: canonicalize(input.metadata ?? {}) as JsonObject,
    };
    const receiptId = deterministicId("e02-checkpoint-delivery", base, 48);
    const receipt: E02CheckpointDeliveryReceipt = {
      receiptId,
      bundleId: base.bundleId,
      bundleHash: base.bundleHash,
      attempt: base.attempt,
      ok: true,
      providerReceiptId: base.providerReceiptId,
      deliveredSnapshotHash: base.deliveredSnapshotHash,
      recordedAt,
      error: null,
      metadata: base.metadata,
      receiptDigest: digest({ receiptId, ...base }),
    };
    this.deliveryReceipts.set(receipt.receiptId, receipt);
    bundle.phase = "delivered";
    bundle.deliveredAt = recordedAt;
    bundle.deliveryReceiptId = receipt.receiptId;
    bundle.deliveryFailure = null;
    return cloneJson(receipt);
  }

  markDeliveryFailure(bundleId: string, error: unknown): E02CheckpointDeliveryReceipt {
    const bundle = this.requireBundle(bundleId);
    this.assertPhase(
      bundle,
      ["committed", "delivery_indeterminate"],
      "record checkpoint delivery failure",
    );
    if (bundle.deliveryAttempts < 1) {
      throw checkpointError(
        "e02_checkpoint_delivery_attempt_missing",
        "checkpoint delivery must be attempted before a failure is recorded",
      );
    }
    const recordedAt = this.timestamp();
    const errorValue = errorObject(error);
    const base = {
      bundleId: bundle.bundleId,
      bundleHash: bundle.bundleHash,
      attempt: bundle.deliveryAttempts,
      ok: false,
      providerReceiptId: null,
      deliveredSnapshotHash: null,
      recordedAt,
      error: errorValue,
      metadata: {},
    };
    const receiptId = deterministicId("e02-checkpoint-delivery-failure", base, 48);
    const receipt: E02CheckpointDeliveryReceipt = {
      receiptId,
      ...base,
      receiptDigest: digest({ receiptId, ...base }),
    };
    this.deliveryReceipts.set(receipt.receiptId, receipt);
    bundle.phase = "delivery_indeterminate";
    bundle.deliveryFailure = errorValue;
    bundle.deliveryReceiptId = receipt.receiptId;
    return cloneJson(receipt);
  }

  reconcileDelivery(
    bundleId: string,
    input: {
      outcome: "delivered" | "not_delivered";
      actor: string;
      reason: string;
      deliveredSnapshotHash?: string;
      providerReceiptId?: string | null;
    },
  ): E02CheckpointBundle {
    const bundle = this.requireBundle(bundleId);
    this.assertPhase(bundle, ["delivery_indeterminate"], "reconcile checkpoint delivery");
    if (!input.actor.trim() || !input.reason.trim()) {
      throw checkpointError(
        "e02_checkpoint_reconciliation_identity_missing",
        "delivery reconciliation requires actor and reason",
      );
    }
    if (input.outcome === "delivered") {
      if (!input.deliveredSnapshotHash) {
        throw checkpointError(
          "e02_checkpoint_reconciliation_snapshot_missing",
          "delivered reconciliation requires the persisted snapshot hash",
        );
      }
      this.markDelivered(bundle.bundleId, {
        providerReceiptId: input.providerReceiptId ?? null,
        deliveredSnapshotHash: input.deliveredSnapshotHash,
        metadata: {
          reconciliation_actor: input.actor,
          reconciliation_reason: input.reason,
        },
      });
      return this.requireBundleClone(bundle.bundleId);
    }
    bundle.phase = "committed";
    bundle.deliveryFailure = {
      reconciled_not_delivered: true,
      actor: input.actor,
      reason: input.reason,
      reconciled_at: this.timestamp(),
    };
    return cloneJson(bundle);
  }

  abort(bundleId: string, reasonValue: string): E02CheckpointBundle {
    const bundle = this.requireBundle(bundleId);
    const reason = reasonValue.trim();
    if (!reason) {
      throw checkpointError(
        "e02_checkpoint_abort_reason_missing",
        "aborting a checkpoint bundle requires a reason",
      );
    }
    if (bundle.phase === "aborted") return cloneJson(bundle);
    this.assertPhase(bundle, ["open", "sealed"], "abort checkpoint bundle");
    bundle.phase = "aborted";
    bundle.abortedAt = this.timestamp();
    bundle.abortReason = reason;
    bundle.bundleHash = checkpointBundleDigest(bundle);
    return cloneJson(bundle);
  }

  bundle(bundleId: string): E02CheckpointBundle | null {
    const bundle = this.bundles.get(bundleId);
    return bundle ? cloneJson(bundle) : null;
  }

  latest(): E02CheckpointBundle | null {
    const committed = [...this.bundles.values()]
      .filter((bundle) => bundle.phase === "committed"
        || bundle.phase === "delivery_indeterminate"
        || bundle.phase === "delivered")
      .sort((left, right) => right.sequence - left.sequence);
    return committed[0] ? cloneJson(committed[0]) : null;
  }

  list(input: {
    phase?: E02CheckpointPhase;
    reason?: string;
    changedDomain?: E02CheckpointDomain;
    limit?: number;
  } = {}): E02CheckpointBundle[] {
    const limit = Math.max(1, Math.min(10_000, input.limit ?? 100));
    return [...this.bundles.values()]
      .filter((bundle) => !input.phase || bundle.phase === input.phase)
      .filter((bundle) => !input.reason || bundle.reason === input.reason)
      .filter((bundle) => !input.changedDomain
        || bundle.records.some((record) => record.domain === input.changedDomain && record.changed))
      .sort((left, right) => right.sequence - left.sequence || right.openedAt.localeCompare(left.openedAt))
      .slice(0, limit)
      .map(cloneJson);
  }

  receipts(bundleId?: string): E02CheckpointDeliveryReceipt[] {
    return [...this.deliveryReceipts.values()]
      .filter((receipt) => !bundleId || receipt.bundleId === bundleId)
      .sort((left, right) => left.recordedAt.localeCompare(right.recordedAt))
      .map(cloneJson);
  }

  pendingDelivery(): E02CheckpointBundle[] {
    return [...this.bundles.values()]
      .filter((bundle) => bundle.phase === "committed" || bundle.phase === "delivery_indeterminate")
      .sort((left, right) => left.sequence - right.sequence)
      .map(cloneJson);
  }

  openBundles(): E02CheckpointBundle[] {
    return [...this.bundles.values()]
      .filter((bundle) => bundle.phase === "open" || bundle.phase === "sealed")
      .sort((left, right) => left.sequence - right.sequence)
      .map(cloneJson);
  }

  verifyBundle(bundleId: string): JsonObject {
    const bundle = this.requireBundle(bundleId);
    const errors: JsonObject[] = [];
    if (bundle.runtimeId !== this.runtime.runtimeId) {
      errors.push({ code: "runtime_id_mismatch" });
    }
    if (!Number.isSafeInteger(bundle.runtimeEpoch)
      || bundle.runtimeEpoch < 1
      || bundle.runtimeEpoch > this.runtime.epoch) {
      errors.push({ code: "runtime_epoch_mismatch" });
    }
    if (bundle.missingDomains.length > 0 && bundle.phase !== "open" && bundle.phase !== "aborted") {
      errors.push({ code: "sealed_bundle_missing_domains", domains: [...bundle.missingDomains] });
    }
    try {
      this.validateRecords(bundle.records);
    } catch (error) {
      errors.push({ code: "record_validation_failed", error: errorObject(error) });
    }
    if (bundle.phase === "open" || bundle.phase === "sealed" || bundle.phase === "aborted") {
      if (!constantTimeDigestEquals(checkpointBundleDigest(bundle), bundle.bundleHash)) {
        errors.push({ code: "bundle_digest_mismatch" });
      }
    }
    if (bundle.deliveryReceiptId && !this.deliveryReceipts.has(bundle.deliveryReceiptId)) {
      errors.push({ code: "delivery_receipt_missing", receipt_id: bundle.deliveryReceiptId });
    }
    return {
      ok: errors.length === 0,
      bundle_id: bundle.bundleId,
      sequence: bundle.sequence,
      phase: bundle.phase,
      error_count: errors.length,
      errors,
    };
  }

  verifyChain(): JsonObject {
    const errors: JsonObject[] = [];
    const committed = [...this.bundles.values()]
      .filter((bundle) => bundle.phase === "committed"
        || bundle.phase === "delivery_indeterminate"
        || bundle.phase === "delivered")
      .sort((left, right) => left.sequence - right.sequence);
    let priorSequence = 0;
    let priorHash = digest({ genesis: "zyra.e02-checkpoint-bundles/v1" });
    for (const bundle of committed) {
      if (bundle.sequence !== priorSequence + 1) {
        errors.push({
          code: "sequence_gap",
          bundle_id: bundle.bundleId,
          expected: priorSequence + 1,
          actual: bundle.sequence,
        });
      }
      if (!constantTimeDigestEquals(bundle.previousBundleHash, priorHash)) {
        errors.push({
          code: "previous_hash_mismatch",
          bundle_id: bundle.bundleId,
          expected: priorHash,
          actual: bundle.previousBundleHash,
        });
      }
      priorSequence = bundle.sequence;
      priorHash = bundle.bundleHash;
    }
    if (priorSequence !== this.sequence) {
      errors.push({ code: "head_sequence_mismatch", expected: priorSequence, actual: this.sequence });
    }
    if (!constantTimeDigestEquals(priorHash, this.previousBundleHash)) {
      errors.push({ code: "head_hash_mismatch", expected: priorHash, actual: this.previousBundleHash });
    }
    return {
      ok: errors.length === 0,
      committed_bundle_count: committed.length,
      head_sequence: this.sequence,
      head_hash: this.previousBundleHash,
      errors,
    };
  }

  compact(retainCountValue = 256): JsonObject {
    const retainCount = Math.max(16, Math.min(10_000, Math.trunc(retainCountValue)));
    const terminal = [...this.bundles.values()]
      .filter((bundle) => bundle.phase === "delivered" || bundle.phase === "aborted")
      .sort((left, right) => right.sequence - left.sequence || right.openedAt.localeCompare(left.openedAt));
    const removable = terminal.slice(retainCount);
    let removedReceipts = 0;
    for (const bundle of removable) {
      this.bundles.delete(bundle.bundleId);
      for (const [receiptId, receipt] of this.deliveryReceipts) {
        if (receipt.bundleId === bundle.bundleId) {
          this.deliveryReceipts.delete(receiptId);
          removedReceipts += 1;
        }
      }
    }
    return {
      retained_terminal_bundles: terminal.length - removable.length,
      removed_bundle_count: removable.length,
      removed_receipt_count: removedReceipts,
      head_sequence: this.sequence,
      head_hash: this.previousBundleHash,
    };
  }

  health(): JsonObject {
    const phases = new Map<E02CheckpointPhase, number>();
    for (const bundle of this.bundles.values()) {
      phases.set(bundle.phase, (phases.get(bundle.phase) ?? 0) + 1);
    }
    const chain = this.verifyChain();
    return {
      canonical_owner: "typescript",
      runtime_epoch: this.runtime.epoch,
      sequence: this.sequence,
      previous_bundle_hash: this.previousBundleHash,
      required_domains: [...this.requiredDomains],
      bundle_count: this.bundles.size,
      delivery_receipt_count: this.deliveryReceipts.size,
      open_bundle_count: this.openBundles().length,
      pending_delivery_count: this.pendingDelivery().length,
      phase_counts: Object.fromEntries(phases),
      chain_ok: chain.ok,
      python_checkpoint_fallback: false,
    };
  }

  snapshot(): E02CheckpointBundleSnapshot {
    const withoutHash = {
      version: "zyra.e02-checkpoint-bundles/v1" as const,
      runtime: cloneJson(this.runtime),
      requiredDomains: [...this.requiredDomains],
      sequence: this.sequence,
      previousBundleHash: this.previousBundleHash,
      lastDomainDigests: [...this.lastDomainDigests.entries()]
        .sort(([left], [right]) => left.localeCompare(right)),
      bundles: [...this.bundles.values()]
        .sort((left, right) => left.sequence - right.sequence || left.openedAt.localeCompare(right.openedAt))
        .map(cloneJson),
      deliveryReceipts: [...this.deliveryReceipts.values()]
        .sort((left, right) => left.recordedAt.localeCompare(right.recordedAt))
        .map(cloneJson),
    };
    return { ...withoutHash, snapshotHash: digest(withoutHash) };
  }

  private restore(snapshotValue: E02CheckpointBundleSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-checkpoint-bundles/v1") {
      throw checkpointError(
        "e02_checkpoint_snapshot_version",
        `unsupported checkpoint bundle snapshot ${snapshot.version}`,
      );
    }
    const { snapshotHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), snapshotHash)) {
      throw checkpointError(
        "e02_checkpoint_snapshot_digest",
        "checkpoint bundle snapshot digest mismatch",
      );
    }
    this.assertRuntimeBinding(snapshot.runtime);
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw checkpointError(
        "e02_checkpoint_snapshot_epoch",
        "checkpoint bundle restore target epoch must advance",
      );
    }
    const restoredRequired = this.normalizeRequiredDomains(snapshot.requiredDomains);
    if (digest(restoredRequired) !== digest(this.requiredDomains)) {
      throw checkpointError(
        "e02_checkpoint_required_domains_changed",
        "checkpoint required domains changed across restore",
      );
    }
    for (const [domain, domainDigest] of snapshot.lastDomainDigests) {
      this.assertDomain(domain);
      if (this.lastDomainDigests.has(domain) || !domainDigest) {
        throw checkpointError(
          "e02_checkpoint_domain_head_invalid",
          `invalid restored domain head ${domain}`,
        );
      }
      this.lastDomainDigests.set(domain, domainDigest);
    }
    for (const bundleValue of snapshot.bundles) {
      const bundle = cloneJson(bundleValue);
      if (this.bundles.has(bundle.bundleId)) {
        throw checkpointError(
          "e02_checkpoint_duplicate_bundle",
          `duplicate restored checkpoint bundle ${bundle.bundleId}`,
        );
      }
      this.validateRestoredBundle(bundle, snapshot.runtime.epoch);
      if (bundle.phase === "open" || bundle.phase === "sealed") {
        bundle.phase = "aborted";
        bundle.abortedAt = this.timestamp();
        bundle.abortReason = "restore_epoch_fence";
        bundle.bundleHash = checkpointBundleDigest(bundle);
      }
      this.bundles.set(bundle.bundleId, bundle);
    }
    for (const receiptValue of snapshot.deliveryReceipts) {
      const receipt = cloneJson(receiptValue);
      if (this.deliveryReceipts.has(receipt.receiptId)) {
        throw checkpointError(
          "e02_checkpoint_duplicate_receipt",
          `duplicate restored delivery receipt ${receipt.receiptId}`,
        );
      }
      const { receiptDigest, ...base } = receipt;
      if (!constantTimeDigestEquals(digest(base), receiptDigest)) {
        throw checkpointError(
          "e02_checkpoint_receipt_digest",
          `delivery receipt ${receipt.receiptId} digest mismatch`,
        );
      }
      this.deliveryReceipts.set(receipt.receiptId, receipt);
    }
    this.sequence = snapshot.sequence;
    this.previousBundleHash = snapshot.previousBundleHash;
    const chain = this.verifyChain();
    if (chain.ok !== true) {
      throw checkpointError(
        "e02_checkpoint_chain_invalid",
        "restored checkpoint bundle chain is invalid",
        { chain },
      );
    }
  }

  private validateRestoredBundle(bundle: E02CheckpointBundle, sourceEpoch: number): void {
    // A durable snapshot contains the complete checkpoint chain, so bundles
    // created by earlier process lifetimes remain bound to their original
    // epoch.  They are valid history for the same runtime id, while epoch zero
    // and future-epoch bundles must still fail closed.
    if (bundle.runtimeId !== this.runtime.runtimeId
      || !Number.isSafeInteger(bundle.runtimeEpoch)
      || bundle.runtimeEpoch < 1
      || bundle.runtimeEpoch > sourceEpoch) {
      throw checkpointError(
        "e02_checkpoint_bundle_binding",
        `restored checkpoint bundle ${bundle.bundleId} binding mismatch`,
      );
    }
    this.validateRecords(bundle.records);
    const required = this.normalizeRequiredDomains(bundle.requiredDomains);
    if (digest(required) !== digest(this.requiredDomains)) {
      throw checkpointError(
        "e02_checkpoint_bundle_domains",
        `restored checkpoint bundle ${bundle.bundleId} required domains mismatch`,
      );
    }
    const computedMissing = required
      .filter((domain) => !bundle.records.some((record) => record.domain === domain));
    if (digest(computedMissing) !== digest(bundle.missingDomains)) {
      throw checkpointError(
        "e02_checkpoint_bundle_missing_domains",
        `restored checkpoint bundle ${bundle.bundleId} missing-domain set mismatch`,
      );
    }
    if (!constantTimeDigestEquals(this.payloadRoot(bundle.records), bundle.payloadRootDigest)) {
      throw checkpointError(
        "e02_checkpoint_payload_root",
        `restored checkpoint bundle ${bundle.bundleId} payload root mismatch`,
      );
    }
  }

  private validateRecords(records: E02CheckpointDomainRecord[]): void {
    const seen = new Set<E02CheckpointDomain>();
    for (const record of records) {
      this.assertDomain(record.domain);
      if (seen.has(record.domain)) {
        throw checkpointError(
          "e02_checkpoint_duplicate_domain",
          `checkpoint contains duplicate domain ${record.domain}`,
        );
      }
      seen.add(record.domain);
      if (record.owner !== DOMAIN_OWNERS[record.domain]) {
        throw checkpointError(
          "e02_checkpoint_domain_owner_mismatch",
          `domain ${record.domain} is attributed to ${record.owner}`,
        );
      }
      const { recordDigest, ...base } = record;
      if (!constantTimeDigestEquals(digest(base), recordDigest)) {
        throw checkpointError(
          "e02_checkpoint_domain_digest",
          `checkpoint domain ${record.domain} record digest mismatch`,
        );
      }
      this.normalizeRevision(record.revision);
    }
  }

  private payloadRoot(records: E02CheckpointDomainRecord[]): string {
    return digest(records
      .map((record) => ({
        domain: record.domain,
        owner: record.owner,
        revision: record.revision,
        payload_digest: record.payloadDigest,
        record_digest: record.recordDigest,
      }))
      .sort((left, right) => left.domain.localeCompare(right.domain)));
  }

  private normalizeRequiredDomains(values: E02CheckpointDomain[]): E02CheckpointDomain[] {
    const output: E02CheckpointDomain[] = [];
    const seen = new Set<E02CheckpointDomain>();
    for (const value of values) {
      this.assertDomain(value);
      if (seen.has(value)) continue;
      seen.add(value);
      output.push(value);
    }
    output.sort();
    if (output.length === 0) {
      throw checkpointError(
        "e02_checkpoint_required_domains_empty",
        "checkpoint bundle requires at least one domain",
      );
    }
    return output;
  }

  private assertDomain(domain: string): asserts domain is E02CheckpointDomain {
    if (!Object.prototype.hasOwnProperty.call(DOMAIN_OWNERS, domain)) {
      throw checkpointError(
        "e02_checkpoint_domain_unknown",
        `unknown checkpoint domain ${domain}`,
      );
    }
  }

  private assertRuntimeBinding(runtime: E02RuntimeIdentity): void {
    if (
      runtime.runtimeId !== this.runtime.runtimeId
      || runtime.runId !== this.runtime.runId
      || runtime.taskId !== this.runtime.taskId
      || runtime.sessionId !== this.runtime.sessionId
      || runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw checkpointError(
        "e02_checkpoint_snapshot_binding",
        "checkpoint bundle snapshot belongs to another runtime binding",
      );
    }
  }

  private assertPhase(
    bundle: E02CheckpointBundle,
    expected: E02CheckpointPhase[],
    operation: string,
  ): void {
    if (!expected.includes(bundle.phase)) {
      throw checkpointError(
        "e02_checkpoint_phase_invalid",
        `cannot ${operation} while bundle ${bundle.bundleId} is ${bundle.phase}`,
        { expected_phases: expected, actual_phase: bundle.phase },
      );
    }
  }

  private normalizeRevision(value: number): number {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw checkpointError(
        "e02_checkpoint_revision_invalid",
        `checkpoint domain revision ${value} is invalid`,
      );
    }
    return value;
  }

  private requireBundle(bundleId: string): E02CheckpointBundle {
    const bundle = this.bundles.get(bundleId);
    if (!bundle) {
      throw checkpointError(
        "e02_checkpoint_bundle_not_found",
        `checkpoint bundle ${bundleId} was not found`,
      );
    }
    return bundle;
  }

  private requireBundleClone(bundleId: string): E02CheckpointBundle {
    return cloneJson(this.requireBundle(bundleId));
  }

  private timestamp(): string {
    return this.now().toISOString();
  }
}

function checkpointBundleDigest(bundle: E02CheckpointBundle): string {
  return digest({
    bundle_id: bundle.bundleId,
    sequence: bundle.sequence,
    runtime_id: bundle.runtimeId,
    runtime_epoch: bundle.runtimeEpoch,
    reason: bundle.reason,
    phase: bundle.phase,
    expected_prior_bundle_hash: bundle.expectedPriorBundleHash,
    expected_prior_sequence: bundle.expectedPriorSequence,
    records: bundle.records.map((record) => record.recordDigest),
    required_domains: bundle.requiredDomains,
    missing_domains: bundle.missingDomains,
    opened_at: bundle.openedAt,
    sealed_at: bundle.sealedAt,
    committed_at: bundle.committedAt,
    delivered_at: bundle.deliveredAt,
    aborted_at: bundle.abortedAt,
    abort_reason: bundle.abortReason,
    delivery_attempts: bundle.deliveryAttempts,
    delivery_receipt_id: bundle.deliveryReceiptId,
    delivery_failure: bundle.deliveryFailure,
    payload_root_digest: bundle.payloadRootDigest,
    previous_bundle_hash: bundle.previousBundleHash,
    metadata: bundle.metadata,
  });
}

function errorObject(error: unknown): JsonObject {
  if (error instanceof Error) {
    const value = error as Error & { code?: unknown; details?: unknown };
    return {
      name: value.name,
      message: value.message,
      code: typeof value.code === "string" ? value.code : "checkpoint_delivery_failed",
      details: canonicalize(value.details ?? {}) as JsonValue,
    };
  }
  return {
    name: "Error",
    message: String(error),
    code: "checkpoint_delivery_failed",
    details: {},
  };
}

function checkpointError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "E02CheckpointBundleError",
    code,
    details: cloneJson(details),
  });
}
