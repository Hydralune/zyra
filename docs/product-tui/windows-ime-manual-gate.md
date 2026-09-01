# Windows IME 人工验收门

此门禁只验证真实 IME 候选窗和组合态；逐字节 UTF-8、中文宽度、组合字符、ZWJ emoji、paste burst、异步重绘和 1,000 次 resize 已由自动化门禁覆盖。ConPTY 不能产生 Windows IME 候选 UI，因此不得用自动注入 Unicode 冒充本项通过。

## 环境

- Windows Terminal 中的 PowerShell；
- 微软拼音或实际比赛机器使用的中文 IME；
- 仓库依赖已安装。

## 执行

```powershell
Set-Location G:\agent-zoo\zyra
.\scripts\product-tui\windows_ime_manual_gate.ps1
```

保持后台“异步重绘压力”持续更新，然后完成以下操作：

1. 用拼音组合态输入 `中文候选窗口不会丢字`，至少翻页或选择一次非首候选；候选窗位置应跟随 composer，选择前不得把未提交拼音发给程序。
2. 紧接着输入 ` é 👨‍👩‍👧‍👦 ✅`；退格一次再恢复该字符，光标不得落入 grapheme 内部。
3. 按 Enter 后，`人工核对提交文本` 必须与最终输入逐字符一致，无 `�`、重复、丢字或意外换行。
4. 输入 `/exit`；PowerShell 应恢复正常回显、光标和方向键行为。

## 记录

通过时在 `docs/product-tui/release-evidence-<date>.md` 记录终端、IME 名称/版本、输入文本、结果和执行者。任何候选窗漂移、组合态被重绘打断、提交不一致或退出后终端异常均为 P0，不能发布。
