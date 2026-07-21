# Scheduler internalization notices

M1-S07A-01 selectively adapts worker lifecycle and durable queue mechanisms from the pinned
AgentScope and oh-my-pi source snapshots recorded in the internalization ledger. The resulting
implementation is modified, decomposed into Zyra-owned modules, and does not depend on either
source repository at runtime. Source repository license and copyright notices remain authoritative
for the cropped mechanisms.

- AgentScope (`b6698c5dbaa1aa916925e27402767f45e2405fa4`): lifecycle, inbox/wakeup, single-flight and
  cancellation ordering.
- oh-my-pi (`c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`): bounded attempt progress, drain, claim/requeue
  and external-worker protocol semantics.

No OpenClaw source, package, process, path, or runtime dependency is included in this slice.
