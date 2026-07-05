# Commands

Slash command parsing and dispatch lives here. Commands are parsed into `ControlCommand` and then recorded as events. The API returns immediate `command_result` views for observable surfaces such as `/status`, `/graph`, `/trace`, `/artifacts`, `/tools`, `/permissions`, `/help`, `/context`, `/cost`, and `/usage`. Stateful context commands update task checkpoint metadata, while `/compact` and `/export` also write task artifacts.

Current command groups:

- Runtime observation: `/status`, `/graph`, `/trace`, `/artifacts`, `/tools`, `/permissions`, `/help`, `/bashes`.
- Context and session: `/clear`, `/compact`, `/context`, `/rewind`, `/resume`, `/export`, `/memory`, `/init`.
- Model and resources: `/model`, `/doctor`, `/cost`, `/usage`.
- Extension and team control: `/mcp`, `/agents`, `/hooks`, `/plan`, `/goal`, `/team-onboarding`.
- Competition harness: `/inject`, `/change`, `/verify`, `/eval`.

Stateful M2 commands:

- `/change` maps to a `requirement_change` event.
- `/inject` maps to a `failure_injected` event.
- `/clear` freezes the current visible context into a resumable snapshot, starts a fresh visible context window, and keeps the durable event trace intact.
- `/rewind` restores the visible context window from a prior snapshot without deleting events.
- `/resume` lists resumable snapshots when called without arguments, or restores a matching snapshot/session when given an id.
- `/compact` writes a context compact summary artifact and records `metadata.compactions`.
- `/export` writes a run export artifact and records `metadata.exports`.
- `/memory` returns task memory, compaction/export records, requirement changes, failure injections, and current context session state.
- `/verify` and `/eval` run the lightweight trace evaluator and record `metadata.evaluations`.

Event-only commands are still stored in checkpoint metadata under `control_commands`, so future hook, goal, evaluator, and extension runtimes can consume them without changing the public command protocol.
