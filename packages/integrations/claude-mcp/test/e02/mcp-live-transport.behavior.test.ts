import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "bun:test";

import {
  HttpMcpTransport,
  StdioMcpTransport,
  type JsonObject,
  type McpTransportRequest,
} from "../../src/index.ts";

function request(id: string, method: string, params: JsonObject): McpTransportRequest {
  return {
    requestId: `request-${id}`,
    method,
    message: { jsonrpc: "2.0", id, method, params },
    timeoutMs: 5_000,
    idempotent: true,
    idempotencyKey: `idempotency-${id}`,
    authorization: null,
    headers: {},
    metadata: { execution_id: "E02", live_transport: true },
  };
}

test("e02.live.mcp.stdio starts a real local child and commits an idempotent JSON-RPC response", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-live-stdio-"));
  const serverPath = join(root, "stdio-server.mjs");
  await writeFile(serverPath, [
    'import { createInterface } from "node:readline";',
    'const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });',
    'for await (const line of lines) {',
    '  const request = JSON.parse(line);',
    '  const response = { jsonrpc: "2.0", id: request.id, result: { method: request.method, params: request.params, process_id: process.pid } };',
    '  process.stdout.write(JSON.stringify(response) + "\\n");',
    '}',
  ].join("\n"), "utf8");

  const transport = new StdioMcpTransport({
    serverId: "e02-live-stdio",
    config: {
      kind: "stdio",
      command: process.execPath,
      arguments: [serverPath],
      cwd: root,
      environmentHandles: {},
      inheritEnvironment: ["PATH", "SystemRoot", "ComSpec", "PATHEXT"],
      stderrLimitBytes: 32_768,
    },
    connectTimeoutMs: 5_000,
    requestTimeoutMs: 5_000,
    shutdownTimeoutMs: 2_000,
  });
  try {
    const started = await transport.start();
    assert.equal(started.connected, true);
    assert.ok(started.processId && started.processId !== process.pid);

    const first = await transport.request(request("stdio-1", "tools/call", { value: 17 }));
    assert.equal(first.replayed, false);
    assert.deepEqual("result" in first.message ? first.message.result : null, {
      method: "tools/call",
      params: { value: 17 },
      process_id: started.processId,
    });
    const replay = await transport.request(request("stdio-1", "tools/call", { value: 17 }));
    assert.equal(replay.replayed, true);
    assert.equal(transport.snapshot().completed.length, 1);
    assert.equal(transport.health().completedRequests, 1);
  } finally {
    await transport.close("behavior-test-complete");
    await rm(root, { recursive: true, force: true });
  }
});

test("e02.live.mcp.http-sse calls a real loopback Streamable HTTP server and parses SSE", async () => {
  const observed: Array<{ method: string; authorization: string | undefined }> = [];
  const server = createServer(async (incoming, response) => {
    if (incoming.method === "DELETE") {
      response.writeHead(204, { "cache-control": "no-store" });
      response.end();
      return;
    }
    if (incoming.method !== "POST") {
      response.writeHead(405, { allow: "POST, DELETE" });
      response.end();
      return;
    }
    const chunks: Buffer[] = [];
    for await (const chunk of incoming) chunks.push(Buffer.from(chunk));
    const message = JSON.parse(Buffer.concat(chunks).toString("utf8")) as {
      id: string;
      method: string;
      params: Record<string, unknown>;
    };
    observed.push({
      method: message.method,
      authorization: incoming.headers.authorization,
    });
    const jsonRpc = JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { echoed: message.params, transport: "streamable-http-sse" },
    });
    response.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "mcp-session-id": "e02-live-http-session",
      "mcp-protocol-version": "2025-03-26",
      "cache-control": "no-store",
    });
    response.end(`id: event-${message.id}\nevent: message\ndata: ${jsonRpc}\n\n`);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address && typeof address === "object");
  const endpoint = `http://127.0.0.1:${address.port}/mcp`;
  const transport = new HttpMcpTransport({
    serverId: "e02-live-http-sse",
    config: {
      kind: "streamable_http",
      url: endpoint,
      headers: { "x-zyra-test": "e02-live" },
      credentialHandles: {},
      allowedRedirectOrigins: [],
      maximumRedirects: 0,
      preferSse: false,
    },
    connectTimeoutMs: 5_000,
    requestTimeoutMs: 5_000,
    authorizationProvider: async () => "Bearer e02-live-token",
  });
  try {
    const started = await transport.start();
    assert.equal(started.connected, true);
    assert.equal(started.endpoint, endpoint);
    const response = await transport.request(request("http-1", "resources/read", { uri: "zyra://live" }));
    assert.equal(response.replayed, false);
    assert.deepEqual("result" in response.message ? response.message.result : null, {
      echoed: { uri: "zyra://live" },
      transport: "streamable-http-sse",
    });
    assert.deepEqual(observed, [{ method: "resources/read", authorization: "Bearer e02-live-token" }]);
    const snapshot = transport.snapshot();
    assert.equal(snapshot.completed.length, 1);
    assert.ok(snapshot.events.some((event) => event.kind === "sse_event"));
    assert.equal(snapshot.metadata.session_id, "e02-live-http-session");
    assert.equal(snapshot.metadata.protocol_version, "2025-03-26");
  } finally {
    await transport.close("behavior-test-complete");
    server.close();
    server.closeAllConnections();
    await once(server, "close");
  }
});
