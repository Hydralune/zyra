import { handleProtocolRequest } from "./protocol.ts";
import { RETRIEVAL_ALGORITHM_PROTOCOL } from "./types.ts";

async function readStandardInput(): Promise<string> {
  const chunks: Uint8Array[] = [];
  for await (const chunk of Bun.stdin.stream()) chunks.push(chunk);
  return new TextDecoder().decode(Buffer.concat(chunks));
}

async function main(): Promise<number> {
  const input = (await readStandardInput()).trim();
  if (!input) {
    process.stdout.write(`${JSON.stringify(handleProtocolRequest(null))}\n`);
    return 2;
  }
  let value: unknown;
  try {
    value = JSON.parse(input);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({
      protocol: RETRIEVAL_ALGORITHM_PROTOCOL,
      requestId: "unknown",
      ok: false,
      result: {},
      error: error instanceof Error ? error.message : String(error),
    })}\n`);
    return 2;
  }
  const response = handleProtocolRequest(value);
  process.stdout.write(`${JSON.stringify(response)}\n`);
  return response.ok ? 0 : 1;
}

process.exitCode = await main();
