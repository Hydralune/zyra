import { installEmergencyTerminalCleanup, TerminalSessionGuard } from "../../src/tui/terminal-session.ts"

installEmergencyTerminalCleanup()
const session = new TerminalSessionGuard(process.stdin, process.stdout)
session.enter()

await new Promise<void>((resolve, reject) => {
  process.stdout.write("\nZYRA_TERMINAL_CRASH_READY\n", (error) => error ? reject(error) : resolve())
})

if (process.env.ZYRA_TEST_WAIT_FOR_FORCE_KILL === "1") {
  await new Promise<never>(() => undefined)
}

await new Promise((resolve) => setTimeout(resolve, 25))
throw new Error("controlled terminal crash")
