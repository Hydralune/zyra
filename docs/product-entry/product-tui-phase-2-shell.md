# Product TUI Phase 2/3 — Shell and canonical controls

## Shipped behavior

- `zyra` and `zyra "<goal>"` use the product projection and inline conversation shell.
- `zyra resume <task|session>` rebuilds the same product state from canonical snapshot/cursor state.
- The shell does not enter the alternate screen. TTY redraws are bounded to the visible terminal height; non-TTY output is emitted once at completion.
- The composer supports multiline input, bracketed paste, history, draft restore, external editing, resize, PageUp/PageDown, and explicit terminal-mode restoration.
- Enter redirects the running task immediately, Tab queues an instruction, and Escape sends an interrupt-mode canonical `/change` command.
- Queue, cancel, continue, redirect, interrupt, retry, and permission decisions reuse `CliControlSession` and `CliPermissionSession`; no local task or command queue was introduced.
- `/ui` launches the Web route for the current canonical task.
- SSE cursor recovery and generation replacement rebuild `ProductProjection`; raw `runtime.*` facts never reach the product renderer.
- The former append-only event transcript is retained at `zyra dev`, with read-only attachment available through `zyra events <task|session>`.

## Terminal policy

The first product release uses Codex-style inline rendering so the final answer remains in normal terminal scrollback. It deliberately avoids `DECSET 1049` alternate-screen sequences. TTY state is restored on submit, EOF, interrupt, normal completion, and errors.

## Verification

- `bun run typecheck:cli`
- `bun test ./apps/cli/test`
- `bun run build:cli`
- Product tests cover 80/120 columns, resize, multiline input, bracketed paste, scroll bounds, raw-mode restoration, final-answer fallback, raw-event filtering, SSE unavailability, and generation/snapshot recovery.
