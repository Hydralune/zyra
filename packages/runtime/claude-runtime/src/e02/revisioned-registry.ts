import type { JsonObject } from "../contracts.ts";
import { cloneJson, constantTimeDigestEquals, deterministicId, digest, optionalObject } from "./canonical.ts";
import type {
  E02Clock,
  RegistryCommit,
  RegistryMutation,
  RegistryStage,
  RevisionedEntry,
  RevisionedRegistrySnapshot,
} from "./contracts.ts";

export class RegistryConflictError extends Error {
  readonly code: string;
  readonly registryName: string;
  readonly stageId: string | null;

  constructor(code: string, message: string, registryName: string, stageId: string | null = null) {
    super(message);
    this.name = "RegistryConflictError";
    this.code = code;
    this.registryName = registryName;
    this.stageId = stageId;
  }
}
export class RevisionedRegistry<T extends JsonObject = JsonObject> {
  readonly name: string;
  private readonly clock: E02Clock;
  private revisionValue = 0;
  private readonly entries = new Map<string, RevisionedEntry<T>>();
  private readonly stages = new Map<string, RegistryStage<T>>();
  private readonly commits = new Map<string, RegistryCommit<T>>();

  constructor(name: string, clock: E02Clock = () => new Date().toISOString()) {
    if (!name) throw new Error("registry name is required");
    this.name = name;
    this.clock = clock;
  }

  get revision(): number {
    return this.revisionValue;
  }

  stage(
    mutations: readonly RegistryMutation<T>[],
    expectedRegistryRevision: number,
    source: string,
    metadata: JsonObject = {},
  ): RegistryStage<T> {
    if (expectedRegistryRevision !== this.revisionValue) {
      throw new RegistryConflictError(
        "registry_revision_conflict",
        `expected registry revision ${expectedRegistryRevision}, observed ${this.revisionValue}`,
        this.name,
      );
    }
    if (!mutations.length) throw new RegistryConflictError("empty_registry_stage", "registry stage has no mutations", this.name);
    const canonicalMutations = mutations.map((mutation) => this.validateMutation(mutation));
    const duplicateIds = canonicalMutations
      .map((mutation) => mutation.id)
      .filter((id, index, all) => all.indexOf(id) !== index);
    if (duplicateIds.length) {
      throw new RegistryConflictError(
        "duplicate_registry_mutation",
        `registry stage mutates the same id more than once: ${[...new Set(duplicateIds)].join(",")}`,
        this.name,
      );
    }
    const mutationHash = digest(canonicalMutations);
    const stageId = deterministicId("e02-registry-stage", {
      name: this.name,
      expectedRegistryRevision,
      source,
      mutationHash,
    }, 40);
    const existingCommit = this.commits.get(stageId);
    if (existingCommit) throw new RegistryConflictError("stage_already_committed", "registry stage is already committed", this.name, stageId);
    const existing = this.stages.get(stageId);
    if (existing) return cloneJson(existing);
    const stage: RegistryStage<T> = {
      stageId,
      expectedRegistryRevision,
      mutations: canonicalMutations,
      mutationHash,
      createdAt: this.clock(),
      source,
      metadata: cloneJson(metadata),
    };
    this.stages.set(stageId, stage);
    return cloneJson(stage);
  }

  commit(stageId: string): RegistryCommit<T> {
    const previous = this.commits.get(stageId);
    if (previous) return cloneJson(previous);
    const stage = this.stages.get(stageId);
    if (!stage) throw new RegistryConflictError("registry_stage_unknown", "registry stage does not exist", this.name, stageId);
    if (stage.expectedRegistryRevision !== this.revisionValue) {
      throw new RegistryConflictError(
        "registry_commit_revision_conflict",
        `stage expected revision ${stage.expectedRegistryRevision}, observed ${this.revisionValue}`,
        this.name,
        stageId,
      );
    }
    if (!constantTimeDigestEquals(digest(stage.mutations), stage.mutationHash)) {
      throw new RegistryConflictError("registry_stage_hash_mismatch", "registry stage mutation hash mismatch", this.name, stageId);
    }
    const now = this.clock();
    const added: RevisionedEntry<T>[] = [];
    const updated: RevisionedEntry<T>[] = [];
    const removed: RevisionedEntry<T>[] = [];
    const unchanged: RevisionedEntry<T>[] = [];
    for (const mutation of stage.mutations) {
      const current = this.entries.get(mutation.id);
      this.assertExpectedEntryRevision(mutation, current);
      if (mutation.kind === "delete") {
        if (!current || current.tombstonedAt) {
          if (current) unchanged.push(cloneJson(current));
          continue;
        }
        const tombstone: RevisionedEntry<T> = {
          ...current,
          revision: current.revision + 1,
          enabled: false,
          updatedAt: now,
          tombstonedAt: now,
          metadata: { ...current.metadata, ...optionalObject(mutation.metadata) },
        };
        this.entries.set(mutation.id, tombstone);
        removed.push(cloneJson(tombstone));
        continue;
      }
      if (mutation.kind === "enable" || mutation.kind === "disable") {
        if (!current || current.tombstonedAt) {
          throw new RegistryConflictError(
            "registry_entry_missing",
            `cannot ${mutation.kind} missing entry ${mutation.id}`,
            this.name,
            stageId,
          );
        }
        const enabled = mutation.kind === "enable";
        if (current.enabled === enabled) {
          unchanged.push(cloneJson(current));
          continue;
        }
        const next: RevisionedEntry<T> = {
          ...current,
          revision: current.revision + 1,
          enabled,
          updatedAt: now,
          metadata: { ...current.metadata, ...optionalObject(mutation.metadata) },
        };
        this.entries.set(mutation.id, next);
        updated.push(cloneJson(next));
        continue;
      }
      if (!mutation.value) throw new RegistryConflictError("registry_value_missing", `put ${mutation.id} has no value`, this.name, stageId);
      const value = cloneJson(mutation.value);
      const valueHash = digest(value);
      if (current && !current.tombstonedAt && constantTimeDigestEquals(current.valueHash, valueHash)) {
        const sameSource = current.source === (mutation.source ?? stage.source)
          && current.sourceRevision === (mutation.sourceRevision ?? "");
        if (sameSource) {
          unchanged.push(cloneJson(current));
          continue;
        }
      }
      const next: RevisionedEntry<T> = {
        id: mutation.id,
        revision: (current?.revision ?? 0) + 1,
        value,
        valueHash,
        enabled: true,
        source: mutation.source ?? stage.source,
        sourceRevision: mutation.sourceRevision ?? "",
        createdAt: current?.createdAt ?? now,
        updatedAt: now,
        tombstonedAt: null,
        metadata: { ...(current?.metadata ?? {}), ...optionalObject(mutation.metadata) },
      };
      this.entries.set(mutation.id, next);
      (current ? updated : added).push(cloneJson(next));
    }
    const revisionBefore = this.revisionValue;
    this.revisionValue += 1;
    const commitBase = {
      stageId,
      revisionBefore,
      revisionAfter: this.revisionValue,
      added,
      updated,
      removed,
      unchanged,
      committedAt: now,
    };
    const commit: RegistryCommit<T> = { ...commitBase, commitHash: digest(commitBase) };
    this.stages.delete(stageId);
    this.commits.set(stageId, commit);
    return cloneJson(commit);
  }

  reject(stageId: string): void {
    if (this.commits.has(stageId)) throw new RegistryConflictError("reject_committed_stage", "committed stage cannot be rejected", this.name, stageId);
    if (!this.stages.delete(stageId)) throw new RegistryConflictError("registry_stage_unknown", "registry stage does not exist", this.name, stageId);
  }

  get(id: string, includeDisabled = false): RevisionedEntry<T> | null {
    const entry = this.entries.get(id);
    if (!entry || entry.tombstonedAt || (!includeDisabled && !entry.enabled)) return null;
    return cloneJson(entry);
  }

  require(id: string, includeDisabled = false): RevisionedEntry<T> {
    const entry = this.get(id, includeDisabled);
    if (!entry) throw new RegistryConflictError("registry_entry_missing", `registry entry ${id} is unavailable`, this.name);
    return entry;
  }

  list(options: { includeDisabled?: boolean; includeTombstones?: boolean; source?: string } = {}): RevisionedEntry<T>[] {
    return [...this.entries.values()]
      .filter((entry) => options.includeTombstones || !entry.tombstonedAt)
      .filter((entry) => options.includeDisabled || entry.enabled)
      .filter((entry) => !options.source || entry.source === options.source)
      .map(cloneJson)
      .sort((left, right) => left.id.localeCompare(right.id));
  }

  snapshot(): RevisionedRegistrySnapshot<T> {
    const base = {
      version: "zyra.e02-revisioned-registry/v1" as const,
      name: this.name,
      revision: this.revisionValue,
      entries: [...this.entries.values()].map(cloneJson).sort((left, right) => left.id.localeCompare(right.id)),
      staged: [...this.stages.values()].map(cloneJson).sort((left, right) => left.stageId.localeCompare(right.stageId)),
    };
    return { ...base, snapshotHash: digest(base) };
  }

  restore(snapshot: RevisionedRegistrySnapshot<T>): void {
    if (snapshot.version !== "zyra.e02-revisioned-registry/v1") throw new Error("unsupported revisioned registry snapshot");
    if (snapshot.name !== this.name) throw new Error(`registry snapshot name mismatch: ${snapshot.name}`);
    const expectedHash = digest({
      version: snapshot.version,
      name: snapshot.name,
      revision: snapshot.revision,
      entries: snapshot.entries,
      staged: snapshot.staged,
    });
    if (!constantTimeDigestEquals(expectedHash, snapshot.snapshotHash)) throw new Error(`registry ${this.name} snapshot hash mismatch`);
    this.revisionValue = snapshot.revision;
    this.entries.clear();
    this.stages.clear();
    this.commits.clear();
    for (const entry of snapshot.entries) {
      if (!constantTimeDigestEquals(digest(entry.value), entry.valueHash)) throw new Error(`registry entry ${entry.id} hash mismatch`);
      if (this.entries.has(entry.id)) throw new Error(`duplicate registry entry ${entry.id}`);
      this.entries.set(entry.id, cloneJson(entry));
    }
    for (const stage of snapshot.staged) {
      if (!constantTimeDigestEquals(digest(stage.mutations), stage.mutationHash)) throw new Error(`registry stage ${stage.stageId} hash mismatch`);
      if (this.stages.has(stage.stageId)) throw new Error(`duplicate registry stage ${stage.stageId}`);
      this.stages.set(stage.stageId, cloneJson(stage));
    }
  }

  private validateMutation(mutation: RegistryMutation<T>): RegistryMutation<T> {
    if (!mutation.id) throw new RegistryConflictError("registry_id_missing", "registry mutation id is required", this.name);
    if (!new Set(["put", "delete", "enable", "disable"]).has(mutation.kind)) {
      throw new RegistryConflictError("registry_mutation_invalid", `unsupported registry mutation ${mutation.kind}`, this.name);
    }
    if (mutation.kind === "put" && !mutation.value) {
      throw new RegistryConflictError("registry_value_missing", `put ${mutation.id} has no value`, this.name);
    }
    if (mutation.expectedEntryRevision !== undefined && mutation.expectedEntryRevision !== null) {
      if (!Number.isSafeInteger(mutation.expectedEntryRevision) || mutation.expectedEntryRevision < 0) {
        throw new RegistryConflictError("registry_entry_revision_invalid", `invalid expected revision for ${mutation.id}`, this.name);
      }
    }
    return cloneJson(mutation);
  }

  private assertExpectedEntryRevision(
    mutation: RegistryMutation<T>,
    current: RevisionedEntry<T> | undefined,
  ): void {
    if (mutation.expectedEntryRevision === undefined) return;
    const observed = current?.revision ?? null;
    if (mutation.expectedEntryRevision !== observed) {
      throw new RegistryConflictError(
        "registry_entry_revision_conflict",
        `entry ${mutation.id} expected revision ${String(mutation.expectedEntryRevision)}, observed ${String(observed)}`,
        this.name,
      );
    }
  }
}
