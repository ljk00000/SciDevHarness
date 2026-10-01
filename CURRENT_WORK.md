# 当前工作

## 2026-10-01 23:51（Asia/Shanghai）· 新建公开仓库并上传

- 新建公开仓库 [ljk00000/SciDevHarness-IDE](https://github.com/ljk00000/SciDevHarness-IDE)，`master` 已上传并验证远端 SHA 与本地一致（`3c4cfe0`）。原 `SciDevHarness` 和 `SciDevHarness-Desktop` 仓库保持不变。
- 上传内容包含本轮响应式工作台修复；107 项单测通过，凭据格式/大体积历史对象检查无命中，忽略的模型、虚拟环境和研究数据没有进入提交。提交 `3c4cfe0` 的 Windows CI 在 Python 3.12/3.13/3.14 均通过，包含依赖 smoke、普通/125% DPI 截图和 QML lint。

下一步：修正本地 Qwen SVG 烟测的 SSE 计时，让首事件延迟与完整响应耗时可区分，再依据实测减少冗余模型往返；原生窗口可见时补桌面交互与布局验收。
