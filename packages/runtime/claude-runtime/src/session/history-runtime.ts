import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const SESSION_HISTORY_SNAPSHOT_VERSION = "zyra.session-history/v1";

export type HistoryRole = "system" | "user" | "assistant" | "tool";
export type HistoryNodeState = "visible" | "superseded" | "compacted" | "deleted";

export interface HistoryNode {
  nodeId: string;
  branchId: string;
  parentNodeId: string | null;
  supersedesNodeId: string | null;
  role: HistoryRole;
  content: JsonValue;
  contentDigest: string;
  turnId: string | null;
  toolCallId: string | null;
  state: HistoryNodeState;
  sequence: number;
  metadata: JsonObject;
  createdAt: string;
  revision: number;
}

export interface HistoryBranch {
  branchId: string;
  name: string;
  parentBranchId: string | null;
  forkNodeId: string | null;
  headNodeId: string | null;
  createdAt: string;
  revision: number;
}

export interface HistoryBoundary {
  boundaryId: string;
  branchId: string;
  summaryNodeId: string;
  compactedNodeIds: string[];
  previousBoundaryId: string | null;
  sourceDigest: string;
  createdAt: string;
}

export interface SessionHistorySnapshot {
  version: typeof SESSION_HISTORY_SNAPSHOT_VERSION;
  revision: number;
  sequence: number;
  activeBranchId: string;
  nodes: HistoryNode[];
  branches: HistoryBranch[];
  boundaries: HistoryBoundary[];
  checksum: string;
}

export class SessionHistoryRuntime {
  private readonly nodes = new Map<string, HistoryNode>();
  private readonly branches = new Map<string, HistoryBranch>();
  private readonly boundaries = new Map<string, HistoryBoundary>();
  private activeBranchId = "main";
  private revision = 0;
  private sequence = 0;

  constructor() {
    this.branches.set("main", {
      branchId: "main",
      name: "main",
      parentBranchId: null,
      forkNodeId: null,
      headNodeId: null,
      createdAt: new Date().toISOString(),
      revision: 1,
    });
  }

  history_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "append") return nodeToJson(this.append({
      role: historyRole(asString(value.role, "user")),
      content: value.content ?? null,
      turnId: asString(value.turn_id) || null,
      toolCallId: asString(value.tool_call_id) || null,
      metadata: asObject(value.metadata),
    }));
    if (action === "edit") return nodeToJson(this.edit(asString(value.node_id), value.content ?? null, asObject(value.metadata)));
    if (action === "branch") return branchToJson(this.fork(asString(value.name, "branch"), asString(value.node_id) || null));
    if (action === "switch") return branchToJson(this.switchBranch(asString(value.branch_id)));
    if (action === "compact") return boundaryToJson(this.compact(Array.isArray(value.node_ids) ? value.node_ids.map(String) : [], asString(value.summary)));
    if (action === "search") return { nodes: this.search(asString(value.query), positive(value.maximum, 50)).map(nodeToJson) };
    return this.project();
  }

  append(input: {
    role: HistoryRole;
    content: JsonValue;
    turnId: string | null;
    toolCallId: string | null;
    metadata?: JsonObject;
    createdAt?: string;
  }): HistoryNode {
    const branch = this.requireBranch(this.activeBranchId);
    this.sequence += 1;
    const node: HistoryNode = {
      nodeId: randomUUID(),
      branchId: branch.branchId,
      parentNodeId: branch.headNodeId,
      supersedesNodeId: null,
      role: input.role,
      content: structuredClone(input.content),
      contentDigest: digest(input.content),
      turnId: input.turnId,
      toolCallId: input.toolCallId,
      state: "visible",
      sequence: this.sequence,
      metadata: sanitizeMetadata(input.metadata ?? {}),
      createdAt: normalizeTimestamp(input.createdAt),
      revision: 1,
    };
    this.nodes.set(node.nodeId, node);
    branch.headNodeId = node.nodeId;
    branch.revision += 1;
    this.revision += 1;
    return structuredClone(node);
  }

  edit(nodeId: string, content: JsonValue, metadata: JsonObject = {}): HistoryNode {
    const original = this.requireNode(nodeId);
    if (original.branchId !== this.activeBranchId) throw new Error("history edit requires active branch ownership");
    if (original.state !== "visible") throw new Error(`history node cannot be edited from ${original.state}`);
    this.sequence += 1;
    const replacement: HistoryNode = {
      ...structuredClone(original),
      nodeId: randomUUID(),
      parentNodeId: original.parentNodeId,
      supersedesNodeId: original.nodeId,
      content: structuredClone(content),
      contentDigest: digest(content),
      sequence: this.sequence,
      metadata: { ...original.metadata, ...sanitizeMetadata(metadata), edited: true },
      createdAt: new Date().toISOString(),
      revision: 1,
    };
    original.state = "superseded";
    original.revision += 1;
    for (const child of this.nodes.values()) if (child.parentNodeId === original.nodeId && child.branchId === original.branchId) {
      child.parentNodeId = replacement.nodeId;
      child.revision += 1;
    }
    this.nodes.set(replacement.nodeId, replacement);
    const branch = this.requireBranch(original.branchId);
    if (branch.headNodeId === original.nodeId) branch.headNodeId = replacement.nodeId;
    branch.revision += 1;
    this.revision += 1;
    return structuredClone(replacement);
  }

  fork(name: string, atNodeId: string | null = null): HistoryBranch {
    const parent = this.requireBranch(this.activeBranchId);
    const forkNodeId = atNodeId ?? parent.headNodeId;
    if (forkNodeId) {
      const node = this.requireNode(forkNodeId);
      if (!this.branchLineage(parent.branchId).includes(node.branchId)) throw new Error("fork node is outside active branch lineage");
    }
    const branch: HistoryBranch = {
      branchId: randomUUID(),
      name: name.trim().slice(0, 256) || "branch",
      parentBranchId: parent.branchId,
      forkNodeId,
      headNodeId: forkNodeId,
      createdAt: new Date().toISOString(),
      revision: 1,
    };
    this.branches.set(branch.branchId, branch);
    this.activeBranchId = branch.branchId;
    this.revision += 1;
    return structuredClone(branch);
  }

  switchBranch(branchId: string): HistoryBranch {
    const branch = this.requireBranch(branchId);
    this.activeBranchId = branch.branchId;
    this.revision += 1;
    return structuredClone(branch);
  }

  transcript(branchId = this.activeBranchId): HistoryNode[] {
    const branch = this.requireBranch(branchId);
    const result: HistoryNode[] = [];
    const seen = new Set<string>();
    let nodeId = branch.headNodeId;
    while (nodeId) {
      if (seen.has(nodeId)) throw new Error("history node cycle detected");
      seen.add(nodeId);
      const node = this.requireNode(nodeId);
      if (node.state === "visible") result.unshift(structuredClone(node));
      nodeId = node.parentNodeId;
    }
    return result;
  }

  compact(nodeIds: readonly string[], summary: string): HistoryBoundary {
    const selected = [...new Set(nodeIds)].map((id) => this.requireNode(id));
    if (selected.length === 0) throw new Error("history compaction selection is empty");
    if (selected.some((node) => node.branchId !== this.activeBranchId)) throw new Error("history compaction cannot span branches");
    const transcript = this.transcript();
    const activeBranch = this.requireBranch(this.activeBranchId);
    const previousHeadNodeId = activeBranch.headNodeId;
    const positions = selected.map((node) => transcript.findIndex((item) => item.nodeId === node.nodeId)).sort((left, right) => left - right);
    if (positions.some((position) => position < 0)) throw new Error("history compaction selection is not visible");
    for (let index = 1; index < positions.length; index += 1) if (positions[index] !== positions[index - 1] + 1) throw new Error("history compaction selection must be contiguous");
    const first = selected.sort((left, right) => left.sequence - right.sequence)[0];
    const summaryNode = this.append({
      role: "system",
      content: { type: "compact_summary", summary: required(summary, "history compact summary") },
      turnId: null,
      toolCallId: null,
      metadata: { compacted_node_ids: selected.map((node) => node.nodeId) },
    });
    const mutableSummary = this.requireNode(summaryNode.nodeId);
    mutableSummary.parentNodeId = first.parentNodeId;
    mutableSummary.revision += 1;
    for (const node of selected) {
      node.state = "compacted";
      node.revision += 1;
    }
    const after = transcript.find((node) => node.parentNodeId === selected.at(-1)!.nodeId);
    if (after) {
      const mutableAfter = this.requireNode(after.nodeId);
      mutableAfter.parentNodeId = summaryNode.nodeId;
      mutableAfter.revision += 1;
    }
    activeBranch.headNodeId = previousHeadNodeId === selected.at(-1)!.nodeId
      ? summaryNode.nodeId
      : previousHeadNodeId;
    activeBranch.revision += 1;
    const previousBoundary = [...this.boundaries.values()].filter((item) => item.branchId === this.activeBranchId).at(-1);
    const boundary: HistoryBoundary = {
      boundaryId: randomUUID(),
      branchId: this.activeBranchId,
      summaryNodeId: summaryNode.nodeId,
      compactedNodeIds: selected.map((node) => node.nodeId),
      previousBoundaryId: previousBoundary?.boundaryId ?? null,
      sourceDigest: digest(selected.map((node) => node.contentDigest)),
      createdAt: new Date().toISOString(),
    };
    this.boundaries.set(boundary.boundaryId, boundary);
    this.revision += 1;
    return structuredClone(boundary);
  }

  search(query: string, maximum = 50): HistoryNode[] {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    return this.transcript()
      .map((node) => ({ node, score: searchScore(node, terms) }))
      .filter((item) => item.score > 0)
      .sort((left, right) => right.score - left.score || right.node.sequence - left.node.sequence)
      .slice(0, Math.max(0, Math.floor(maximum)))
      .map((item) => structuredClone(item.node));
  }

  project(): JsonObject {
    const transcript = this.transcript();
    return {
      active_branch_id: this.activeBranchId,
      branch_count: this.branches.size,
      node_count: this.nodes.size,
      visible_node_count: transcript.length,
      boundary_count: this.boundaries.size,
      head_node_id: this.requireBranch(this.activeBranchId).headNodeId,
      transcript_digest: digest(transcript.map((node) => node.contentDigest)),
      revision: this.revision,
      sequence: this.sequence,
    };
  }

  snapshot(): SessionHistorySnapshot {
    const unsigned: Omit<SessionHistorySnapshot, "checksum"> = {
      version: SESSION_HISTORY_SNAPSHOT_VERSION,
      revision: this.revision,
      sequence: this.sequence,
      activeBranchId: this.activeBranchId,
      nodes: structuredClone([...this.nodes.values()]),
      branches: structuredClone([...this.branches.values()]),
      boundaries: structuredClone([...this.boundaries.values()]),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: SessionHistorySnapshot): void {
    if (snapshot.version !== SESSION_HISTORY_SNAPSHOT_VERSION) throw new Error("unsupported session history snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("session history snapshot checksum mismatch");
    validateSnapshot(snapshot);
    this.nodes.clear();
    for (const node of snapshot.nodes) this.nodes.set(node.nodeId, structuredClone(node));
    this.branches.clear();
    for (const branch of snapshot.branches) this.branches.set(branch.branchId, structuredClone(branch));
    this.boundaries.clear();
    for (const boundary of snapshot.boundaries) this.boundaries.set(boundary.boundaryId, structuredClone(boundary));
    this.activeBranchId = snapshot.activeBranchId;
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
  }

  private branchLineage(branchId: string): string[] {
    const result: string[] = [];
    let current: string | null = branchId;
    while (current) {
      if (result.includes(current)) throw new Error("history branch cycle detected");
      result.push(current);
      current = this.requireBranch(current).parentBranchId;
    }
    return result;
  }

  private requireNode(nodeId: string): HistoryNode {
    const node = this.nodes.get(nodeId);
    if (!node) throw new Error(`history node not found: ${nodeId}`);
    return node;
  }

  private requireBranch(branchId: string): HistoryBranch {
    const branch = this.branches.get(branchId);
    if (!branch) throw new Error(`history branch not found: ${branchId}`);
    return branch;
  }
}

function validateSnapshot(snapshot: SessionHistorySnapshot): void {
  const nodes = new Map(snapshot.nodes.map((node) => [node.nodeId, node]));
  const branches = new Map(snapshot.branches.map((branch) => [branch.branchId, branch]));
  if (!branches.has(snapshot.activeBranchId)) throw new Error("history snapshot active branch is missing");
  for (const node of snapshot.nodes) {
    if (digest(node.content) !== node.contentDigest) throw new Error(`history node digest mismatch: ${node.nodeId}`);
    if (!branches.has(node.branchId)) throw new Error(`history node branch is missing: ${node.branchId}`);
    if (node.parentNodeId && !nodes.has(node.parentNodeId)) throw new Error(`history node parent is missing: ${node.parentNodeId}`);
    if (node.supersedesNodeId && !nodes.has(node.supersedesNodeId)) throw new Error(`history superseded node is missing: ${node.supersedesNodeId}`);
  }
  for (const branch of snapshot.branches) {
    if (branch.parentBranchId && !branches.has(branch.parentBranchId)) throw new Error(`history parent branch is missing: ${branch.parentBranchId}`);
    if (branch.headNodeId && !nodes.has(branch.headNodeId)) throw new Error(`history branch head is missing: ${branch.headNodeId}`);
  }
}

function searchScore(node: HistoryNode, terms: readonly string[]): number {
  if (terms.length === 0) return 0;
  const text = `${node.role} ${canonicalJson(node.content)} ${canonicalJson(node.metadata)}`.toLowerCase();
  return terms.reduce((sum, term) => sum + (text.includes(term) ? 10 : 0) + countOccurrences(text, term), 0);
}

function countOccurrences(value: string, term: string): number {
  let count = 0;
  let index = 0;
  while ((index = value.indexOf(term, index)) >= 0) {
    count += 1;
    index += Math.max(1, term.length);
  }
  return count;
}

function sanitizeMetadata(value: JsonObject): JsonObject {
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) result[key] = /secret|password|token|api.?key|authorization/i.test(key) ? "[redacted]" : item;
  return result;
}

function nodeToJson(value: HistoryNode): JsonObject {
  return { node_id: value.nodeId, branch_id: value.branchId, parent_node_id: value.parentNodeId, supersedes_node_id: value.supersedesNodeId, role: value.role, content: value.content, content_digest: value.contentDigest, turn_id: value.turnId, tool_call_id: value.toolCallId, state: value.state, sequence: value.sequence, metadata: value.metadata, created_at: value.createdAt, revision: value.revision };
}

function branchToJson(value: HistoryBranch): JsonObject {
  return { branch_id: value.branchId, name: value.name, parent_branch_id: value.parentBranchId, fork_node_id: value.forkNodeId, head_node_id: value.headNodeId, created_at: value.createdAt, revision: value.revision };
}

function boundaryToJson(value: HistoryBoundary): JsonObject {
  return { boundary_id: value.boundaryId, branch_id: value.branchId, summary_node_id: value.summaryNodeId, compacted_node_ids: value.compactedNodeIds, previous_boundary_id: value.previousBoundaryId, source_digest: value.sourceDigest, created_at: value.createdAt };
}

function historyRole(value: string): HistoryRole {
  if (value === "system" || value === "assistant" || value === "tool") return value;
  return "user";
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function normalizeTimestamp(value?: string): string {
  if (!value) return new Date().toISOString();
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid history timestamp: ${value}`);
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
