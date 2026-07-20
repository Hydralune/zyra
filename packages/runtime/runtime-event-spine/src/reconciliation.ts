import {
  DISABLE_MATRIX_SCHEMA,
  RECONCILIATION_SCHEMA,
  type DisableMatrixCell,
  type DisableMatrixReport,
  type ReconciliationFinding,
  type ReconciliationReport,
} from "./integration-contracts.ts";
import { EventDurability, type RouteDecision, type RuntimeEventEnvelope } from "./contracts.ts";
import { assertEnvelopeDigest, type SubscriptionSpec } from "./contracts.ts";
import { digestJson, utcNow, type JsonValue } from "./canonical.ts";
import { eventDefinition, systemCriticalBroadcastTypes } from "./event-catalog.ts";
import { EventSpineError, EventSpineErrorCode, asEventSpineError } from "./errors.ts";
import type { RuntimeEventSpine } from "./runtime.ts";

export interface ReconciliationOptions {
  aggregateId?: string;
  repairDeliveries?: boolean;
  repairProjection?: boolean;
  rebuildProjection?: boolean;
  verifyArtifacts?: boolean;
  maxEvents?: number;
}

export interface DisableObservation {
  component: DisableMatrixCell["component"];
  canonicalWrite: boolean;
  liveDelivery: boolean;
  replay: boolean;
  queryView: boolean;
  typedFrameAdmission: boolean;
  observedFailure?: string;
}

interface AggregateAuditState {
  aggregateId: string;
  lastSequence: number;
  lastGlobalSequence: number;
  lastEventId?: string;
  lastDigest?: string;
  eventCount: number;
}

function finding(
  code: string,
  severity: ReconciliationFinding["severity"],
  summary: string,
  options: Partial<ReconciliationFinding> = {},
): ReconciliationFinding {
  return {
    code,
    severity,
    repairable: options.repairable ?? false,
    repaired: options.repaired ?? false,
    summary,
    aggregateId: options.aggregateId,
    eventId: options.eventId,
    deliveryId: options.deliveryId,
    expected: options.expected,
    actual: options.actual,
  };
}

function recipientKey(value: { kind: string; id: string }): string {
  return `${value.kind}:${value.id}`;
}

function matchesRoute(spec: SubscriptionSpec, route: RouteDecision): boolean {
  return route.recipients.some((recipient) => recipientKey(recipient) === recipientKey(spec.recipient));
}

function expectedDisableCell(component: DisableMatrixCell["component"]): Omit<DisableMatrixCell, "observedFailure" | "passed"> {
  switch (component) {
    case "event_store":
      return {
        component,
        canonicalWrite: false,
        liveDelivery: false,
        replay: false,
        queryView: false,
        typedFrameAdmission: false,
        expectedFailure: "canonical admission and replay fail closed; no legacy fallback",
      };
    case "message_bus":
      return {
        component,
        canonicalWrite: true,
        liveDelivery: false,
        replay: true,
        queryView: false,
        typedFrameAdmission: true,
        expectedFailure: "facts remain durable while consumers and projector stall",
      };
    case "projector":
      return {
        component,
        canonicalWrite: true,
        liveDelivery: true,
        replay: true,
        queryView: false,
        typedFrameAdmission: true,
        expectedFailure: "execution continues but API/M2 query view is unavailable",
      };
    case "omp_mapper":
      return {
        component,
        canonicalWrite: true,
        liveDelivery: true,
        replay: true,
        queryView: true,
        typedFrameAdmission: false,
        expectedFailure: "OMP typed tool/subagent frames fail closed without generic fallback",
      };
  }
}

export class RuntimeEventReconciler {
  readonly spine: RuntimeEventSpine;

  constructor(spine: RuntimeEventSpine) {
    this.spine = spine;
  }

  reconcile(options: ReconciliationOptions = {}): ReconciliationReport {
    const startedAt = utcNow();
    const findings: ReconciliationFinding[] = [];
    const states = new Map<string, AggregateAuditState>();
    const byId = new Map<string, RuntimeEventEnvelope>();
    let scannedEvents = 0;
    let cursor: string | undefined;
    let previousGlobal = 0;
    const maxEvents = Math.max(1, Math.min(options.maxEvents ?? 1_000_000, 1_000_000));
    do {
      const page = this.spine.query({
        ...(options.aggregateId ? { aggregateId: options.aggregateId } : {}),
        ...(cursor ? { cursor } : {}),
        limit: Math.min(1000, maxEvents - scannedEvents),
      });
      for (const event of page.items) {
        scannedEvents += 1;
        byId.set(event.eventId, event);
        const state = states.get(event.aggregateId) ?? {
          aggregateId: event.aggregateId,
          lastSequence: -1,
          lastGlobalSequence: 0,
          eventCount: 0,
        };
        this.auditEnvelope(event, state, previousGlobal, findings);
        state.lastSequence = event.aggregateSequence;
        state.lastGlobalSequence = event.globalSequence;
        state.lastEventId = event.eventId;
        state.lastDigest = event.contentDigest;
        state.eventCount += 1;
        states.set(event.aggregateId, state);
        previousGlobal = event.globalSequence;
      }
      cursor = scannedEvents < maxEvents ? page.nextCursor : undefined;
    } while (cursor);
    this.auditCausality(byId, findings);
    this.auditRoutes(byId, findings);
    const repairedDeliveries = options.repairDeliveries === false
      ? this.auditDeliveries(false, findings)
      : this.auditDeliveries(true, findings);
    let rebuiltAggregates = 0;
    let equivalentAfterRepair = true;
    if (this.spine.projectionDelivery) {
      if (options.repairProjection !== false) {
        const repaired = this.spine.projectionDelivery.reconcileAckGaps();
        if (repaired > 0) {
          findings.push(finding(
            "projector_ack_gap_repaired",
            "warning",
            "acknowledged projector deliveries without read rows were reopened",
            { repairable: true, repaired: true, actual: repaired },
          ));
        }
        const report = this.spine.projectionDelivery.pump({ catchUp: true });
        if (report.failed > 0) {
          findings.push(finding(
            "projector_repair_failed",
            "warning",
            "projector catch-up reported failed deliveries; coverage verification decides repair status",
            { repairable: true, repaired: false, actual: report.failed },
          ));
        }
      }
      let coverage = this.spine.projectionDelivery.coverage(options.aggregateId);
      if (!coverage.complete && options.repairProjection !== false && !options.rebuildProjection) {
        const rebuild = this.spine.projectionDelivery.rebuild(options.aggregateId);
        rebuiltAggregates += Number(rebuild.aggregate_count ?? 0);
        coverage = this.spine.projectionDelivery.coverage(options.aggregateId);
        findings.push(finding(
          "projection_coverage_rebuilt",
          coverage.complete ? "warning" : "error",
          "projector coverage lag required a canonical-history rebuild",
          {
            aggregateId: options.aggregateId,
            expected: coverage.canonicalEvents,
            actual: coverage.projectedEvents,
            repairable: true,
            repaired: coverage.complete,
          },
        ));
      }
      if (!coverage.complete) {
        equivalentAfterRepair = false;
        findings.push(finding(
          "projection_coverage_gap",
          "error",
          "projector timeline, task views, or cursor do not cover canonical history",
          {
            aggregateId: options.aggregateId,
            expected: coverage.canonicalEvents,
            actual: coverage.projectedEvents,
            repairable: true,
            repaired: false,
          },
        ));
      }
      if (options.rebuildProjection) {
        const rebuild = this.spine.projectionDelivery.rebuild(options.aggregateId);
        rebuiltAggregates = Number(rebuild.aggregate_count ?? 0);
        equivalentAfterRepair = equivalentAfterRepair && rebuild.equivalent === true;
        if (rebuild.equivalent !== true) {
          findings.push(finding(
            "projection_rebuild_divergence",
            "error",
            "empty projector rebuild differs from online projection",
            {
              aggregateId: options.aggregateId,
              expected: rebuild.online_checksum as JsonValue,
              actual: rebuild.rebuilt_checksum as JsonValue,
              repairable: false,
            },
          ));
        }
      }
    } else if (options.repairProjection || options.rebuildProjection) {
      equivalentAfterRepair = false;
      findings.push(finding(
        "projector_disabled",
        "error",
        "projection repair was requested while projector is disabled",
        { repairable: false },
      ));
    }
    const errors = findings.filter((item) => item.severity === "error" && !item.repaired);
    return {
      schema: RECONCILIATION_SCHEMA,
      startedAt,
      completedAt: utcNow(),
      highWatermark: this.spine.store.latestGlobalSequence(),
      scannedEvents,
      scannedAggregates: states.size,
      repairedDeliveries,
      rebuiltAggregates,
      findings,
      equivalentAfterRepair: equivalentAfterRepair && errors.length === 0,
    };
  }

  private auditEnvelope(
    event: RuntimeEventEnvelope,
    state: AggregateAuditState,
    previousGlobal: number,
    findings: ReconciliationFinding[],
  ): void {
    try {
      assertEnvelopeDigest(event);
    } catch (error) {
      findings.push(finding(
        "event_digest_mismatch",
        "error",
        error instanceof Error ? error.message : String(error),
        { aggregateId: event.aggregateId, eventId: event.eventId },
      ));
    }
    if (event.aggregateSequence !== state.lastSequence + 1) {
      findings.push(finding(
        "aggregate_sequence_gap",
        "error",
        "aggregate sequence is not contiguous",
        {
          aggregateId: event.aggregateId,
          eventId: event.eventId,
          expected: state.lastSequence + 1,
          actual: event.aggregateSequence,
        },
      ));
    }
    if (previousGlobal > 0 && event.globalSequence !== previousGlobal + 1) {
      findings.push(finding(
        "global_sequence_gap",
        "error",
        "global canonical sequence is not contiguous",
        {
          eventId: event.eventId,
          expected: previousGlobal + 1,
          actual: event.globalSequence,
        },
      ));
    }
    if (event.durability !== EventDurability.DURABLE) {
      findings.push(finding(
        "live_event_in_event_store",
        "error",
        "live-only event was persisted in canonical history",
        { aggregateId: event.aggregateId, eventId: event.eventId },
      ));
    }
    try {
      const definition = eventDefinition(event.eventType);
      if (definition.requiresCausation && !event.causationId) {
        findings.push(finding(
          "required_causation_missing",
          "error",
          "event catalog requires a causation event",
          { aggregateId: event.aggregateId, eventId: event.eventId },
        ));
      }
    } catch (error) {
      findings.push(finding(
        "event_catalog_missing",
        "error",
        error instanceof Error ? error.message : String(error),
        { aggregateId: event.aggregateId, eventId: event.eventId },
      ));
    }
    if (event.inlineBytes > 4096) {
      findings.push(finding("inline_limit_exceeded", "error", "canonical inline payload exceeds 4 KiB", {
        eventId: event.eventId,
        expected: 4096,
        actual: event.inlineBytes,
      }));
    }
    if (event.envelopeBytes > 8192) {
      findings.push(finding("envelope_limit_exceeded", "error", "canonical envelope exceeds 8 KiB", {
        eventId: event.eventId,
        expected: 8192,
        actual: event.envelopeBytes,
      }));
    }
    if (Buffer.byteLength(event.summary, "utf8") > 1024) {
      findings.push(finding("summary_limit_exceeded", "error", "canonical summary exceeds 1 KiB", {
        eventId: event.eventId,
      }));
    }
    if (event.artifactRefs.length + event.evidenceRefs.length > 64) {
      findings.push(finding("reference_limit_exceeded", "error", "canonical reference count exceeds 64", {
        eventId: event.eventId,
        expected: 64,
        actual: event.artifactRefs.length + event.evidenceRefs.length,
      }));
    }
  }

  private auditCausality(
    events: ReadonlyMap<string, RuntimeEventEnvelope>,
    findings: ReconciliationFinding[],
  ): void {
    for (const event of events.values()) {
      if (!event.causationId) continue;
      const cause = this.spine.get(event.causationId);
      if (!cause) {
        findings.push(finding(
          "causation_target_missing",
          "error",
          "causation id does not resolve to a canonical event",
          { aggregateId: event.aggregateId, eventId: event.eventId, actual: event.causationId },
        ));
        continue;
      }
      if (cause.correlationId !== event.correlationId) {
        findings.push(finding(
          "causation_correlation_mismatch",
          "error",
          "cause and effect have different correlation ids",
          {
            aggregateId: event.aggregateId,
            eventId: event.eventId,
            expected: cause.correlationId,
            actual: event.correlationId,
          },
        ));
      }
      if (cause.globalSequence >= event.globalSequence) {
        findings.push(finding(
          "causation_order_invalid",
          "error",
          "cause is not ordered before effect",
          {
            aggregateId: event.aggregateId,
            eventId: event.eventId,
            expected: `<${event.globalSequence}`,
            actual: cause.globalSequence,
          },
        ));
      }
    }
  }

  private auditRoutes(
    events: ReadonlyMap<string, RuntimeEventEnvelope>,
    findings: ReconciliationFinding[],
  ): void {
    const critical = systemCriticalBroadcastTypes();
    for (const event of events.values()) {
      const route = this.spine.store.persistedRoute(event.eventId);
      if (!route) continue;
      if (route.broadcast && !critical.has(event.eventType)) {
        findings.push(finding(
          "broadcast_not_allowlisted",
          "error",
          "non-critical event used broadcast delivery",
          { eventId: event.eventId, actual: event.eventType },
        ));
      }
      if (route.broadcast && (!route.fanoutReason || route.recipients.length === 0 || !route.policyId)) {
        findings.push(finding(
          "broadcast_audit_incomplete",
          "error",
          "broadcast is missing policy, reason, or recipients",
          { eventId: event.eventId },
        ));
      }
      if (route.recipients.length > route.availableRecipientCount) {
        findings.push(finding(
          "route_density_invalid",
          "error",
          "route selected more recipients than were addressable",
          {
            eventId: event.eventId,
            expected: route.availableRecipientCount,
            actual: route.recipients.length,
          },
        ));
      }
    }
  }

  private auditDeliveries(repair: boolean, findings: ReconciliationFinding[]): number {
    let repaired = 0;
    const specs = this.spine.store.subscriptions(true).filter(
      (spec) => spec.subscriptionId !== this.spine.projectionDelivery?.subscriptionId,
    );
    for (const spec of specs) {
      let cursor: string | undefined;
      do {
        const page = this.spine.query({ ...(cursor ? { cursor } : {}), limit: 1000 });
        for (const event of page.items) {
          const route = this.spine.store.persistedRoute(event.eventId);
          if (!route || !matchesRoute(spec, route)) continue;
          const delivery = this.spine.store.deliveriesForEvent(event.eventId)
            .find((item) => item.subscriptionId === spec.subscriptionId);
          if (delivery) continue;
          let repairedThis = false;
          if (repair) {
            repairedThis = this.spine.bus.ensureDelivery(spec.subscriptionId, event, false).length > 0;
            if (repairedThis) repaired += 1;
          }
          findings.push(finding(
            "delivery_missing",
            repairedThis ? "warning" : "error",
            "persisted route has no durable delivery",
            {
              aggregateId: event.aggregateId,
              eventId: event.eventId,
              expected: spec.subscriptionId,
              repairable: true,
              repaired: repairedThis,
            },
          ));
        }
        cursor = page.nextCursor;
      } while (cursor);
    }
    return repaired;
  }
}

export class RuntimeDisableMatrix {
  evaluate(aggregateId: string, observations: readonly DisableObservation[]): DisableMatrixReport {
    const byComponent = new Map(observations.map((item) => [item.component, item]));
    const components: DisableMatrixCell["component"][] = [
      "event_store",
      "message_bus",
      "projector",
      "omp_mapper",
    ];
    const cells = components.map((component): DisableMatrixCell => {
      const expected = expectedDisableCell(component);
      const actual = byComponent.get(component);
      if (!actual) {
        return {
          ...expected,
          observedFailure: "scenario_not_executed",
          passed: false,
        };
      }
      const passed = expected.canonicalWrite === actual.canonicalWrite
        && expected.liveDelivery === actual.liveDelivery
        && expected.replay === actual.replay
        && expected.queryView === actual.queryView
        && expected.typedFrameAdmission === actual.typedFrameAdmission;
      return {
        ...expected,
        observedFailure: actual.observedFailure,
        passed,
      };
    });
    return {
      schema: DISABLE_MATRIX_SCHEMA,
      aggregateId,
      cells,
      passed: cells.every((cell) => cell.passed),
      checkedAt: utcNow(),
    };
  }

  contract(): Record<string, JsonValue> {
    return {
      schema: DISABLE_MATRIX_SCHEMA,
      components: ["event_store", "message_bus", "projector", "omp_mapper"],
      fallback_allowed: false,
      legacy_jsonl_may_replace_store: false,
      raw_event_fold_may_replace_projector: false,
      generic_mapper_may_replace_omp: false,
      expected: [
        expectedDisableCell("event_store"),
        expectedDisableCell("message_bus"),
        expectedDisableCell("projector"),
        expectedDisableCell("omp_mapper"),
      ],
    } as unknown as Record<string, JsonValue>;
  }
}

export function reconciliationDigest(report: ReconciliationReport): string {
  return digestJson({
    high_watermark: report.highWatermark,
    scanned_events: report.scannedEvents,
    scanned_aggregates: report.scannedAggregates,
    repaired_deliveries: report.repairedDeliveries,
    rebuilt_aggregates: report.rebuiltAggregates,
    equivalent_after_repair: report.equivalentAfterRepair,
    findings: report.findings.map((item) => ({
      code: item.code,
      severity: item.severity,
      event_id: item.eventId ?? null,
      repaired: item.repaired,
    })),
  });
}
