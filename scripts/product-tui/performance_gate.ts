import { performance } from "node:perf_hooks"
import { PromptDraft } from "../../apps/cli/src/input/draft.ts"
import { ProductSessionState } from "../../apps/cli/src/product/state/session-state.ts"
import { ZYRA_UI_EVENT_SCHEMA } from "../../apps/cli/src/presentation/events.ts"
import { renderProductState } from "../../apps/cli/src/presentation/renderer.ts"

interface GateOptions {
  messages: number
  events: number
  iterations: number
  soakSeconds: number
  maximumInputP95Ms: number
  maximumRepaintP95Ms: number
  maximumRssMiB: number
  maximumSoakGrowthMiB: number
}

function integer(value: string | undefined, fallback: number, label: string): number {
  const selected = value === undefined ? fallback : Number(value)
  if (!Number.isSafeInteger(selected) || selected < 0) throw new TypeError(`${label} must be a non-negative integer`)
  return selected
}

function positive(value: string | undefined, fallback: number, label: string): number {
  const selected = value === undefined ? fallback : Number(value)
  if (!Number.isFinite(selected) || selected <= 0) throw new TypeError(`${label} must be positive`)
  return selected
}

function options(args: readonly string[]): GateOptions {
  const values = new Map<string, string>()
  for (let index = 0; index < args.length; index += 1) {
    const key = args[index]!
    if (!key.startsWith("--")) throw new TypeError(`unknown argument ${key}`)
    const value = args[index + 1]
    if (!value || value.startsWith("--")) throw new TypeError(`${key} requires a value`)
    values.set(key, value)
    index += 1
  }
  return {
    messages: integer(values.get("--messages"), 10_000, "--messages"),
    events: integer(values.get("--events"), 100_000, "--events"),
    iterations: integer(values.get("--iterations"), 200, "--iterations"),
    soakSeconds: integer(values.get("--soak-seconds"), 0, "--soak-seconds"),
    maximumInputP95Ms: positive(values.get("--maximum-input-p95-ms"), 50, "--maximum-input-p95-ms"),
    maximumRepaintP95Ms: positive(values.get("--maximum-repaint-p95-ms"), 100, "--maximum-repaint-p95-ms"),
    maximumRssMiB: positive(values.get("--maximum-rss-mib"), 512, "--maximum-rss-mib"),
    maximumSoakGrowthMiB: positive(values.get("--maximum-soak-growth-mib"), 128, "--maximum-soak-growth-mib"),
  }
}

function percentile(values: readonly number[], fraction: number): number {
  const selected = [...values].sort((left, right) => left - right)
  const index = Math.max(0, Math.min(selected.length - 1, Math.ceil(selected.length * fraction) - 1))
  return selected[index] ?? 0
}

function rounded(value: number): number { return Math.round(value * 1_000) / 1_000 }
function rssMiB(): number { return process.memoryUsage().rss / (1024 * 1024) }

function collectGarbage(): void {
  const runtime = globalThis as typeof globalThis & { Bun?: { gc?: (force?: boolean) => void } }
  runtime.Bun?.gc?.(true)
}

function buildLongState(selected: GateOptions): { state: ProductSessionState; replayMs: number } {
  const state = new ProductSessionState()
  const started = performance.now()
  for (let index = 0; index < selected.events; index += 1) {
    if (index < selected.messages) {
      state.apply({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `perf-message-event-${index}`,
        type: "assistant.message.completed",
        messageId: `perf-message-${index}`,
        text: `历史消息 ${index} · **Markdown** · emoji 👨‍👩‍👧‍👦 · combining e\u0301`,
        source: "stream",
      })
    } else {
      const activityId = `perf-activity-${index % 2_000}`
      state.apply({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `perf-activity-event-${index}`,
        type: index % 3 === 0 ? "activity.completed" : index % 3 === 1 ? "activity.started" : "activity.updated",
        activityId,
        label: `长程步骤 ${index % 2_000}`,
        ...(index % 3 === 0 ? { outcome: "completed" } : {}),
      })
    }
  }
  return { state, replayMs: performance.now() - started }
}

function measureInput(iterations: number): number[] {
  const samples: number[] = []
  const draft = new PromptDraft()
  const count = Math.max(1, iterations)
  for (let index = 0; index < count; index += 1) {
    const started = performance.now()
    draft.insert(index % 7 === 0 ? "中" : "x")
    draft.snapshot()
    if (index % 40 === 39) draft.set("")
    samples.push(performance.now() - started)
  }
  return samples
}

function measureRepaint(state: ProductSessionState, iterations: number): number[] {
  const samples: number[] = []
  const count = Math.max(1, iterations)
  for (let index = 0; index < count; index += 1) {
    const started = performance.now()
    renderProductState(state.snapshot(), {
      width: 60 + (index % 141),
      height: 40,
      workspace: "<performance-workspace>",
      composerText: `input ${index}`,
      scrollOffset: index % 25,
    })
    samples.push(performance.now() - started)
  }
  return samples
}

async function soak(state: ProductSessionState, seconds: number): Promise<{ samples: number[]; repaintP95Ms: number }> {
  const rssSamples = [rssMiB()]
  const repaintSamples: number[] = []
  const deadline = performance.now() + seconds * 1_000
  let sequence = 0
  while (performance.now() < deadline) {
    const batchStarted = performance.now()
    for (let index = 0; index < 100; index += 1) {
      sequence += 1
      state.apply({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `soak-${sequence}`,
        type: "tool.updated",
        toolCallId: `soak-tool-${sequence % 2_000}`,
        name: "soak",
        summary: `bounded update ${sequence}`,
        durationMs: sequence,
      })
    }
    renderProductState(state.snapshot(), {
      width: 60 + (sequence % 141),
      height: 40,
      workspace: "<soak-workspace>",
      composerText: `soak input ${sequence}`,
      scrollOffset: sequence % 100,
    })
    repaintSamples.push(performance.now() - batchStarted)
    if (sequence % 1_000 === 0) {
      collectGarbage()
      rssSamples.push(rssMiB())
    }
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
  collectGarbage()
  rssSamples.push(rssMiB())
  return { samples: rssSamples, repaintP95Ms: percentile(repaintSamples, 0.95) }
}

async function main(): Promise<number> {
  const selected = options(process.argv.slice(2))
  collectGarbage()
  const initialRss = rssMiB()
  const { state, replayMs } = buildLongState(selected)
  collectGarbage()
  const stableRss = rssMiB()
  const snapshot = state.snapshot()
  const input = measureInput(selected.iterations * 5)
  const repaint = measureRepaint(state, selected.iterations)
  const soakResult = selected.soakSeconds ? await soak(state, selected.soakSeconds) : undefined
  const maximumRss = Math.max(stableRss, ...(soakResult?.samples ?? []))
  const soakGrowth = soakResult ? Math.max(...soakResult.samples) - soakResult.samples[0]! : 0
  const inputP95 = percentile(input, 0.95)
  const repaintP95 = percentile(repaint, 0.95)
  const checks = {
    retained_messages_exact: snapshot.messages.length === Math.min(selected.messages, 10_000),
    retained_activities_bounded: snapshot.activities.length <= 2_000,
    input_p95: inputP95 <= selected.maximumInputP95Ms,
    repaint_p95: repaintP95 <= selected.maximumRepaintP95Ms,
    rss: maximumRss <= selected.maximumRssMiB,
    soak_growth: soakGrowth <= selected.maximumSoakGrowthMiB,
  }
  const report = {
    schema: "zyra.product-tui-performance-gate/v1",
    runtime: `bun ${Bun.version}`,
    input: selected,
    result: {
      replay_ms: rounded(replayMs),
      retained_messages: snapshot.messages.length,
      retained_activities: snapshot.activities.length,
      evicted: snapshot.evicted,
      input_p95_ms: rounded(inputP95),
      repaint_p95_ms: rounded(repaintP95),
      soak_repaint_p95_ms: soakResult ? rounded(soakResult.repaintP95Ms) : null,
      initial_rss_mib: rounded(initialRss),
      stable_rss_mib: rounded(stableRss),
      maximum_rss_mib: rounded(maximumRss),
      soak_growth_mib: rounded(soakGrowth),
      soak_rss_samples: soakResult?.samples.map(rounded) ?? [],
    },
    checks,
    all_passed: Object.values(checks).every(Boolean),
  }
  process.stdout.write(`${JSON.stringify(report)}\n`)
  return report.all_passed ? 0 : 1
}

process.exitCode = await main()
