import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { McpClient } from "../src/index.ts";

const python = process.env.ZYRA_TEST_PYTHON
  ?? resolve(".venv", "Scripts", "python.exe");
const fakeServer = resolve("tests", "support", "fake_mcp_server.py");

function statePath(name: string): string {
  return join(mkdtempSync(join(tmpdir(), `zyra-mcp-${name}-`)), "state.json");
}

test("MCP initialization exposes a durable needs-auth state instead of silently falling back", async () => {
  const client = new McpClient({
    id: "auth-required",
    transport: "stdio",
    command: [python, fakeServer, "--state", statePath("auth"), "--require-auth"],
    requestTimeoutMs: 5_000,
  });
  try {
    await assert.rejects(client.initialize(), /authorization required/);
    const catalog = client.catalog();
    assert.equal(catalog.authStatus, "needs_auth");
    assert.equal(catalog.connected, false);
    assert.match(catalog.lastError, /authorization required/);
  } finally {
    await client.close();
  }
});

test("MCP list-change notifications invalidate and refresh every catalog domain", async () => {
  const client = new McpClient({
    id: "list-change",
    transport: "stdio",
    command: [python, fakeServer, "--state", statePath("refresh")],
    requestTimeoutMs: 5_000,
  });
  try {
    const initial = await client.initialize();
    assert.equal(initial.tools.some((tool) => tool.name === "inspect"), false);
    const initialGeneration = initial.generation;

    await client.callTool("expand_catalog", {});
    const refreshed = await client.refreshCatalog();

    assert.equal(refreshed.tools.some((tool) => tool.name === "inspect"), true);
    assert.equal(
      refreshed.resources.some((resource) => resource.uri === "memo://live/expanded"),
      true,
    );
    assert.equal(refreshed.prompts.some((prompt) => prompt.name === "expanded"), true);
    assert.ok(refreshed.generation > initialGeneration);
  } finally {
    await client.close();
  }
});

test("MCP reconnect replaces the stdio process and rebuilds a live catalog", async () => {
  const state = statePath("reconnect");
  const client = new McpClient({
    id: "reconnect",
    transport: "stdio",
    command: [python, fakeServer, "--state", state],
    requestTimeoutMs: 5_000,
  });
  try {
    const initial = await client.initialize();
    const initialPid = Number(JSON.parse(readFileSync(state, "utf8")).pid);
    const reconnected = await client.reconnect();
    const replacementPid = Number(JSON.parse(readFileSync(state, "utf8")).pid);

    assert.equal(initial.connected, true);
    assert.equal(reconnected.connected, true);
    assert.equal(reconnected.authStatus, "ready");
    assert.notEqual(replacementPid, initialPid);
    assert.ok(reconnected.generation > initial.generation);
  } finally {
    await client.close();
  }
});

test("MCP HTTP transport performs authenticated JSON-RPC lifecycle and tool calls", async () => {
  const methods: string[] = [];
  let authorization = "";
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
    request.on("end", () => {
      const message = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      methods.push(String(message.method ?? ""));
      authorization = String(request.headers.authorization ?? authorization);
      if (message.id === undefined) {
        response.writeHead(204).end();
        return;
      }
      const result = message.method === "initialize"
        ? {
          protocolVersion: "2025-06-18",
          serverInfo: { name: "zyra-http-test", version: "1" },
          capabilities: { tools: {}, resources: {}, prompts: {} },
        }
        : message.method === "tools/list"
          ? {
            tools: [{
              name: "echo",
              description: "HTTP echo",
              inputSchema: { type: "object" },
            }],
          }
          : message.method === "resources/list"
            ? { resources: [] }
            : message.method === "prompts/list"
              ? { prompts: [] }
              : message.method === "tools/call"
                ? { content: [{ type: "text", text: String(message.params?.arguments?.message ?? "") }] }
                : {};
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  process.env.ZYRA_MCP_HTTP_TEST_TOKEN = "opaque-test-token";
  const client = new McpClient({
    id: "http",
    transport: "http",
    url: `http://127.0.0.1:${address.port}/mcp`,
    bearerTokenEnv: "ZYRA_MCP_HTTP_TEST_TOKEN",
    requestTimeoutMs: 5_000,
  });
  try {
    const catalog = await client.initialize();
    const result = await client.callTool("echo", { message: "over-http" });
    assert.equal(catalog.connected, true);
    assert.equal(catalog.tools[0]?.name, "echo");
    assert.equal(authorization, "Bearer opaque-test-token");
    assert.deepEqual(methods.slice(0, 5), [
      "initialize",
      "notifications/initialized",
      "tools/list",
      "resources/list",
      "prompts/list",
    ]);
    assert.equal(JSON.stringify(result).includes("over-http"), true);
  } finally {
    delete process.env.ZYRA_MCP_HTTP_TEST_TOKEN;
    await client.close();
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    });
  }
});
