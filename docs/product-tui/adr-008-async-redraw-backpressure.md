# ADR-008：输入优先的异步重绘与输出背压

状态：Accepted
日期：2026-09-01

## 决策

1. `LiveProductRenderer` 把后台 event/resize 更新标记为 dirty，并在 microtask 边界合并；composer、picker、pager 和用户 notice 使用即时重绘。
2. 一次 frame 的清屏前缀与 snapshot 使用单次 `write`，避免两个 write 之间被其他终端输出拆开。
3. 当终端 `write()` 返回 false 时，只保留最新 dirty state，等待 `drain` 后生成一个最新 snapshot；不得为每个中间 event 排队完整屏幕。
4. renderer 暴露只读 diagnostics（request、snapshot、write、coalesced、backpressure），供性能门和真实 PTY 断言使用；这些指标不进入 canonical task state。
5. composer draft 继续独立拥有文本、grapheme cursor、history 和 persistence；重绘只读取 snapshot，不能修改输入状态。

## 证据

- 组件测试在 5,000+ event/resize request 中逐字节注入中文、组合字符和 ZWJ emoji，提交文本逐字一致，写入次数有界。
- 背压测试在首个 terminal write 阻塞时注入 10,000 次重绘，只保留一个最新 frame，`drain` 后收敛。
- Windows ConPTY 测试同时进行后台 event pressure、60–200 列 resize 和逐字节 UTF-8 输入，验证 exact submit、无替换字符、bracketed-paste 启停平衡且不进入 alternate screen。

## 边界

该证据覆盖异步输出期间输入正确性及碎片化 UTF-8 commit；它不等同于人工 Windows IME 候选窗/组合态验收，COMP-02 的 IME 实机项仍保持未完成。
