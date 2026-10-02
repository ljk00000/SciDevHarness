# 当前工作

## 2026-10-02 14:07（Asia/Shanghai）· 通用工具调用与模型实测

- 文本工具调用增加安全的解析/schema 错误定位；SVG 定向修复避免重复读取、越权整文件重写，并预留有限验证回合。
- 全量 `unittest` 165/165、`compileall`、`git diff --check` 通过。固定提示词本机 Qwen2.5-Coder-7B 已完整进入视觉验收，但图像仍不合格；未下载模型。
- 这些改动待提交并跑 Windows CI；唯一公开仓库仍为 SciDevHarness，v5 历史与 `release/v5` 保留。

## 2026-10-02 13:13（Asia/Shanghai）· 推送与 CI 验收

- `9b6306d` 已推送到唯一公开仓库 `master`；GitHub Windows CI 的 Python 3.12/3.13/3.14 全通过。
- 下一步继续改善本机模型的工具调用/修复稳定性；固定 pelican 提示词仍失败，保留此基线，不重复下载模型。
