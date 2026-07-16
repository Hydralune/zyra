import { expect, test } from "bun:test";

import { E01RuntimeCoordinator } from "../../src/e01/coordinator.ts";
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
