import { createHash } from "node:crypto";
import { mkdirSync, openSync, closeSync, fsyncSync, renameSync, rmSync, writeFileSync, existsSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";
import {
  ARTIFACT_REF_SCHEMA_VERSION,
  EventEffect,
  type ArtifactPointer,
  type RuntimeEventDraft,
} from "./contracts.ts";
import {
  assertJsonValue,
  boundedText,
  byteLength,
  canonicalBytes,
  canonicalJson,
  digestJson,
  isPlainObject,
  sha256Bytes,
  stableId,
  type JsonValue,
} from "./canonical.ts";
import { eventDefinition } from "./event-catalog.ts";
import { EventSpineErrorCode, PayloadBudgetError } from "./errors.ts";

export const INLINE_PAYLOAD_LIMIT_BYTES = 4 * 1024;
export const ENVELOPE_LIMIT_BYTES = 8 * 1024;
export const SUMMARY_LIMIT_BYTES = 1024;
export const ARTIFACT_REF_LIMIT = 64;

export interface PayloadPolicyOptions {
  inlineLimitBytes?: number;
  envelopeLimitBytes?: number;
  summaryLimitBytes?: number;
  artifactRefLimit?: number;
  scalarSpillThresholdBytes?: number;
  objectSpillThresholdBytes?: number;
  maxDepth?: number;
  forbiddenInlineKeys?: readonly string[];
  forceArtifactKeys?: readonly string[];
}

export interface ExternalizedPayload {
  draft: RuntimeEventDraft;
  sourceBytes: number;
  inlineBytes: number;
  estimatedEnvelopeBytes: number;
  artifactSpillCount: number;
  offloadedBytes: number;
  findings: readonly string[];
}

export interface ArtifactWriteRequest {
  runId: string;
  taskId: string;
  producerNodeId?: string;
  title: string;
  mediaType: string;
  extension: string;
  content: Uint8Array;
  metadata?: Readonly<Record<string, JsonValue>>;
}

export interface ArtifactContentStore {
  write(request: ArtifactWriteRequest): ArtifactPointer;
  read(pointer: ArtifactPointer): Uint8Array;
  exists(pointer: ArtifactPointer): boolean;
}

function safeSegment(value: string): string {
  const normalized = value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return normalized || "unknown";
}

function extensionForMediaType(mediaType: string): string {
  if (mediaType === "application/json") return ".json";
  if (mediaType === "application/x-ndjson") return ".jsonl";
  if (mediaType === "text/markdown") return ".md";
  if (mediaType.startsWith("text/")) return ".txt";
  if (mediaType === "image/png") return ".png";
  if (mediaType === "image/jpeg") return ".jpg";
  return ".bin";
}

export class LocalContentAddressedArtifactStore implements ArtifactContentStore {
  readonly root: string;

  constructor(root: string) {
    this.root = resolve(root);
  }

  write(request: ArtifactWriteRequest): ArtifactPointer {
    const digest = sha256Bytes(request.content);
    const hash = digest.slice("sha256:".length);
    const extension = request.extension.startsWith(".") ? request.extension : `.${request.extension}`;
    const directory = join(this.root, safeSegment(request.runId), safeSegment(request.taskId), "runtime-events", hash.slice(0, 2));
    const target = join(directory, `${hash}${extension || extensionForMediaType(request.mediaType)}`);
    mkdirSync(directory, { recursive: true });
    if (!existsSync(target)) {
      const temporary = join(directory, `.${hash}.${process.pid}.${Date.now()}.tmp`);
      try {
        writeFileSync(temporary, request.content, { flag: "wx" });
        // Windows rejects fsync on a read-only descriptor with EPERM.  The
        // temporary artifact is ours, so open it read/write for the durable
        // flush before the atomic rename.
        const descriptor = openSync(temporary, "r+");
        try {
          fsyncSync(descriptor);
        } finally {
          closeSync(descriptor);
        }
        renameSync(temporary, target);
      } catch (error) {
        rmSync(temporary, { force: true });
        if (!existsSync(target)) throw error;
      }
    }
    const relativePath = relative(this.root, target).replaceAll("\\", "/");
    const artifactId = stableId(
      "artifact",
      digest,
      request.runId,
      request.taskId,
      request.producerNodeId ?? "",
      request.title,
    );
    return {
      schema: ARTIFACT_REF_SCHEMA_VERSION,
      artifactId,
      digest,
      mediaType: request.mediaType,
      sizeBytes: request.content.byteLength,
      title: boundedText(request.title, 512),
      uri: `artifact://${artifactId}`,
      producerNodeId: request.producerNodeId,
      metadata: {
        storage: "zyra-content-addressed-local",
        relative_path: relativePath,
        immutable: true,
        ...(request.metadata ?? {}),
      },
    };
  }

  read(pointer: ArtifactPointer): Uint8Array {
    const path = this.resolvePointer(pointer);
    const content = readFileSync(path);
    const digest = sha256Bytes(content);
    if (digest !== pointer.digest) {
      throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact digest mismatch", {
        artifact_id: pointer.artifactId,
        expected: pointer.digest,
        actual: digest,
      });
    }
    return content;
  }

  exists(pointer: ArtifactPointer): boolean {
    try {
      const path = this.resolvePointer(pointer);
      return existsSync(path) && statSync(path).isFile();
    } catch {
      return false;
    }
  }

  private resolvePointer(pointer: ArtifactPointer): string {
    const relativePath = pointer.metadata.relative_path;
    const selected = typeof relativePath === "string" ? resolve(this.root, relativePath) : resolve(pointer.uri ?? "");
    const prefix = this.root.endsWith("\\") || this.root.endsWith("/") ? this.root : `${this.root}${process.platform === "win32" ? "\\" : "/"}`;
    if (selected !== this.root && !selected.startsWith(prefix)) {
      throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact path escapes store", {
        artifact_id: pointer.artifactId,
      });
    }
    return selected;
  }
}

interface SpillContext {
  runId: string;
  taskId: string;
  producerNodeId?: string;
  path: readonly string[];
  artifacts: ArtifactPointer[];
  findings: string[];
  offloadedBytes: number;
}

const DEFAULT_FORBIDDEN_KEYS = [
  "transcript",
  "full_transcript",
  "raw_transcript",
  "source_code",
  "source_pool",
  "dom_snapshot",
  "binary",
  "raw_result",
  "tool_raw_result",
  "provider_response",
  "session_dump",
  "workspace_archive",
];

const DEFAULT_FORCE_ARTIFACT_KEYS = [
  "content",
  "body",
  "output",
  "stdout",
  "stderr",
  "text",
  "result",
  "snapshot",
  "diff",
  "patch",
  "html",
  "markdown",
  "source",
];

export class LowEntropyPayloadPolicy {
  readonly inlineLimitBytes: number;
  readonly envelopeLimitBytes: number;
  readonly summaryLimitBytes: number;
  readonly artifactRefLimit: number;
  readonly scalarSpillThresholdBytes: number;
  readonly objectSpillThresholdBytes: number;
  readonly maxDepth: number;
  readonly forbiddenInlineKeys: ReadonlySet<string>;
  readonly forceArtifactKeys: ReadonlySet<string>;
  readonly artifacts: ArtifactContentStore;

  constructor(artifacts: ArtifactContentStore, options: PayloadPolicyOptions = {}) {
    this.artifacts = artifacts;
    this.inlineLimitBytes = options.inlineLimitBytes ?? INLINE_PAYLOAD_LIMIT_BYTES;
    this.envelopeLimitBytes = options.envelopeLimitBytes ?? ENVELOPE_LIMIT_BYTES;
    this.summaryLimitBytes = options.summaryLimitBytes ?? SUMMARY_LIMIT_BYTES;
    this.artifactRefLimit = options.artifactRefLimit ?? ARTIFACT_REF_LIMIT;
    this.scalarSpillThresholdBytes = options.scalarSpillThresholdBytes ?? 1536;
    this.objectSpillThresholdBytes = options.objectSpillThresholdBytes ?? 2048;
    this.maxDepth = options.maxDepth ?? 12;
    this.forbiddenInlineKeys = new Set((options.forbiddenInlineKeys ?? DEFAULT_FORBIDDEN_KEYS).map((item) => item.toLowerCase()));
    this.forceArtifactKeys = new Set((options.forceArtifactKeys ?? DEFAULT_FORCE_ARTIFACT_KEYS).map((item) => item.toLowerCase()));
    for (const [field, value] of [
      ["inlineLimitBytes", this.inlineLimitBytes],
      ["envelopeLimitBytes", this.envelopeLimitBytes],
      ["summaryLimitBytes", this.summaryLimitBytes],
      ["artifactRefLimit", this.artifactRefLimit],
      ["maxDepth", this.maxDepth],
    ] as const) {
      if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`${field} must be positive`);
    }
  }

  externalize(draft: RuntimeEventDraft, source: unknown = draft.inline ?? {}): ExternalizedPayload {
    assertJsonValue(source, "source");
    const sourceBytes = byteLength(source);
    const summary = this.normalizeSummary(draft.summary ?? "");
    const existingArtifacts = [...(draft.artifactRefs ?? [])];
    const context: SpillContext = {
      runId: draft.identity.runId,
      taskId: draft.identity.taskId,
      producerNodeId: draft.identity.nodeId,
      path: [],
      artifacts: existingArtifacts,
      findings: [],
      offloadedBytes: 0,
    };
    // Spill an over-budget root object as one immutable artifact before the
    // recursive selector can manufacture dozens of leaf artifacts.  The
    // canonical draft.inline fields are merged back below, so routing and
    // projection retain their required low-entropy facts.
    const selectedInline = sourceBytes > this.inlineLimitBytes && isPlainObject(source)
      ? this.pointerPlaceholder(
          this.writeArtifact(context, "source-payload", source, "application/json", ".json", "root_budget_spill"),
          "large_root_payload",
        )
      : this.walk(source, context, 0, "inline");
    const normalizedInline = isPlainObject(selectedInline)
      ? (selectedInline as Record<string, JsonValue>)
      : { value: selectedInline };
    const requiredInline = draft.inline ?? {};
    const sanitizedRequired = this.walk(
      requiredInline as JsonValue,
      { ...context, path: ["canonical_inline"] },
      0,
      "canonical_inline",
    );
    const normalizedRequired = isPlainObject(sanitizedRequired)
      ? sanitizedRequired as Record<string, JsonValue>
      : {};
    let inline: Record<string, JsonValue> = {
      ...normalizedInline,
      ...normalizedRequired,
    };
    let inlineBytes = byteLength(inline);
    if (inlineBytes > this.inlineLimitBytes) {
      const spill = this.writeArtifact(context, "inline-payload", normalizedInline, "application/json", ".json", "inline_budget_spill");
      const requiredKeys = eventDefinition(draft.eventType).requiredInlineKeys;
      const requiredFacts = Object.fromEntries(
        requiredKeys
          .filter((key) => key in normalizedRequired)
          .map((key) => [key, normalizedRequired[key]!]),
      ) as Record<string, JsonValue>;
      inline = {
        ...requiredFacts,
        spilled: true,
        artifact_id: spill.artifactId,
        digest: spill.digest,
        original_bytes: inlineBytes,
      };
      inlineBytes = byteLength(inline);
    }
    if (inlineBytes > this.inlineLimitBytes) {
      throw new PayloadBudgetError(EventSpineErrorCode.INLINE_PAYLOAD_TOO_LARGE, "inline payload exceeds hard limit after spill", {
        inline_bytes: inlineBytes,
        maximum: this.inlineLimitBytes,
      });
    }
    if (context.artifacts.length > this.artifactRefLimit) {
      throw new PayloadBudgetError(EventSpineErrorCode.TOO_MANY_REFS, "artifact ref limit exceeded", {
        count: context.artifacts.length,
        maximum: this.artifactRefLimit,
      });
    }
    const nextDraft: RuntimeEventDraft = {
      ...draft,
      summary,
      inline,
      artifactRefs: Object.freeze(context.artifacts),
      sourceBytes,
      effect: draft.effect ?? (draft.stateDelta?.effective ? EventEffect.EFFECTIVE : EventEffect.UNKNOWN),
      metadata: {
        ...(draft.metadata ?? {}),
        payload_policy: "zyra.low-entropy/v1",
        source_bytes: sourceBytes,
        inline_bytes: inlineBytes,
        offloaded_bytes: context.offloadedBytes,
        spill_count: context.artifacts.length - existingArtifacts.length,
      },
    };
    const estimatedEnvelopeBytes = this.estimateEnvelopeBytes(nextDraft, inlineBytes);
    if (estimatedEnvelopeBytes > this.envelopeLimitBytes) {
      throw new PayloadBudgetError(EventSpineErrorCode.EVENT_TOO_LARGE, "serialized envelope exceeds hard limit after spill", {
        bytes: estimatedEnvelopeBytes,
        maximum: this.envelopeLimitBytes,
        inline_bytes: inlineBytes,
        artifact_refs: context.artifacts.length,
      });
    }
    return {
      draft: nextDraft,
      sourceBytes,
      inlineBytes,
      estimatedEnvelopeBytes,
      artifactSpillCount: context.artifacts.length - existingArtifacts.length,
      offloadedBytes: context.offloadedBytes,
      findings: Object.freeze(context.findings),
    };
  }

  assertCommittedEnvelope(value: unknown): void {
    const bytes = byteLength(value);
    if (bytes > this.envelopeLimitBytes) {
      throw new PayloadBudgetError(EventSpineErrorCode.EVENT_TOO_LARGE, "committed envelope exceeds hard limit", {
        bytes,
        maximum: this.envelopeLimitBytes,
      });
    }
    if (!isPlainObject(value)) return;
    const summary = typeof value.summary === "string" ? value.summary : "";
    if (Buffer.byteLength(summary, "utf8") > this.summaryLimitBytes) {
      throw new PayloadBudgetError(EventSpineErrorCode.SUMMARY_TOO_LARGE, "committed summary exceeds hard limit");
    }
    const inline = isPlainObject(value.inline) ? value.inline : {};
    const inlineBytes = byteLength(inline);
    if (inlineBytes > this.inlineLimitBytes) {
      throw new PayloadBudgetError(EventSpineErrorCode.INLINE_PAYLOAD_TOO_LARGE, "committed inline payload exceeds hard limit", {
        inline_bytes: inlineBytes,
        maximum: this.inlineLimitBytes,
      });
    }
  }

  normalizeSummary(summary: string): string {
    const normalized = summary.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
    if (Buffer.byteLength(normalized, "utf8") <= this.summaryLimitBytes) return normalized;
    return boundedText(normalized, this.summaryLimitBytes);
  }

  private walk(value: JsonValue, context: SpillContext, depth: number, key: string): JsonValue {
    if (depth > this.maxDepth) {
      const pointer = this.writeArtifact(context, key, value, "application/json", ".json", "depth_spill");
      return this.pointerPlaceholder(pointer, "depth_limit");
    }
    if (typeof value === "string") return this.walkString(value, context, key);
    if (value === null || typeof value !== "object") return value;
    const size = byteLength(value);
    if (size > this.objectSpillThresholdBytes && this.forceArtifactKeys.has(key.toLowerCase())) {
      const pointer = this.writeArtifact(context, key, value, "application/json", ".json", "object_key_spill");
      return this.pointerPlaceholder(pointer, "large_structured_value");
    }
    if (Array.isArray(value)) {
      const output: JsonValue[] = [];
      for (let index = 0; index < value.length; index += 1) {
        output.push(this.walk(value[index]!, { ...context, path: [...context.path, String(index)] }, depth + 1, key));
      }
      return output;
    }
    const output: Record<string, JsonValue> = {};
    for (const [childKey, child] of Object.entries(value)) {
      const lowered = childKey.toLowerCase();
      const nextContext = { ...context, path: [...context.path, childKey] };
      if (this.forbiddenInlineKeys.has(lowered)) {
        const pointer = this.writeArtifact(nextContext, childKey, child, this.mediaType(child), this.extension(child), "forbidden_inline_key");
        output[`${childKey}_ref`] = this.pointerPlaceholder(pointer, "forbidden_inline_content");
        continue;
      }
      output[childKey] = this.walk(child, nextContext, depth + 1, childKey);
    }
    if (byteLength(output) > this.objectSpillThresholdBytes && depth > 0) {
      const pointer = this.writeArtifact(context, key, output, "application/json", ".json", "object_budget_spill");
      return this.pointerPlaceholder(pointer, "large_object");
    }
    return output;
  }

  private walkString(value: string, context: SpillContext, key: string): JsonValue {
    const bytes = Buffer.byteLength(value, "utf8");
    const lowered = key.toLowerCase();
    const looksLikeLargeContent = this.forceArtifactKeys.has(lowered) && bytes > 512;
    if (bytes <= this.scalarSpillThresholdBytes && !looksLikeLargeContent) return value;
    const mediaType = this.stringMediaType(key, value);
    const pointer = this.artifacts.write({
      runId: context.runId,
      taskId: context.taskId,
      producerNodeId: context.producerNodeId,
      title: this.title(context.path, key),
      mediaType,
      extension: extensionForMediaType(mediaType),
      content: Buffer.from(value, "utf8"),
      metadata: {
        source_path: [...context.path, key].join("."),
        spill_reason: looksLikeLargeContent ? "content_key_spill" : "scalar_budget_spill",
      },
    });
    context.artifacts.push(pointer);
    context.offloadedBytes += bytes;
    context.findings.push(`${[...context.path, key].join(".")}:offloaded:${bytes}`);
    return this.pointerPlaceholder(pointer, "large_text");
  }

  private writeArtifact(
    context: SpillContext,
    key: string,
    value: JsonValue,
    mediaType: string,
    extension: string,
    reason: string,
  ): ArtifactPointer {
    const content = mediaType.startsWith("text/") && typeof value === "string" ? Buffer.from(value, "utf8") : canonicalBytes(value);
    const pointer = this.artifacts.write({
      runId: context.runId,
      taskId: context.taskId,
      producerNodeId: context.producerNodeId,
      title: this.title(context.path, key),
      mediaType,
      extension,
      content,
      metadata: {
        source_path: [...context.path, key].join("."),
        spill_reason: reason,
        source_digest: sha256Bytes(content),
      },
    });
    context.artifacts.push(pointer);
    context.offloadedBytes += content.byteLength;
    context.findings.push(`${[...context.path, key].join(".")}:offloaded:${content.byteLength}`);
    return pointer;
  }

  private pointerPlaceholder(pointer: ArtifactPointer, reason: string): Record<string, JsonValue> {
    return {
      artifact_ref: pointer.artifactId,
      digest: pointer.digest,
      media_type: pointer.mediaType,
      size_bytes: pointer.sizeBytes,
      offload_reason: reason,
    };
  }

  private title(path: readonly string[], key: string): string {
    const value = [...path, key].filter(Boolean).join(".") || "runtime-event-payload";
    return boundedText(`Runtime event payload: ${value}`, 512);
  }

  private mediaType(value: JsonValue): string {
    if (typeof value === "string") return "text/plain";
    return "application/json";
  }

  private extension(value: JsonValue): string {
    return typeof value === "string" ? ".txt" : ".json";
  }

  private stringMediaType(key: string, value: string): string {
    const extension = extname(key).toLowerCase();
    if (extension === ".json" || this.looksLikeJson(value)) return "application/json";
    if (extension === ".md" || key.toLowerCase().includes("markdown")) return "text/markdown";
    if (extension === ".html" || /^\s*<!doctype html/i.test(value)) return "text/html";
    return "text/plain";
  }

  private looksLikeJson(value: string): boolean {
    const trimmed = value.trim();
    if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) return false;
    try {
      JSON.parse(trimmed);
      return true;
    } catch {
      return false;
    }
  }

  private estimateEnvelopeBytes(draft: RuntimeEventDraft, inlineBytes: number): number {
    const estimate = {
      schema: "zyra.runtime-event/v1",
      eventId: draft.eventId ?? "evt_00000000000000000000000000000000",
      eventType: draft.eventType,
      eventVersion: draft.eventVersion ?? 1,
      aggregateId: draft.aggregateId,
      aggregateSequence: draft.expectedSequence ?? 9_999_999,
      globalSequence: 9_999_999,
      producerSequence: draft.producerSequence ?? 9_999_999,
      idempotencyKey: draft.idempotencyKey,
      correlationId: draft.correlationId,
      causationId: draft.causationId,
      createdAt: draft.createdAt ?? "2026-01-01T00:00:00.000Z",
      committedAt: "2026-01-01T00:00:00.000Z",
      durability: draft.durability ?? "durable",
      effect: draft.effect ?? "unknown",
      identity: draft.identity,
      sender: draft.sender,
      intent: draft.intent,
      target: draft.target,
      topKRecipients: draft.topKRecipients ?? [],
      summary: draft.summary ?? "",
      stateDelta: draft.stateDelta,
      evidenceRefs: draft.evidenceRefs ?? [],
      artifactRefs: draft.artifactRefs ?? [],
      uncertainty: draft.uncertainty,
      provenance: draft.provenance,
      inline: draft.inline ?? {},
      sourceBytes: draft.sourceBytes ?? inlineBytes,
      inlineBytes,
      envelopeBytes: 9999,
      contentDigest: "sha256:" + "f".repeat(64),
      metadata: draft.metadata ?? {},
    };
    const serializableEstimate = JSON.parse(JSON.stringify(estimate)) as JsonValue;
    return Buffer.byteLength(canonicalJson(serializableEstimate), "utf8");
  }
}

export function artifactPointerDigest(pointer: ArtifactPointer): string {
  return digestJson({
    artifact_id: pointer.artifactId,
    digest: pointer.digest,
    media_type: pointer.mediaType,
    size_bytes: pointer.sizeBytes,
  });
}

export function artifactRefsDigest(pointers: readonly ArtifactPointer[]): string {
  return digestJson(pointers.map((pointer) => artifactPointerDigest(pointer)).sort());
}

export function inlinePayloadContainsForbiddenContent(
  value: unknown,
  forbiddenKeys: ReadonlySet<string> = new Set(DEFAULT_FORBIDDEN_KEYS),
): readonly string[] {
  const findings: string[] = [];
  const visit = (item: unknown, path: string, depth: number): void => {
    if (depth > 32 || item === null || typeof item !== "object") return;
    if (Array.isArray(item)) {
      item.forEach((child, index) => visit(child, `${path}[${index}]`, depth + 1));
      return;
    }
    for (const [key, child] of Object.entries(item)) {
      const childPath = path ? `${path}.${key}` : key;
      if (forbiddenKeys.has(key.toLowerCase())) findings.push(childPath);
      visit(child, childPath, depth + 1);
    }
  };
  visit(value, "", 0);
  return findings;
}
