import { afterEach, describe, expect, test } from "bun:test"
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  canonicalJson,
  checksum,
  FRAME_KINDS,
  type TerminalFrame,
} from "../src/terminal/contracts.ts"
import { TerminalNodeRegistration } from "../src/terminal/registration.ts"
import { TerminalNodeLifecycle } from "../src/terminal/lifecycle.ts"
import { TerminalNodeServer } from "../src/terminal/server.ts"

const servers: TerminalNodeServer[] = []
const roots: string[] = []

afterEach(async () => {
  for (const server of servers.splice(0)) {
    await server.drain("test cleanup")
    await server.settle(1_000)
    await server.close()
  }
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true })
})

async function fixture(): Promise<{ server: TerminalNodeServer; root: string; workspace: string; artifacts: string }> {
  const root = await mkdtemp(join(tmpdir(), "zyra-terminal-test-"))
  const workspace = join(root, "workspace")
  const artifacts = join(root, "artifacts")
  await mkdir(workspace)
  await mkdir(artifacts)
  const server = await TerminalNodeServer.create({ startupRoot: root, maximumConcurrency: 4 })
  await server.listen()
  roots.push(root)
  servers.push(server)
  return { server, root, workspace, artifacts }
}

function request(server: TerminalNodeServer, input: {
  workspace: string
  artifacts: string
  toolName: string
  arguments: Record<string, unknown>
  envelopeId?: string
  toolCallId?: string
}): string {
  const toolCallId = input.toolCallId ?? `tool_${crypto.randomUUID()}`
  const operation = `tool.${input.toolName}`
  const payload = {
    schema: "zyra.terminal-action/v1",
    tool_name: input.toolName,
    tool_call_id: toolCallId,
    arguments: input.arguments,
    metadata: { session_id: "session-test" },
    permission: { receipt_id: `permit_${toolCallId}`, tool_call_id: toolCallId, allowed: true },
  }
  const envelopeBody = {
    schema: "zyra.backend-dispatch-envelope/v2",
    envelope_id: input.envelopeId ?? `envelope_${crypto.randomUUID()}`,
    run_id: "run-terminal-test",
    task_id: "task-terminal-test",
    node_id: "node-terminal-test",
    turn_id: "turn-terminal-test",
    runtime_worker: "CodeWorkerRuntime",
    backend_lease_id: `lease_${crypto.randomUUID()}`,
    backend_id: server.backendId,
    backend_kind: "edge_http",
    backend_location: "local",
    workspace_root: input.workspace,
    artifact_root: input.artifacts,
    provider_route_id: "route-terminal-test",
    provider_route_checksum: `sha256:${"1".repeat(64)}`,
    provider_catalog_revision: 1,
    provider_credential_version: 1,
    provider_credential_fingerprint: `sha256:${"2".repeat(64)}`,
    provider_transport_id: "transport-terminal-test",
    m0_execution_ref: `tool_call:${toolCallId}`,
    physical_worker_lease_ref: null,
    idempotency_key: `terminal-action:${toolCallId}`,
    deadline_at: Date.now() / 1000 + 60,
    attempt: 1,
    previous_envelope_id: null,
    created_at: Date.now() / 1000,
    metadata: { source: "terminal-test" },
  }
  const envelope = { ...envelopeBody, checksum: checksum(envelopeBody) }
  return canonicalJson({
    schema: "zyra.backend-transport-request/v1",
    envelope,
    operation,
    payload,
    input_digest: checksum({ m0_execution_ref: envelope.m0_execution_ref, operation, payload }),
  })
}

async function dispatch(server: TerminalNodeServer, source: string): Promise<{ response: Response; frames: TerminalFrame[] }> {
  const value = JSON.parse(source) as Record<string, any>
  const response = await fetch(`${server.endpoint}/v1/dispatch`, {
    method: "POST",
    headers: dispatchHeaders(value),
    body: source,
  })
  const body = await response.text()
  const frames = body.trim().split(/\r?\n/u).filter(Boolean).map((line) => JSON.parse(line) as TerminalFrame)
  return { response, frames }
}

function dispatchHeaders(value: Record<string, any>): Record<string, string> {
  return {
    "content-type": "application/json",
    "idempotency-key": value.envelope.idempotency_key,
    "x-zyra-envelope-id": value.envelope.envelope_id,
    "x-zyra-backend-lease-id": value.envelope.backend_lease_id,
    "x-zyra-provider-route-ref": value.envelope.provider_route_id,
    "x-zyra-m0-execution-ref": value.envelope.m0_execution_ref,
  }
}

function expectFrames(frames: TerminalFrame[]): void {
  expect(frames.length).toBeGreaterThanOrEqual(3)
  for (const [index, frame] of frames.entries()) {
    expect(frame.sequence).toBe(index + 1)
    expect(FRAME_KINDS).toContain(frame.kind)
    const { digest, ...body } = frame
    expect(digest).toBe(checksum(body))
  }
}

describe("FE-S04 terminal node", () => {
  test("binds only loopback and hides the per-start capability token", async () => {
    const { server } = await fixture()
    expect(server.endpoint).toStartWith("http://127.0.0.1:")
    expect(server.capabilityToken.length).toBeGreaterThanOrEqual(43)
    expect(JSON.stringify(server.status())).not.toContain(server.capabilityToken)
    expect(server.safeEndpoint).not.toContain(server.capabilityToken)

    const health = await fetch(`${server.endpoint}/health`)
    expect(health.status).toBe(200)
    const projection = await health.json() as Record<string, unknown>
    expect(projection.schema).toBe("zyra.backend-health/v1")
    expect(projection.backend_id).toBe(server.backendId)
    expect(projection.generation).toBe(server.generation)
    expect(projection.accepting).toBe(true)

    const wrong = await fetch(server.endpoint.replace(server.capabilityToken, "wrong-capability-token") + "/health")
    expect(wrong.status).toBe(404)
    expect(await wrong.text()).not.toContain(server.capabilityToken)
    const missing = await fetch(new URL("/health", server.endpoint))
    expect(missing.status).toBe(404)
    const externalHost = await fetch(`${server.endpoint}/health`, { headers: { host: "192.0.2.1" } })
    expect(externalHost.status).toBe(404)
  })

  test("executes permission-bound file and artifact actions with validated frames", async () => {
    const { server, workspace, artifacts } = await fixture()
    const replayToolCallId = `tool_${crypto.randomUUID()}`
    const writeSource = request(server, {
      workspace,
      artifacts,
      toolName: "file_write",
      arguments: { path: "src/proof.txt", content: "terminal-dispatch-proof" },
      toolCallId: replayToolCallId,
    })
    const write = await dispatch(server, writeSource)
    expect(write.response.status).toBe(200)
    expectFrames(write.frames)
    expect(write.frames.map((frame) => frame.kind)).toEqual(["accepted", "progress", "result"])
    const writeResult = write.frames.at(-1)?.payload as Record<string, any>
    expect(writeResult.ok).toBe(true)
    expect(writeResult.metadata.workspace_mutation_committed).toBe("true")
    expect(writeResult.metadata.workspace_path_disposition).toBe("created")
    expect(writeResult.metadata.workspace_path_created).toBe("true")
    expect(writeResult.output.path).toBe("src/proof.txt")
    expect(await readFile(join(workspace, "src", "proof.txt"), "utf8")).toBe("terminal-dispatch-proof")
    const replay = await dispatch(server, writeSource)
    expect(replay.response.headers.get("x-zyra-replayed")).toBe("true")
    expect(replay.frames).toEqual(write.frames)
    const collision = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "file_write",
      arguments: { path: "src/proof.txt", content: "different-input" },
      toolCallId: replayToolCallId,
    }))
    expect(collision.response.status).toBe(409)
    expect(await readFile(join(workspace, "src", "proof.txt"), "utf8")).toBe("terminal-dispatch-proof")

    const read = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "file_read",
      arguments: { path: "src/proof.txt" },
    }))
    expect((read.frames.at(-1)?.payload.output as Record<string, unknown>).content).toBe("terminal-dispatch-proof")
    const edit = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "file_edit",
      arguments: { path: "src/proof.txt", old: "terminal-dispatch", new: "terminal-search" },
    }))
    const editResult = edit.frames.at(-1)?.payload as Record<string, any>
    expect(editResult.ok).toBe(true)
    expect(editResult.metadata.workspace_mutation_committed).toBe("true")
    expect(editResult.metadata.workspace_path_disposition).toBe("modified")
    const search = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "web_search",
      arguments: { query: "terminal-search", paths: ["src"] },
    }))
    const searchOutput = search.frames.at(-1)?.payload.output as Record<string, any>
    expect(searchOutput.matches[0].relative_path).toBe("src/proof.txt")
    const deleted = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "file_delete",
      arguments: { path: "src/proof.txt" },
    }))
    const deleteResult = deleted.frames.at(-1)?.payload as Record<string, any>
    expect(deleteResult.ok).toBe(true)
    expect(deleteResult.metadata.workspace_mutation_committed).toBe("true")
    expect(deleteResult.metadata.workspace_path_disposition).toBe("deleted")
    await expect(readFile(join(workspace, "src", "proof.txt"), "utf8")).rejects.toThrow()

    const artifact = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "artifact_write",
      arguments: { title: "proof", extension: ".txt", content: "artifact-proof" },
    }))
    expectFrames(artifact.frames)
    expect(artifact.frames.map((frame) => frame.kind)).toEqual(["accepted", "progress", "artifact", "result"])
    expect(JSON.stringify(artifact.frames)).not.toContain(server.startupRoot)
  })

  test("streams stdout, stderr, and heartbeat frames from a real local process", async () => {
    const { server, workspace, artifacts } = await fixture()
    const command = process.platform === "win32"
      ? "Write-Output 'terminal-stdout'; Write-Output ((Get-Location).Path); [Console]::Error.WriteLine('terminal-stderr'); Start-Sleep -Milliseconds 2200"
      : "printf 'terminal-stdout\\n'; pwd; printf 'terminal-stderr\\n' >&2; sleep 2.2"
    const shell = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "shell",
      arguments: { command, timeout_seconds: 10 },
    }))
    expectFrames(shell.frames)
    const kinds = shell.frames.map((frame) => frame.kind)
    expect(kinds).toContain("stdout")
    expect(kinds).toContain("stderr")
    expect(kinds).toContain("heartbeat")
    expect(kinds.at(-1)).toBe("result")
    expect(JSON.stringify(shell.frames)).not.toContain(workspace)
    expect(JSON.stringify(shell.frames)).toContain("[redacted]")
  }, 8_000)

  test("fails closed for path escape, unknown action, digest mutation, and request overflow", async () => {
    const { server, workspace, artifacts } = await fixture()
    const escaped = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "file_write",
      arguments: { path: "../escape.txt", content: "blocked" },
    }))
    expectFrames(escaped.frames)
    expect(escaped.frames.at(-1)?.kind).toBe("error")
    expect(JSON.stringify(escaped.frames.at(-1))).toContain("workspace_attestation_failed")

    const external = await mkdtemp(join(tmpdir(), "zyra-terminal-external-"))
    roots.push(external)
    await writeFile(join(external, "secret.txt"), "outside")
    await symlink(external, join(workspace, "outside"), process.platform === "win32" ? "junction" : "dir")
    const symlinkEscape = await dispatch(server, request(server, {
      workspace,
      artifacts,
      toolName: "file_read",
      arguments: { path: "outside/secret.txt" },
    }))
    expect(symlinkEscape.frames.at(-1)?.kind).toBe("error")
    expect(JSON.stringify(symlinkEscape.frames.at(-1))).toContain("workspace_attestation_failed")

    const notDirectory = join(server.startupRoot, "artifact-root-is-file")
    await writeFile(notDirectory, "not a directory")
    const invalidArtifactRoot = await dispatch(server, request(server, {
      workspace,
      artifacts: notDirectory,
      toolName: "file_read",
      arguments: { path: "missing.txt" },
    }))
    expect(invalidArtifactRoot.frames.at(-1)?.kind).toBe("error")
    expect(JSON.stringify(invalidArtifactRoot.frames.at(-1))).toContain("workspace_attestation_failed")

    const unknown = await dispatch(server, request(server, { workspace, artifacts, toolName: "unknown", arguments: {} }))
    expect(unknown.frames.at(-1)?.kind).toBe("error")
    expect(JSON.stringify(unknown.frames.at(-1))).toContain("unsupported_operation")

    const permissionSource = request(server, { workspace, artifacts, toolName: "file_read", arguments: { path: "missing.txt" } })
    const permissionMismatch = JSON.parse(permissionSource) as Record<string, any>
    permissionMismatch.payload.permission.tool_call_id = "different-tool-call"
    permissionMismatch.input_digest = checksum({
      m0_execution_ref: permissionMismatch.envelope.m0_execution_ref,
      operation: permissionMismatch.operation,
      payload: permissionMismatch.payload,
    })
    const permissionRejected = await dispatch(server, canonicalJson(permissionMismatch))
    expect(permissionRejected.frames.at(-1)?.kind).toBe("error")
    expect(JSON.stringify(permissionRejected.frames.at(-1))).toContain("permission_denied")

    const source = request(server, { workspace, artifacts, toolName: "file_read", arguments: { path: "missing.txt" } })
    const mutated = JSON.parse(source) as Record<string, any>
    mutated.input_digest = `sha256:${"0".repeat(64)}`
    const digestResponse = await fetch(`${server.endpoint}/v1/dispatch`, {
      method: "POST",
      headers: dispatchHeaders(mutated),
      body: canonicalJson(mutated),
    })
    expect(digestResponse.status).toBe(400)
    expect(await digestResponse.text()).toContain("backend_protocol")

    const small = await TerminalNodeServer.create({ startupRoot: server.startupRoot, maximumRequestBytes: 256 })
    await small.listen()
    servers.push(small)
    const overflow = await fetch(`${small.endpoint}/v1/dispatch`, { method: "POST", body: "x".repeat(257) })
    expect(overflow.status).toBe(413)
  })

  test("supports dispatch and envelope cancellation plus drain and resume", async () => {
    const { server, workspace, artifacts } = await fixture()
    const envelopeId = `envelope_${crypto.randomUUID()}`
    const source = request(server, {
      workspace,
      artifacts,
      toolName: "shell",
      arguments: { command: process.platform === "win32" ? "Start-Sleep -Seconds 30" : "sleep 30" },
      envelopeId,
    })
    const parsed = JSON.parse(source) as Record<string, any>
    const responsePromise = fetch(`${server.endpoint}/v1/dispatch`, {
      method: "POST",
      headers: dispatchHeaders(parsed),
      body: source,
    })
    const deadline = Date.now() + 3_000
    while (server.activeDispatches === 0 && Date.now() < deadline) await Bun.sleep(10)
    expect(server.activeDispatches).toBe(1)
    const cancel = await fetch(`${server.endpoint}/v1/envelopes/${encodeURIComponent(envelopeId)}/cancel`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason: "test envelope cancel" }),
    })
    expect(cancel.status).toBe(200)
    const response = await responsePromise
    const frames = (await response.text()).trim().split(/\r?\n/u).filter(Boolean).map((line) => JSON.parse(line) as TerminalFrame)
    expectFrames(frames)
    expect(frames.at(-1)?.kind).toBe("cancelled")

    const completed = await dispatch(server, request(server, { workspace, artifacts, toolName: "file_write", arguments: { path: "cancel-target.txt", content: "ready" } }))
    const dispatchId = (completed.frames[0].payload as Record<string, string>).dispatch_id
    const dispatchCancel = await fetch(`${server.endpoint}/v1/dispatches/${encodeURIComponent(dispatchId)}/cancel`, { method: "POST", body: "{}" })
    expect(dispatchCancel.status).toBe(200)

    const drain = await fetch(`${server.endpoint}/v1/control/drain`, { method: "POST", body: JSON.stringify({ reason: "test drain" }) })
    expect(drain.status).toBe(200)
    expect((await drain.json() as Record<string, unknown>).accepting).toBe(false)
    const rejected = await dispatch(server, request(server, { workspace, artifacts, toolName: "file_read", arguments: { path: "cancel-target.txt" } }))
    expect(rejected.response.status).toBe(503)
    const resume = await fetch(`${server.endpoint}/v1/control/resume`, { method: "POST", body: "{}" })
    expect(resume.status).toBe(200)
    expect((await resume.json() as Record<string, unknown>).accepting).toBe(true)
  })

  test("registers and disables only its own EDGE_HTTP LOCAL definition with revision fencing", async () => {
    const { server } = await fixture()
    let revision = 7
    const writes: Array<Record<string, any>> = []
    const fakeFetch = (async (_input: URL | RequestInfo, init?: RequestInit | BunFetchRequestInit) => {
      if (init?.method === "POST") {
        const value = JSON.parse(String(init.body)) as Record<string, any>
        expect(value.expected_revision).toBe(revision)
        writes.push(value)
        revision += 1
        return Response.json({ ok: true, schema: "zyra.provider-backend-api/v1", result: { backend_id: server.backendId, registry_revision: revision } })
      }
      return Response.json({ ok: true, schema: "zyra.provider-backend-api/v1", result: { registry_revision: revision } })
    }) as typeof fetch
    const registration = new TerminalNodeRegistration(server, { baseUrl: "http://127.0.0.1:8000", fetch: fakeFetch })
    const enabled = await registration.enable()
    expect(enabled.enabled).toBe(true)
    expect(writes[0].backend.kind).toBe("edge_http")
    expect(writes[0].backend.location).toBe("local")
    expect(writes[0].backend.metadata.execution_mode).toBe("terminal_http")
    expect(writes[0].backend.metadata.real_terminal_dispatch_claimed).toBe(true)
    expect(writes[0].backend.metadata.real_edge_dispatch_claimed).toBeUndefined()
    expect(JSON.stringify(writes[0].backend.metadata)).not.toContain(server.capabilityToken)
    const disabled = await registration.disable()
    expect(disabled.enabled).toBe(false)
    expect(writes[1].backend.enabled).toBe(false)
    expect(writes[1].backend.backend_id).toBe(writes[0].backend.backend_id)
    expect(writes[1].terminal_registration.owner_id).toBe(writes[0].terminal_registration.owner_id)
  })

  test("shutdown bounds terminal deregistration to one conflict attempt", async () => {
    const root = await mkdtemp(join(tmpdir(), "zyra-terminal-lifecycle-test-"))
    roots.push(root)
    let revision = 3
    let enabled = true
    let disableAttempts = 0
    const fakeFetch = (async (_input: URL | RequestInfo, init?: RequestInit | BunFetchRequestInit) => {
      if (init?.method !== "POST") {
        return Response.json({
          ok: true,
          schema: "zyra.provider-backend-api/v1",
          result: { registry_revision: revision },
        })
      }
      const body = JSON.parse(String(init.body)) as Record<string, any>
      if (body.backend.enabled === false) {
        disableAttempts += 1
        return Response.json({
          ok: false,
          schema: "zyra.provider-backend-api/v1",
          error: "provider_backend_operation_rejected",
          message: "Terminal backend must drain active leases before disable.",
        }, { status: 409 })
      }
      enabled = true
      revision += 1
      return Response.json({
        ok: true,
        schema: "zyra.provider-backend-api/v1",
        result: { backend_id: body.backend.backend_id, registry_revision: revision },
      })
    }) as typeof fetch
    const lifecycle = await TerminalNodeLifecycle.create({
      baseUrl: "http://127.0.0.1:8000",
      startupRoot: root,
      fetch: fakeFetch,
      shutdownTimeoutMs: 250,
    })
    servers.push(lifecycle.server)
    await lifecycle.start()
    expect(enabled).toBe(true)

    const started = performance.now()
    await expect(lifecycle.stop()).rejects.toThrow("drain active leases")
    expect(performance.now() - started).toBeLessThan(1_000)
    expect(disableAttempts).toBe(1)
    expect(lifecycle.server.status().accepting).toBe(false)
  })
})
