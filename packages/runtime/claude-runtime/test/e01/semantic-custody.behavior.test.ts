import { expect, test } from "bun:test";

import { E01RuntimeCoordinator } from "../../src/e01/coordinator.ts";
import { ContextTokenRuntime } from "../../src/context/token-runtime.ts";
import { ProviderRecoveryRuntime } from "../../src/provider/recovery-runtime.ts";

test("e01.semantic.output-token-recovery-is-bounded", () => {
  const recovery = new ProviderRecoveryRuntime();
  recovery.createContext(
    "output-token-recovery",
    "anthropic",
    "claude-runtime",
    16_000,
    5,
    1_000,
  );

  const plan = recovery.plan(
    "output-token-recovery",
    { status: 400, message: "context length exceeded by max output tokens" },
    [],
    1_100,
  );

  expect(plan.action).toBe("reduce_output");
  expect(plan.nextOutputTokenLimit).toBe(12_000);
  expect(plan.nextOutputTokenLimit).toBeLessThan(
    plan.context.originalOutputTokenLimit,
  );
  expect(plan.context.attempt).toBe(1);
});

test("e01.semantic.tool-planning-partitions-read-only-and-write-calls", async () => {
  const runtime = new E01RuntimeCoordinator(
    "semantic-run",
    "semantic-session",
    "semantic-task",
    "semantic-worker",
  );
  await runtime.bootstrap();
  runtime.beginCanonicalTurn("semantic-turn", 0, "partition tool calls");

  const batches = runtime.planToolBatches(
    "semantic-turn",
    0,
    [
      {
        step: { step_id: "read-1", tool_name: "read_file", arguments: {} },
        readOnly: true,
      },
      {
        step: { step_id: "read-2", tool_name: "search_files", arguments: {} },
        readOnly: true,
      },
      {
        step: { step_id: "write-1", tool_name: "write_file", arguments: {} },
        readOnly: false,
      },
    ],
    4,
  );

  expect(batches.map((batch) => batch.executionMode)).toEqual([
    "concurrent_read_only",
    "serial_non_read_only",
  ]);
  expect(batches[0]?.steps.map((step) => step.step_id)).toEqual([
    "read-1",
    "read-2",
  ]);
  expect(batches[1]?.steps.map((step) => step.step_id)).toEqual(["write-1"]);
  expect(runtime.snapshot().tools.calls).toHaveLength(3);
});

test("e01.semantic.token-budget-continues-then-stops-on-diminishing-progress", async () => {
  const tokens = new ContextTokenRuntime(10_000);
  const first = tokens.checkContinuationBudget({ budget: 1_000, globalTurnTokens: 100, now: 1_000 });
  const second = tokens.checkContinuationBudget({ budget: 1_000, globalTurnTokens: 200, now: 2_000 });
  const third = tokens.checkContinuationBudget({ budget: 1_000, globalTurnTokens: 300, now: 3_000 });
  const stopped = tokens.checkContinuationBudget({ budget: 1_000, globalTurnTokens: 350, now: 4_000 });

  expect(first.action).toBe("continue");
  expect(first.action === "continue" ? first.nudgeMessage : "").toContain("10%");
  expect(second.action).toBe("continue");
  expect(third.action).toBe("continue");
  expect(stopped.action).toBe("stop");
  expect(stopped.action === "stop" ? stopped.completionEvent?.diminishingReturns : false).toBe(true);
  expect(tokens.snapshot().continuation.lastAction).toBe("stop");
  expect(tokens.snapshot().continuation.continuationCount).toBe(3);

  const runtime = new E01RuntimeCoordinator("token-run", "token-session", "token-task", "token-worker");
  await runtime.bootstrap();
  const decision = runtime.decideContinuationBudget(100, 1_000);
  expect(decision.action).toBe("continue");
  expect(runtime.snapshot().tokens.continuation.continuationCount).toBe(1);
});

test("e01.semantic.gateway-detection-is-recorded-by-provider-custody", async () => {
  const runtime = new E01RuntimeCoordinator("gateway-run", "gateway-session", "gateway-task", "gateway-worker");
  await runtime.bootstrap();
  runtime.recordProvider("gateway_response", {
    response_headers: { "x-kong-upstream-latency": "12" },
    base_url: "https://provider.invalid/v1",
  });

  const events = runtime.snapshot().telemetry.events;
  const event = events.find((item) => item.name === "provider.gateway_response");
  expect(event?.attributes.detected_gateway).toBe("kong");
  expect(runtime.telemetry.detectGateway({ baseUrl: "https://abc.cloud.databricks.com/serving" })).toBe("databricks");
});
