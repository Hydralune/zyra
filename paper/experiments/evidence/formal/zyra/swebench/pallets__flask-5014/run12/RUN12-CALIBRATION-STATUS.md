# run12 校准状态（不可计入正式成功率）

## 已核验事实

- 运行参数固定为：45 分钟外部截止、80 回合、声明的 80,000 总 Token 上限。
- 工作器在基准容器中提交了空名称 Blueprint 修复及其回归测试。
- 独立官方 SWE-bench 评测（`zyra-run12-patch-only-r3`）对冻结补丁返回 `resolved=1/1`：1 个 FAIL_TO_PASS 测试通过，60 个 PASS_TO_PASS 测试全部通过。
- 官方报告位于 `official-swebench-report/`；评分补丁为基准提交 `7ee9ceb` 到工作器提交 `967cfded` 的差异，未包含环境性 `pyproject.toml` 改动。

## 不能作为成功样本的原因

- 主任务没有产生正常终态：启动器退出后 API 为 502，任务仍停留在 `running`，没有可审计的完成回执。
- 控制面实际累计 Provider 用量为 `272,854` Token，超过声明上限；E01 层的预算限制未覆盖 Provider Control Plane 路径。

## 后续修复与准入条件

- 已将累计用量准入检查移至 Provider Control Plane 的真实请求入口，并将每请求输出上限固定为 4,096。
- 下一次运行只有同时满足“端到端终态回执存在、实际累计 Token 不超过上限、官方评分完成”时，才允许标注为正式样本。
