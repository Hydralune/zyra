# Phase Plans

Project-local milestone records live here. The canonical first-stage plan is now `../../docs/第一阶段总工程计划.md` plus the execution units under `../../docs/milestones/` in the parent workspace. The older `../../docs/第一阶段工程计划.md` is background.

The previous M0-M5 records are now historical subrecords of the consolidated new `M0: foundation and main-path bootstrap`. They are retained as implementation notes and verification entrypoints, not as proof that six heavyweight milestones were completed.

- `M0.md`: historical M0.0 engineering baseline.
- `M1.md`: historical M0.1 task graph and checkpoint control plane.
- `M2.md`: historical M0.2 worker runtime and tool governance.
- `M3.md`: historical M0.3 structured collaboration and neuro-symbolic control self-check.
- `M4-memory-compact-trajectory.md`: historical M0.4 memory fabric, context compact, and long trajectory replay self-check.
- `M5-scheduler-fault-recovery.md`: historical M0.5 resource scheduler, worker manifests, backend gateway, fault injection, and recovery planner self-check.
- `M0-M3-heavyweight-self-check.md`: historical audit of whether early M0.0-M0.3 work satisfied the heavyweight integration target.

Future work starts from the execution unit selected by the user, not from a whole milestone. Each unit must follow the source-to-target indices in `../../docs/比赛项目开源Agent架构借鉴分析.md` section `0.4`, the total plan in `../../docs/第一阶段总工程计划.md`, and its own `../../docs/milestones/**/unit-*.md` requirements. It must include code-internalization evidence: source repository modules reused, target `zyra` paths, adapter/runtime boundaries, verification commands, effective code line-count review, and remaining vendor-pool work.
