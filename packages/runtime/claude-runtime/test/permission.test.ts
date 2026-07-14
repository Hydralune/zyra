import assert from "node:assert/strict";
import test from "node:test";

import {
  argumentsDigest,
  TypeScriptPermissionEvaluator,
} from "../src/permission/index.ts";

const base = {
  runId: "run-1",
  taskId: "task-1",
  sessionId: "session-1",
  toolCallId: "tool-1",
  toolName: "shell",
  namespace: "builtin",
  serverId: "",
  version: "",
  schemaDigest: "",
  operation: "execute",
  arguments: { command: "git status" },
  metadata: {},
};

test("TypeScript permission evaluator binds rules to the exact request", () => {
  const evaluator = new TypeScriptPermissionEvaluator({
    mode: "default",
    interactive: true,
    headless: false,
    workspace_root: "C:/workspace",
    rules: [{
      rule_id: "rule-shell-deny",
      effect: "deny",
      source: "project",
      tool_pattern: "shell",
      namespace_pattern: "*",
      server_pattern: "*",
      operation_pattern: "execute",
      priority: 900,
      enabled: true,
      scope: { session_id: "session-1" },
    }],
  }, { sessionId: "session-1", workspaceRoot: "C:/workspace" });
  const decision = evaluator.evaluate(base);
  assert.equal(decision.canonical_owner, "typescript");
  assert.equal(decision.effect, "deny");
  assert.deepEqual(decision.matched_rule_ids, ["rule-shell-deny"]);
  assert.equal(decision.arguments_digest, argumentsDigest(base.arguments));
  assert.equal(decision.request_binding.tool_use_id, "tool-1");
  assert.match(String(decision.request_fingerprint), /^sha256:zyra-permission-request-v1:/);
});

test("sealed policy allows safe reads and denies mutations without pausing", () => {
  const evaluator = new TypeScriptPermissionEvaluator({
    mode: "sealed",
    interactive: false,
    headless: true,
    workspace_root: "C:/workspace",
    rules: [],
  }, { sessionId: "session-1", workspaceRoot: "C:/workspace" });
  const read = evaluator.evaluate({
    ...base,
    toolCallId: "read-1",
    toolName: "file_read",
    operation: "read",
    arguments: { path: "src/main.ts" },
  });
  const write = evaluator.evaluate({
    ...base,
    toolCallId: "write-1",
    toolName: "file_write",
    operation: "write",
    arguments: { path: "src/main.ts", content: "x" },
  });
  assert.equal(read.effect, "allow");
  assert.equal(write.effect, "deny");
  assert.notEqual(write.effect, "ask");
});

test("interactive MCP execution asks while autonomous MCP execution fails closed", () => {
  const interactive = new TypeScriptPermissionEvaluator({
    mode: "default",
    interactive: true,
    headless: false,
    rules: [],
  }, { sessionId: "session-1", workspaceRoot: "C:/workspace" });
  const autonomous = new TypeScriptPermissionEvaluator({
    mode: "auto",
    interactive: false,
    headless: true,
    rules: [],
  }, { sessionId: "session-1", workspaceRoot: "C:/workspace" });
  const request = {
    ...base,
    toolName: "mcp__peer__record",
    namespace: "mcp",
    serverId: "peer",
    arguments: { value: 1 },
  };
  assert.equal(interactive.evaluate(request).effect, "ask");
  assert.equal(autonomous.evaluate(request).effect, "deny");
});
