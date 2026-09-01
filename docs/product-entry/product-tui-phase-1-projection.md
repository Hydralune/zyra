# Product TUI Phase 1：实时投影状态机

状态：完成。

`ProductProjection` 是产品 TUI 与 canonical transport 之间的唯一有状态边界。它持有 task snapshot、event generation、sequence、cursor 和 permission snapshot，再通过纯 `projectProductEvents` 重建完整 `ZyraUiEvent/v1` 序列。

## 保证

- exact replay 以 sequence + event identity 幂等去重；
- conflicting duplicate、reorder、gap 和 cross-task/generation frame fail closed；
- generation 变化只能通过 canonical snapshot replacement；
- snapshot replacement 先校验内部连续性，再原子替换旧状态；
- task terminal state 通过 canonical task refresh 收敛，不要求存在 raw terminal event；
- permission snapshot 由 canonical custody API 覆盖 raw permission event fallback；
- offline replay 与逐帧 online apply 产生相同产品事件序列；
- reconnecting/recovered 作为产品 transport state，不把底层异常文本倒入 transcript；
- `runtime.node.failed`、lease、route、audit、artifact 和 system message 不会升级为 task failure。

## 恢复流程

```text
SSE frame
  ├─ sequence 连续 → apply → 重建 ZyraUiEvent/v1
  ├─ exact replay  → no-op
  └─ gap / generation / binding failure
         ↓
  ingress capabilities
         ↓
  canonical snapshot replacement
         ↓
  cursor 恢复并继续消费
```

## 测试

`apps/cli/test/product-presentation.test.ts` 覆盖：

- real physical fixture 重建；
- duplicate/reorder/gap；
- generation replacement；
- online/offline convergence；
- missing raw terminal event；
- node failure 不误报；
- assistant final answer fallback；
- canonical permission deduplication；
- 80/120/40 列 rendering。

Phase 2/3 的 event loop 只能调用该状态机，不允许在 TUI widget 中新增 `runtime.*` 分支。
