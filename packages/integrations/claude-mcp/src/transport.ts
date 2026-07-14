import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface, type Interface } from "node:readline";

import {
  asObject,
  type JsonObject,
  type JsonRpcMessage,
  McpProtocolError,
  type McpHttpServerConfig,
  type McpStdioServerConfig,
  type McpTransport,
} from "./contracts.ts";

export class StdioMcpTransport implements McpTransport {
  readonly serverId: string;
  private readonly config: McpStdioServerConfig;
  private child: ChildProcessWithoutNullStreams | null = null;
  private reader: Interface | null = null;
  private connected = false;
  private closing = false;
  private stderrTail = "";

  constructor(config: McpStdioServerConfig) {
    this.config = config;
    this.serverId = config.id;
  }

  async connect(onMessage: (message: JsonRpcMessage) => void): Promise<void> {
    if (this.connected) {
      return;
    }
    this.closing = false;
    this.stderrTail = "";
    if (!Array.isArray(this.config.command) || this.config.command.length === 0) {
      throw new McpProtocolError(
        "invalid_stdio_command",
        "MCP stdio command must contain an executable",
        this.serverId,
      );
    }
    const [executable, ...args] = this.config.command;
    const environment = { ...process.env };
    for (const [target, sourceHandle] of Object.entries(this.config.envHandles ?? {})) {
      const value = process.env[sourceHandle];
      if (typeof value !== "string") {
        throw new McpProtocolError(
          "missing_auth_environment",
          `MCP environment handle ${sourceHandle} is unavailable`,
          this.serverId,
        );
      }
      environment[target] = value;
    }
    const child = spawn(executable, args, {
      cwd: this.config.cwd,
      env: environment,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.child = child;
    this.closing = false;
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      this.stderrTail = (this.stderrTail + chunk).slice(-4096);
    });
    child.once("error", (error) => {
      this.connected = false;
      if (!this.closing) {
        onMessage({
          jsonrpc: "2.0",
          method: "notifications/zyra_transport_error",
          params: { message: error.message },
        });
      }
    });
    child.once("exit", (code, signal) => {
      this.connected = false;
      if (!this.closing) {
        onMessage({
          jsonrpc: "2.0",
          method: "notifications/zyra_transport_closed",
          params: {
            code: code ?? -1,
            signal: signal ?? "",
            stderr: this.stderrTail,
          },
        });
      }
    });
    const reader = createInterface({ input: child.stdout, crlfDelay: Infinity });
    this.reader = reader;
    reader.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed) {
        return;
      }
      try {
        onMessage(JSON.parse(trimmed) as JsonRpcMessage);
      } catch (error) {
        onMessage({
          jsonrpc: "2.0",
          method: "notifications/zyra_protocol_error",
          params: {
            message: error instanceof Error ? error.message : String(error),
          },
        });
      }
    });
    this.connected = true;
  }

  async send(message: JsonRpcMessage): Promise<void> {
    if (!this.child || !this.connected || !this.child.stdin.writable) {
      throw new McpProtocolError(
        "transport_not_connected",
        "MCP stdio transport is not connected",
        this.serverId,
      );
    }
    await new Promise<void>((resolve, reject) => {
      this.child!.stdin.write(JSON.stringify(message) + "\n", (error) => {
        if (error) {
          reject(error);
        } else {
          resolve();
        }
      });
    });
  }

  isConnected(): boolean {
    return this.connected;
  }

  async close(): Promise<void> {
    this.closing = true;
    this.connected = false;
    this.reader?.close();
    this.reader = null;
    if (!this.child) {
      return;
    }
    const child = this.child;
    this.child = null;
    child.stdin.end();
    if (child.exitCode === null) {
      const exited = new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, 1_000);
        child.once("exit", () => {
          clearTimeout(timer);
          resolve();
        });
      });
      child.kill();
      await exited;
    }
  }
}

export class HttpMcpTransport implements McpTransport {
  readonly serverId: string;
  private readonly config: McpHttpServerConfig;
  private connected = false;
  private onMessage: ((message: JsonRpcMessage) => void) | null = null;

  constructor(config: McpHttpServerConfig) {
    this.config = config;
    this.serverId = config.id;
  }

  async connect(onMessage: (message: JsonRpcMessage) => void): Promise<void> {
    this.onMessage = onMessage;
    this.connected = true;
  }

  async send(message: JsonRpcMessage): Promise<void> {
    if (!this.connected || !this.onMessage) {
      throw new McpProtocolError(
        "transport_not_connected",
        "MCP HTTP transport is not connected",
        this.serverId,
      );
    }
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      ...(this.config.headers ?? {}),
    };
    if (this.config.bearerTokenEnv) {
      const token = process.env[this.config.bearerTokenEnv];
      if (!token) {
        throw new McpProtocolError(
          "missing_auth_environment",
          `MCP bearer token handle ${this.config.bearerTokenEnv} is unavailable`,
          this.serverId,
        );
      }
      headers.authorization = `Bearer ${token}`;
    }
    const response = await fetch(this.config.url, {
      method: "POST",
      headers,
      body: JSON.stringify(message),
    });
    if (!response.ok) {
      throw new McpProtocolError(
        "http_transport_error",
        `MCP HTTP transport returned ${response.status}`,
        this.serverId,
      );
    }
    if (!("id" in message)) {
      return;
    }
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("text/event-stream")) {
      const text = await response.text();
      for (const block of text.split(/\r?\n\r?\n/)) {
        const data = block.split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("\n");
        if (data) {
          this.onMessage(JSON.parse(data) as JsonRpcMessage);
        }
      }
      return;
    }
    const payload = asObject(await response.json());
    this.onMessage(payload as unknown as JsonRpcMessage);
  }

  isConnected(): boolean {
    return this.connected;
  }

  async close(): Promise<void> {
    this.connected = false;
    this.onMessage = null;
  }
}
