import {
  runStdioRuntime,
  runtimeContract,
} from "../../../packages/runtime/claude-runtime/src/index.ts";

const flag = process.argv[2] ?? "--health";

function print(payload: unknown): void {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

if (flag === "--stdio") {
  await runStdioRuntime();
} else if (flag === "--health") {
  print(runtimeContract("health"));
} else if (flag === "--snapshot") {
  print(runtimeContract("snapshot"));
} else if (flag === "--inventory") {
  print(runtimeContract("inventory"));
} else if (flag === "--query-contract") {
  print(runtimeContract("query"));
} else if (flag === "--session-contract") {
  print(runtimeContract("session"));
} else if (flag === "--tool-loop-contract") {
  print(runtimeContract("tools"));
} else {
  print({
    ok: false,
    error: "unsupported_code_worker_command",
    command: flag,
  });
  process.exitCode = 2;
}
