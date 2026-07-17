import assert from "node:assert/strict";
import { test } from "bun:test";

import type { JsonObject } from "../../src/contracts.ts";
import { E02CheckpointBundleRuntime, type E02RuntimeIdentity } from "../../src/e02/index.ts";

interface CheckpointCase {
  id: string;
  revision: number;
  delivery: "delivered" | "indeterminate";
  changedDomain: string;
  retainCount: number;
}

const runtime: E02RuntimeIdentity = {
  runtimeId: "checkpoint-matrix-runtime",
  runId: "checkpoint-matrix-run",
  taskId: "checkpoint-matrix-task",
  sessionId: "checkpoint-matrix-session",
  workerRequestId: "checkpoint-matrix-worker",
  epoch: 1,
};

const cases: CheckpointCase[] = [
  {
    id: "checkpoint-001",
    revision: 1,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 32,
  },
  {
    id: "checkpoint-002",
    revision: 2,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 33,
  },
  {
    id: "checkpoint-003",
    revision: 3,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 34,
  },
  {
    id: "checkpoint-004",
    revision: 4,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 35,
  },
  {
    id: "checkpoint-005",
    revision: 5,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 36,
  },
  {
    id: "checkpoint-006",
    revision: 6,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 37,
  },
  {
    id: "checkpoint-007",
    revision: 7,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 38,
  },
  {
    id: "checkpoint-008",
    revision: 8,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 39,
  },
  {
    id: "checkpoint-009",
    revision: 9,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 40,
  },
  {
    id: "checkpoint-010",
    revision: 10,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 41,
  },
  {
    id: "checkpoint-011",
    revision: 11,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 42,
  },
  {
    id: "checkpoint-012",
    revision: 12,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 43,
  },
  {
    id: "checkpoint-013",
    revision: 13,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 44,
  },
  {
    id: "checkpoint-014",
    revision: 14,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 45,
  },
  {
    id: "checkpoint-015",
    revision: 15,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 46,
  },
  {
    id: "checkpoint-016",
    revision: 16,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 47,
  },
  {
    id: "checkpoint-017",
    revision: 17,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 32,
  },
  {
    id: "checkpoint-018",
    revision: 18,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 33,
  },
  {
    id: "checkpoint-019",
    revision: 19,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 34,
  },
  {
    id: "checkpoint-020",
    revision: 20,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 35,
  },
  {
    id: "checkpoint-021",
    revision: 21,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 36,
  },
  {
    id: "checkpoint-022",
    revision: 22,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 37,
  },
  {
    id: "checkpoint-023",
    revision: 23,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 38,
  },
  {
    id: "checkpoint-024",
    revision: 24,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 39,
  },
  {
    id: "checkpoint-025",
    revision: 25,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 40,
  },
  {
    id: "checkpoint-026",
    revision: 26,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 41,
  },
  {
    id: "checkpoint-027",
    revision: 27,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 42,
  },
  {
    id: "checkpoint-028",
    revision: 28,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 43,
  },
  {
    id: "checkpoint-029",
    revision: 29,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 44,
  },
  {
    id: "checkpoint-030",
    revision: 30,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 45,
  },
  {
    id: "checkpoint-031",
    revision: 31,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 46,
  },
  {
    id: "checkpoint-032",
    revision: 32,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 47,
  },
  {
    id: "checkpoint-033",
    revision: 33,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 32,
  },
  {
    id: "checkpoint-034",
    revision: 34,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 33,
  },
  {
    id: "checkpoint-035",
    revision: 35,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 34,
  },
  {
    id: "checkpoint-036",
    revision: 36,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 35,
  },
  {
    id: "checkpoint-037",
    revision: 37,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 36,
  },
  {
    id: "checkpoint-038",
    revision: 38,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 37,
  },
  {
    id: "checkpoint-039",
    revision: 39,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 38,
  },
  {
    id: "checkpoint-040",
    revision: 40,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 39,
  },
  {
    id: "checkpoint-041",
    revision: 41,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 40,
  },
  {
    id: "checkpoint-042",
    revision: 42,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 41,
  },
  {
    id: "checkpoint-043",
    revision: 43,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 42,
  },
  {
    id: "checkpoint-044",
    revision: 44,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 43,
  },
  {
    id: "checkpoint-045",
    revision: 45,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 44,
  },
  {
    id: "checkpoint-046",
    revision: 46,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 45,
  },
  {
    id: "checkpoint-047",
    revision: 47,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 46,
  },
  {
    id: "checkpoint-048",
    revision: 48,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 47,
  },
  {
    id: "checkpoint-049",
    revision: 49,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 32,
  },
  {
    id: "checkpoint-050",
    revision: 50,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 33,
  },
  {
    id: "checkpoint-051",
    revision: 51,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 34,
  },
  {
    id: "checkpoint-052",
    revision: 52,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 35,
  },
  {
    id: "checkpoint-053",
    revision: 53,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 36,
  },
  {
    id: "checkpoint-054",
    revision: 54,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 37,
  },
  {
    id: "checkpoint-055",
    revision: 55,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 38,
  },
  {
    id: "checkpoint-056",
    revision: 56,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 39,
  },
  {
    id: "checkpoint-057",
    revision: 57,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 40,
  },
  {
    id: "checkpoint-058",
    revision: 58,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 41,
  },
  {
    id: "checkpoint-059",
    revision: 59,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 42,
  },
  {
    id: "checkpoint-060",
    revision: 60,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 43,
  },
  {
    id: "checkpoint-061",
    revision: 61,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 44,
  },
  {
    id: "checkpoint-062",
    revision: 62,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 45,
  },
  {
    id: "checkpoint-063",
    revision: 63,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 46,
  },
  {
    id: "checkpoint-064",
    revision: 64,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 47,
  },
  {
    id: "checkpoint-065",
    revision: 65,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 32,
  },
  {
    id: "checkpoint-066",
    revision: 66,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 33,
  },
  {
    id: "checkpoint-067",
    revision: 67,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 34,
  },
  {
    id: "checkpoint-068",
    revision: 68,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 35,
  },
  {
    id: "checkpoint-069",
    revision: 69,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 36,
  },
  {
    id: "checkpoint-070",
    revision: 70,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 37,
  },
  {
    id: "checkpoint-071",
    revision: 71,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 38,
  },
  {
    id: "checkpoint-072",
    revision: 72,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 39,
  },
  {
    id: "checkpoint-073",
    revision: 73,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 40,
  },
  {
    id: "checkpoint-074",
    revision: 74,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 41,
  },
  {
    id: "checkpoint-075",
    revision: 75,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 42,
  },
  {
    id: "checkpoint-076",
    revision: 76,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 43,
  },
  {
    id: "checkpoint-077",
    revision: 77,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 44,
  },
  {
    id: "checkpoint-078",
    revision: 78,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 45,
  },
  {
    id: "checkpoint-079",
    revision: 79,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 46,
  },
  {
    id: "checkpoint-080",
    revision: 80,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 47,
  },
  {
    id: "checkpoint-081",
    revision: 81,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 32,
  },
  {
    id: "checkpoint-082",
    revision: 82,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 33,
  },
  {
    id: "checkpoint-083",
    revision: 83,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 34,
  },
  {
    id: "checkpoint-084",
    revision: 84,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 35,
  },
  {
    id: "checkpoint-085",
    revision: 85,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 36,
  },
  {
    id: "checkpoint-086",
    revision: 86,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 37,
  },
  {
    id: "checkpoint-087",
    revision: 87,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 38,
  },
  {
    id: "checkpoint-088",
    revision: 88,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 39,
  },
  {
    id: "checkpoint-089",
    revision: 89,
    delivery: "delivered",
    changedDomain: "command",
    retainCount: 40,
  },
  {
    id: "checkpoint-090",
    revision: 90,
    delivery: "delivered",
    changedDomain: "agent",
    retainCount: 41,
  },
  {
    id: "checkpoint-091",
    revision: 91,
    delivery: "indeterminate",
    changedDomain: "route",
    retainCount: 42,
  },
  {
    id: "checkpoint-092",
    revision: 92,
    delivery: "delivered",
    changedDomain: "transition",
    retainCount: 43,
  },
  {
    id: "checkpoint-093",
    revision: 93,
    delivery: "delivered",
    changedDomain: "event",
    retainCount: 44,
  },
  {
    id: "checkpoint-094",
    revision: 94,
    delivery: "indeterminate",
    changedDomain: "execution",
    retainCount: 45,
  },
  {
    id: "checkpoint-095",
    revision: 95,
    delivery: "delivered",
    changedDomain: "host-port",
    retainCount: 46,
  },
  {
    id: "checkpoint-096",
    revision: 96,
    delivery: "delivered",
    changedDomain: "custody",
    retainCount: 47,
  },
  {
    id: "checkpoint-097",
    revision: 97,
    delivery: "indeterminate",
    changedDomain: "permission",
    retainCount: 32,
  },
  {
    id: "checkpoint-098",
    revision: 98,
    delivery: "delivered",
    changedDomain: "mcp",
    retainCount: 33,
  },
  {
    id: "checkpoint-099",
    revision: 99,
    delivery: "delivered",
    changedDomain: "skill",
    retainCount: 34,
  },
  {
    id: "checkpoint-100",
    revision: 100,
    delivery: "indeterminate",
    changedDomain: "plugin",
    retainCount: 35,
  },
  // __CHECKPOINT_CASES__
];

function payloads(value: CheckpointCase): Parameters<E02CheckpointBundleRuntime["stageAll"]>[1] {
  const domain = (name: string): { value: JsonObject; revision: number } => ({
    value: {
      domain: name,
      scenario_id: value.id,
      revision: value.revision,
      changed: name === value.changedDomain,
    },
    revision: value.revision,
  });
  return {
    permission: domain("permission"),
    mcp: domain("mcp"),
    skill: domain("skill"),
    plugin: domain("plugin"),
    command: domain("command"),
    agent: domain("agent"),
    route: domain("route"),
    transition: domain("transition"),
    event: domain("event"),
    execution: domain("execution"),
    "host-port": domain("host-port"),
    custody: domain("custody"),
  };
}

for (const value of cases) {
  test(`checkpoint matrix ${value.id} atomically commits and reconciles delivery`, () => {
    const bundles = new E02CheckpointBundleRuntime({ runtime });
    const open = bundles.begin(`matrix-${value.id}`, {
      scenario_id: value.id,
      changed_domain: value.changedDomain,
    });
    const staged = bundles.stageAll(open.bundleId, payloads(value));
    assert.equal(staged.records.length, 12);
    assert.equal(staged.missingDomains.length, 0);
    const sealed = bundles.seal(open.bundleId);
    assert.equal(sealed.phase, "sealed");
    const committed = bundles.commit(open.bundleId);
    assert.equal(committed.phase, "committed");
    assert.equal(committed.sequence, 1);
    assert.equal(bundles.verifyBundle(committed.bundleId).ok, true);
    assert.equal(bundles.verifyChain().ok, true);
    bundles.recordDeliveryAttempt(committed.bundleId);
    if (value.delivery === "delivered") {
      const receipt = bundles.markDelivered(committed.bundleId, {
        providerReceiptId: `provider-${value.id}`,
        deliveredSnapshotHash: `sha256:snapshot-${value.id}`,
      });
      assert.equal(receipt.ok, true);
      assert.equal(bundles.bundle(committed.bundleId)?.phase, "delivered");
    } else {
      const receipt = bundles.markDeliveryFailure(committed.bundleId, new Error(`delivery-${value.id}`));
      assert.equal(receipt.ok, false);
      assert.equal(bundles.pendingDelivery().length, 1);
      const reconciled = bundles.reconcileDelivery(committed.bundleId, {
        outcome: "not_delivered",
        actor: "matrix-verifier",
        reason: `provider absence ${value.id}`,
      });
      assert.equal(reconciled.phase, "committed");
    }
    const health = bundles.health();
    assert.equal(health.canonical_owner, "typescript");
    assert.equal(health.python_checkpoint_fallback, false);
    assert.ok(Number(health.bundle_count) >= 1);
  });
}
