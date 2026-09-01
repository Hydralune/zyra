# ADR-012：首次使用引导消费 canonical readiness，不收集 secret

状态：已接受  
日期：2026-09-01

## 背景

Codex 在进入 composer 前有 startup orchestration 与 preflight。Zyra 先前能自动启动 daemon，但首次使用者看不到 workspace、runtime readiness、可用 provider/model 和权限边界；错误只能在提交任务后暴露。

## 决策

- 首次真实 TTY、无参数启动时，在同一产品 shell 中显示 workspace、daemon/API、runtime owner readiness、canonical provider/model 数量和 permission custody 边界。
- 引导只读取 `/runtime/readiness` 与 `/providers/models`；不在 TUI 中收集或持久化 provider secret。
- 有可用模型时允许使用 canonical 自动路由，或为本次会话选择模型与官方支持的推理强度；无可用模型时明确转向 `/doctor`，不伪装 ready。
- 引导完成状态使用 `zyra.product-onboarding/v1`，只记录时间和选择类别，不记录 workspace、provider identity、token 或路径。写入采用同目录临时文件、flush 与原子 rename。
- 未知、过大或损坏状态保持原件并安全降级，给出 `zyra doctor` 恢复动作。
- 自动化性能/生命周期门可显式设置 `ZYRA_SKIP_ONBOARDING=1`，但首次使用 ConPTY 门必须使用隔离空状态并完成真实 picker。
- Windows product picker 与 composer 共用“持久 bracketed paste 关闭”决策；overlay 不得重新开启强杀后可能残留的 terminal mode。

## 结果

无参数启动同时具备首次配置发现和稳定 composer 路径。引导依赖 canonical 能力，不形成第二套配置真相；其状态可安全分享与迁移，且真实 ConPTY 验证了首次启动、picker、状态落盘、resize 和退出恢复。
