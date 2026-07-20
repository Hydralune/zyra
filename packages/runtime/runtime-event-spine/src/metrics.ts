import type { DatabaseSync } from "node:sqlite";
import {
  type BaselineComparison,
  type LowEntropyReport,
  type RecipientRef,
  type RouteDecision,
  type RuntimeEventEnvelope,
  type SpineMetrics,
} from "./contracts.ts";
import { LowEntropyBaselineHarness, type BaselineFact } from "./baseline-harness.ts";
import { INLINE_PAYLOAD_LIMIT_BYTES, ENVELOPE_LIMIT_BYTES } from "./payload-policy.ts";
import { RuntimeEventSqliteStore } from "./sqlite-store.ts";
import { digestJson } from "./canonical.ts";

function sum(values: readonly number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

function average(values: readonly number[]): number {
  return values.length > 0 ? sum(values) / values.length : 0;
}

function ratio(numerator: number, denominator: number): number {
  return denominator > 0 ? numerator / denominator : 0;
}

function quantile(values: readonly number[], percentile: number): number {
  if (values.length === 0) return 0;
  const ordered = [...values].sort((left, right) => left - right);
  const index = Math.min(ordered.length - 1, Math.max(0, Math.ceil(percentile * ordered.length) - 1));
  return ordered[index]!;
}

function round(value: number, digits = 6): number {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

function estimateTokens(bytes: number): number {
  return Math.max(1, Math.ceil(bytes / 4));
}

interface MetricRow {
  event_id: string | null;
  metric_kind: string;
  value: number;
  dimensions_json: string;
}

interface EventMetricRow {
  event_id: string;
  envelope_bytes: number;
  inline_bytes: number;
  source_bytes: number;
  artifact_ref_count: number;
  effect: string;
}

interface DeliveryMetricRow {
  event_id: string;
  state: string;
  attempt: number;
  redelivered: number;
  route_policy_id: string;
}

interface RouteMetricRow {
  event_id: string;
  route_json: string;
}

export class LowEntropyMetrics {
  readonly store: RuntimeEventSqliteStore;

  constructor(store: RuntimeEventSqliteStore) {
    this.store = store;
  }

  collect(): SpineMetrics {
    const events = this.store.db.prepare(`
      SELECT event_id, envelope_bytes, inline_bytes, source_bytes, artifact_ref_count, effect
      FROM runtime_events ORDER BY committed_at, event_id
    `).all() as unknown as EventMetricRow[];
    const deliveries = this.store.db.prepare(`
      SELECT event_id, state, attempt, redelivered, route_policy_id
      FROM runtime_event_deliveries ORDER BY created_at, delivery_id
    `).all() as unknown as DeliveryMetricRow[];
    const metrics = this.store.db.prepare(`
      SELECT event_id, metric_kind, value, dimensions_json
      FROM runtime_event_metrics ORDER BY metric_id
    `).all() as unknown as MetricRow[];
    const routeRecipients = metrics.filter((row) => row.metric_kind === "route.recipient_count").map((row) => Number(row.value));
    const availableRecipients = metrics.filter((row) => row.metric_kind === "route.available_recipient_count").map((row) => Number(row.value));
    const broadcastCount = sum(metrics.filter((row) => row.metric_kind === "route.broadcast").map((row) => Number(row.value)));
    const duplicateEventCount = sum(metrics.filter((row) => row.metric_kind === "event.duplicate").map((row) => Number(row.value)));
    const artifactSpillCount = sum(metrics.filter((row) => row.metric_kind === "event.artifact_spills").map((row) => Number(row.value)));
    const offloadedBytesTotal = sum(metrics.filter((row) => row.metric_kind === "event.offloaded_bytes").map((row) => Number(row.value)));
    return {
      eventCount: events.length,
      effectiveEventCount: events.filter((event) => event.effect === "effective").length,
      nonEffectiveEventCount: events.filter((event) => event.effect === "non_effective").length,
      duplicateEventCount,
      deliveryCount: deliveries.length,
      acknowledgedDeliveryCount: deliveries.filter((delivery) => delivery.state === "acknowledged").length,
      redeliveryCount: deliveries.filter((delivery) => Boolean(delivery.redelivered) || delivery.attempt > 1).length,
      deadLetterCount: deliveries.filter((delivery) => delivery.state === "dead_letter").length,
      artifactRefCount: sum(events.map((event) => Number(event.artifact_ref_count))),
      artifactSpillCount,
      eventsWithArtifactRef: events.filter((event) => Number(event.artifact_ref_count) > 0).length,
      offloadedBytesTotal,
      inlineBytesTotal: sum(events.map((event) => Number(event.inline_bytes))),
      sourceBytesTotal: sum(events.map((event) => Number(event.source_bytes))),
      envelopeBytes: events.map((event) => Number(event.envelope_bytes)),
      routeRecipientCounts: routeRecipients,
      availableRecipientCounts: availableRecipients,
      broadcastCount,
      messageCount: deliveries.length,
      tokenEstimate: sum(events.map((event) => estimateTokens(Number(event.source_bytes)))),
    };
  }

  report(taskSuccess?: number): LowEntropyReport {
    const metrics = this.collect();
    const measuredTaskSuccess = taskSuccess
      ?? (metrics.eventCount > 0 && metrics.deadLetterCount === 0 ? 1 : 0);
    const maxEnvelope = Math.max(0, ...metrics.envelopeBytes);
    const p95 = quantile(metrics.envelopeBytes, 0.95);
    const averageRecipients = average(metrics.routeRecipientCounts);
    const averageAvailable = average(metrics.availableRecipientCounts);
    const findings: string[] = [];
    if (maxEnvelope > ENVELOPE_LIMIT_BYTES) findings.push(`max_envelope_bytes_exceeds_8k:${maxEnvelope}`);
    const oversizedInline = Number((this.store.db.prepare("SELECT COUNT(*) AS count FROM runtime_events WHERE inline_bytes > ?").get(INLINE_PAYLOAD_LIMIT_BYTES) as { count: number }).count);
    if (oversizedInline > 0) findings.push(`inline_payload_hard_limit_violations:${oversizedInline}`);
    const duplicateFacts = this.duplicateFacts();
    return {
      eventCount: metrics.eventCount,
      envelopeBytes: {
        p50: quantile(metrics.envelopeBytes, 0.5),
        p95,
        max: maxEnvelope,
      },
      inlineToSourceByteRatio: round(ratio(metrics.inlineBytesTotal, metrics.sourceBytesTotal)),
      artifactRefRate: round(ratio(metrics.eventsWithArtifactRef, metrics.eventCount)),
      artifactRefOffloadRatio: round(ratio(metrics.offloadedBytesTotal, metrics.sourceBytesTotal)),
      duplicateRate: round(ratio(metrics.duplicateEventCount, metrics.eventCount + metrics.duplicateEventCount)),
      redeliveryRate: round(ratio(metrics.redeliveryCount, metrics.deliveryCount)),
      routeDensity: round(ratio(averageRecipients, averageAvailable)),
      broadcastRatio: round(ratio(metrics.broadcastCount, metrics.eventCount)),
      messagesPerThousandTokens: round(ratio(metrics.messageCount * 1000, metrics.tokenEstimate)),
      duplicateFactRate: round(ratio(duplicateFacts, metrics.eventCount)),
      taskSuccess: round(measuredTaskSuccess),
      passedHardLimits: findings.length === 0,
      findings,
    };
  }

  baselines(): readonly BaselineComparison[] {
    return this.dynamicBaseline().strategies.map((replay) => replay.comparison);
  }

  compare(): Readonly<Record<string, unknown>> {
    const dynamic = this.dynamicBaseline();
    const baselines = dynamic.strategies.map((replay) => replay.comparison);
    const primary = baselines[0]!;
    const fullBroadcast = baselines.find((item) => item.strategy === "full_broadcast")!;
    const fullText = baselines.find((item) => item.strategy === "full_text_inline")!;
    return {
      schema: "zyra.low-entropy-comparison/v1",
      workload_id: dynamic.workloadId,
      replay_schema: dynamic.schema,
      primary,
      baselines,
      improvements: {
        route_density_reduction_vs_broadcast: round(1 - ratio(primary.routeDensity, fullBroadcast.routeDensity)),
        message_reduction_vs_broadcast: round(1 - ratio(primary.messageCount, fullBroadcast.messageCount)),
        inline_token_reduction_vs_full_text: round(1 - ratio(primary.inlineTokenEstimate, fullText.inlineTokenEstimate)),
        duplicate_fact_reduction_vs_broadcast: round(fullBroadcast.duplicateFactRate - primary.duplicateFactRate),
      },
      task_success_not_significantly_lower: dynamic.taskSuccessNotSignificantlyLower,
    };
  }

  private dynamicBaseline(): ReturnType<LowEntropyBaselineHarness["run"]> {
    const events = this.store.db.prepare(`
      SELECT event_id, source_bytes, inline_bytes
      FROM runtime_events ORDER BY global_sequence
    `).all() as unknown as Array<{ event_id: string; source_bytes: number; inline_bytes: number }>;
    const routes = this.store.db.prepare(`
      SELECT event_id, route_json FROM runtime_event_routes ORDER BY created_at, event_id
    `).all() as unknown as RouteMetricRow[];
    const routeByEvent = new Map(
      routes.map((row) => [row.event_id, JSON.parse(row.route_json) as RouteDecision]),
    );
    const offloadedRows = this.store.db.prepare(`
      SELECT event_id, value FROM runtime_event_metrics
      WHERE metric_kind = 'event.offloaded_bytes' AND event_id IS NOT NULL
    `).all() as unknown as Array<{ event_id: string; value: number }>;
    const offloadedByEvent = new Map(offloadedRows.map((row) => [row.event_id, Number(row.value)]));
    const rosterByKey = new Map<string, RecipientRef>();
    for (const spec of this.store.subscriptions(true)) {
      rosterByKey.set(`${spec.recipient.kind}:${spec.recipient.id}`, spec.recipient);
    }
    for (const route of routeByEvent.values()) {
      for (const candidate of route.candidates) {
        rosterByKey.set(`${candidate.recipient.kind}:${candidate.recipient.id}`, candidate.recipient);
      }
      for (const recipient of route.recipients) {
        rosterByKey.set(`${recipient.kind}:${recipient.id}`, recipient);
      }
    }
    const roster = [...rosterByKey.values()].sort((left, right) =>
      `${left.kind}:${left.id}`.localeCompare(`${right.kind}:${right.id}`),
    );
    if (roster.length === 0) {
      throw new RangeError("dynamic baseline requires at least one registered or routed recipient");
    }
    const facts: BaselineFact[] = events.map((event) => ({
      factId: event.event_id,
      sourceBytes: Number(event.source_bytes),
      inlineBytes: Number(event.inline_bytes),
      offloadedBytes: offloadedByEvent.get(event.event_id) ?? 0,
      requiredRecipients: routeByEvent.get(event.event_id)?.recipients ?? [],
    }));
    return new LowEntropyBaselineHarness().run({
      workloadId: digestJson({
        event_ids: facts.map((fact) => fact.factId),
        route_digests: routes.map((row) => digestJson(JSON.parse(row.route_json))),
      }),
      facts,
      addressableRecipients: roster,
      staticRecipients: roster.slice(0, Math.min(3, roster.length)),
    });
  }

  private duplicateFacts(): number {
    const rows = this.store.db.prepare("SELECT envelope_json FROM runtime_events ORDER BY global_sequence").all() as unknown as Array<{ envelope_json: string }>;
    const occurrences = new Map<string, number>();
    for (const row of rows) {
      const event = JSON.parse(row.envelope_json) as RuntimeEventEnvelope;
      const fingerprint = digestJson({
        event_type: event.eventType,
        task_id: event.identity.taskId,
        state_domain: event.stateDelta.domain,
        state_path: event.stateDelta.path,
        after_digest: event.stateDelta.afterDigest ?? null,
        evidence: event.evidenceRefs.map((item) => [item.evidenceId, item.artifactId ?? null]),
        artifacts: event.artifactRefs.map((item) => item.digest).sort(),
      });
      occurrences.set(fingerprint, (occurrences.get(fingerprint) ?? 0) + 1);
    }
    return [...occurrences.values()].reduce((total, count) => total + Math.max(0, count - 1), 0);
  }
}

export function eventEntropyObservation(event: RuntimeEventEnvelope): Readonly<Record<string, number | string | boolean>> {
  return {
    event_id: event.eventId,
    event_type: event.eventType,
    source_bytes: event.sourceBytes,
    inline_bytes: event.inlineBytes,
    envelope_bytes: event.envelopeBytes,
    artifact_ref_count: event.artifactRefs.length,
    inline_source_ratio: round(ratio(event.inlineBytes, event.sourceBytes)),
    within_inline_limit: event.inlineBytes <= INLINE_PAYLOAD_LIMIT_BYTES,
    within_envelope_limit: event.envelopeBytes <= ENVELOPE_LIMIT_BYTES,
  };
}
