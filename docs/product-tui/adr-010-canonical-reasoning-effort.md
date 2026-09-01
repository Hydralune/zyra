# ADR-010：模型推理强度属于 canonical task 配置

状态：已接受  
日期：2026-09-01

## 背景

Codex 的模型选择体验允许用户同时理解模型和推理强度。Zyra 先前的 `/model` 已能选择 canonical provider/model，但强度仅作为只读默认值展示；若 CLI 自行猜测可选值，界面选择可能被 provider 忽略或拒绝，也会形成与真实 task 脱节的本地状态。

## 决策

- provider control plane 的模型目录显式声明 `supportedReasoningEfforts`；集合只能来自 provider 官方文档核验，不按 UI 需要臆造。
- `/model` 先选择模型，再从该模型声明的集合选择强度；`provider default` 不写覆盖值。
- 选择只作用于后续新 task，并写入 `product_execution_config`。运行中 task 固定，状态页只读显示其 canonical metadata。
- API 在创建 task 时按当时可用的 canonical model catalog 校验 provider、model 和强度。未知强度 fail closed。
- physical dispatch 将已校验值放入 provider control plane 的 `extraBody`，由真实 OpenAI-compatible 请求序列化；它不是仅供 TUI 展示的 metadata。
- 未声明集合的 provider 仍可选模型，但 CLI 不提供未经验证的强度覆盖。

当前集合依据：

- DeepSeek Chat Completions：`low`、`high`、`max`，官方参考 <https://api-docs.deepseek.com/api/create-chat-completion/>。
- 智谱 GLM-5.2：`none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`，官方参考 <https://docs.bigmodel.cn/api-reference/模型-api/对话补全异步>。
- Kimi 当前 profile 未声明官方可覆盖集合，保持 provider 默认。

## 结果

推理强度具备一条可审计的端到端链路：目录能力 → TUI 选择 → task 配置 → API 校验 → physical provider 请求。新增 provider 时必须先补官方能力证据和传输测试，不能只增加 picker 文案。
