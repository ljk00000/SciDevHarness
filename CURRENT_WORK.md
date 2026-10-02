# 当前工作

## 2026-10-02 16:51（Asia/Shanghai）· 推理模型兼容与固定提示词对照

- 视觉盲审/固定提示词验证已提交并推送至唯一公开 `master`（`b6aefc2`）；175 项全量回归和远端 Windows CI #22 三任务通过，细节见历史记录。
- 将 Ollama 临时 loopback 服务设为高 context 做对照（运行态 32768、约 8.7 GB）；同一原句仍产出错误图形，证明低完成率不只是 4096 context 所致。临时服务已停止、标准服务未改动、未下载权重。Ollama `/v1` 本身不支持逐请求设 context，配置需在服务/模型层控制。
- 新增 opt-in `SCIDEV_REASONING_EFFORT`（默认不发送），将配置透传给支持它的 OpenAI-compatible 模型；定向单测通过。已安装 Qwen3.5-4B 在 16K context 下也收到完全相同原句，5 次请求、约 255 秒，生成了可辨认的 pelican/bicycle 插画，但严格结构预检仍失败（缺多项车/鸟部件与关系）；未达验收，且本机速度不适合实时编码。临时服务已停止。
- 当前仍有两项未结：本机 Qwen2.5-Coder-7B 与 Qwen3.5-4B 的固定单提示词严格 smoke 均未过；CUA 未枚举原生桌面窗口，所以只有离屏截图验证、没有原生实机截图结论。
- 下一步跑全量回归、提交并验证这次推理模型配置；继续提升通用工具调用与任务完成率，并在桌面接口恢复可见窗口后完成原生响应式 UI 检查；保持主仓库唯一公开与 `release/v5` 历史分支。

## 2026-10-02 15:04（Asia/Shanghai）· 代码提交与 CI 验收

- `bd35043` 已推送至唯一公开仓库 `SciDevHarness/master`；[Windows CI](https://github.com/ljk00000/SciDevHarness/actions/runs/36976220009) 的 Python 3.12/3.13/3.14 全通过，远端与本地提交一致，工作区干净。
- 下一步继续改善通用 Harness 的代码任务完成率与 UI 实机验收；当前 Qwen 7B 的固定 SVG 任务仍未通过视觉标准，不能结项。

## 2026-10-02 14:57（Asia/Shanghai）· 单文件路径补全与温度 A/B

- SVG 定向修复只在刚读取且唯一的目标文件上放宽 `replace_in_file.path`，模型漏传时由 Harness 依据该读取记录补齐；路径多于一个仍为必填。通用 `SCIDEV_TEMPERATURE` 可配置，本机 Qwen 启动默认 0、保留显式覆盖。
- 全量 `unittest` 166/166、`compileall`、`git diff --check` 通过。固定原句 0.0 运行走完 3 轮修复、11 次本机请求后仍未通过视觉语义验收；0.2 运行在无进展时有界停止。未下载模型。
- 离屏 UI 截图检查覆盖 1500×920、1180×760、940×620、820×600、780×480，交互/适配 smoke 通过；本轮桌面接口没有可控窗口，因此未作原生窗口实机验收。
- 待提交并跑 Windows CI。下一步继续提升生成/修复质量；Qwen 7B 当前产图仍不合格，许可证审计仍需人工复核。

## 2026-10-02 14:13（Asia/Shanghai）· 通用工具调用与模型实测

- 文本工具调用增加安全的解析/schema 错误定位；SVG 定向修复避免重复读取、越权整文件重写，并预留有限验证回合。
- 全量 `unittest` 165/165、`compileall`、`git diff --check` 通过。固定提示词本机 Qwen2.5-Coder-7B 已完整进入视觉验收，但图像仍不合格；未下载模型。
- 提交 `13927a1` 已推送至唯一公开仓库 `master`；[Windows CI](https://github.com/ljk00000/SciDevHarness/actions/runs/36972322392) 的 Python 3.12/3.13/3.14 全通过。工作区干净，v5 历史与 `release/v5` 保留。
- 下一步继续改善通用编码/工具调用的真实产出质量；当前 7B 模型的固定 SVG 任务仍不合格，不记为成功。

## 2026-10-02 13:13（Asia/Shanghai）· 推送与 CI 验收

- `9b6306d` 已推送到唯一公开仓库 `master`；GitHub Windows CI 的 Python 3.12/3.13/3.14 全通过。
- 下一步继续改善本机模型的工具调用/修复稳定性；固定 pelican 提示词仍失败，保留此基线，不重复下载模型。
