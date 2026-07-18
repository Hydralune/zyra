import { createHash, randomUUID } from "node:crypto";
import { dirname, isAbsolute, normalize, relative, resolve } from "node:path";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";
import { ContextTokenRuntime } from "../context/token-runtime.ts";
import type { AttachmentCandidate, CompactAttachmentBlock } from "./context-runtime.ts";

export const COMPACT_RESTORE_SNAPSHOT_VERSION = "zyra.compact-restore/v1";

export type RestoreCandidateState = "discovered" | "allowed" | "denied" | "read" | "selected" | "dropped" | "failed";

export interface RestoreCandidate {
  candidateId: string;
  kind: CompactAttachmentBlock["attachmentKind"];
  name: string;
  path: string | null;
  sourceId: string;
  priority: number;
  required: boolean;
  lastAccessedAt: string | null;
  estimatedTokens: number;
  sourceDigest: string | null;
  content: string | null;
  state: RestoreCandidateState;
  reason: string;
  metadata: JsonObject;
  revision: number;
}

export interface RestoreReceipt {
  receiptId: string;
  candidateId: string;
  state: RestoreCandidateState;
  sourceDigest: string | null;
  selectedTokens: number;
  truncated: boolean;
  reason: string;
  createdAt: string;
}

export interface RestorePolicy {
  workspaceRoot: string;
  allowedRoots: string[];
  deniedPatterns: string[];
  maximumTotalTokens: number;
  maximumFileTokens: number;
  maximumSkillTokens: number;
  maximumMemoryTokens: number;
  maximumAgentTokens: number;
  maximumFiles: number;
  maximumSkills: number;
  allowSymlinksOutsideWorkspace: boolean;
}

export interface CompactRestoreSnapshot {
  version: typeof COMPACT_RESTORE_SNAPSHOT_VERSION;
  revision: number;
  policy: RestorePolicy;
  candidates: RestoreCandidate[];
  receipts: RestoreReceipt[];
  resumeProcesses?: ResumeProcessReceipt[];
  checksum: string;
}

export interface ResumeConversationInput {
  sessionId: string | null;
  messages: JsonValue[];
  fileHistorySnapshots?: JsonValue[];
  contentReplacements?: JsonValue[];
  agentName?: string | null;
  agentColor?: string | null;
  agentSetting?: string | null;
  mode?: string | null;
  contextCollapseCommits?: JsonValue[];
  contextCollapseSnapshot?: JsonValue | null;
  metadata?: JsonObject;
}

export interface ProcessResumeOptions {
  forkSession: boolean;
  sessionIdOverride?: string;
  transcriptPath?: string;
  includeAttribution?: boolean;
}

export interface ProcessResumeContext {
  currentSessionId: string;
  currentWorkspace: string;
  availableAgentSettings?: readonly string[];
}

export interface ProcessedResume {
  processId: string;
  sessionId: string;
  sourceSessionId: string | null;
  forked: boolean;
  messages: JsonValue[];
  fileHistorySnapshots: JsonValue[];
  contentReplacements: JsonValue[];
  seededContentReplacements: boolean;
  agentName: string | null;
  agentColor: string | null;
  restoredAgentSetting: string | null;
  mode: string | null;
  workspace: string;
  contextCollapseCommits: JsonValue[];
  contextCollapseSnapshot: JsonValue | null;
  attachments: CompactAttachmentBlock[];
  metadata: JsonObject;
}

export interface ResumeProcessReceipt {
  processId: string;
  inputDigest: string;
  result: ProcessedResume;
  createdAt: string;
}

export interface RestoreReader {
  read(path: string): Promise<{ content: string; canonicalPath: string; sizeBytes: number }>;
}

export class CompactRestoreRuntime {
  private readonly tokens: ContextTokenRuntime;
  private readonly candidates = new Map<string, RestoreCandidate>();
  private readonly receipts: RestoreReceipt[] = [];
  private readonly resumeProcesses = new Map<string, ResumeProcessReceipt>();
  private policy: RestorePolicy;
  private revision = 0;

  constructor(policy: Partial<RestorePolicy> = {}) {
    this.policy = normalizePolicy(policy);
    this.tokens = new ContextTokenRuntime(this.policy.maximumTotalTokens);
  }

  /**
   * Adapted from claude-code-best processResumedConversation. Global session,
   * worktree and agent registries become explicit Zyra inputs and a durable
   * receipt; forked restores keep the fresh session identity and seed content
   * replacements, while non-fork restores adopt the resumed identity.
   */
  async processResumedConversation(
    input: ResumeConversationInput,
    options: ProcessResumeOptions,
    context: ProcessResumeContext,
  ): Promise<ProcessedResume> {
    if (process.env.ZYRA_DISABLE_E04_COMPACT_SOURCE_RUNTIME === "1") {
      throw new Error("e04_compact_source_runtime_disabled");
    }
    const sourceSessionId = input.sessionId?.trim() || null;
    const sessionId = options.forkSession
      ? required(context.currentSessionId, "current session id")
      : required(options.sessionIdOverride?.trim() || sourceSessionId || context.currentSessionId, "resumed session id");
    const workspace = options.forkSession || !options.transcriptPath
      ? normalize(resolve(context.currentWorkspace))
      : normalize(dirname(resolve(options.transcriptPath)));
    const available = new Set(context.availableAgentSettings ?? []);
    const requestedAgent = input.agentSetting?.trim() || null;
    const restoredAgentSetting = requestedAgent !== null
      && (available.size === 0 || available.has(requestedAgent))
      ? requestedAgent
      : null;
    const inputDigest = digest({
      input,
      options,
      context: {
        currentSessionId: context.currentSessionId,
        currentWorkspace: context.currentWorkspace,
        availableAgentSettings: [...available].sort(),
      },
    });
    const processId = `resume-${inputDigest.slice(0, 24)}`;
    const existing = this.resumeProcesses.get(processId);
    if (existing) return structuredClone(existing.result);
    const result: ProcessedResume = {
      processId,
      sessionId,
      sourceSessionId,
      forked: options.forkSession,
      messages: structuredClone(input.messages),
      fileHistorySnapshots: structuredClone(input.fileHistorySnapshots ?? []),
      contentReplacements: structuredClone(input.contentReplacements ?? []),
      seededContentReplacements: options.forkSession && (input.contentReplacements?.length ?? 0) > 0,
      agentName: input.agentName?.trim() || null,
      agentColor: input.agentColor === "default" ? null : (input.agentColor?.trim() || null),
      restoredAgentSetting,
      mode: input.mode?.trim() || null,
      workspace,
      contextCollapseCommits: structuredClone(input.contextCollapseCommits ?? []),
      contextCollapseSnapshot: structuredClone(input.contextCollapseSnapshot ?? null),
      attachments: this.select(),
      metadata: {
        ...sanitizeMetadata(input.metadata ?? {}),
        source: "claude_session_restore_process",
        attribution_requested: options.includeAttribution === true,
        adopted_transcript: !options.forkSession && Boolean(options.transcriptPath),
      },
    };
    this.resumeProcesses.set(processId, {
      processId,
      inputDigest,
      result: structuredClone(result),
      createdAt: new Date().toISOString(),
    });
    this.revision += 1;
    return structuredClone(result);
  }

  restore_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "discover") return candidateToJson(this.discover(candidateInputFromJson(asObject(value.candidate))));
    if (action === "content") return candidateToJson(this.provideContent(asString(value.candidate_id), asString(value.content)));
    if (action === "select") return { attachments: this.select().map(attachmentToJson) };
    if (action === "configure") return policyToJson(this.configure(asObject(value.policy) as Partial<RestorePolicy>));
    return { candidates: [...this.candidates.values()].map(candidateToJson), receipts: this.receipts.map(receiptToJson), revision: this.revision };
  }

  discover(input: {
    candidateId?: string;
    kind: CompactAttachmentBlock["attachmentKind"];
    name: string;
    path?: string | null;
    sourceId: string;
    priority?: number;
    required?: boolean;
    lastAccessedAt?: string | null;
    content?: string | null;
    metadata?: JsonObject;
  }): RestoreCandidate {
    const candidateId = input.candidateId?.trim() || randomUUID();
    const existing = this.candidates.get(candidateId);
    if (existing) return structuredClone(existing);
    const path = input.path?.trim() || null;
    const candidate: RestoreCandidate = {
      candidateId,
      kind: attachmentKind(input.kind),
      name: input.name.trim() || path?.split(/[\\/]/).at(-1) || "restore candidate",
      path,
      sourceId: required(input.sourceId, "restore source id"),
      priority: boundedInteger(input.priority ?? 0, -10_000, 10_000),
      required: input.required ?? false,
      lastAccessedAt: input.lastAccessedAt ? normalizeTimestamp(input.lastAccessedAt) : null,
      estimatedTokens: input.content ? this.tokens.estimate(input.content).estimatedTokens : 0,
      sourceDigest: input.content ? digest(input.content) : null,
      content: input.content ?? null,
      state: "discovered",
      reason: "candidate_discovered",
      metadata: sanitizeMetadata(input.metadata ?? {}),
      revision: 1,
    };
    const policy = this.evaluatePath(candidate);
    candidate.state = policy.allowed ? "allowed" : "denied";
    candidate.reason = policy.reason;
    this.candidates.set(candidateId, candidate);
    this.receipt(candidate, false, policy.reason);
    this.revision += 1;
    return structuredClone(candidate);
  }

  provideContent(candidateId: string, content: string): RestoreCandidate {
    const candidate = this.requireCandidate(candidateId);
    if (candidate.state === "denied") throw new Error(`restore candidate is denied: ${candidate.reason}`);
    const normalized = normalizeContent(content);
    candidate.content = normalized;
    candidate.sourceDigest = digest(normalized);
    candidate.estimatedTokens = this.tokens.estimate(normalized).estimatedTokens;
    candidate.state = "read";
    candidate.reason = "content_provided";
    candidate.revision += 1;
    this.receipt(candidate, false, candidate.reason);
    this.revision += 1;
    return structuredClone(candidate);
  }

  async readCandidate(candidateId: string, reader: RestoreReader): Promise<RestoreCandidate> {
    const candidate = this.requireCandidate(candidateId);
    if (candidate.state === "denied") throw new Error(`restore candidate is denied: ${candidate.reason}`);
    if (!candidate.path) throw new Error("restore candidate has no path");
    try {
      const result = await reader.read(this.resolvePath(candidate.path));
      const canonical = this.resolvePath(result.canonicalPath);
      const pathCheck = this.evaluateResolvedPath(canonical);
      if (!pathCheck.allowed) {
        candidate.state = "denied";
        candidate.reason = pathCheck.reason;
        candidate.revision += 1;
        this.receipt(candidate, false, candidate.reason);
        throw new Error(pathCheck.reason);
      }
      candidate.path = canonical;
      candidate.content = normalizeContent(result.content);
      candidate.sourceDigest = digest(candidate.content);
      candidate.estimatedTokens = this.tokens.estimate(candidate.content).estimatedTokens;
      candidate.state = "read";
      candidate.reason = "content_read";
      candidate.metadata = { ...candidate.metadata, size_bytes: result.sizeBytes };
      candidate.revision += 1;
      this.receipt(candidate, false, candidate.reason);
      this.revision += 1;
      return structuredClone(candidate);
    } catch (error) {
      if (candidate.state !== "denied") {
        candidate.state = "failed";
        candidate.reason = error instanceof Error ? error.message.slice(0, 2_048) : String(error).slice(0, 2_048);
        candidate.revision += 1;
        this.receipt(candidate, false, candidate.reason);
        this.revision += 1;
      }
      throw error;
    }
  }

  select(): CompactAttachmentBlock[] {
    const values = [...this.candidates.values()]
      .filter((candidate) => candidate.state === "read" || (candidate.state === "allowed" && candidate.content !== null))
      .sort(candidateOrder);
    const selected: CompactAttachmentBlock[] = [];
    const digests = new Set<string>();
    const counts = new Map<CompactAttachmentBlock["attachmentKind"], number>();
    let tokens = 0;
    for (const candidate of values) {
      const digestValue = candidate.sourceDigest ?? digest(candidate.content ?? "");
      if (digests.has(digestValue)) {
        candidate.state = "dropped";
        candidate.reason = "duplicate_source_digest";
        candidate.revision += 1;
        this.receipt(candidate, false, candidate.reason);
        continue;
      }
      if (!this.withinKindLimit(candidate, counts)) {
        candidate.state = "dropped";
        candidate.reason = "kind_count_limit";
        candidate.revision += 1;
        this.receipt(candidate, false, candidate.reason);
        continue;
      }
      const maximum = this.kindTokenLimit(candidate.kind);
      const truncated = truncateTokens(this.tokens, candidate.content ?? "", maximum);
      const selectedTokens = this.tokens.estimate(truncated.content).estimatedTokens;
      if (tokens + selectedTokens > this.policy.maximumTotalTokens && !candidate.required) {
        candidate.state = "dropped";
        candidate.reason = "total_token_limit";
        candidate.revision += 1;
        this.receipt(candidate, false, candidate.reason);
        continue;
      }
      const attachment: CompactAttachmentBlock = {
        type: "attachment",
        attachmentKind: candidate.kind,
        path: candidate.path,
        name: candidate.name,
        content: truncated.content,
        sourceDigest: digestValue,
        truncated: truncated.truncated,
      };
      selected.push(attachment);
      digests.add(digestValue);
      counts.set(candidate.kind, (counts.get(candidate.kind) ?? 0) + 1);
      tokens += selectedTokens;
      candidate.state = "selected";
      candidate.reason = truncated.truncated ? "selected_truncated" : "selected";
      candidate.revision += 1;
      this.receipt(candidate, truncated.truncated, candidate.reason, selectedTokens);
    }
    this.revision += 1;
    return structuredClone(selected);
  }

  toAttachmentCandidates(): AttachmentCandidate[] {
    return [...this.candidates.values()]
      .filter((candidate) => candidate.content !== null && candidate.state !== "denied" && candidate.state !== "failed")
      .map((candidate) => ({
        kind: candidate.kind,
        path: candidate.path ?? undefined,
        name: candidate.name,
        content: candidate.content!,
        lastReadAt: candidate.lastAccessedAt ?? undefined,
        priority: candidate.priority + (candidate.required ? 10_000 : 0),
      }));
  }

  configure(value: Partial<RestorePolicy>): RestorePolicy {
    this.policy = normalizePolicy({ ...this.policy, ...value });
    for (const candidate of this.candidates.values()) {
      const result = this.evaluatePath(candidate);
      if (!result.allowed) {
        candidate.state = "denied";
        candidate.reason = result.reason;
        candidate.revision += 1;
      }
    }
    this.revision += 1;
    return structuredClone(this.policy);
  }

  snapshot(): CompactRestoreSnapshot {
    const unsigned: Omit<CompactRestoreSnapshot, "checksum"> = {
      version: COMPACT_RESTORE_SNAPSHOT_VERSION,
      revision: this.revision,
      policy: structuredClone(this.policy),
      candidates: structuredClone([...this.candidates.values()]),
      receipts: structuredClone(this.receipts),
      resumeProcesses: structuredClone([...this.resumeProcesses.values()]),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: CompactRestoreSnapshot): void {
    if (snapshot.version !== COMPACT_RESTORE_SNAPSHOT_VERSION) throw new Error("unsupported compact restore snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("compact restore snapshot checksum mismatch");
    this.policy = normalizePolicy(snapshot.policy);
    this.candidates.clear();
    for (const candidate of snapshot.candidates) {
      if (candidate.content !== null && digest(candidate.content) !== candidate.sourceDigest) throw new Error(`restore candidate digest mismatch: ${candidate.candidateId}`);
      this.candidates.set(candidate.candidateId, structuredClone(candidate));
    }
    this.receipts.splice(0, this.receipts.length, ...structuredClone(snapshot.receipts));
    this.resumeProcesses.clear();
    for (const receipt of snapshot.resumeProcesses ?? []) {
      if (receipt.processId !== `resume-${receipt.inputDigest.slice(0, 24)}`) {
        throw new Error(`compact resume process digest mismatch: ${receipt.processId}`);
      }
      this.resumeProcesses.set(receipt.processId, structuredClone(receipt));
    }
    this.revision = snapshot.revision;
  }

  private evaluatePath(candidate: RestoreCandidate): { allowed: boolean; reason: string } {
    if (!candidate.path) return { allowed: true, reason: "non_file_candidate" };
    try {
      return this.evaluateResolvedPath(this.resolvePath(candidate.path));
    } catch (error) {
      return { allowed: false, reason: error instanceof Error ? error.message : "invalid_path" };
    }
  }

  private evaluateResolvedPath(path: string): { allowed: boolean; reason: string } {
    const normalizedPath = normalize(path).replaceAll("\\", "/");
    for (const source of this.policy.deniedPatterns) {
      try {
        if (new RegExp(source, "i").test(normalizedPath)) return { allowed: false, reason: `denied_pattern:${source}` };
      } catch {
        return { allowed: false, reason: `invalid_denied_pattern:${source}` };
      }
    }
    const allowed = this.policy.allowedRoots.some((root) => inside(root, normalizedPath));
    return allowed ? { allowed: true, reason: "allowed_root" } : { allowed: false, reason: "outside_allowed_roots" };
  }

  private resolvePath(path: string): string {
    return normalize(isAbsolute(path) ? path : resolve(this.policy.workspaceRoot, path));
  }

  private withinKindLimit(candidate: RestoreCandidate, counts: ReadonlyMap<CompactAttachmentBlock["attachmentKind"], number>): boolean {
    if (candidate.required) return true;
    const count = counts.get(candidate.kind) ?? 0;
    if (candidate.kind === "file") return count < this.policy.maximumFiles;
    if (candidate.kind === "skill") return count < this.policy.maximumSkills;
    return true;
  }

  private kindTokenLimit(kind: CompactAttachmentBlock["attachmentKind"]): number {
    if (kind === "file") return this.policy.maximumFileTokens;
    if (kind === "skill") return this.policy.maximumSkillTokens;
    if (kind === "memory") return this.policy.maximumMemoryTokens;
    if (kind === "agent") return this.policy.maximumAgentTokens;
    return Math.max(this.policy.maximumFileTokens, this.policy.maximumMemoryTokens);
  }

  private receipt(candidate: RestoreCandidate, truncated: boolean, reason: string, selectedTokens = 0): RestoreReceipt {
    const receipt: RestoreReceipt = {
      receiptId: randomUUID(),
      candidateId: candidate.candidateId,
      state: candidate.state,
      sourceDigest: candidate.sourceDigest,
      selectedTokens,
      truncated,
      reason,
      createdAt: new Date().toISOString(),
    };
    this.receipts.push(receipt);
    return receipt;
  }

  private requireCandidate(candidateId: string): RestoreCandidate {
    const candidate = this.candidates.get(candidateId);
    if (!candidate) throw new Error(`compact restore candidate not found: ${candidateId}`);
    return candidate;
  }
}

function normalizePolicy(value: Partial<RestorePolicy>): RestorePolicy {
  const workspaceRoot = normalize(resolve(value.workspaceRoot ?? process.cwd()));
  const allowedRoots = [...new Set((value.allowedRoots ?? [workspaceRoot]).map((root) => normalize(resolve(root))))];
  return {
    workspaceRoot,
    allowedRoots,
    deniedPatterns: [...new Set(value.deniedPatterns ?? ["(^|/)(?:\\.git|node_modules)(?:/|$)", "(^|/)(?:\\.env|credentials|secrets?)(?:\\.|/|$)"])],
    maximumTotalTokens: boundedInteger(value.maximumTotalTokens ?? 50_000, 1_000, 1_000_000),
    maximumFileTokens: boundedInteger(value.maximumFileTokens ?? 5_000, 100, 100_000),
    maximumSkillTokens: boundedInteger(value.maximumSkillTokens ?? 5_000, 100, 100_000),
    maximumMemoryTokens: boundedInteger(value.maximumMemoryTokens ?? 10_000, 100, 200_000),
    maximumAgentTokens: boundedInteger(value.maximumAgentTokens ?? 10_000, 100, 200_000),
    maximumFiles: boundedInteger(value.maximumFiles ?? 5, 0, 1_000),
    maximumSkills: boundedInteger(value.maximumSkills ?? 20, 0, 1_000),
    allowSymlinksOutsideWorkspace: value.allowSymlinksOutsideWorkspace ?? false,
  };
}

function truncateTokens(tokens: ContextTokenRuntime, content: string, maximum: number): { content: string; truncated: boolean } {
  if (tokens.estimate(content).estimatedTokens <= maximum) return { content, truncated: false };
  let low = 0;
  let high = content.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (tokens.estimate(content.slice(0, middle)).estimatedTokens <= maximum) low = middle;
    else high = middle - 1;
  }
  return { content: `${content.slice(0, low)}\n...[restore content truncated]`, truncated: true };
}

function candidateOrder(left: RestoreCandidate, right: RestoreCandidate): number {
  return Number(right.required) - Number(left.required) || right.priority - left.priority || Date.parse(right.lastAccessedAt ?? "1970-01-01") - Date.parse(left.lastAccessedAt ?? "1970-01-01") || left.name.localeCompare(right.name);
}

function inside(root: string, path: string): boolean {
  const relation = relative(normalize(root), normalize(path));
  return relation === "" || (!relation.startsWith("..") && !isAbsolute(relation));
}

function normalizeContent(value: string): string {
  return value.normalize("NFC").replaceAll("\r\n", "\n").replaceAll("\r", "\n").replace(/\u0000/g, "");
}

function sanitizeMetadata(value: JsonObject): JsonObject {
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) result[key] = /secret|password|token|api.?key|authorization/i.test(key) ? "[redacted]" : item;
  return result;
}

function candidateInputFromJson(value: JsonObject): Parameters<CompactRestoreRuntime["discover"]>[0] {
  return {
    candidateId: asString(value.candidate_id) || undefined,
    kind: attachmentKind(asString(value.kind, "file")),
    name: asString(value.name),
    path: asString(value.path) || null,
    sourceId: asString(value.source_id, randomUUID()),
    priority: number(value.priority, 0),
    required: asBoolean(value.required, false),
    lastAccessedAt: asString(value.last_accessed_at) || null,
    content: typeof value.content === "string" ? value.content : null,
    metadata: asObject(value.metadata),
  };
}

function candidateToJson(value: RestoreCandidate): JsonObject {
  return {
    candidate_id: value.candidateId,
    kind: value.kind,
    name: value.name,
    path: value.path,
    source_id: value.sourceId,
    priority: value.priority,
    required: value.required,
    last_accessed_at: value.lastAccessedAt,
    estimated_tokens: value.estimatedTokens,
    source_digest: value.sourceDigest,
    content: value.content,
    state: value.state,
    reason: value.reason,
    metadata: value.metadata,
    revision: value.revision,
  };
}

function receiptToJson(value: RestoreReceipt): JsonObject {
  return { receipt_id: value.receiptId, candidate_id: value.candidateId, state: value.state, source_digest: value.sourceDigest, selected_tokens: value.selectedTokens, truncated: value.truncated, reason: value.reason, created_at: value.createdAt };
}

function attachmentToJson(value: CompactAttachmentBlock): JsonObject {
  return { type: value.type, attachment_kind: value.attachmentKind, path: value.path, name: value.name, content: value.content, source_digest: value.sourceDigest, truncated: value.truncated };
}

function policyToJson(value: RestorePolicy): JsonObject {
  return {
    workspace_root: value.workspaceRoot,
    allowed_roots: value.allowedRoots,
    denied_patterns: value.deniedPatterns,
    maximum_total_tokens: value.maximumTotalTokens,
    maximum_file_tokens: value.maximumFileTokens,
    maximum_skill_tokens: value.maximumSkillTokens,
    maximum_memory_tokens: value.maximumMemoryTokens,
    maximum_agent_tokens: value.maximumAgentTokens,
    maximum_files: value.maximumFiles,
    maximum_skills: value.maximumSkills,
    allow_symlinks_outside_workspace: value.allowSymlinksOutsideWorkspace,
  };
}

function attachmentKind(value: string): CompactAttachmentBlock["attachmentKind"] {
  if (value === "plan" || value === "skill" || value === "agent" || value === "memory") return value;
  return "file";
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function boundedInteger(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function normalizeTimestamp(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid restore timestamp: ${value}`);
  return new Date(timestamp).toISOString();
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
