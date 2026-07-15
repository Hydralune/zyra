import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  asJsonValue,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
} from "../core/runtime-primitives.js";

export type CorrelationNodeKind =
  | "query"
  | "turn"
  | "reasoning"
  | "tool_call"
  | "tool_effect"
  | "permission"
  | "provider_request"
  | "compact"
  | "message"
  | "artifact"
  | "transition"
  | "event"
  | "recovery";

export type CorrelationEdgeKind =
  | "contains"
  | "causes"
  | "follows"
  | "observes"
  | "retries"
  | "compensates"
  | "restores";

export type CorrelationNodeStatus = "open" | "completed" | "failed" | "cancelled";

export interface CorrelationNode {
  nodeId: string;
  kind: CorrelationNodeKind;
  externalId: string;
  sessionId: string;
  runId: string;
  restartEpoch: number;
  correlationId: string;
  parentNodeId: string | null;
  status: CorrelationNodeStatus;
  openedAt: number;
  closedAt: number | null;
  sequenceStart: number | null;
  sequenceEnd: number | null;
  payloadDigest: string;
  resultDigest: string | null;
  revision: number;
  metadata: JsonRecord;
}

export interface CorrelationEdge {
  edgeId: string;
  kind: CorrelationEdgeKind;
  sourceNodeId: string;
  targetNodeId: string;
  occurredAt: number;
  sequence: number;
  metadata: JsonRecord;
}

export interface CorrelationTrace {
  correlationId: string;
  roots: CorrelationNode[];
  nodes: CorrelationNode[];
  edges: CorrelationEdge[];
  openNodeIds: string[];
  sequenceStart: number | null;
  sequenceEnd: number | null;
  digest: string;
}

export interface CorrelationIntegrityReport {
  valid: boolean;
  orphanNodeIds: string[];
  missingEdgeEndpointIds: string[];
  cyclicCausationEdgeIds: string[];
  invalidSequenceNodeIds: string[];
  duplicateExternalKeys: string[];
  openTerminalNodeIds: string[];
}

export interface CorrelationRuntimeSnapshot {
  version: "zyra.session-correlation/v1";
  restartEpoch: number;
  sequence: number;
  nodes: CorrelationNode[];
  edges: CorrelationEdge[];
  checksum: string;
}

export interface CorrelationRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  maximumNodes?: number;
  maximumEdges?: number;
}

export class SessionCorrelationRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumNodes: number;
  private readonly maximumEdges: number;
  private readonly nodes = new Map<string, CorrelationNode>();
  private readonly edges = new Map<string, CorrelationEdge>();
  private readonly externalIndex = new Map<string, string>();
  private readonly correlationIndex = new Map<string, Set<string>>();
  private readonly outgoing = new Map<string, Set<string>>();
  private readonly incoming = new Map<string, Set<string>>();
  private restartEpoch = 0;
  private sequence = 0;

  constructor(options: CorrelationRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumNodes = options.maximumNodes ?? 100_000;
    this.maximumEdges = options.maximumEdges ?? 500_000;
    assertNonNegativeInteger(this.maximumNodes, "maximumNodes");
    assertNonNegativeInteger(this.maximumEdges, "maximumEdges");
  }

  open(input: {
    nodeId?: string;
    kind: CorrelationNodeKind;
    externalId: string;
    sessionId: string;
    runId: string;
    correlationId: string;
    parentNodeId?: string | null;
    sequenceStart?: number | null;
    payload: unknown;
    metadata?: JsonRecord;
  }): CorrelationNode {
    validateOpenInput(input);
    const indexKey = externalKey(input.kind, input.externalId);
    const existingId = this.externalIndex.get(indexKey);
    if (existingId !== undefined) {
      const existing = this.requireNode(existingId);
      if (
        existing.sessionId !== input.sessionId ||
        existing.runId !== input.runId ||
        existing.correlationId !== input.correlationId ||
        existing.payloadDigest !== digestJson(input.payload)
      ) {
        throw new RuntimeInvariantError("correlation_external_id_conflict", {
          kind: input.kind,
          externalId: input.externalId,
          nodeId: existingId,
        });
      }
      return deepClone(existing);
    }
    if (this.nodes.size >= this.maximumNodes) {
      throw new RuntimeInvariantError("correlation_node_limit_exceeded", {
        maximum: this.maximumNodes,
      });
    }
    const parentNodeId = input.parentNodeId ?? null;
    if (parentNodeId !== null) {
      const parent = this.requireNode(parentNodeId);
      if (
        parent.sessionId !== input.sessionId ||
        parent.correlationId !== input.correlationId
      ) {
        throw new RuntimeInvariantError("correlation_parent_scope_mismatch", {
          parentNodeId,
          correlationId: input.correlationId,
        });
      }
    }
    const nodeId = input.nodeId ?? this.ids.next("correlation-node");
    if (this.nodes.has(nodeId)) {
      throw new RuntimeInvariantError("correlation_node_already_exists", {
        nodeId,
      });
    }
    const node: CorrelationNode = {
      nodeId,
      kind: input.kind,
      externalId: input.externalId,
      sessionId: input.sessionId,
      runId: input.runId,
      restartEpoch: this.restartEpoch,
      correlationId: input.correlationId,
      parentNodeId,
      status: "open",
      openedAt: this.clock.now(),
      closedAt: null,
      sequenceStart: input.sequenceStart ?? null,
      sequenceEnd: null,
      payloadDigest: digestJson(input.payload),
      resultDigest: null,
      revision: 1,
      metadata: deepClone(input.metadata ?? {}),
    };
    this.nodes.set(nodeId, node);
    this.externalIndex.set(indexKey, nodeId);
    const correlated = this.correlationIndex.get(node.correlationId) ?? new Set();
    correlated.add(nodeId);
    this.correlationIndex.set(node.correlationId, correlated);
    this.outgoing.set(nodeId, new Set());
    this.incoming.set(nodeId, new Set());
    if (parentNodeId !== null) {
      this.link({
        kind: "contains",
        sourceNodeId: parentNodeId,
        targetNodeId: nodeId,
        metadata: {},
      });
    }
    return deepClone(node);
  }

  close(input: {
    nodeId: string;
    status: Exclude<CorrelationNodeStatus, "open">;
    sequenceEnd?: number | null;
    result: unknown;
    metadata?: JsonRecord;
  }): CorrelationNode {
    const node = this.requireNode(input.nodeId);
    const resultDigest = digestJson(input.result);
    if (node.status !== "open") {
      if (node.status !== input.status || node.resultDigest !== resultDigest) {
        throw new RuntimeInvariantError("correlation_close_conflict", {
          nodeId: input.nodeId,
          existingStatus: node.status,
          requestedStatus: input.status,
        });
      }
      return deepClone(node);
    }
    const sequenceEnd = input.sequenceEnd ?? null;
    if (
      sequenceEnd !== null &&
      node.sequenceStart !== null &&
      sequenceEnd < node.sequenceStart
    ) {
      throw new RuntimeInvariantError("correlation_sequence_regression", {
        nodeId: node.nodeId,
        sequenceStart: node.sequenceStart,
        sequenceEnd,
      });
    }
    const next: CorrelationNode = {
      ...node,
      status: input.status,
      closedAt: this.clock.now(),
      sequenceEnd,
      resultDigest,
      revision: node.revision + 1,
      metadata: { ...node.metadata, ...deepClone(input.metadata ?? {}) },
    };
    this.nodes.set(node.nodeId, next);
    return deepClone(next);
  }

  annotate(
    nodeId: string,
    expectedRevision: number,
    metadata: JsonRecord,
  ): CorrelationNode {
    const node = this.requireNode(nodeId);
    if (node.revision !== expectedRevision) {
      throw new RuntimeInvariantError("correlation_revision_conflict", {
        nodeId,
        expectedRevision,
        actualRevision: node.revision,
      });
    }
    const next = {
      ...node,
      metadata: { ...node.metadata, ...deepClone(metadata) },
      revision: node.revision + 1,
    };
    this.nodes.set(nodeId, next);
    return deepClone(next);
  }

  link(input: {
    edgeId?: string;
    kind: CorrelationEdgeKind;
    sourceNodeId: string;
    targetNodeId: string;
    metadata?: JsonRecord;
  }): CorrelationEdge {
    const source = this.requireNode(input.sourceNodeId);
    const target = this.requireNode(input.targetNodeId);
    if (source.nodeId === target.nodeId) {
      throw new RuntimeInvariantError("correlation_self_edge", {
        nodeId: source.nodeId,
        kind: input.kind,
      });
    }
    if (source.sessionId !== target.sessionId) {
      throw new RuntimeInvariantError("correlation_cross_session_edge", {
        sourceSessionId: source.sessionId,
        targetSessionId: target.sessionId,
      });
    }
    const duplicate = this.findEdge(
      input.kind,
      input.sourceNodeId,
      input.targetNodeId,
    );
    if (duplicate !== null) {
      return duplicate;
    }
    if (this.edges.size >= this.maximumEdges) {
      throw new RuntimeInvariantError("correlation_edge_limit_exceeded", {
        maximum: this.maximumEdges,
      });
    }
    if (
      isCausalEdge(input.kind) &&
      this.isReachable(input.targetNodeId, input.sourceNodeId, true)
    ) {
      throw new RuntimeInvariantError("correlation_causation_cycle", {
        sourceNodeId: input.sourceNodeId,
        targetNodeId: input.targetNodeId,
        kind: input.kind,
      });
    }
    this.sequence += 1;
    const edge: CorrelationEdge = {
      edgeId: input.edgeId ?? this.ids.next("correlation-edge"),
      kind: input.kind,
      sourceNodeId: input.sourceNodeId,
      targetNodeId: input.targetNodeId,
      occurredAt: this.clock.now(),
      sequence: this.sequence,
      metadata: deepClone(input.metadata ?? {}),
    };
    if (this.edges.has(edge.edgeId)) {
      throw new RuntimeInvariantError("correlation_edge_already_exists", {
        edgeId: edge.edgeId,
      });
    }
    this.edges.set(edge.edgeId, edge);
    this.outgoing.get(source.nodeId)?.add(edge.edgeId);
    this.incoming.get(target.nodeId)?.add(edge.edgeId);
    return deepClone(edge);
  }

  getNode(nodeId: string): CorrelationNode {
    return deepClone(this.requireNode(nodeId));
  }

  findByExternalId(
    kind: CorrelationNodeKind,
    externalId: string,
  ): CorrelationNode | null {
    const nodeId = this.externalIndex.get(externalKey(kind, externalId));
    return nodeId === undefined ? null : this.getNode(nodeId);
  }

  ancestors(nodeId: string, maximumDepth = 100): CorrelationNode[] {
    return this.walk(nodeId, "incoming", maximumDepth);
  }

  descendants(nodeId: string, maximumDepth = 100): CorrelationNode[] {
    return this.walk(nodeId, "outgoing", maximumDepth);
  }

  shortestPath(sourceNodeId: string, targetNodeId: string): CorrelationNode[] {
    this.requireNode(sourceNodeId);
    this.requireNode(targetNodeId);
    const queue: string[] = [sourceNodeId];
    const previous = new Map<string, string | null>([[sourceNodeId, null]]);
    while (queue.length > 0) {
      const current = queue.shift();
      if (current === undefined || current === targetNodeId) {
        break;
      }
      for (const edgeId of this.outgoing.get(current) ?? []) {
        const edge = this.edges.get(edgeId);
        if (edge !== undefined && !previous.has(edge.targetNodeId)) {
          previous.set(edge.targetNodeId, current);
          queue.push(edge.targetNodeId);
        }
      }
    }
    if (!previous.has(targetNodeId)) {
      return [];
    }
    const path: string[] = [];
    let current: string | null = targetNodeId;
    while (current !== null) {
      path.push(current);
      current = previous.get(current) ?? null;
    }
    return path.reverse().map((nodeId) => this.getNode(nodeId));
  }

  trace(correlationId: string): CorrelationTrace {
    const nodeIds = this.correlationIndex.get(correlationId) ?? new Set();
    const nodes = [...nodeIds]
      .map((nodeId) => this.requireNode(nodeId))
      .sort(nodeComparator)
      .map((node) => deepClone(node));
    const included = new Set(nodes.map((node) => node.nodeId));
    const edges = [...this.edges.values()]
      .filter(
        (edge) =>
          included.has(edge.sourceNodeId) && included.has(edge.targetNodeId),
      )
      .sort(edgeComparator)
      .map((edge) => deepClone(edge));
    const roots = nodes.filter(
      (node) => node.parentNodeId === null || !included.has(node.parentNodeId),
    );
    const sequenceStarts = nodes
      .map((node) => node.sequenceStart)
      .filter((value): value is number => value !== null);
    const sequenceEnds = nodes
      .map((node) => node.sequenceEnd)
      .filter((value): value is number => value !== null);
    const body = {
      correlationId,
      roots,
      nodes,
      edges,
      openNodeIds: nodes
        .filter((node) => node.status === "open")
        .map((node) => node.nodeId),
      sequenceStart:
        sequenceStarts.length === 0 ? null : Math.min(...sequenceStarts),
      sequenceEnd: sequenceEnds.length === 0 ? null : Math.max(...sequenceEnds),
    };
    return { ...body, digest: digestJson(body) };
  }

  audit(): CorrelationIntegrityReport {
    const orphanNodeIds: string[] = [];
    const missingEdgeEndpointIds: string[] = [];
    const cyclicCausationEdgeIds: string[] = [];
    const invalidSequenceNodeIds: string[] = [];
    const duplicateExternalKeys: string[] = [];
    const openTerminalNodeIds: string[] = [];
    const seenExternal = new Set<string>();
    for (const node of this.nodes.values()) {
      if (node.parentNodeId !== null && !this.nodes.has(node.parentNodeId)) {
        orphanNodeIds.push(node.nodeId);
      }
      if (
        node.sequenceStart !== null &&
        node.sequenceEnd !== null &&
        node.sequenceEnd < node.sequenceStart
      ) {
        invalidSequenceNodeIds.push(node.nodeId);
      }
      const key = externalKey(node.kind, node.externalId);
      if (seenExternal.has(key)) {
        duplicateExternalKeys.push(key);
      }
      seenExternal.add(key);
      if (node.status === "open" && node.closedAt !== null) {
        openTerminalNodeIds.push(node.nodeId);
      }
    }
    for (const edge of this.edges.values()) {
      if (
        !this.nodes.has(edge.sourceNodeId) ||
        !this.nodes.has(edge.targetNodeId)
      ) {
        missingEdgeEndpointIds.push(edge.edgeId);
      } else if (
        isCausalEdge(edge.kind) &&
        this.hasAlternativeCausalPath(edge)
      ) {
        cyclicCausationEdgeIds.push(edge.edgeId);
      }
    }
    const report = {
      orphanNodeIds: orphanNodeIds.sort(compareStrings),
      missingEdgeEndpointIds: missingEdgeEndpointIds.sort(compareStrings),
      cyclicCausationEdgeIds: cyclicCausationEdgeIds.sort(compareStrings),
      invalidSequenceNodeIds: invalidSequenceNodeIds.sort(compareStrings),
      duplicateExternalKeys: duplicateExternalKeys.sort(compareStrings),
      openTerminalNodeIds: openTerminalNodeIds.sort(compareStrings),
    };
    return {
      valid: Object.values(report).every((values) => values.length === 0),
      ...report,
    };
  }

  snapshot(): CorrelationRuntimeSnapshot {
    const body = {
      version: "zyra.session-correlation/v1" as const,
      restartEpoch: this.restartEpoch,
      sequence: this.sequence,
      nodes: [...this.nodes.values()]
        .sort(nodeComparator)
        .map((node) => deepClone(node)),
      edges: [...this.edges.values()]
        .sort(edgeComparator)
        .map((edge) => deepClone(edge)),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: CorrelationRuntimeSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.session-correlation/v1") {
      throw new RuntimeInvariantError("unsupported_correlation_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("correlation_snapshot_checksum_mismatch");
    }
    this.clear();
    for (const node of snapshot.nodes) {
      if (this.nodes.has(node.nodeId)) {
        throw new RuntimeInvariantError("duplicate_correlation_node_snapshot", {
          nodeId: node.nodeId,
        });
      }
      this.nodes.set(node.nodeId, deepClone(node));
      this.externalIndex.set(externalKey(node.kind, node.externalId), node.nodeId);
      const correlated =
        this.correlationIndex.get(node.correlationId) ?? new Set<string>();
      correlated.add(node.nodeId);
      this.correlationIndex.set(node.correlationId, correlated);
      this.outgoing.set(node.nodeId, new Set());
      this.incoming.set(node.nodeId, new Set());
    }
    for (const edge of snapshot.edges) {
      if (
        !this.nodes.has(edge.sourceNodeId) ||
        !this.nodes.has(edge.targetNodeId)
      ) {
        throw new RuntimeInvariantError("correlation_snapshot_edge_orphan", {
          edgeId: edge.edgeId,
        });
      }
      this.edges.set(edge.edgeId, deepClone(edge));
      this.outgoing.get(edge.sourceNodeId)?.add(edge.edgeId);
      this.incoming.get(edge.targetNodeId)?.add(edge.edgeId);
    }
    this.sequence = snapshot.sequence;
    this.restartEpoch = snapshot.restartEpoch + 1;
    const report = this.audit();
    if (!report.valid) {
      throw new RuntimeInvariantError("correlation_snapshot_integrity_failed", {
        report: asJsonValue(report),
      });
    }
  }

  private walk(
    originNodeId: string,
    direction: "incoming" | "outgoing",
    maximumDepth: number,
  ): CorrelationNode[] {
    this.requireNode(originNodeId);
    assertNonNegativeInteger(maximumDepth, "maximumDepth");
    const visited = new Set<string>([originNodeId]);
    let frontier = [originNodeId];
    const result: CorrelationNode[] = [];
    for (let depth = 0; depth < maximumDepth && frontier.length > 0; depth += 1) {
      const next: string[] = [];
      for (const nodeId of frontier) {
        const edgeIds =
          direction === "outgoing"
            ? this.outgoing.get(nodeId)
            : this.incoming.get(nodeId);
        for (const edgeId of edgeIds ?? []) {
          const edge = this.edges.get(edgeId);
          if (edge === undefined) {
            continue;
          }
          const adjacent =
            direction === "outgoing" ? edge.targetNodeId : edge.sourceNodeId;
          if (!visited.has(adjacent)) {
            visited.add(adjacent);
            next.push(adjacent);
            result.push(this.getNode(adjacent));
          }
        }
      }
      frontier = next;
    }
    return result.sort(nodeComparator);
  }

  private isReachable(
    sourceNodeId: string,
    targetNodeId: string,
    causalOnly: boolean,
  ): boolean {
    const visited = new Set<string>([sourceNodeId]);
    const queue = [sourceNodeId];
    while (queue.length > 0) {
      const current = queue.shift();
      if (current === undefined) {
        break;
      }
      for (const edgeId of this.outgoing.get(current) ?? []) {
        const edge = this.edges.get(edgeId);
        if (edge === undefined || (causalOnly && !isCausalEdge(edge.kind))) {
          continue;
        }
        if (edge.targetNodeId === targetNodeId) {
          return true;
        }
        if (!visited.has(edge.targetNodeId)) {
          visited.add(edge.targetNodeId);
          queue.push(edge.targetNodeId);
        }
      }
    }
    return false;
  }

  private hasAlternativeCausalPath(edge: CorrelationEdge): boolean {
    const outgoing = this.outgoing.get(edge.targetNodeId) ?? new Set<string>();
    const visited = new Set<string>([edge.targetNodeId]);
    const queue = [...outgoing]
      .map((edgeId) => this.edges.get(edgeId))
      .filter((candidate): candidate is CorrelationEdge => candidate !== undefined)
      .filter((candidate) => isCausalEdge(candidate.kind))
      .map((candidate) => candidate.targetNodeId);
    while (queue.length > 0) {
      const current = queue.shift();
      if (current === undefined) {
        break;
      }
      if (current === edge.sourceNodeId) {
        return true;
      }
      if (visited.has(current)) {
        continue;
      }
      visited.add(current);
      for (const edgeId of this.outgoing.get(current) ?? []) {
        const candidate = this.edges.get(edgeId);
        if (candidate !== undefined && isCausalEdge(candidate.kind)) {
          queue.push(candidate.targetNodeId);
        }
      }
    }
    return false;
  }

  private findEdge(
    kind: CorrelationEdgeKind,
    sourceNodeId: string,
    targetNodeId: string,
  ): CorrelationEdge | null {
    for (const edgeId of this.outgoing.get(sourceNodeId) ?? []) {
      const edge = this.edges.get(edgeId);
      if (edge?.kind === kind && edge.targetNodeId === targetNodeId) {
        return deepClone(edge);
      }
    }
    return null;
  }

  private requireNode(nodeId: string): CorrelationNode {
    const node = this.nodes.get(nodeId);
    if (node === undefined) {
      throw new RuntimeInvariantError("unknown_correlation_node", { nodeId });
    }
    return node;
  }

  private clear(): void {
    this.nodes.clear();
    this.edges.clear();
    this.externalIndex.clear();
    this.correlationIndex.clear();
    this.outgoing.clear();
    this.incoming.clear();
  }
}

function validateOpenInput(input: {
  kind: CorrelationNodeKind;
  externalId: string;
  sessionId: string;
  runId: string;
  correlationId: string;
  sequenceStart?: number | null;
}): void {
  assertNonEmpty(input.externalId, "externalId");
  assertNonEmpty(input.sessionId, "sessionId");
  assertNonEmpty(input.runId, "runId");
  assertNonEmpty(input.correlationId, "correlationId");
  if (input.sequenceStart !== undefined && input.sequenceStart !== null) {
    assertNonNegativeInteger(input.sequenceStart, "sequenceStart");
  }
}

function externalKey(kind: CorrelationNodeKind, externalId: string): string {
  return `${kind}:${externalId}`;
}

function isCausalEdge(kind: CorrelationEdgeKind): boolean {
  return kind === "causes" || kind === "retries" || kind === "restores";
}

function nodeComparator(left: CorrelationNode, right: CorrelationNode): number {
  return (
    compareNumbers(left.openedAt, right.openedAt) ||
    compareStrings(left.nodeId, right.nodeId)
  );
}

function edgeComparator(left: CorrelationEdge, right: CorrelationEdge): number {
  return (
    compareNumbers(left.sequence, right.sequence) ||
    compareStrings(left.edgeId, right.edgeId)
  );
}
