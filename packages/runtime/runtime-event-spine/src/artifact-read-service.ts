import { createHash } from "node:crypto";
import {
  ARTIFACT_READ_SCHEMA,
  type ArtifactReadRequest,
  type ArtifactReadResult,
} from "./integration-contracts.ts";
import type { ArtifactPointer, RuntimeEventEnvelope } from "./contracts.ts";
import { normalizeIdentifier, optionalInteger, optionalString, requireRecord, requireString, type JsonValue } from "./canonical.ts";
import { EventSpineError, EventSpineErrorCode, PayloadBudgetError } from "./errors.ts";
import type { RuntimeEventSpine } from "./runtime.ts";

export interface ArtifactAuditResult {
  schema: "zyra.runtime-artifact-audit/v1";
  artifactId: string;
  found: boolean;
  exists: boolean;
  digestMatches: boolean;
  sizeMatches: boolean;
  readableAfterRestart: boolean;
  referencedBy: readonly string[];
  pointer?: ArtifactPointer;
  findings: readonly string[];
}

export interface ArtifactReadPolicy {
  maxReadBytes: number;
  allowUtf8MediaPrefixes: readonly string[];
  requireProjectedReference: boolean;
  requireImmutable: boolean;
}

const DEFAULT_POLICY: ArtifactReadPolicy = {
  maxReadBytes: 1024 * 1024,
  allowUtf8MediaPrefixes: ["text/", "application/json", "application/x-ndjson"],
  requireProjectedReference: true,
  requireImmutable: true,
};

function parseRequest(value: ArtifactReadRequest | unknown): ArtifactReadRequest {
  const input = requireRecord(value, "artifact read request");
  const encoding = input.encoding ?? "base64";
  if (encoding !== "base64" && encoding !== "utf8") {
    throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact encoding is unsupported", {
      encoding: String(encoding),
    });
  }
  const offset = optionalInteger(input.offset, "artifact read offset") ?? 0;
  const length = optionalInteger(input.length, "artifact read length");
  if (offset < 0 || (length !== undefined && length < 1)) {
    throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact read range is invalid", {
      offset,
      length: length ?? null,
    });
  }
  return {
    artifactId: normalizeIdentifier(input.artifactId, "artifact read artifactId"),
    expectedDigest: optionalString(input.expectedDigest, "artifact read expectedDigest", 80),
    offset,
    length,
    encoding,
  };
}

function sha256(content: Uint8Array): string {
  return `sha256:${createHash("sha256").update(content).digest("hex")}`;
}

function pointerKey(pointer: ArtifactPointer): string {
  return `${pointer.artifactId}:${pointer.digest}`;
}

export class RuntimeArtifactReadService {
  readonly spine: RuntimeEventSpine;
  readonly policy: Readonly<ArtifactReadPolicy>;

  constructor(spine: RuntimeEventSpine, policy: Partial<ArtifactReadPolicy> = {}) {
    this.spine = spine;
    this.policy = Object.freeze({ ...DEFAULT_POLICY, ...policy });
  }

  locate(artifactId: string): { pointer: ArtifactPointer; events: readonly RuntimeEventEnvelope[] } | undefined {
    const id = normalizeIdentifier(artifactId, "artifactId");
    const page = this.spine.query({ artifactId: id, limit: 1000 });
    const events = [...page.items];
    let cursor = page.nextCursor;
    while (cursor) {
      const next = this.spine.query({ artifactId: id, cursor, limit: 1000 });
      events.push(...next.items);
      cursor = next.nextCursor;
    }
    const pointers = new Map<string, ArtifactPointer>();
    for (const event of events) {
      for (const pointer of event.artifactRefs) {
        if (pointer.artifactId === id) pointers.set(pointerKey(pointer), pointer);
      }
    }
    if (pointers.size === 0) return undefined;
    if (pointers.size > 1) {
      throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact id resolves to conflicting digests", {
        artifact_id: id,
        pointer_count: pointers.size,
        digests: [...pointers.values()].map((pointer) => pointer.digest),
      });
    }
    return { pointer: [...pointers.values()][0]!, events };
  }

  read(value: ArtifactReadRequest | unknown): ArtifactReadResult {
    const request = parseRequest(value);
    const located = this.locate(request.artifactId);
    if (!located) {
      throw new EventSpineError({
        code: EventSpineErrorCode.DELIVERY_NOT_FOUND,
        message: "artifact reference is not present in canonical history",
        details: { artifact_id: request.artifactId },
      });
    }
    const pointer = located.pointer;
    if (request.expectedDigest && request.expectedDigest !== pointer.digest) {
      throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact expected digest does not match pointer", {
        artifact_id: pointer.artifactId,
        expected: request.expectedDigest,
        actual: pointer.digest,
      });
    }
    if (this.policy.requireImmutable && pointer.metadata.immutable !== true) {
      throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact pointer is not immutable", {
        artifact_id: pointer.artifactId,
      });
    }
    if (request.encoding === "utf8" && !this.policy.allowUtf8MediaPrefixes.some((prefix) => pointer.mediaType.startsWith(prefix))) {
      throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "binary artifact cannot be decoded as utf8", {
        artifact_id: pointer.artifactId,
        media_type: pointer.mediaType,
      });
    }
    const content = this.spine.artifacts.read(pointer);
    if (content.byteLength !== pointer.sizeBytes) {
      throw new PayloadBudgetError(EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT, "artifact size does not match pointer", {
        artifact_id: pointer.artifactId,
        expected: pointer.sizeBytes,
        actual: content.byteLength,
      });
    }
    const requestedLength = Math.min(
      request.length ?? this.policy.maxReadBytes,
      this.policy.maxReadBytes,
    );
    const end = Math.min(content.byteLength, request.offset + requestedLength);
    const selected = content.slice(request.offset, end);
    return {
      schema: ARTIFACT_READ_SCHEMA,
      artifact: pointer,
      verified: true,
      offset: request.offset,
      returnedBytes: selected.byteLength,
      totalBytes: content.byteLength,
      truncated: end < content.byteLength,
      encoding: request.encoding,
      data: request.encoding === "base64"
        ? Buffer.from(selected).toString("base64")
        : Buffer.from(selected).toString("utf8"),
    };
  }

  audit(artifactId: string): ArtifactAuditResult {
    const located = this.locate(artifactId);
    if (!located) {
      return {
        schema: "zyra.runtime-artifact-audit/v1",
        artifactId,
        found: false,
        exists: false,
        digestMatches: false,
        sizeMatches: false,
        readableAfterRestart: false,
        referencedBy: [],
        findings: ["artifact_reference_missing"],
      };
    }
    const pointer = located.pointer;
    const findings: string[] = [];
    const exists = this.spine.artifacts.exists(pointer);
    let digestMatches = false;
    let sizeMatches = false;
    if (!exists) {
      findings.push("artifact_content_missing");
    } else {
      try {
        const content = this.spine.artifacts.read(pointer);
        digestMatches = sha256(content) === pointer.digest;
        sizeMatches = content.byteLength === pointer.sizeBytes;
        if (!digestMatches) findings.push("artifact_digest_mismatch");
        if (!sizeMatches) findings.push("artifact_size_mismatch");
      } catch (error) {
        findings.push(`artifact_read_failed:${error instanceof Error ? error.message : String(error)}`);
      }
    }
    const projected = located.events.every((event) => {
      const projection = this.spine.projection(event.aggregateId);
      const artifacts = Array.isArray(projection.artifacts) ? projection.artifacts : [];
      return artifacts.some((item) => {
        if (typeof item !== "object" || item === null) return false;
        const artifact = (item as { artifact?: { artifactId?: string } }).artifact;
        return artifact?.artifactId === pointer.artifactId;
      });
    });
    if (this.policy.requireProjectedReference && !projected) findings.push("artifact_not_present_in_projected_read_model");
    return {
      schema: "zyra.runtime-artifact-audit/v1",
      artifactId: pointer.artifactId,
      found: true,
      exists,
      digestMatches,
      sizeMatches,
      readableAfterRestart: exists && digestMatches && sizeMatches && projected,
      referencedBy: located.events.map((event) => event.eventId),
      pointer,
      findings,
    };
  }

  list(aggregateId: string): readonly ArtifactPointer[] {
    const projection = this.spine.projection(aggregateId);
    const values = Array.isArray(projection.artifacts) ? projection.artifacts : [];
    const pointers = new Map<string, ArtifactPointer>();
    for (const value of values) {
      if (typeof value !== "object" || value === null) continue;
      const pointer = (value as { artifact?: ArtifactPointer }).artifact;
      if (pointer) pointers.set(pointerKey(pointer), pointer);
    }
    return [...pointers.values()].sort((left, right) => left.artifactId.localeCompare(right.artifactId));
  }

  contract(): Readonly<Record<string, JsonValue>> {
    return {
      schema: ARTIFACT_READ_SCHEMA,
      transport_store_only: true,
      domain_artifact_owner: "LocalArtifactStore",
      max_read_bytes: this.policy.maxReadBytes,
      require_projected_reference: this.policy.requireProjectedReference,
      require_immutable: this.policy.requireImmutable,
      path_fallback_allowed: false,
      digest_verification_required: true,
      restart_read_required: true,
    };
  }
}
