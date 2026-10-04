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

客户端包含：

- 编码会话：在右侧 Agent 对话栏输入任务，Agent 自主读取文件、写代码、修复错误
- 界面方案：Studio、Paper、Focus 三套可切换并记住选择的外观/布局配置
- 工具调用：`list_files`、`read_file`、`write_file`、`replace_in_file`、`run_command`、`git_diff`；Agent 每次执行 shell 命令前都须用户批准
- 本地任务队列：网络失败时指数退避并定时重试，进程重启后可恢复会话
- Git 集成：查看状态和提交记录；自动提交仅包含任务开始时干净、且本次会话改动的文件，不会把已有暂存或未提交文件混进来
- 过程记录：`.research/events.jsonl`、`.research/runs.db`、`.research/sessions/`

自动 Git 安全策略：Agent 发给模型的 Git 状态/diff 会隐藏常见凭据路径（包括重命名涉及凭据文件的整个差异）；Agent 自动提交会跳过敏感/内部路径、符号链接或目录联接、超过 240,000 字节的文件，以及二进制/非 UTF-8 文件，并在聊天记录中说明，文件仍留在工作区供你从 Git 面板审核。`.env.example` 等模板不受凭据过滤。Git 面板里的手动 diff 与提交是用户主动操作，不受 Agent 自动筛选策略限制。

其他安全边界：文件工具会阻止项目根目录之外的路径和常见凭据文件（例如 `.env`、`.ssh`、`.npmrc`）。文本编辑只接受有效 UTF-8，不会把含 NUL/非法编码的文件当文本覆盖；完整重写会拒绝超大既有文件，文本替换会校验结果大小和 NUL 字节。Agent 的每条 shell 命令都需单独批准；批准后命令以当前 Windows 用户权限执行，应用不是操作系统级沙箱。

## LLM 配置

使用 OpenAI-compatible Chat Completions 接口，并要求模型支持 function/tool calling：

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

实际打包会按固定版本安装 Nuitka（已缓存时复用）；CI 的暂存和打包器 dry-run 不下载 Nuitka，也不编译 Windows 二进制。发布前仍需在具备 MSVC 的 Windows 环境中完成打包并审查 Qt 第三方许可声明。

仓库 Actions 中的“Windows Package Verification”是手动触发的完整构建检查：它会在固定的 Windows Server 2022 runner 初始化 MSVC、构建 standalone 并实际启动 exe 验证 QML；7 天工件现在包含打包 native binary 的路径/大小/SHA-256，以及构建环境的许可证 metadata，供人工核对；不上传二进制，也不创建 GitHub Release。清单明确标记需人工审核，并不代表许可证合规结论。构建固定使用 `requirements-build.txt` 中的 PySide6-Essentials 6.11.2，Dependabot 每周检查依赖更新，升级后需重新通过打包烟测。公开分发前仍要按实际打包组件补齐 Qt/PySide 第三方许可与声明；Nuitka 编译器受 AGPLv3 约束，其 [Runtime Library Exception](https://github.com/Nuitka/Nuitka/blob/develop/LICENSE-RUNTIME.txt) 对符合条件的编译输出另有许可，二者不能混为一谈。详见 [Qt 官方部署说明](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html) 与 [Nuitka 4.1.1 元数据](https://pypi.org/project/Nuitka/4.1.1/)。以上不是法律意见。

当前版本先把 Codex 式编码闭环跑通：

```text
用户任务 → 项目上下文 → LLM → 工具调用 → 文件修改/检查 → Git diff → 自动提交
```
