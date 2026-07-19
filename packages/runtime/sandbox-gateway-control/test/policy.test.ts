import assert from "node:assert/strict";
import test from "node:test";

import {
  Effects,
  StructuredCommandPolicy,
  createCommandEnvelope,
} from "../src/index.ts";

function command(
  executable: string,
  argv: readonly string[] = [],
  options: {
    readonly networkProfile?: string;
    readonly metadata?: Record<string, boolean>;
  } = {},
) {
  return createCommandEnvelope({
    sessionId: "session-policy",
    runId: "run-policy",
    taskId: "task-policy",
    workerId: "CodeWorkerRuntime",
    toolUseId: "tool-policy",
    executable,
    argv,
    networkProfile: options.networkProfile,
    metadata: options.metadata,
  });
}

test("read-only Git is allowed and destructive Git is denied", () => {
  const policy = new StructuredCommandPolicy();
  const readOnly = policy.evaluate(command("git", ["status", "--short"]));
  const destructive = policy.evaluate(
    command("git", ["reset", "--hard", "HEAD"]),
  );

  assert.equal(readOnly.effect, Effects.allow);
  assert.equal(readOnly.eligibleForSealedAutoAllow, true);
  assert.equal(destructive.effect, Effects.deny);
  assert.ok(
    destructive.evidence.some((item) => item.code === "git.destructive"),
  );
});

test("shell redirection and encoded PowerShell fail closed", () => {
  const policy = new StructuredCommandPolicy();
  const redirected = policy.evaluate(
    command("cmd.exe", ["/c", "echo unsafe > bypass.txt"]),
  );
  const encoded = policy.evaluate(
    command("pwsh", ["-EncodedCommand", "ZQBjAGwAbwA="]),
  );

  assert.equal(redirected.effect, Effects.deny);
  assert.ok(
    redirected.evidence.some((item) => item.code === "shell.redirection"),
  );
  assert.equal(encoded.effect, Effects.deny);
  assert.ok(
    encoded.evidence.some(
      (item) => item.code === "shell.powershell_dynamic",
    ),
  );
});

test("sealed mode denies asks and ignores caller bypass", () => {
  const policy = new StructuredCommandPolicy();
  const decision = policy.evaluate(
    command("unknown-program", [], {
      metadata: { permissionBypass: true, autoApprove: true },
    }),
    { sealed: true },
  );

  assert.equal(decision.effect, Effects.deny);
  assert.ok(
    decision.evidence.some(
      (item) => item.code === "command.untrusted_override_ignored",
    ),
  );
  assert.ok(
    decision.evidence.some((item) => item.code === "sealed.ask_denied"),
  );
});

test("network profile permits exact hosts with approval and rejects others", () => {
  const policy = new StructuredCommandPolicy({
    allowNetworkProfiles: {
      docs: ["docs.example.test"],
    },
  });
  const allowed = policy.evaluate(
    command("curl", ["https://docs.example.test/page"], {
      networkProfile: "docs",
    }),
  );
  const denied = policy.evaluate(
    command("curl", ["https://collector.example.test/upload"], {
      networkProfile: "docs",
    }),
  );

  assert.equal(allowed.effect, Effects.ask);
  assert.equal(denied.effect, Effects.deny);
});

test("TypeScript policy is supplementary and names existing owners", () => {
  const descriptor = new StructuredCommandPolicy().descriptor();

  assert.equal(
    (descriptor as Record<string, unknown>).finalAuthority,
    "typescript.PermissionCoordinator",
  );
  assert.equal(
    (descriptor as Record<string, unknown>).canonicalGatewayOwner,
    "SandboxGatewayRuntime",
  );
  assert.equal((descriptor as Record<string, unknown>).role, "supplementary");
});
