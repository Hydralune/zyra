import { installEmergencyTerminalCleanup, TerminalSessionGuard } from "../../src/tui/terminal-session.ts"

installEmergencyTerminalCleanup()
const session = new TerminalSessionGuard(process.stdin, process.stdout)
session.enter()

await new Promise<void>((resolve, reject) => {
  process.stdout.write("\nZYRA_TERMINAL_CRASH_READY\n", (error) => error ? reject(error) : resolve())
})

await new Promise((resolve) => setTimeout(resolve, 25))
throw new Error("controlled terminal crash")
