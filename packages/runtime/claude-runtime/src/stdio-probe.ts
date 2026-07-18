import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { PassThrough } from "node:stream";

import type { JsonObject } from "./contracts.ts";
import {
  createFrame,
  decodeFrame,
  FrameSequence,
  type RuntimeFrameKind,
} from "./protocol.ts";
import { runStdioRuntimeWithStreams } from "./stdio.ts";

export interface StdioProbeResult extends JsonObject {
  ok: boolean;
  entry: string;
  executable: string;
  runtime_frames: number;
  checkpoint_requests: number;
  closing_checkpoint_before_terminal: boolean;
  terminal_results: number;
  terminal_closed: number;
  requests_after_terminal_result: number;
  canonical_owner: string;
  terminal_protocol: string;
  python_fallback: boolean;
}

export async function runCurrentEntryStdioProbe(): Promise<StdioProbeResult> {
  const entry = resolve(process.argv[1] ?? "");
  if (!entry) throw new Error("stdio probe cannot resolve the current entry");
  const stateRoot = mkdtempSync(join(tmpdir(), "zyra-e04-stdio-probe-"));
  const workspace = join(stateRoot, "workspace");
  mkdirSync(workspace, { recursive: true });
  const runId = `e04-stdio-${basename(entry).replace(/[^a-z0-9]+/gi, "-")}`;
  const runtimeInput = new PassThrough();
  const runtimeOutput = new PassThrough();
  const outbound = new FrameSequence();
  const inbound = new FrameSequence();
  const send = (
    kind: RuntimeFrameKind,
    payload: JsonObject,
    correlationId = "",
  ): void => {
    runtimeInput.write(
      `${JSON.stringify(createFrame(outbound.next(), runId, kind, payload, correlationId))}\n`,
    );
  };
  let frames = 0;
  let checkpoints = 0;
  let closingCheckpointBeforeTerminal = false;
  let terminalResults = 0;
  let terminalClosed = 0;
  let requestsAfterTerminal = 0;
  let resultPayload: JsonObject = {};
  const seenKinds: string[] = [];
  const timeout = setTimeout(
    () => runtimeOutput.destroy(new Error(`stdio probe timed out after ${seenKinds.join(",")}`)),
    30_000,
  );
  try {
    const runtime = runStdioRuntimeWithStreams(
      runtimeInput,
      (line) => runtimeOutput.write(line),
    );
    send("run.start", {
      task_id: "e04-stdio-probe-task",
      node_id: "e04-stdio-probe-node",
      worker_request_id: "e04-stdio-probe-request",
      session_id: "e04-stdio-probe-session",
      messages: [{ role: "user", content: "Complete the empty deterministic probe." }],
      turns: [],
      tools: [],
      config: {
        maxTurns: 4,
        maxToolResultChars: 16_000,
        permissionPolicy: { mode: "sealed", default_effect: "allow" },
        runtimeConstraints: {
          workspaceRoot: workspace,
          e04_stdio_probe: true,
        },
        controlCommands: [],
      },
      session_seed: {},
      context_snapshot: {},
      restored_state: {},
      metadata: { e04_stdio_probe: true },
    });
    const lines = createInterface({ input: runtimeOutput, crlfDelay: Infinity });
    for await (const line of lines) {
      const frame = decodeFrame(line, runId);
      seenKinds.push(frame.kind);
      inbound.accept(frame.sequence);
      frames += 1;
      if (
        terminalResults > 0 &&
        [
          "runtime.checkpoint",
          "tool.batch.request",
          "tool.request",
          "tool.settle",
          "agent.mutate",
          "artifact.request",
        ].includes(frame.kind)
      )
        requestsAfterTerminal += 1;
      if (frame.kind === "runtime.checkpoint") {
        checkpoints += 1;
        const checkpointSnapshot = frame.payload.snapshot;
        const e02 = checkpointSnapshot
          && typeof checkpointSnapshot === "object"
          && !Array.isArray(checkpointSnapshot)
          ? checkpointSnapshot.e02
          : undefined;
        if (
          terminalResults === 0
          && e02
          && typeof e02 === "object"
          && !Array.isArray(e02)
          && e02.closing === true
        ) {
          closingCheckpointBeforeTerminal = true;
        }
        send(
          "runtime.checkpoint.result",
          { accepted: true, durable: true },
          frame.correlation_id,
        );
      } else if (frame.kind === "tool.batch.request") {
        const requests = Array.isArray(frame.payload.requests)
          ? frame.payload.requests
          : [];
        send(
          "tool.batch.result",
          {
            results: requests.map((request) => ({
              tool_call_id:
                request && typeof request === "object" && "tool_call_id" in request
                  ? String(request.tool_call_id)
                  : "",
              ok: false,
              summary: "stdio probe does not execute tools",
              output: {},
              artifacts: [],
              error: "unexpected_probe_tool",
              completed_at: new Date().toISOString(),
              metadata: {},
            })),
          },
          frame.correlation_id,
        );
      } else if (frame.kind === "tool.settle") {
        send(
          "tool.settle.result",
          {
            accepted: true,
            tool_call_id: String(frame.payload.tool_call_id ?? ""),
            canonical_permission_owner: "typescript",
          },
          frame.correlation_id,
        );
      } else if (frame.kind === "agent.mutate") {
        send(
          "agent.mutate.result",
          {
            accepted: true,
            task_id: String(frame.payload.task_id ?? ""),
            status: String(frame.payload.action ?? "acknowledged"),
            revision: Number(frame.payload.expected_revision ?? 0) + 1,
            error: "",
          },
          frame.correlation_id,
        );
      } else if (frame.kind === "artifact.request") {
        send(
          "artifact.result",
          {
            artifact: {
              artifact_id: `e04-probe-${String(frame.payload.request_id ?? "artifact")}`,
              kind: String(frame.payload.kind ?? "probe"),
              uri: "memory://e04-stdio-probe",
              title: String(frame.payload.title ?? "E04 stdio probe"),
              metadata: {},
            },
          },
          frame.correlation_id,
        );
      } else if (frame.kind === "run.result") {
        terminalResults += 1;
        resultPayload =
          frame.payload.result && typeof frame.payload.result === "object"
            ? (frame.payload.result as JsonObject)
            : {};
        send(
          "run.result.ack",
          {
            accepted: true,
            durable: true,
            terminal_id: String(frame.payload.terminal_id ?? ""),
            terminal_revision: Number(frame.payload.terminal_revision ?? 0),
          },
          frame.correlation_id,
        );
      } else if (frame.kind === "run.closed") {
        terminalClosed += 1;
        break;
      } else if (frame.kind === "runtime.error") {
        throw new Error(
          `stdio runtime failed: ${String(frame.payload.code)}: ${String(frame.payload.message)}`,
        );
      }
    }
    runtimeInput.end();
    await runtime;
    runtimeOutput.end();
    const metadata =
      resultPayload.metadata && typeof resultPayload.metadata === "object"
        ? (resultPayload.metadata as JsonObject)
        : {};
    const ok =
      resultPayload.ok === true &&
      terminalResults === 1 &&
      terminalClosed === 1 &&
      closingCheckpointBeforeTerminal &&
      requestsAfterTerminal === 0 &&
      metadata.canonical_permission_owner === "typescript" &&
      metadata.canonical_agent_owner === "typescript" &&
      metadata.python_policy_fallback === "false";
    if (!ok)
      throw new Error(
        `stdio probe invariant failed: entry=${entry} result=${terminalResults} closed=${terminalClosed} closing_checkpoint=${closingCheckpointBeforeTerminal} post_terminal=${requestsAfterTerminal}`,
      );
    return {
      ok: true,
      entry,
      executable: process.execPath,
      runtime_frames: frames,
      checkpoint_requests: checkpoints,
      closing_checkpoint_before_terminal: closingCheckpointBeforeTerminal,
      terminal_results: terminalResults,
      terminal_closed: terminalClosed,
      requests_after_terminal_result: requestsAfterTerminal,
      canonical_owner: "typescript",
      terminal_protocol: String(metadata.terminal_commit_protocol ?? ""),
      python_fallback: false,
    };
  } finally {
    clearTimeout(timeout);
    runtimeInput.destroy();
    runtimeOutput.destroy();
    rmSync(stateRoot, { recursive: true, force: true });
  }
}
