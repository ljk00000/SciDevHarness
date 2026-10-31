# SciDevHarness

版本树使用 Qt Quick/QML 绘制，并通过 PySide6 的 QQuickWidget 嵌入现有桌面客户端；编辑器、文件树和 Agent 面板继续使用 Qt Widgets，因此不需要额外安装前端运行时。

SciDevHarness 是一个原生 Windows 桌面编码客户端，第一阶段仿照 Codex，重点是让 LLM 直接读取项目、修改代码、执行必要检查并查看 Git diff；暂不包含“运行科研实验”流程。

客户端使用 PySide6/Qt，采用 VSCode + Codex 风格的左侧资源管理器、中间编码工作区和右侧 Agent 对话栏。

顶栏“主题”菜单可随时切换三套完整界面方案：Studio 深色均衡、Paper 明亮浅色、Focus 专注宽编辑器。选择会保存到本机设置；颜色同步覆盖编辑器语法、版本树与 Git 面板，Focus 保留项目导航空间、收窄对话栏，把更多宽度留给代码区。

## 许可证与依赖

- 项目代码采用 [Apache-2.0](LICENSE)；Qt/PySide6 与可选模型权重使用各自许可证，模型权重不随仓库分发。
- 运行时依赖只选 PySide6-Essentials（项目使用的 Qt Widgets/QML/QuickWidgets），不安装未使用的 PySide6-Addons；两者是独立发行组件，详见 [Qt for Python 包结构](https://doc.qt.io/qtforpython-6/package_details.html)。
- 当前只发布源码，不含 Qt/PySide6 运行时二进制。Qt for Python 社区版依组件采用 LGPLv3、GPLv3 等许可证，也提供商业授权。
- 若今后分发独立可执行文件，须审核实际打包组件并随包提供对应版权、许可证和第三方声明；不能把这些运行时组件误标为 Apache-2.0。

详见 [Qt for Python 官方许可说明](https://doc.qt.io/qtforpython-6/commercial/index.html)、[第三方许可证清单](https://doc.qt.io/qtforpython-6/licenses.html) 和 [LGPLv3 说明](https://doc.qt.io/qtforpython-6/overviews/qtdoc-lgpl.html)。以上不是法律意见。

## 启动

推荐直接双击 `start_client.bat`，它会校验并使用项目自己的 `.venv`；只有虚拟环境或 PySide6 Essentials 版本不符合要求时才自动创建/更新环境。若已有 `.venv` 使用不兼容的 Python，启动器会报错但不会删除或覆盖它。
客户端目前以 Windows 桌面为首要支持平台，需要 Python 3.12–3.14、Git CLI；GitHub Actions 配置为对 Python 3.12、3.13 和 3.14 分别执行安装和回归检查。
在资源管理器的项目栏点“⋯”或按 `Ctrl+K`、`Ctrl+O` 可在新窗口打开其他文件夹；之后启动会恢复最近使用的工作区。命令行也可指定：

```powershell
.\start_client.bat
.\start_client.bat --workspace "D:\Projects\my-project"
```

如果已经完成初始化，也可以直接使用虚拟环境启动：

```powershell
.\.venv\Scripts\python.exe scidev_client.py
```

### 无界面执行（CLI）

不启动 Qt，可用于脚本、CI 和评测：

```powershell
.\.venv\Scripts\python.exe scidev_cli.py exec `
  --workspace "D:\Projects\my-project" `
  --task "修复登录接口的空指针并运行相关测试" `
  --execution-mode workspace-write `
  --json
```

`--execution-mode` 支持 `read-only`、`manual`、`workspace-write`、`full-access`；`--no-verify` 关闭提交前验证门；`--restore-on-failure` 在任务失败时自动回滚本次任务写过的文件；`--api-base`/`--api-key`/`--model` 可临时覆盖环境变量。成功退出码为 0，任务失败为 1，参数错误为 2，取消为 130。CLI 与桌面端共用 Agent、执行策略、Git 自动提交和 `.research` 审计记录。

列出最近会话，再用同一个 `--session-id` 继续该会话的上下文：

```powershell
.\.venv\Scripts\python.exe scidev_cli.py sessions --workspace "D:\Projects\my-project" --json
.\.venv\Scripts\python.exe scidev_cli.py exec --workspace "D:\Projects\my-project" --session-id <session_id> --task "继续修复剩余失败测试"
```

客户端包含：

- 编码会话：在右侧 Agent 对话栏输入任务，Agent 自主读取文件、写代码、修复错误
- 会话恢复：Agent 设置列出最近会话，选择并恢复后，下一条消息继续该会话的历史上下文；不会自动重放已完成的工具调用
- 界面方案：Studio、Paper、Focus 三套可切换并记住选择的外观/布局配置
- 工作台：响应式分栏、按主题记住拖动宽度；Agent 设置独立滚动，欢迎页不再伪装成代码，打开文件后自动隐藏欢迎标签
- 设置页：左下角齿轮 / `Ctrl+,` / `> settings` 打开；分类与关键词搜索、只看已修改、单项及整体恢复默认。主题、强调色、字体/字重/字号、缩放、密度、侧栏尺寸和编辑器显示即时应用并保存到本机；Agent 配置仍复用项目设置。
- 代码高亮：按文件类型区分 Python、HTML（含 CSS/JavaScript）、JS/TS、CSS、JSON、Markdown 和 YAML；属于词法高亮。Python 文件会异步启动本机 Ruff LSP，把 diagnostics 放进“问题”面板，双击可跳到行列；这是按文件的一次性诊断，不是完整 IDE 语言服务（无补全/跨文件跳转/重命名/调试器）。
- 文件打开：资源树或编辑器标签右键 HTML →“在浏览器中打开”，无需服务器即可查看本地网页；其他文件可“使用默认程序打开”。双击仍进入编辑器；外部程序读取磁盘版本，未保存修改请先 `Ctrl+S`。
- 版本树：专注画布、可折叠节点详情和 Agent 面板，2.5D 卡片与动态连线；尝试分支可自由拖动并记住位置
- 工具调用：`search_code`、`get_project_context`、`list_files`、`read_file`、`write_file`、`replace_in_file`、`update_plan`、`run_command`、`git_diff`；执行策略可选只读、手动审批、工作区可写或完全访问
- 项目上下文：按任务关键词（含有限中英软件术语映射）与当前文件自动定位相关代码，附带项目结构、依赖声明、Git 状态和适用目录指令；点击聊天输入区 `@` →“查看项目上下文”可预览、刷新
- 任务规划：右侧计划面板提供自动规划、必须先规划、关闭三种模式；计划步骤随会话保存，重试可恢复，未完成计划不会被当作成功提交
- 本地任务队列：最多 6 次尝试（含首次）；429/5xx 优先遵从服务端 `Retry-After`，否则指数退避，进程重启后可恢复。相同任务/请求体重试保持幂等键，任务或请求体改变则更换键；服务端可能不支持幂等键，不能保证网络中断时绝不重复计费
- Git 集成：查看状态和提交记录；自动提交仅包含任务开始时干净、且本次会话改动的文件，不会把已有暂存或未提交文件混进来
- 过程记录：`.research/events.jsonl`、`.research/runs.db`、`.research/sessions/`

自动 Git 安全策略：Agent 发给模型的 Git 状态/diff 会隐藏常见凭据路径（包括重命名涉及凭据文件的整个差异）；Agent 自动提交会跳过敏感/内部路径、符号链接或目录联接、超过 240,000 字节的文件，以及二进制/非 UTF-8 文件，并在聊天记录中说明，文件仍留在工作区供你从 Git 面板审核。`.env.example` 等模板不受凭据过滤。Git 面板里的手动 diff 与提交是用户主动操作，不受 Agent 自动筛选策略限制。

任务检查点：Agent 在每次写文件前把原始内容保存到 `.research/checkpoints/<id>/`；任务结束后可在 Agent 设置里点“回滚上次任务改动的文件”，只恢复本次任务实际写过的文件，并删除任务开始前不存在的新文件。CLI 可用 `--restore-on-failure` 在任务失败时自动回滚。回滚会覆盖你在任务开始后对这些文件做的修改；检查点只保留最近 10 个，不替代 Git。

其他安全边界：文件工具会阻止项目根目录之外的路径和常见凭据文件（例如 `.env`、`.ssh`、`.npmrc`）。文本编辑只接受有效 UTF-8，不会把含 NUL/非法编码的文件当文本覆盖；完整重写会拒绝超大既有文件，文本替换会校验结果大小和 NUL 字节。应用不是操作系统级沙箱：批准后的命令仍以当前 Windows 用户权限执行。

执行策略在 Agent 设置中选择，默认“工作区可写”：

- **只读**：禁止写文件和运行终端命令，适合审查、问答。
- **手动审批**：允许写文件，但每条终端命令都需确认。
- **工作区可写**：允许写文件；自动批准可识别的 pytest/unittest、npm/pnpm/yarn test、ruff/eslint、mypy/pyright、`tsc --noEmit` 等验证命令，其他命令仍需逐条确认。
- **完全访问**：自动批准所有终端命令，仅保留破坏性命令和 Harness 自有 Git 操作的拦截；只应在可信项目中使用。

自动批准只接受单条、无 shell 链接/管道/重定向、无绝对路径或上级目录、无网络下载的验证命令。测试和 lint 输出会解析成文件/行号/错误码的结构化诊断，附加在 `run_command` 结果后供模型继续修复。策略层不是网络或文件系统沙箱，不能保证已批准命令无法访问项目外资源。

## 项目上下文

发送任务后自动生成项目概况与相关文件片段；继续任务、网络重试和 Agent 修改文件后刷新同一份上下文，不累积旧的目录快照。当前编辑器文件优先检索，选区仍随任务附上；敏感或内部文件不自动引用。未保存的打开文件会以有界只读快照进入检索与 `read_file`；预算外的草稿会从检索中排除，也不会回退读取磁盘旧版本。Agent 不会写入任务开始时有未保存修改的文件，保存后重新发起即可修改。

项目上下文会静态推断 Python pytest/unittest、Node 根/子 package 与 `test:*` 脚本、嵌套 Go/Rust/Maven/Gradle/.NET 项目，以及 tox/nox、Makefile、Justfile 中明确的测试入口；候选附证据并限定到对应子项目，本地普通 `.venv` 解释器存在时优先引用。对 `.venv`/`venv`/`env` 的 `pyvenv.cfg` 只读取 Python 创建版本及共享系统包标志，不执行解释器或泄露配置中的绝对路径；这不等于实际解释器/依赖核验。上下文页“核验环境”可在用户主动点击并批准一次后，检查项目 Python venv 和根清单所声明的 Node/Rust/Go 工具链版本，并在本机可用时探测 NVIDIA/CUDA 工具；全程不联网、不安装/解析依赖、不运行项目脚本、构建或测试，也不会随打开项目、生成上下文或编码任务自动触发。Python 不回退到 Harness 解释器；其他工具只运行固定版本参数。`pip check` 不证明项目声明依赖齐全/可导入；Node/Rust/Go 版本可用不代表工程可构建；GPU/CUDA 工具存在也不代表框架能实际使用 GPU。目前只检查根清单，未发现嵌套 monorepo 的运行时配置。普通测试发现仍为静态候选；执行时按当前执行策略处理：工作区可写会自动批准可识别的验证命令，手动审批和完全访问模式分别逐条确认或全部自动放行。当模型结束任务但代码改动尚未验证时，Harness 会在工作区可写/完全访问模式下自动选择第一个安全的 test/lint/typecheck 候选运行一次，把退出码和结构化诊断加入后续修复轮次；同一编辑版本不重复自动运行，最多 3 次。无法安全引用路径、清单冲突、文件或输出预算耗尽时会省略候选并标记范围受限；未知入口仍可能漏检，候选命令和覆盖范围都未实际验证。

`search_code` 是字面文本检索，可指定 `path`、`glob`、大小写和相邻行；例如查 `InventoryManager`，或限定 `path="src"`、`glob="**/*.py"`。`get_project_context` 可按新关键词和准备修改的路径获取概况/目录指令。读取源文件时也会附上所在目录的 `AGENTS.md`；下级指令只适用于该目录及子目录。

编辑器快照最多 8 个文件、合计 1 MB；超预算文件标为只读并从自动检索/读取中排除，避免误用旧磁盘内容。快照不会自动写入工作区源文件。为支持队列/重启重试，待处理或可重试任务的原文快照会暂存在本机 `.research/runs.db`；创建事件不记源码正文，只记快照路径和字节数，任务成功后从队列记录中清除。最终失败但仍可手动重试的任务会保留快照。项目上下文片段和工具读取结果仍按现有会话机制保存在 `.research/sessions/`。项目清单若有未保存修改，也会用于运行时和依赖摘要。

本地缓存按文件时间和大小失效，无需额外模型或下载。默认概况最多 18000 字符，最多发现 5000 个文件、扫描 8 MB 文本，文本源文件上限 2 MB；局部读取最多 400 行/25000 字符，达到预算会提示继续行号。检索截断时可缩小路径或模式。Git 项目使用 Git 自身的忽略规则，非 Git 项目兼容常见 `.gitignore` 模式。当前是关键词/标识符检索，不含向量语义检索或完整依赖图；依赖声明不代表已安装。

## 跨会话项目记忆

新任务可从本项目已完成会话的总结中检索少量相关记录；索引保存在本地 `.research/memory.sqlite3`，不跨项目共享、不额外调用模型或网络。聊天栏 Agent 设置可关闭检索，或调整每次引用的总结条数和上下文长度。检索基于关键词而非向量语义；常见凭据格式会在索引前脱敏，但不能保证识别所有私有凭据。

## 任务规划

右侧计划面板可切换“自动规划 / 必须先规划 / 关闭”。每个任务最多 12 个计划步骤，Agent 会更新待处理、进行中、已完成、受阻或已跳过状态。计划随会话保存；网络重试及任务历史重试会恢复原计划，同一会话的新任务会归档上一计划。若最终回答时计划仍未完成，Harness 对连续未完成的回答最多要求继续两次；取得计划进度后重新计数。仍未完成会失败并保留计划，不自动提交。步骤完成状态由模型报告，不替代实际测试或独立验收；暂不支持逐步人工批准和 DAG 拖动。

## 独立语义验收

聊天区“Agent 设置”中可选“关闭 / 建议 / 必需”，默认关闭，不会悄悄增加模型调用。启用后会向配置的验收模型发送用户任务、编码模型的最终说明、最多 40 个安全文本变更文件的完整 diff（总计最多 36,000 字符）、任务计划摘要和最近的检查摘要；审查请求不提供文件/命令工具。请注意：这会把变更源码额外发送到当前配置的模型服务。

验收模型 ID 必须与编码模型 ID 不同。它可以仍使用同一个 API 地址和服务商，因此“独立”指隔离的模型审查步骤，不代表机构或数据源独立，也不是形式化正确性证明。模型必须返回逐项判定和引用的变更文件；格式错误、缺少引用、超预算/敏感/不可读 diff，或审查等待期间代码变化，都不能标为通过。

“建议”模式仅报告结论，不阻止现有自动提交；“必需”模式在需求未满足时最多要求 Agent 修正一次，然后重新审查，若审查不可用、不确定、范围不完整或最终不通过，则保留改动且不自动提交。此功能不替代执行策略或人工审批；模型评审结果只作为额外证据。

## LLM 配置

使用 OpenAI-compatible Chat Completions 接口，并要求模型支持 function/tool calling：

Agent 设置 →“模型接入”可直接填写 API Base URL、模型和 API Key。API Key 只写入 Windows 凭据管理器（目标 `SciDevHarness/provider/default`），不写入 `.research/provider.json` 或审计日志；保存后下一次任务生效。项目内只保存地址、模型、超时、temperature 等非敏感字段。没有保存配置时继续使用下面的环境变量；保存了配置时环境变量作为 API Key 的后备。CLI 显式传入 `--api-base`/`--api-key`/`--model` 时会强制走环境变量路径，便于临时覆盖。推理强度可选“默认”/none/low/medium/high/max；只有 endpoint 支持时才应使用。“探测能力”会请求 `/models` 并发送一次极小的 `ping` 工具调用，显示工具调用、上下文长度和视觉元数据；这些信息由 endpoint 报告，不保证准确。可填写“回退模型”：主模型返回 HTTP 404 或模型不存在的 HTTP 400 时，本次任务自动用回退模型重试一次；其他错误不回退。

Agent 设置里的“任务预算”可限制单次任务最多模型轮次（1–64）和最多 token（0 = 不限）。token 预算只统计服务端实际返回的 usage，并按任务起点扣除历史累计；用尽后任务失败、改动保留在工作区且不自动提交。轮次上限始终受 Harness 内置的 32 轮硬上限约束。

```powershell
$env:SCIDEV_API_BASE="https://your-endpoint/v1"
$env:SCIDEV_API_KEY="your-key"
$env:SCIDEV_MODEL="your-model"
.\start_client.bat
```

也支持 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。如果只配置 `OPENAI_API_KEY`，地址默认使用 `https://api.openai.com/v1`，模型默认使用 `gpt-5`。

### 本机 Qwen2.5-Coder-7B

本地 Qwen 可通过 Ollama 接入；Q4_K_M 权重约 4.68GB，来自 [ModelScope 上的 Qwen GGUF 仓库](https://modelscope.cn/models/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF)，许可证为 Apache-2.0。权重不随本仓库分发。需要先安装并启动 Ollama，且本机已有一个可用模型标签；启动脚本默认查找开发环境使用的 `scidev-qwen2.5-coder-7b:q4_k_m`，也可以指定自己的标签：

```powershell
$env:SCIDEV_MODEL="your-local-ollama-model-tag"
.\start_qwen_local.bat
```

脚本会先验证模型标签，再仅为客户端进程设置本地 API 地址和工具调用兼容选项，不改全局环境变量。兼容选项仅接受与 Harness 已声明工具及其参数匹配的明确 JSON 工具调用。

客户端默认启用流式响应，让模型生成过程实时显示在对话中；可设置 `SCIDEV_STREAMING=false` 关闭。`SCIDEV_TEMPERATURE` 可配置为 0–2（通用默认 0.2；本机 Qwen 启动脚本默认 0，并尊重用户已设置的值）；其对稳定性、工具调用和内容质量的影响取决于模型，降低温度不保证更好。`SCIDEV_PRESENCE_PENALTY` 可选配置为 -2–2。`SCIDEV_REASONING_EFFORT` 可选配置为 `none`、`low`、`medium`、`high` 或 `max`，仅适用于 endpoint 支持该字段的推理模型；默认不发送。`SCIDEV_REQUEST_TIMEOUT_SECONDS` 可配置为 5–600 秒，通用启动默认 90 秒；本机 Qwen 启动脚本默认 240 秒以适配较慢的本地推理。这些变量仅在启动器子进程内生效。

可选的独立视觉盲审需要兼容当前 API endpoint 的多模态模型。例如已有 Ollama 视觉模型时，可在启动前设置：

```powershell
$env:SCIDEV_VISION_MODEL="qwen3.5:4b"
$env:SCIDEV_VISION_REASONING_EFFORT="none"
.\start_qwen_local.bat
```

启用后，Harness 会在 SVG 写入/修改后把安全栅格化的图像交给该模型；常规编码任务也可调用 `inspect_visual_artifact` 检查 PNG/JPEG/WebP/SVG。审查模型看不到原始任务、文件名或 SVG 源码，只给出可见缺陷线索，不判定任务通过。`SCIDEV_VISION_REASONING_EFFORT` 为可选项（`none`/`low`/`medium`/`high`/`max`），用于支持该 OpenAI-compatible 扩展的推理模型；Qwen3.5 经 Ollama `/v1` 时建议设为 `none`，以避免只输出 thinking 而没有审查正文。此功能默认关闭；若当前 endpoint 是云服务，启用后图像会发送到该 endpoint，请勿对不适合外传的素材启用。

要在隔离临时仓库里端到端验证本地模型的工具调用、代码能力、Git 自动提交和对话总结，可运行以下烟测。除固定安全脚本外，还会让模型新写奇偶判断、正数过滤求和、区间夹取，并修复一个已有函数的边界 bug；共检查 18 个样例。生成函数先经过 AST 语法白名单校验（拒绝导入、函数调用、属性访问、推导式和顶层副作用；循环只可遍历测试输入），再用空内建环境运行；只连接回环地址、仅使用已安装模型，绝不拉取或下载权重：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_local_ollama.py
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe smoke_dependency.py
.\.venv\Scripts\python.exe smoke_screenshot.py --output-dir "$env:TEMP\scidev-ui-shots"
.\start_client.bat --check-runtime
```

Windows CI 会验证 Qt 窗口与 QML 加载、资源管理器双击打开、Ctrl+S 保存、工作区选择/记忆、多窗口行为、shell 拒绝审批，以及版本树分支拖拽、画布平移、滚轮滚动/Ctrl+滚轮缩放；另在宽屏/窄屏渲染编辑器和版本树截图。每周和手动 CI 会保留一周的截图工件，供视觉复查；每次变更仍会跑单测、依赖/编译检查和 QML lint。

## Windows 独立版打包

独立打包需要 Windows C++ 编译工具链。先用 Python 3.14 创建项目 `.venv` 并安装 `requirements.txt`，再从仓库根目录执行；暂存目录必须在仓库之外且不存在：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
$repo = (Get-Location).Path
$stage = Join-Path $env:TEMP ("scidev-release-" + [guid]::NewGuid().ToString("N"))
.\scripts\stage_release.ps1 -Destination $stage
$previousPath = $env:PATH
$env:PATH = (Join-Path $repo ".venv\Scripts") + [IO.Path]::PathSeparator + $env:PATH
Push-Location $stage
try {
    & (Join-Path $repo ".venv\Scripts\pyside6-deploy.exe") --config-file pysidedeploy.spec --nuitka-version=4.1.1
} finally {
    Pop-Location
    $env:PATH = $previousPath
}
```

首次运行默认打开 `examples/tetris` 里的俄罗斯方块网页样例。双击 `index.html` 就能离线游玩，无需 Python、依赖或服务器。打包后会将 HTML 和说明文件放到 exe 旁边，供默认工作区定位；构建完成后执行：

```powershell
$exe = Get-ChildItem (Join-Path $stage "deployment") -Filter "scidev_client.exe" -File -Recurse | Select-Object -First 1
& (Join-Path $repo "scripts\copy_default_sample.ps1") -ApplicationDirectory $exe.DirectoryName
```

实际打包会按固定版本安装 Nuitka（已缓存时复用）；CI 的暂存和打包器 dry-run 不下载 Nuitka，也不编译 Windows 二进制。发布前仍需在具备 MSVC 的 Windows 环境中完成打包并审查 Qt 第三方许可声明。

仓库 Actions 中的“Windows Package Verification”是手动触发的完整构建检查：它会在固定的 Windows Server 2022 runner 初始化 MSVC、构建 standalone 并实际启动 exe 验证 QML；7 天工件现在包含打包 native binary 的路径/大小/SHA-256，以及构建环境的许可证 metadata，供人工核对；不上传二进制，也不创建 GitHub Release。清单明确标记需人工审核，并不代表许可证合规结论。构建固定使用 `requirements-build.txt` 中的 PySide6-Essentials 6.11.2，Dependabot 每周检查依赖更新，升级后需重新通过打包烟测。公开分发前仍要按实际打包组件补齐 Qt/PySide 第三方许可与声明；Nuitka 编译器受 AGPLv3 约束，其 [Runtime Library Exception](https://github.com/Nuitka/Nuitka/blob/develop/LICENSE-RUNTIME.txt) 对符合条件的编译输出另有许可，二者不能混为一谈。详见 [Qt 官方部署说明](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html) 与 [Nuitka 4.1.1 元数据](https://pypi.org/project/Nuitka/4.1.1/)。以上不是法律意见。

当前版本先把 Codex 式编码闭环跑通：

```text
用户任务 → 项目上下文 → LLM → 工具调用 → 文件修改/检查 → Git diff → 自动提交
```
