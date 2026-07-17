import type { JsonObject } from "../contracts.ts";
import { cloneJson, constantTimeDigestEquals, deterministicId, digest, hashChain, redactSecrets } from "./canonical.ts";
import type {
  E02Clock,
  E02Domain,
  E02EventEnvelope,
  E02EventSnapshot,
  E02RuntimeIdentity,
  TransitionPhase,
} from "./contracts.ts";

export interface E02EventInput {
  eventType: string;
  transitionId: string;
  domain: E02Domain;
  phase: TransitionPhase | "observation";
  revision: number;
  payload: JsonObject;
  causeIds?: string[];
  occurredAt?: string;
}

export class E02EventLog {
  readonly runtime: E02RuntimeIdentity;
  private readonly clock: E02Clock;
  private sequence = 0;
  private previousEventHash = digest({ genesis: "zyra.e02-events/v1", version: 1 });
  private readonly events: E02EventEnvelope[] = [];
  private readonly ids = new Set<string>();

  constructor(runtime: E02RuntimeIdentity, clock: E02Clock = () => new Date().toISOString()) {
    this.runtime = cloneJson(runtime);
    this.clock = clock;
  }

  append(input: E02EventInput): E02EventEnvelope {
    if (!input.eventType || !input.transitionId) throw new Error("event type and transition id are required");
    if (!Number.isSafeInteger(input.revision) || input.revision < 0) throw new Error("event revision is invalid");
    const occurredAt = input.occurredAt ?? this.clock();
    const payload = redactSecrets(input.payload) as JsonObject;
    const payloadHash = digest(payload);
    const causeIds = [...new Set(input.causeIds ?? [])].sort();
    for (const causeId of causeIds) {
      if (!this.ids.has(causeId)) throw new Error(`event cause ${causeId} is not present in the local causal log`);
    }
    const eventBase = {
      canonical_owner: "typescript" as const,
      runtime_id: "zyra-typescript-claude-runtime" as const,
      event_type: input.eventType,
      occurred_at: occurredAt,
      run_id: this.runtime.runId,
      session_id: this.runtime.sessionId,
      task_id: this.runtime.taskId,
      worker_request_id: this.runtime.workerRequestId,
      transition_id: input.transitionId,
      domain: input.domain,
      phase: input.phase,
      revision: input.revision,
      payload,
      cause_ids: causeIds,
      payload_hash: payloadHash,
      previous_event_hash: this.previousEventHash,
      sequence: this.sequence + 1,
      epoch: this.runtime.epoch,
    };
    const eventId = deterministicId("e02-event", eventBase, 40);
    const eventHash = hashChain(this.previousEventHash, { event_id: eventId, ...eventBase });
    const event: E02EventEnvelope = {
      event_id: eventId,
      ...eventBase,
      event_hash: eventHash,
    };
    this.sequence += 1;
    this.previousEventHash = eventHash;
    this.events.push(event);
    this.ids.add(eventId);
    return cloneJson(event);
  }

  appendTransition(
    eventType: string,
    transitionId: string,
    domain: E02Domain,
    phase: TransitionPhase | "observation",
    revision: number,
    payload: JsonObject,
    causeIds: string[] = [],
  ): E02EventEnvelope {
    return this.append({ eventType, transitionId, domain, phase, revision, payload, causeIds });
  }

  byTransition(transitionId: string): E02EventEnvelope[] {
    return this.events.filter((event) => event.transition_id === transitionId).map(cloneJson);
  }

  byDomain(domain: E02Domain): E02EventEnvelope[] {
    return this.events.filter((event) => event.domain === domain).map(cloneJson);
  }

  after(eventId: string | null): E02EventEnvelope[] {
    if (!eventId) return this.events.map(cloneJson);
    const index = this.events.findIndex((event) => event.event_id === eventId);
    if (index < 0) throw new Error(`unknown event cursor ${eventId}`);
    return this.events.slice(index + 1).map(cloneJson);
  }

  latest(): E02EventEnvelope | null {
    const event = this.events.at(-1);
    return event ? cloneJson(event) : null;
  }

  snapshot(): E02EventSnapshot {
    const base = {
      version: "zyra.e02-events/v1" as const,
      sequence: this.sequence,
      previousEventHash: this.previousEventHash,
      events: this.events.map(cloneJson),
    };
    return { ...base, snapshotHash: digest(base) };
  }

  restore(snapshot: E02EventSnapshot): void {
    if (snapshot.version !== "zyra.e02-events/v1") throw new Error("unsupported E02 event snapshot");
    const expectedSnapshotHash = digest({
      version: snapshot.version,
      sequence: snapshot.sequence,
      previousEventHash: snapshot.previousEventHash,
      events: snapshot.events,
    });
    if (!constantTimeDigestEquals(expectedSnapshotHash, snapshot.snapshotHash)) throw new Error("E02 event snapshot hash mismatch");
    let previousHash = digest({ genesis: "zyra.e02-events/v1", version: 1 });
    const ids = new Set<string>();
    for (const [index, event] of snapshot.events.entries()) {
      if (event.run_id !== this.runtime.runId || event.session_id !== this.runtime.sessionId) {
        throw new Error(`E02 event runtime binding mismatch at ${index}`);
      }
      if (event.previous_event_hash !== previousHash) throw new Error(`E02 event chain mismatch at ${index}`);
      for (const causeId of event.cause_ids) {
        if (!ids.has(causeId)) throw new Error(`E02 event cause order mismatch at ${index}`);
      }
      const eventBase = {
        canonical_owner: event.canonical_owner,
        runtime_id: event.runtime_id,
        event_type: event.event_type,
        occurred_at: event.occurred_at,
        run_id: event.run_id,
        session_id: event.session_id,
        task_id: event.task_id,
        worker_request_id: event.worker_request_id,
        transition_id: event.transition_id,
        domain: event.domain,
        phase: event.phase,
        revision: event.revision,
        payload: event.payload,
        cause_ids: event.cause_ids,
        payload_hash: event.payload_hash,
        previous_event_hash: event.previous_event_hash,
        sequence: (event as unknown as JsonObject).sequence,
        epoch: (event as unknown as JsonObject).epoch,
      };
      if (!constantTimeDigestEquals(digest(event.payload), event.payload_hash)) throw new Error(`E02 event payload hash mismatch at ${index}`);
      const expectedId = deterministicId("e02-event", eventBase, 40);
      if (expectedId !== event.event_id) throw new Error(`E02 event id mismatch at ${index}`);
      const expectedHash = hashChain(previousHash, { event_id: expectedId, ...eventBase });
      if (!constantTimeDigestEquals(expectedHash, event.event_hash)) throw new Error(`E02 event hash mismatch at ${index}`);
      previousHash = event.event_hash;
      ids.add(event.event_id);
    }
    if (snapshot.sequence !== snapshot.events.length) throw new Error("E02 event sequence mismatch");
    if (snapshot.previousEventHash !== previousHash) throw new Error("E02 event tail hash mismatch");
    this.sequence = snapshot.sequence;
    this.previousEventHash = snapshot.previousEventHash;
    this.events.splice(0, this.events.length, ...snapshot.events.map(cloneJson));
    this.ids.clear();
    for (const event of snapshot.events) this.ids.add(event.event_id);
  }
}
