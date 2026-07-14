import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { McpClient, TypeScriptMcpRuntime } from "../src/index.ts";

const serverScript = fileURLToPath(
  new URL("../../../../tests/support/fake_mcp_server.py", import.meta.url),
);

test("TypeScript MCP client owns real stdio connection, catalogs, calls, resources, and prompts", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-ts-mcp-"));
  const statePath = join(root, "state.json");
  const client = new McpClient({
    id: "peer",
    transport: "stdio",
    command: [process.env.ZYRA_PYTHON ?? "python", serverScript, "--state", statePath],
    requestTimeoutMs: 5_000,
  });
  try {
    const catalog = await client.initialize();
    assert.equal(catalog.connected, true);
    assert.ok(catalog.tools.some((tool) => tool.name === "echo"));
    assert.ok(catalog.resources.some((resource) => resource.uri === "memo://live/status"));
    assert.ok(catalog.prompts.some((prompt) => prompt.name === "welcome"));

    const call = await client.callTool("echo", { message: "hello" });
    assert.ok(Array.isArray(call.content));
    const resource = await client.readResource("memo://live/status");
    assert.ok(Array.isArray(resource.contents));
    const prompt = await client.getPrompt("welcome", { name: "Zyra" });
    assert.ok(Array.isArray(prompt.messages));
  } finally {
    await client.close();
  }
  const persisted = JSON.parse(await readFile(statePath, "utf8"));
  assert.equal(persisted.tool_calls.echo, 1);
  await rm(root, { recursive: true, force: true });
});

test("TypeScript MCP runtime projects canonical tools without Python handlers", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-ts-mcp-runtime-"));
  const statePath = join(root, "state.json");
  const runtime = new TypeScriptMcpRuntime([{
    id: "peer",
    transport: "stdio",
    command: [process.env.ZYRA_PYTHON ?? "python", serverScript, "--state", statePath],
    requestTimeoutMs: 5_000,
  }]);
  try {
    await runtime.open();
    const names = runtime.toolSpecs().map((tool) => tool.name);
    assert.ok(names.includes("mcp__peer__echo"));
    const result = await runtime.execute("mcp__peer__echo", { message: "runtime" });
    assert.equal(result.metadata.canonical_runtime_owner, "typescript");
    assert.equal(result.metadata.capability_owner, "typescript-mcp");
  } finally {
    await runtime.close();
    await rm(root, { recursive: true, force: true });
  }
});
