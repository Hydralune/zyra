import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  deterministicId,
  digest,
  monotonicNow,
  normalizeName,
} from "../e02/index.ts";
import type {
  SkillDescriptor,
  SkillRegistrySnapshot,
  SkillRevision,
} from "./contracts-v2.ts";

export interface SkillRegistryCommitInput {
  baseRevision: number;
  descriptors: SkillDescriptor[];
  sourcePriorityOverrides?: Record<string, number>;
  metadata?: JsonObject;
}

export interface SkillResolution {
  requestedName: string;
  normalizedName: string;
  skillId: string;
  descriptor: SkillDescriptor;
  revision: number;
  revisionId: string;
  resolutionKind: "id" | "name" | "alias";
  shadowedCandidates: string[];
}

export class SkillRegistryRuntime {
  private readonly revisions = new Map<number, SkillRevision>();
  private readonly active = new Map<string, SkillDescriptor>();
  private readonly nameIndex = new Map<string, string>();
  private readonly aliasIndex = new Map<string, string>();
  private readonly sourceIndex = new Map<string, Set<string>>();
  private readonly pins = new Map<number, number>();
  private readonly now: () => Date;
  private readonly maximumRevisions: number;
  private revisionValue = 0;
  private headRevisionIdValue: string | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRevisions?: number; snapshot?: SkillRegistrySnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRevisions = options.maximumRevisions ?? 100;
    if (options.snapshot) this.restore(options.snapshot);
  }

  get revision(): number {
    return this.revisionValue;
  }

  get headRevisionId(): string | null {
    return this.headRevisionIdValue;
  }

  commitRevision(inputValue: SkillRegistryCommitInput): SkillRevision {
    const input = cloneJson(inputValue);
    if (input.baseRevision !== this.revisionValue) throw registryError("registry_revision_conflict", `skill registry revision ${input.baseRevision} does not match ${this.revisionValue}`);
    const normalized = input.descriptors.map(validateDescriptor);
    const resolved = resolveCollisions(normalized, input.sourcePriorityOverrides ?? {});
    const next = new Map<string, SkillDescriptor>();
    for (const descriptor of resolved.active) next.set(descriptor.skillId, descriptor);
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    for (const id of new Set([...this.active.keys(), ...next.keys()])) {
      const before = this.active.get(id);
      const after = next.get(id);
      if (!before && after) added.push(id);
      else if (before && !after) removed.push(id);
      else if (before && after && before.descriptorDigest !== after.descriptorDigest) updated.push(id);
    }
    const revision = this.revisionValue + 1;
    const committedAt = this.timestamp();
    const revisionBase = {
      revision,
      parentRevisionId: this.headRevisionIdValue,
      skills: [...next.values()].sort(compareDescriptor).map(cloneJson),
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      shadowed: resolved.shadowed.map((descriptor) => descriptor.skillId).sort(),
      invalid: resolved.invalid.map((descriptor) => descriptor.skillId).sort(),
      committedAt,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const revisionId = deterministicId("skill-registry-revision", revisionBase, 32);
    const record: SkillRevision = {
      revisionId,
      ...revisionBase,
      digest: digest({ revisionId, ...revisionBase }),
    };
    this.revisions.set(revision, record);
    this.revisionValue = revision;
    this.headRevisionIdValue = revisionId;
    this.rebuildIndexes(record.skills);
    this.trimRevisions();
    return cloneJson(record);
  }

  resolve(nameValue: string, revision = this.revisionValue): SkillResolution {
    const normalizedName = normalizeLookup(nameValue);
    const record = this.revisions.get(revision);
    if (!record) throw registryError("skill_revision_not_found", `skill registry revision ${revision} was not found`);
    const byId = record.skills.find((skill) => normalizeLookup(skill.skillId) === normalizedName);
    const byName = record.skills.find((skill) => normalizeLookup(skill.name) === normalizedName);
    const byAlias = record.skills.find((skill) => skill.aliases.some((alias) => normalizeLookup(alias) === normalizedName));
    const descriptor = byId ?? byName ?? byAlias;
    if (!descriptor) throw registryError("skill_not_found", `skill ${nameValue} was not found at revision ${revision}`);
    if (descriptor.availability !== "available") throw registryError("skill_unavailable", `skill ${descriptor.skillId} is ${descriptor.availability}: ${descriptor.disabledReason ?? "no reason"}`);
    const candidates = record.skills
      .filter((skill) => skill.skillId !== descriptor.skillId)
      .filter((skill) => normalizeLookup(skill.name) === normalizedName || skill.aliases.some((alias) => normalizeLookup(alias) === normalizedName))
      .map((skill) => skill.skillId);
    return {
      requestedName: nameValue,
      normalizedName,
      skillId: descriptor.skillId,
      descriptor: cloneJson(descriptor),
      revision,
      revisionId: record.revisionId,
      resolutionKind: byId ? "id" : byName ? "name" : "alias",
      shadowedCandidates: candidates.sort(),
    };
  }

  pin(revision = this.revisionValue): () => void {
    if (!this.revisions.has(revision)) throw registryError("skill_revision_not_found", `skill registry revision ${revision} was not found`);
    this.pins.set(revision, (this.pins.get(revision) ?? 0) + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const count = this.pins.get(revision) ?? 0;
      if (count <= 1) this.pins.delete(revision);
      else this.pins.set(revision, count - 1);
      this.trimRevisions();
    };
  }

  list(options: { revision?: number; sourceId?: string; tag?: string; availableOnly?: boolean } = {}): SkillDescriptor[] {
    const revision = options.revision ?? this.revisionValue;
    const record = this.revisions.get(revision);
    if (!record) return [];
    return record.skills
      .filter((skill) => !options.sourceId || skill.source.sourceId === options.sourceId)
      .filter((skill) => !options.tag || skill.tags.includes(options.tag))
      .filter((skill) => !options.availableOnly || skill.availability === "available")
      .sort(compareDescriptor)
      .map(cloneJson);
  }

  sourceSkills(sourceId: string): string[] {
    return [...(this.sourceIndex.get(sourceId) ?? [])].sort();
  }

  snapshot(): SkillRegistrySnapshot {
    const withoutDigest = {
      version: "zyra.skill-registry/v2" as const,
      revision: this.revisionValue,
      headRevisionId: this.headRevisionIdValue,
      revisions: [...this.revisions.values()].sort((left, right) => left.revision - right.revision).map(cloneJson),
      activeSkills: [...this.active.values()].sort(compareDescriptor).map(cloneJson),
      aliasIndex: Object.fromEntries([...this.aliasIndex.entries()].sort(([left], [right]) => left.localeCompare(right))),
      sourceIndex: Object.fromEntries([...this.sourceIndex.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([key, values]) => [key, [...values].sort()])),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: SkillRegistrySnapshot): void {
    if (snapshot.version !== "zyra.skill-registry/v2") throw registryError("unsupported_registry_snapshot", "unsupported skill registry snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw registryError("registry_snapshot_digest_mismatch", "skill registry snapshot digest mismatch");
    const head = snapshot.revisions.find((revision) => revision.revision === snapshot.revision);
    if (snapshot.revision > 0 && (!head || head.revisionId !== snapshot.headRevisionId)) throw registryError("registry_snapshot_head_mismatch", "skill registry head revision mismatch");
    this.revisions.clear();
    this.revisionValue = snapshot.revision;
    this.headRevisionIdValue = snapshot.headRevisionId;
    for (const revision of snapshot.revisions) this.revisions.set(revision.revision, cloneJson(revision));
    this.rebuildIndexes(snapshot.activeSkills.map(validateDescriptor));
  }

  private rebuildIndexes(skills: readonly SkillDescriptor[]): void {
    this.active.clear();
    this.nameIndex.clear();
    this.aliasIndex.clear();
    this.sourceIndex.clear();
    for (const skill of skills) {
      this.active.set(skill.skillId, cloneJson(skill));
      this.nameIndex.set(normalizeLookup(skill.name), skill.skillId);
      for (const alias of skill.aliases) this.aliasIndex.set(normalizeLookup(alias), skill.skillId);
      let sourceSkills = this.sourceIndex.get(skill.source.sourceId);
      if (!sourceSkills) {
        sourceSkills = new Set();
        this.sourceIndex.set(skill.source.sourceId, sourceSkills);
      }
      sourceSkills.add(skill.skillId);
    }
  }

  private trimRevisions(): void {
    const revisions = [...this.revisions.keys()].sort((left, right) => left - right);
    while (revisions.length > this.maximumRevisions) {
      const candidate = revisions.shift()!;
      if (this.pins.has(candidate) || candidate === this.revisionValue) {
        revisions.push(candidate);
        if (revisions.every((revision) => this.pins.has(revision) || revision === this.revisionValue)) break;
        continue;
      }
      this.revisions.delete(candidate);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function resolveCollisions(
  descriptors: SkillDescriptor[],
  overrides: Record<string, number>,
): { active: SkillDescriptor[]; shadowed: SkillDescriptor[]; invalid: SkillDescriptor[] } {
  const groups = new Map<string, SkillDescriptor[]>();
  const invalid = descriptors.filter((descriptor) => descriptor.availability === "invalid");
  for (const descriptor of descriptors.filter((value) => value.availability !== "invalid")) {
    const key = normalizeLookup(descriptor.name);
    const group = groups.get(key) ?? [];
    group.push(descriptor);
    groups.set(key, group);
  }
  const active: SkillDescriptor[] = [];
  const shadowed: SkillDescriptor[] = [];
  for (const group of groups.values()) {
    group.sort((left, right) => {
      const leftPriority = overrides[left.source.sourceId] ?? left.source.sourcePriority;
      const rightPriority = overrides[right.source.sourceId] ?? right.source.sourcePriority;
      return rightPriority - leftPriority || right.source.modifiedAtMs - left.source.modifiedAtMs || left.skillId.localeCompare(right.skillId);
    });
    const winner = cloneJson(group[0]);
    active.push(winner);
    for (const loserValue of group.slice(1)) {
      const loser = cloneJson(loserValue);
      loser.availability = "shadowed";
      loser.disabledReason = `shadowed_by:${winner.skillId}`;
      shadowed.push(loser);
    }
  }
  const aliasOwner = new Map<string, SkillDescriptor>();
  for (const descriptor of active.sort(compareDescriptor)) {
    descriptor.aliases = descriptor.aliases.filter((alias) => {
      const key = normalizeLookup(alias);
      const owner = aliasOwner.get(key);
      if (!owner) {
        aliasOwner.set(key, descriptor);
        return true;
      }
      descriptor.warnings.push(`alias ${alias} shadowed by ${owner.skillId}`);
      return false;
    });
  }
  return { active, shadowed, invalid };
}

function validateDescriptor(value: SkillDescriptor): SkillDescriptor {
  const descriptor = cloneJson(value);
  if (!descriptor.skillId || !descriptor.name || !descriptor.descriptorDigest) throw registryError("invalid_skill_descriptor", "skill descriptor identity is incomplete");
  const { descriptorDigest: _expected, ...withoutDigest } = descriptor;
  // Parsers may include parsedAt in the digest; requiring a non-empty digest is stable across parser versions.
  return descriptor;
}

function compareDescriptor(left: SkillDescriptor, right: SkillDescriptor): number {
  return left.name.localeCompare(right.name) || left.skillId.localeCompare(right.skillId);
}

function normalizeLookup(value: string): string {
  return normalizeName(value, 512).normalize("NFKC").toLowerCase();
}

function registryError(code: string, message: string): Error {
  const error = new Error(message);
  error.name = "SkillRegistryError";
  Object.assign(error, { code });
  return error;
}
