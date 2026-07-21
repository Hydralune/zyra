import {
  cloneJson,
  contractError,
  digest,
  nowIso,
  stableId,
  uniqueStrings,
  type CompactArchiveReference,
  type JsonObject,
  type SafeCutPlan,
} from "./contracts.ts";

export interface CompactArchiveInput {
  artifactId: string;
  boundaryId: string;
  sessionId: string;
  compactGeneration: number;
  contentDigest: string;
  summary: string;
  safeCutPlan: SafeCutPlan;
  tokenCountAfter: number;
  metadata?: JsonObject;
}

export interface CompactArchiveSnapshot {
  version: "zyra.compact-archive/v1";
  archives: CompactArchiveReference[];
  revision: number;
  checksum: string;
}

export class CompactArchiveRuntime {
  private readonly archives = new Map<string, CompactArchiveReference>();
  private readonly boundaryBindings = new Map<string, string>();
  private readonly artifactBindings = new Map<string, string>();
  private readonly now: () => Date;
  private revision = 0;

  constructor(now: () => Date = () => new Date()) {
    this.now = now;
  }

  commit(inputValue: CompactArchiveInput): CompactArchiveReference {
    const input = normalizeInput(inputValue);
    if (!input.safeCutPlan.valid) throw contractError("compact_archive_invalid_cut", "cannot archive an invalid safe cut plan", { plan_id: input.safeCutPlan.planId, reason: input.safeCutPlan.reason });
    const existingByBoundary = this.boundaryBindings.get(input.boundaryId);
    if (existingByBoundary) {
      const existing = this.require(existingByBoundary);
      if (existing.artifactId !== input.artifactId || existing.contentDigest !== input.contentDigest) {
        throw contractError("compact_archive_boundary_conflict", `compact boundary ${input.boundaryId} already has a different archive`);
      }
      return cloneJson(existing);
    }
    const existingByArtifact = this.artifactBindings.get(input.artifactId);
    if (existingByArtifact) {
      const existing = this.require(existingByArtifact);
      if (existing.boundaryId !== input.boundaryId) throw contractError("compact_archive_artifact_reused", "compact artifact cannot represent multiple boundaries");
      return cloneJson(existing);
    }
    const createdAt = nowIso(this.now);
    const archive: CompactArchiveReference = {
      archiveId: stableId("compact-archive", input.sessionId, input.boundaryId, input.artifactId, input.contentDigest),
      artifactId: input.artifactId,
      boundaryId: input.boundaryId,
      sessionId: input.sessionId,
      compactGeneration: input.compactGeneration,
      contentDigest: input.contentDigest,
      summaryDigest: digest(input.summary),
      summarizedMessageIds: [...input.safeCutPlan.summarizedMessageIds],
      preservedMessageIds: [...input.safeCutPlan.preservedMessageIds],
      tokenCountBefore: input.safeCutPlan.sourceTokens,
      tokenCountAfter: input.tokenCountAfter,
      createdAt,
      metadata: {
        ...input.metadata,
        safe_cut_plan_id: input.safeCutPlan.planId,
        safe_cut_plan_digest: input.safeCutPlan.planDigest,
        summary_characters: input.summary.length,
        archive_is_canonical_session: false,
        canonical_compact_owner: "02D ContextCompactionRuntime",
      },
    };
    this.archives.set(archive.archiveId, archive);
    this.boundaryBindings.set(archive.boundaryId, archive.archiveId);
    this.artifactBindings.set(archive.artifactId, archive.archiveId);
    this.revision += 1;
    return cloneJson(archive);
  }

  get(archiveId: string): CompactArchiveReference | null {
    const archive = this.archives.get(archiveId.trim());
    return archive ? cloneJson(archive) : null;
  }

  getByBoundary(boundaryId: string): CompactArchiveReference | null {
    const archiveId = this.boundaryBindings.get(boundaryId.trim());
    return archiveId ? this.get(archiveId) : null;
  }

  list(sessionId = "", limit = 1_000): CompactArchiveReference[] {
    return [...this.archives.values()]
      .filter((archive) => !sessionId || archive.sessionId === sessionId)
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
      .slice(-Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  snapshot(): CompactArchiveSnapshot {
    const unsigned: Omit<CompactArchiveSnapshot, "checksum"> = {
      version: "zyra.compact-archive/v1",
      archives: this.list("", 100_000),
      revision: this.revision,
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: CompactArchiveSnapshot): void {
    if (snapshotValue.version !== "zyra.compact-archive/v1") throw contractError("compact_archive_snapshot_version", "unsupported compact archive snapshot version");
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) throw contractError("compact_archive_snapshot_checksum", "compact archive snapshot checksum mismatch");
    this.archives.clear();
    this.boundaryBindings.clear();
    this.artifactBindings.clear();
    for (const archive of snapshotValue.archives) {
      validateArchive(archive);
      if (this.archives.has(archive.archiveId)) throw contractError("compact_archive_snapshot_duplicate", `duplicate compact archive ${archive.archiveId}`);
      if (this.boundaryBindings.has(archive.boundaryId)) throw contractError("compact_archive_snapshot_boundary_duplicate", `duplicate compact boundary ${archive.boundaryId}`);
      if (this.artifactBindings.has(archive.artifactId)) throw contractError("compact_archive_snapshot_artifact_duplicate", `duplicate compact artifact ${archive.artifactId}`);
      this.archives.set(archive.archiveId, cloneJson(archive));
      this.boundaryBindings.set(archive.boundaryId, archive.archiveId);
      this.artifactBindings.set(archive.artifactId, archive.archiveId);
    }
    this.revision = snapshotValue.revision;
  }

  private require(archiveId: string): CompactArchiveReference {
    const archive = this.archives.get(archiveId);
    if (!archive) throw contractError("compact_archive_not_found", `compact archive not found: ${archiveId}`);
    return archive;
  }
}

function normalizeInput(value: CompactArchiveInput): CompactArchiveInput {
  const required = (input: string, label: string): string => {
    const text = input?.trim();
    if (!text) throw contractError("compact_archive_required", `${label} is required`);
    return text;
  };
  if (!Number.isSafeInteger(value.compactGeneration) || value.compactGeneration < 0) throw contractError("compact_archive_generation", "compact archive generation must be non-negative");
  if (!Number.isSafeInteger(value.tokenCountAfter) || value.tokenCountAfter < 0) throw contractError("compact_archive_token_count", "compact archive token count must be non-negative");
  const contentDigest = required(value.contentDigest, "archive content digest").replace(/^sha256:/, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(contentDigest)) throw contractError("compact_archive_content_digest", "archive content digest must be sha256");
  const summary = required(value.summary, "compact summary");
  const { planDigest, ...planUnsigned } = value.safeCutPlan;
  if (digest(planUnsigned) !== planDigest) throw contractError("compact_archive_safe_cut_digest", "safe cut plan digest mismatch");
  if (value.tokenCountAfter >= value.safeCutPlan.sourceTokens) throw contractError("compact_archive_no_reduction", "compact archive token count must be lower than source tokens");
  return {
    artifactId: required(value.artifactId, "compact artifact id"),
    boundaryId: required(value.boundaryId, "compact boundary id"),
    sessionId: required(value.sessionId, "compact session id"),
    compactGeneration: value.compactGeneration,
    contentDigest,
    summary,
    safeCutPlan: cloneJson(value.safeCutPlan),
    tokenCountAfter: value.tokenCountAfter,
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function validateArchive(value: CompactArchiveReference): void {
  if (!value.archiveId || !value.artifactId || !value.boundaryId || !value.sessionId) throw contractError("compact_archive_restore_required", "restored compact archive is incomplete");
  if (!/^[0-9a-f]{64}$/.test(value.contentDigest) || !/^[0-9a-f]{64}$/.test(value.summaryDigest)) throw contractError("compact_archive_restore_digest", "restored compact archive has invalid digest");
  if (value.tokenCountAfter >= value.tokenCountBefore) throw contractError("compact_archive_restore_tokens", "restored compact archive did not reduce tokens");
  if (new Set(uniqueStrings([...value.summarizedMessageIds, ...value.preservedMessageIds])).size !== value.summarizedMessageIds.length + value.preservedMessageIds.length) {
    throw contractError("compact_archive_restore_overlap", "summarized and preserved messages overlap");
  }
}
