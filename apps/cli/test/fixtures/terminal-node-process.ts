import { createInterface } from "node:readline"
import { TerminalNodeServer } from "../../src/terminal/server.ts"

const startupRoot = process.argv[2]
if (!startupRoot) throw new Error("terminal-node-process requires a startup root")

const server = await TerminalNodeServer.create({ startupRoot, maximumConcurrency: 4 })
await server.listen()
process.stdout.write(`${JSON.stringify({
  schema: "zyra.test-terminal-process/v1",
  backend_id: server.backendId,
  generation: server.generation,
  owner_id: server.ownerId,
  capability_token: server.capabilityToken,
  endpoint: server.endpoint,
})}\n`)

const input = createInterface({ input: process.stdin, crlfDelay: Infinity })
try {
  for await (const line of input) {
    const command = line.trim()
    if (!command) continue
    if (command === "drain") await server.drain("fixture drain")
    else if (command === "resume") server.resume()
    else if (command === "status") process.stdout.write(`${JSON.stringify(server.status())}\n`)
    else if (command === "stop") break
  }
} finally {
  input.close()
  await server.drain("fixture stopped")
  await server.settle(3_000)
  await server.close()
}
