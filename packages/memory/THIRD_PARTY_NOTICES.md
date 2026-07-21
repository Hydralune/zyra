# Retrieval and Index Source Notices

M1-S06A-01 adapts selected design and implementation mechanisms from these
source repositories into Zyra-owned modules. The parent repositories are not
runtime dependencies and their complete source trees are not redistributed in
this package.

- AgentScope, commit `b6698c5dbaa1aa916925e27402767f45e2405fa4`, Apache License 2.0:
  document/knowledge boundaries and the index worker/task/sweeper lifecycle.
- oh-my-pi, commit `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`, MIT License:
  bounded MMR, query-intent, temporal, polyphonic retrieval and derived-vector
  rebuild mechanisms.

The corresponding source paths, target bindings, roles, tests and exclusions
are recorded in Zyra's internalization ledger under owner `M1-S06A-01`.

## Memory curator foundation

M1-S06B-01 adapts selected implementation mechanisms into Zyra-owned curator
modules. Neither source repository is a runtime path dependency.

- Hermes Agent, commit `44ddc552f5e054759a6970af8997ea588a9d81c9`, MIT License:
  memory-provider/manager lifecycle, serialized background work, protected
  trajectory extraction and deterministic compression degradation.
- oh-my-pi, commit `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`, MIT License:
  ownership-token/lease/heartbeat/retry watermarks and bounded candidate
  consolidation/veracity mechanisms, retained in TypeScript.

The corresponding paths, target bindings, roles and behavioral tests are
recorded under owner `M1-S06B-01`. OMP storage/filesystem memory and model-to-
file writes are excluded; the retained TypeScript process receives no database
path and cannot author canonical memory.
