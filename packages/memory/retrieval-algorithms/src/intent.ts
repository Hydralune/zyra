import type { QueryIntent, QueryIntentCategory } from "./types.ts";

// Cropped from oh-my-pi Mnemopi core/query-intent.ts at c6b83c1d. Zyra keeps
// the original language and weights, with Unicode-aware entity matching.

type PatternGroup = readonly [QueryIntentCategory, readonly RegExp[]];

const INTENT_PATTERNS: readonly PatternGroup[] = [
  [
    "temporal",
    [
      /\b(when|last|yesterday|today|tomorrow|ago|before|after|since|until|during|recently|latest|lately)\b/i,
      /\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b/i,
      /\b(january|february|march|april|may|june|july|august|september|october|november|december)\b/i,
      /\b\d{4}-\d{2}-\d{2}\b/,
      /\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b/,
      /\b(this|next|last)\s+(week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b/i,
      /\b\d+\s+(day|week|month|year|hour|minute)s?\s+(ago|from now|later|earlier)\b/i,
    ],
  ],
  [
    "factual",
    [
      /\bwhat\s+is\b/i,
      /\bwho\s+is\b/i,
      /\bwhere\s+is\b/i,
      /\b(definition|define|explain|meaning)\b/i,
      /\bhow\s+(many|much|long|far)\b/i,
    ],
  ],
  [
    "entity",
    [
      /\b(tell\s+me\s+about|what\s+do\s+you\s+know\s+about)\b/i,
      /\b(who\s+is|what\s+does)\s+[\p{L}\p{N}_-]+\b/iu,
      /\b(about|regarding|concerning)\s+[\p{L}\p{N}_-]+\b/iu,
    ],
  ],
  [
    "preference",
    [
      /\b(prefer|like|dislike|want|hate|love|enjoy|favorite|best|worst)\b/i,
      /\b(should\s+i|would\s+you|do\s+you\s+recommend)\b/i,
      /\b(choose|pick|select|option|choice|decide)\b/i,
    ],
  ],
  [
    "procedural",
    [
      /\bhow\s+(to|do|can|should|would)\b/i,
      /\b(step|process|procedure|workflow|guide|tutorial)\b/i,
      /\b(setup|install|configure|build|deploy|run|execute|start|stop)\b/i,
    ],
  ],
];

const INTENT_WEIGHTS: Readonly<Record<QueryIntentCategory, readonly [number, number, number]>> = {
  temporal: [0.6, 1.5, 0.8],
  factual: [1.0, 1.2, 0.9],
  entity: [1.1, 1.0, 1.3],
  preference: [0.9, 0.8, 1.5],
  procedural: [1.3, 0.9, 0.7],
  general: [1.0, 1.0, 1.0],
};

export function classifyIntent(query: string): QueryIntent {
  let category: QueryIntentCategory = "general";
  let confidence = 0;
  const signals: QueryIntentCategory[] = [];
  for (const [candidate, patterns] of INTENT_PATTERNS) {
    let matches = 0;
    for (const pattern of patterns) {
      pattern.lastIndex = 0;
      if (!pattern.test(query)) continue;
      matches += 1;
      signals.push(candidate);
    }
    const score = matches === 0 ? 0 : Math.min(0.3 + matches * 0.15, 1);
    if (score <= confidence) continue;
    category = candidate;
    confidence = score;
  }
  const [vectorBias, ftsBias, importanceBias] = INTENT_WEIGHTS[category];
  return { category, confidence, signals, vectorBias, ftsBias, importanceBias };
}

export function normalizedIntentWeights(
  intent: QueryIntent,
  base: readonly [number, number, number] = [0.35, 0.45, 0.2],
): readonly [number, number, number] {
  const weighted = [
    Math.max(0, base[0] * intent.vectorBias),
    Math.max(0, base[1] * intent.ftsBias),
    Math.max(0, base[2] * intent.importanceBias),
  ] as const;
  const total = weighted[0] + weighted[1] + weighted[2];
  if (total <= 0) return [0, 1, 0];
  return [weighted[0] / total, weighted[1] / total, weighted[2] / total];
}
