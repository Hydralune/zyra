import type { JsonObject, JsonValue, RestoreAttachmentReference } from "./contracts.ts";

export interface ContextAssemblySectionInput {
  sectionId?: string;
  kind: "system" | "instruction" | "message" | "tool_use" | "tool_result" | "memory" | "attachment" | "notice";
  title: string;
  content: JsonValue;
  text: string;
  priority: number;
  pinned: boolean;
  required: boolean;
  disclosure: "public" | "model_only" | "worker_only" | "secret";
  toolPairId: string | null;
  turnIndex: number | null;
  provenance: {
    source: "system" | "user" | "assistant" | "tool" | "memory" | "skill" | "workspace" | "artifact" | "hook" | "control" | "restore";
    sourceId: string;
    sourceDigest: string;
    parentSectionId: string | null;
    trust: "untrusted" | "external" | "internal" | "verified" | "system";
    createdAt: string;
    expiresAt: string | null;
  };
  metadata: JsonObject;
}

export interface ContextAssemblySectionResult {
  sectionId: string;
  contentDigest: string;
  tokenEstimate: number;
  state: string;
}

export interface ContextAssemblyResult {
  assemblyId: string;
  revision: number;
  systemPrompt: string;
  messages: JsonObject[];
  contextDigest: string;
  selectedTokens: number;
  pairInvariantOk: boolean;
}

export interface ContextAssemblyPort {
  add(input: ContextAssemblySectionInput): ContextAssemblySectionResult;
  assemble(maximumTokens?: number): ContextAssemblyResult;
  snapshot(): unknown;
}

export interface CompactRestoreCandidateInput {
  candidateId?: string;
  kind: "file" | "skill" | "memory" | "agent" | "plan";
  name: string;
  path?: string | null;
  sourceId: string;
  priority?: number;
  required?: boolean;
  lastAccessedAt?: string | null;
  content?: string | null;
  metadata?: JsonObject;
}

export interface CompactRestoreCandidateResult {
  candidateId: string;
  state: string;
  sourceDigest: string | null;
  content: string | null;
  estimatedTokens: number;
  required: boolean;
  metadata: JsonObject;
}

export interface CompactRestorePort {
  discover(input: CompactRestoreCandidateInput): CompactRestoreCandidateResult;
  provideContent(candidateId: string, content: string): CompactRestoreCandidateResult;
  select(): Array<{
    type: "attachment";
    attachmentKind: "file" | "skill" | "memory" | "agent" | "plan";
    path: string | null;
    name: string;
    content: string;
    sourceDigest: string;
    truncated: boolean;
  }>;
  snapshot(): unknown;
}

export interface MemorySignalTransport {
  publish(signal: JsonObject): Promise<void> | void;
}

export interface ProcedureProjectionPort {
  list(input: {
    taskId: string;
    sessionId: string;
    consumers: Array<"routing" | "recovery" | "context" | "audit">;
    validatedOnly: boolean;
    limit: number;
  }): Promise<JsonObject[]> | JsonObject[];
}
