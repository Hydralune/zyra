import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "bun:test";

import type { JsonObject, RuntimeRunInput } from "../../src/contracts.ts";
import { E02CapabilityCoordinator } from "../../src/e02/coordinator.ts";

function runtimeInput(
  workspaceRoot: string,
  label: string,
  permissionPolicy: JsonObject,
): RuntimeRunInput {
  return {
    runId: `run-${label}`,
    taskId: `task-${label}`,
    workerRequestId: `worker-${label}`,
    sessionId: `session-${label}`,
    messages: [],
    turns: [],
    tools: [],
    config: {
      runtimeConstraints: {
        workspaceRoot,
        projectRoot: workspaceRoot,
        watchSkills: false,
        watchPlugins: false,
      },
      permissionPolicy,
    },
    restoredState: null,
    metadata: { session_revision: 0, test_label: label },
  };
}

async function shellDecision(
  coordinator: E02CapabilityCoordinator,
  workspaceRoot: string,
  label: string,
  command = "python3 -c \"print(1)\"",
) {
  const authorization = await coordinator.authorize({
    runId: `run-${label}`,
    taskId: `task-${label}`,
    sessionId: `session-${label}`,
    sessionRevision: 0,
    workerRequestId: `worker-${label}`,
    toolCallId: `call-${label}`,
    toolName: "shell",
    namespace: "builtin",
    operation: "execute",
    workspaceRoot,
    arguments: { command },
    metadata: { behavior_id: label },
    issueExecutionPermit: false,
  });
  return authorization.enforcement;
}

// A physical dispatch declares ``permission_interactive: false`` and
// ``permission_headless: true`` because nothing in an autonomous run can answer
// an approval prompt.  The coordinator used to derive interactivity from the
// mode name alone, so an acceptEdits dispatch stayed interactive, ASK survived
// as ASK, and the tool call suspended until the task failed.
test("headless policy converts an unanswerable ASK into a replannable denial", async () => {
  const workspaceRoot = await mkdtemp(join(tmpdir(), "zyra-headless-permission-"));
  try {
    const coordinator = await E02CapabilityCoordinator.open(runtimeInput(
      workspaceRoot,
      "headless",
      { mode: "acceptEdits", interactive: false, headless: true, rules: [] },
    ));
    try {
      const modes = coordinator.permission.evaluator.modes;
      assert.equal(modes.state.interactive, false);
      assert.equal(modes.state.headless, true);
      assert.equal(modes.canAsk(), false);
      assert.equal(modes.convertsAskToDeny(), true);

      // A settled denial returns to the model as a tool result it can act on;
      // a pending approval has nobody to answer it and strands the call.
      const enforcement = await shellDecision(coordinator, workspaceRoot, "headless");
      assert.equal(enforcement.decision.effect, "deny");
      assert.equal(enforcement.decision.reasonCode, "headless_ask_denied");
      assert.equal(enforcement.pendingApproval, false);

      // A high-risk command additionally carries the replan contract, so the
      // downstream recovery planner can route around the denied capability.
      const destructive = await shellDecision(
        coordinator,
        workspaceRoot,
        "headless-destructive",
        "rm -rf build",
      );
      assert.equal(destructive.decision.effect, "deny");
      assert.equal(destructive.decision.reasonCode, "headless_ask_denied");
      assert.equal(destructive.decision.replanRequired, true);
      assert.ok(destructive.decision.recoveryInput);
    } finally {
      await coordinator.close();
    }
  } finally {
    await rm(workspaceRoot, { recursive: true, force: true });
  }
});

// The same policy field must not silently disarm approval for a session that
// really does have an approver, so a policy that omits the flags keeps ASK.
test("a policy without the headless flags keeps ASK answerable", async () => {
  const workspaceRoot = await mkdtemp(join(tmpdir(), "zyra-interactive-permission-"));
  try {
    const coordinator = await E02CapabilityCoordinator.open(runtimeInput(
      workspaceRoot,
      "interactive",
      { mode: "acceptEdits", rules: [] },
    ));
    try {
      const modes = coordinator.permission.evaluator.modes;
      assert.equal(modes.state.interactive, true);
      assert.equal(modes.state.headless, false);
      assert.equal(modes.canAsk(), true);

      const enforcement = await shellDecision(coordinator, workspaceRoot, "interactive");
      assert.equal(enforcement.decision.effect, "ask");
      assert.equal(enforcement.decision.reasonCode, "default_unruled_ask");
    } finally {
      await coordinator.close();
    }
  } finally {
    await rm(workspaceRoot, { recursive: true, force: true });
  }
});
