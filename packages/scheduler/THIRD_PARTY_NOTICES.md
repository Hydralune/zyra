# Scheduler internalization notices

M1-S07A-01 selectively adapts worker lifecycle and durable queue mechanisms from the pinned
AgentScope and oh-my-pi source snapshots recorded in the internalization ledger. The resulting
implementation is modified, decomposed into Zyra-owned modules, and does not depend on either
source repository at runtime. Source repository license and copyright notices remain authoritative
for the cropped mechanisms.

- AgentScope (`b6698c5dbaa1aa916925e27402767f45e2405fa4`): lifecycle, inbox/wakeup, single-flight and
  cancellation ordering.
- oh-my-pi (`c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`): TaskTool semaphore/concurrency and
  AsyncJob progress, park/revive, drain and cancellation control flow is cropped and modified in
  its original TypeScript under `packages/runtime/claude-runtime/src/omp-worker-control`; the
  Python WorkerPoolStore remains the sole durable attempt/lease/receipt owner. Bounded
  claim/requeue and external-worker protocol adaptation remains in the Zyra worker-pool modules.

No OpenClaw source, package, process, path, or runtime dependency is included in this slice.
