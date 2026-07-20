import type { BaselineComparison, RecipientRef } from "./contracts.ts";

export type BaselineStrategy = BaselineComparison["strategy"];

export interface BaselineFact {
  factId: string;
  sourceBytes: number;
  inlineBytes: number;
  offloadedBytes: number;
  requiredRecipients: readonly RecipientRef[];
}

export interface BaselineWorkload {
  workloadId: string;
  facts: readonly BaselineFact[];
  addressableRecipients: readonly RecipientRef[];
  staticRecipients: readonly RecipientRef[];
  /** Immutable model/task token denominator shared by every strategy. */
  taskTokenCount: number;
  /** The same verifier is invoked for every isolated strategy replay. */
  verify: (deliveries: ReadonlyMap<string, ReadonlySet<string>>) => number;
}

export interface BaselineReplay {
  strategy: BaselineStrategy;
  comparison: BaselineComparison;
  deliveredFactCount: number;
  transmittedBytes: number;
  verifier: "shared_workload_verifier";
}

export interface DynamicBaselineReport {
  schema: "zyra.low-entropy-dynamic-baseline/v1";
  workloadId: string;
  strategies: readonly BaselineReplay[];
  primary: BaselineReplay;
  taskSuccessNotSignificantlyLower: boolean;
}

function recipientKey(recipient: RecipientRef): string {
  return `${recipient.kind}:${recipient.id}`;
}

function unique(recipients: readonly RecipientRef[]): readonly RecipientRef[] {
  return [...new Map(recipients.map((recipient) => [recipientKey(recipient), recipient])).values()];
}

function ratio(numerator: number, denominator: number): number {
  return denominator > 0 ? numerator / denominator : 0;
}

function round(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function defaultVerifier(
  facts: readonly BaselineFact[],
  deliveries: ReadonlyMap<string, ReadonlySet<string>>,
): number {
  if (facts.length === 0) return 0;
  const complete = facts.every((fact) => {
    const delivered = deliveries.get(fact.factId) ?? new Set<string>();
    return unique(fact.requiredRecipients).every((recipient) => delivered.has(recipientKey(recipient)));
  });
  return complete ? 1 : 0;
}

/**
 * Execute four isolated communication policies over one immutable semantic
 * workload.  Unlike an arithmetic extrapolation from the primary strategy,
 * every strategy produces its own recipient set, transfer bytes, duplicate
 * facts and verifier result.
 */
export class LowEntropyBaselineHarness {
  run(workload: BaselineWorkload): DynamicBaselineReport {
    const roster = unique(workload.addressableRecipients);
    if (roster.length === 0) throw new RangeError("baseline workload requires an addressable recipient roster");
    const strategies: BaselineStrategy[] = [
      "targeted_artifact_ref",
      "static_route",
      "full_broadcast",
      "full_text_inline",
    ];
    const replays = strategies.map((strategy) => this.replay(workload, roster, strategy));
    const primary = replays[0]!;
    const competitors = replays.slice(1);
    return {
      schema: "zyra.low-entropy-dynamic-baseline/v1",
      workloadId: workload.workloadId,
      strategies: replays,
      primary,
      taskSuccessNotSignificantlyLower: competitors.every(
        (candidate) => primary.comparison.taskSuccess >= candidate.comparison.taskSuccess - 0.05,
      ),
    };
  }

  private replay(
    workload: BaselineWorkload,
    roster: readonly RecipientRef[],
    strategy: BaselineStrategy,
  ): BaselineReplay {
    const deliveries = new Map<string, ReadonlySet<string>>();
    let messageCount = 0;
    let transmittedBytes = 0;
    let sourceBytes = 0;
    let offloadedBytes = 0;
    let routeDensity = 0;
    let duplicateFacts = 0;
    for (const fact of workload.facts) {
      const recipients = this.recipients(strategy, fact, workload.staticRecipients, roster);
      const keys = new Set(recipients.map(recipientKey));
      deliveries.set(fact.factId, keys);
      messageCount += keys.size;
      routeDensity += ratio(keys.size, roster.length);
      const bytesPerMessage = strategy === "full_text_inline" ? fact.sourceBytes : fact.inlineBytes;
      transmittedBytes += bytesPerMessage * keys.size;
      sourceBytes += fact.sourceBytes * Math.max(1, keys.size);
      if (strategy !== "full_text_inline") offloadedBytes += fact.offloadedBytes * Math.max(1, keys.size);
      duplicateFacts += Math.max(0, keys.size - unique(fact.requiredRecipients).length);
    }
    // Success is always supplied by the real workload verifier.  Recipient
    // presence is communication coverage, not proof that the task succeeded.
    const taskSuccess = workload.verify(deliveries);
    const estimatedTokens = Math.max(1, Math.ceil(transmittedBytes / 4));
    const comparison: BaselineComparison = {
      strategy,
      routeDensity: round(ratio(routeDensity, workload.facts.length)),
      broadcastRatio: strategy === "full_broadcast" ? 1 : 0,
      messageCount,
      messagesPerThousandTokens: round(ratio(messageCount * 1000, Math.max(1, workload.taskTokenCount))),
      duplicateFactRate: round(ratio(duplicateFacts, messageCount)),
      artifactRefOffloadRatio: round(ratio(offloadedBytes, sourceBytes)),
      inlineTokenEstimate: estimatedTokens,
      taskSuccess: round(taskSuccess),
    };
    return {
      strategy,
      comparison,
      deliveredFactCount: deliveries.size,
      transmittedBytes,
      verifier: "shared_workload_verifier",
    };
  }

  private recipients(
    strategy: BaselineStrategy,
    fact: BaselineFact,
    staticRecipients: readonly RecipientRef[],
    roster: readonly RecipientRef[],
  ): readonly RecipientRef[] {
    if (strategy === "full_broadcast") return roster;
    if (strategy === "static_route") return unique(staticRecipients);
    return unique(fact.requiredRecipients);
  }
}
