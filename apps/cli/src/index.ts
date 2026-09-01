#!/usr/bin/env node
import { runMain } from "./main.ts"
import { emergencyTerminalCleanup, installEmergencyTerminalCleanup } from "./tui/terminal-session.ts"

const disposeTerminalCleanup = installEmergencyTerminalCleanup()
try {
  const exitCode = await runMain(process.argv.slice(2))
  process.exitCode = exitCode
} finally {
  emergencyTerminalCleanup()
  disposeTerminalCleanup()
}
