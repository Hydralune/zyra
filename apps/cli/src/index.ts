#!/usr/bin/env node
import { runMain } from "./main.ts"

const exitCode = await runMain(process.argv.slice(2))
process.exitCode = exitCode
