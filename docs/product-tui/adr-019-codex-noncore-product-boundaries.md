# ADR-019：Codex 非核心产品面的逐项边界

## 状态

已评审。此决定只关闭覆盖矩阵中的 P2/N/A 归类，不豁免任何 P0/P1 能力。

## 逐项决定

### 账户：N/A

Codex 的登录/退出拥有 ChatGPT/API account identity 和 app-server account API。Zyra 当前只从部署环境读取 provider credential，并通过 onboarding/doctor 暴露脱敏 readiness。没有 canonical user identity owner 时增加 `/login` 会产生第二套 credential 真相，因此不实现。

### Apps：N/A

Codex Apps 绑定 ChatGPT connector catalog、安装状态、OAuth/custody 和工具调用元数据。Zyra Web 看板不具备这些语义，不能因名称相近而冒充等价入口。未来若后端建立正式 connector owner，必须重新进入覆盖审计。

### Plugins：P2

Zyra 已有 runtime 级集成和工具扩展，但插件发现、签名、安装、信任、升级和移除是独立产品面。比赛核心软件工程和长程控制闭环不依赖终端插件市场；本任务不增加只展示目录却不能安全安装和授权的半成品 `/plugins`。该能力延期到单独任务。

### Pets：N/A

Codex Pets 是终端图像装饰能力，不改变任务提交、呈现、控制、权限、恢复或交付。复制它会扩大 Windows terminal image protocol 兼容范围而不提供比赛核心价值，因此明确排除。

## 重新评审触发条件

- Zyra 增加 canonical user identity/account API；
- 增加带 auth/custody 的 connector 服务；
- 比赛规则或真实用户工作流要求在 TUI 内安装扩展；
- 上述能力被提升为 P0/P1。
