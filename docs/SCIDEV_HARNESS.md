# SciDevHarness Python 版技术手册

本文收录旧版 Python 的配置、操作、CLI 与打包参考。[项目首页](../README.md)介绍默认 C# 版，旧版使用 `start_python_client.bat` 启动。GitHub 的 `v*` 标签发布 C# 包，Python 保留手动打包。

[模型接入](#模型接入与网络配置) · [桌面与策略](#桌面功能与执行策略) · [CLI](#命令行参考) · [上下文与验收](#上下文规划与验收) · [开发与发布](#开发与发布) · [技术架构](#能力与技术架构)

## 模型接入与网络配置

使用 OpenAI-compatible Chat Completions 接口，并要求模型支持 function/tool calling：

Agent 设置 →“模型接入”可直接填写 API Base URL、模型和 API Key。API Key 只写入 Windows 凭据管理器（目标 `SciDevHarness/provider/default`），不写入 `.research/provider.json` 或审计日志；保存后下一次任务生效。项目内只保存地址、模型、超时、temperature 等非敏感字段。没有保存配置时继续使用下面的环境变量；保存了配置时环境变量作为 API Key 的后备。CLI 显式传入 `--api-base`/`--api-key`/`--model` 时会强制走环境变量路径，便于临时覆盖。推理强度可选“默认”/none/low/medium/high/max；只有 endpoint 支持时才应使用。“探测能力”会请求 `/models` 并发送一次极小的 `ping` 工具调用，显示工具调用、上下文长度和视觉元数据；这些信息由 endpoint 报告，不保证准确。可填写“回退模型”：主模型返回 HTTP 404 或模型不存在的 HTTP 400 时，本次任务自动用回退模型重试一次；其他错误不回退。

Agent 设置里的“任务预算”可限制单次任务最多模型轮次（1–64）和最多 token（0 = 不限）。token 预算优先使用服务端返回的 usage，并按任务起点扣除历史累计；没有 token 报告时，按本次任务请求/响应的 UTF-8 字节数除以 4 估算并继续执行预算上限。用尽后任务失败、改动保留在工作区且不自动提交。轮次上限始终受 Harness 内置的 32 轮硬上限约束。

```powershell
$env:SCIDEV_API_BASE="https://your-endpoint/v1"
$env:SCIDEV_API_KEY="your-key"
$env:SCIDEV_MODEL="your-model"
.\start_python_client.bat
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

## 桌面功能与执行策略

### 窗口与编辑

#### 编码会话

在右侧 Agent 对话栏输入任务，Agent 自主读取文件、写代码、修复错误
#### 会话恢复

Agent 设置列出最近会话，选择并恢复后，下一条消息继续该会话的历史上下文；不会自动重放已完成的工具调用
#### 界面方案

Studio、Paper、Focus 三套可切换并记住选择的外观/布局配置
#### 工作台

响应式分栏、按主题记住拖动宽度；Agent 设置独立滚动，欢迎页不再伪装成代码，打开文件后自动隐藏欢迎标签
#### 设置页

左下角齿轮 / `Ctrl+,` / `> settings` 打开；分类与关键词搜索、只看已修改、单项及整体恢复默认。主题、强调色、字体/字重/字号、缩放、密度、侧栏尺寸和编辑器显示即时应用并保存到本机；Agent 配置仍复用项目设置。
#### 代码高亮

按文件类型区分 Python、HTML（含 CSS/JavaScript）、JS/TS、CSS、JSON、Markdown 和 YAML；属于词法高亮。Python 文件使用常驻 Ruff LSP 进程异步诊断，把 diagnostics 放进“问题”面板，双击可跳到行列；同一窗口复用进程并合并连续输入。TypeScript/JavaScript 文件在检测到 `typescript-language-server`（或设置 `SCIDEV_TYPESCRIPT_LSP` 命令）时使用独立常驻 LSP，Python/TS 进程互不重启。`Ctrl+Space` 用本机 Jedi 显示 Python 补全，`Ctrl+F12` 跳转到工作区内定义。这是本地 Python/可选 TypeScript 智能，不是完整 IDE 语言服务（无重命名/跨文件引用；F5 在集成终端启动标准库 `pdb`，不是 DAP 图形调试器）。
#### 调试

打开 `.py` 后按 `F5`，或命令面板输入 `> debug`/`> 调试`，使用项目 `.venv` 解释器在集成终端运行 `python -m pdb <相对路径>`；启动时会自动在当前光标行设置断点并 `continue`，之后可在终端里输入 `b`、`n`、`s`、`c` 等 pdb 命令，或用“更多 → Pdb 控制”发送继续/单步跳过/单步进入/跳出/退出。未保存修改会提示先保存；终端已有程序时拒绝启动。没有断点图形界面、变量面板或调用栈视图。
#### 错误趋势

命令面板输入 `> errors` 打开只读面板，统计 1–90 天内任务开始/完成/失败、成功率、按错误分类的分布、可重试标记和每日失败 sparkline；数据来自本机 `.research/runs.db`，不上传。
#### 文件打开

资源树或编辑器标签右键 HTML →“在浏览器中打开”，无需服务器即可查看本地网页；其他文件可“使用默认程序打开”。双击仍进入编辑器；外部程序读取磁盘版本，未保存修改请先 `Ctrl+S`。
#### 版本树

专注画布、可折叠节点详情和 Agent 面板，2.5D 卡片与动态连线；尝试分支可自由拖动并记住位置
### 工具与扩展

#### 工具调用

`search_code`、`get_project_context`、`list_files`、`read_file`、`write_file`、`replace_in_file`、`update_plan`、`run_command`、`git_diff`；执行策略可选只读、手动审批、工作区可写或完全访问
#### MCP 工具

在 `.research/mcp.json` 中显式设置 `"enabled": true` 并配置 stdio server；启动后把 server 的 tools 以 `mcp__<server>__<tool>` 暴露给模型。每次调用仍走命令审批；只读模式禁用；单个 server 启动失败只记录错误，不影响内置工具。HTTP 服务把 `command`/`args` 换成 `"url": "http://127.0.0.1:3000/mcp"`（可选 `"transport": "http"`），支持 JSON/SSE 响应；网络策略为 `deny` 时拒绝 HTTP 服务。示例：
```json
{"enabled": true, "servers": {"demo": {"command": "python", "args": ["tools/mcp_server.py"], "cwd": "."}}}
```

#### 查看 MCP 能力

`.\.venv\Scripts\python.exe -m scidev.cli mcp --workspace . --json` 启动服务并列出 tools/resources/prompts；`--read SERVER URI` 读取资源，`--prompt SERVER NAME --arg key=value` 渲染 prompt。桌面命令面板输入 `> mcp` 打开图形浏览器，可查看工具/资源/提示词、读取内容并显式插入输入框；不会自动注入模型。

#### 生命周期 hooks

`.research/hooks.json` 显式启用后可配置 `task_start`、`before_tool`、`after_tool`、`before_commit`；命令不经 shell、工作区内执行、有超时和输出上限。`task_start`/`before_tool`/`before_commit` 非零退出会阻止继续，`after_tool` 失败只记录。事件与有界 JSON 上下文通过 `SCIDEV_HOOK_EVENT`/`SCIDEV_HOOK_CONTEXT` 传入。

### 补丁、终端与隔离任务

#### 补丁审核

聊天卡片逐块勾选；可打开“补丁 diff”编辑器标签，彩色显示增删行并在同一处勾选、应用或拒绝；拒绝或部分批准会停止当前任务
#### 终端

Windows 优先使用 ConPTY（`pywinpty`）运行命令，保留交互式 stdin；ConPTY 不可用时回退到隐藏窗口管道。终端输出做 ANSI 清理，命令结束显示退出码
#### 图片附件

聊天输入区“图片”可附加 PNG/JPEG/GIF/WebP（最多 4 张、单张 5 MB、总计 12 MB），随任务作为多模态 `image_url` 发送；CLI 用重复的 `--image` 传图。图片以 data URL 保存到会话，模型必须支持视觉输入
#### 隔离 worktree

命令面板输入 `> new worktree`（或“新建 worktree”/“隔离工作区”），在当前 Git 仓库创建 `scidev/agent-gui-*` 分支，并打开一个独立窗口。两个窗口可以并行跑任务，文件和提交互相隔离；worktree 默认保留，需要手动清理。
#### 任务锁

同一项目同时只允许一个编码任务（进程内线程锁 + `.git/scidev-task.lock` 跨进程锁）；第二个进程直接失败并提示改用独立 worktree，避免两个窗口互相覆盖或一起提交。
#### 管理 worktree

`> worktrees` 打开列表，可查看分支、路径、HEAD，打开或删除；删除有未提交改动时会失败，勾选“强制删除”才忽略改动并删除分支。
### 评测、上下文与记录

#### 真实仓库 eval

`.\.venv\Scripts\python.exe -m scidev.cli eval --suite evals/standard/suite.json --output .research/evals/report.json --jobs 2` 会在隔离副本里跑固定任务，用确定性 check 判断成败，报告完成率、平均轮数、prompt/completion token、耗时、相对上次的回归和按定价估算的成本。`evals/smoke/` 是 3 个冒烟任务，`evals/standard/suite.json` 是 20 个固定任务；测试会确认每个任务在修复前确实失败。价格可在 suite 写 `"pricing": {"input_per_million": 1.0, "output_per_million": 2.0}`，或用 `SCIDEV_EVAL_INPUT_PRICE`/`SCIDEV_EVAL_OUTPUT_PRICE` 覆盖。
#### 本地弱模型基线

`evals/local/suite.json` 自动打开 `SCIDEV_TEXT_TOOL_CALL_FALLBACK=1` 并把轮次限制为 8；本机 Qwen2.5-Coder-7B 的 3 项冒烟结果为 **1/3**（无 fallback 时 0/3），记录在 `evals/baselines/local-qwen2.5-coder-7b.json`。这是弱模型基线，不代表强模型效果；Ollama 不返回 usage，token/成本为 0。
#### 项目上下文

按任务关键词（含有限中英软件术语映射）与当前文件自动定位相关代码，附带项目结构、依赖声明、Git 状态和适用目录指令；点击聊天输入区 `@` →“查看项目上下文”可预览、刷新
#### 任务规划

右侧计划面板提供自动规划、必须先规划、关闭三种模式；计划步骤随会话保存，重试可恢复，未完成计划不会被当作成功提交
#### 本地任务队列

最多 6 次尝试（含首次）；429/5xx 优先遵从服务端 `Retry-After`，否则指数退避，进程重启后可恢复。相同任务/请求体重试保持幂等键，任务或请求体改变则更换键；服务端可能不支持幂等键，不能保证网络中断时绝不重复计费
#### Git 集成

查看状态和提交记录；自动提交仅包含任务开始时干净、且本次会话改动的文件，不会把已有暂存或未提交文件混进来
#### 过程记录

`.research/events.jsonl`、`.research/runs.db`、`.research/sessions/`

### 执行与文件保护

#### Git 与文件保护

Agent 发给模型的 Git 状态/diff 会隐藏常见凭据路径（包括重命名涉及凭据文件的整个差异）；Agent 自动提交会跳过敏感/内部路径、符号链接或目录联接、超过 240,000 字节的文件，以及二进制/非 UTF-8 文件，并在聊天记录中说明，文件仍留在工作区供你从 Git 面板审核。`.env.example` 等模板不受凭据过滤。Git 面板里的手动 diff 与提交是用户主动操作，不受 Agent 自动筛选策略限制。

任务检查点：Agent 在每次写文件前把原始内容保存到 `.research/checkpoints/<id>/`；任务结束后可在 Agent 设置里点“回滚上次任务改动的文件”，只恢复本次任务实际写过的文件，并删除任务开始前不存在的新文件。CLI 可用 `--restore-on-failure` 在任务失败时自动回滚。回滚会覆盖你在任务开始后对这些文件做的修改；检查点只保留最近 10 个，不替代 Git。

其他安全边界：文件工具会阻止项目根目录之外的路径和常见凭据文件（例如 `.env`、`.ssh`、`.npmrc`）。文本编辑只接受有效 UTF-8，不会把含 NUL/非法编码的文件当文本覆盖；完整重写会拒绝超大既有文件，文本替换会校验结果大小和 NUL 字节。默认 `local` 执行后端不是操作系统级沙箱：批准后的命令仍以当前用户权限执行；需要更强边界时可选 Docker 后端。

执行策略在 Agent 设置中选择，默认“工作区可写”：

- **只读**：禁止写文件和运行终端命令，适合审查、问答。
- **手动审批**：允许写文件，但每条终端命令都需确认。
- **工作区可写**：允许写文件；自动批准可识别的 pytest/unittest、npm/pnpm/yarn test、ruff/eslint、mypy/pyright、`tsc --noEmit` 等验证命令，其他命令仍需逐条确认。
- **完全访问**：自动批准所有终端命令，仅保留破坏性命令和 Harness 自有 Git 操作的拦截；只应在可信项目中使用。

Agent 设置里另有“网络策略”，只对 `curl`/`wget`/`ssh`/`scp` 等识别出的网络命令生效：

- **拒绝**：硬阻断，即使完全访问也不能执行。
- **询问**：手动批准后执行；工作区可写不会自动执行。
- **允许**：工作区可写/完全访问自动执行。

默认是“询问”，可用 `SCIDEV_NETWORK_POLICY=deny|ask|allow` 覆盖。Agent 设置中的“命令白名单”可填精确前缀（`;` 分隔，例如 `pytest -q; ruff check .`）；匹配的命令在工作区可写/完全访问下自动执行，仍受破坏性命令、网络策略和路径检查约束。也可用 `SCIDEV_ALLOWED_COMMANDS` 覆盖。自动批准只接受单条、无 shell 链接/管道/重定向、无绝对路径或上级目录、无网络下载的验证命令。测试和 lint 输出会解析成文件/行号/错误码的结构化诊断，附加在 `run_command` 结果后供模型继续修复。策略层不是网络或文件系统沙箱，不能保证已批准命令无法访问项目外资源。

可选 Docker 执行后端：`--sandbox docker --sandbox-image <含项目工具链的镜像>`（或 `SCIDEV_SANDBOX_BACKEND=docker`、`SCIDEV_SANDBOX_IMAGE`）。桌面端 Agent 设置也有“命令沙箱/沙箱镜像”，保存后对新命令生效；选 Docker 时必须填镜像。命令改为 `docker run --rm --init`，只挂载当前工作区到 `/workspace`，默认 `--network none`、`--cap-drop ALL`、`no-new-privileges`，并限制 pids/内存/CPU；容器内以 `sh -lc` 执行。Docker CLI/daemon/镜像不可用或后端名非法时直接拒绝执行，不会静默回退到本机。镜像必须自带项目所需解释器/依赖；默认 `local` 后端行为不变。该后端未在本机 Docker daemon 上做真实项目 E2E；需要网络时必须显式设 `SCIDEV_SANDBOX_NETWORK=bridge` 且宿主网络策略为 `allow`，否则直接拒绝，不能靠容器绕过策略。任务前可用 `python -m scidev.cli sandbox-check --backend docker --image <镜像> --json` 检查 CLI/daemon/镜像；`local` 会返回 `ready=true` 但 `os_sandbox=false`，明确表示不是 OS 沙箱。`python -m scidev.cli doctor --workspace . --json` 另做离线环境诊断：运行时/PySide6、provider 模型与密钥来源（不打印密钥）、Git 状态、沙箱边界和本机 LSP 工具；加 `--tests` 只静态推断 test/lint/typecheck 命令，不执行；加 `--online` 才会请求 `/models` 并做一次最小 tool-call 探针（会产生少量 token）。若 endpoint 是 loopback 且原生 tool-call 探测失败，会提示本地模型可尝试 `SCIDEV_TEXT_TOOL_CALL_FALLBACK=true`。

## 命令行参考

### 执行任务

不启动 Qt，可用于脚本、CI 和评测：

```powershell
.\.venv\Scripts\python.exe -m scidev.cli exec `
  --workspace "D:\Projects\my-project" `
  --task "修复登录接口的空指针并运行相关测试" `
  --execution-mode workspace-write `
  --json
```

| 参数 | 用途 |
|---|---|
| `--execution-mode` | `read-only`、`manual`、`workspace-write`、`full-access`；与桌面端共用策略。 |
| `--sandbox` / `--sandbox-image` | 选择 `local` 或 Docker 后端；未指定时读取 `SCIDEV_SANDBOX_BACKEND`。 |
| `--max-turns` / `--max-tokens` | 本次任务的轮次与 token 预算。 |
| `--result-file PATH` | 将最终 JSON 原子写入工作区；越界路径拒绝并返回 1。 |
| `--patch-approval auto\|reject` | 自动应用已校验补丁块，或拒绝修改已有文件；仍受路径与执行策略限制。 |
| `--no-verify` | 关闭提交前验证门。 |
| `--restore-on-failure` | 失败时恢复本次任务捕获的文件检查点。 |
| `--allow-no-changes` | 允许问答/分析任务不产生修改；默认无修改任务返回 `status=no_changes`。 |
| `--api-base` / `--api-key` / `--model` | 临时覆盖本次请求使用的模型配置。 |

成功退出码为 0，失败为 1，参数错误为 2，取消为 130。会话、执行记录和 Git 自动提交与桌面端共用。

`--worktree` 会把任务放进 `.research/worktrees/<id>` 的独立 Git worktree，分支为 `scidev/agent-<id>`；返回 JSON 包含 `worktree`/`worktree_branch`/`worktree_base_sha`。任务结束后 worktree 默认保留，便于检查、合并或丢弃；需要清理时用 `git worktree remove` 和 `git branch -D`。`--worktree-id` 可指定稳定 id，避免重复创建。

### 只读 MCP 服务

```powershell
.\.venv\Scripts\python.exe -m scidev.cli mcp-serve --workspace .
```

以 MCP stdio 暴露项目的 search/read/list/context/git-diff 能力；不暴露写入文件或执行命令的工具。

### 创建 PR

在任务分支或 worktree 内运行：

```powershell
.\.venv\Scripts\python.exe -m scidev.cli pr --workspace .research/worktrees/login --title "登录页" --draft --json
```

- 自动从 `origin` 识别 GitHub `owner/repo`
- 默认 base 取 `origin/HEAD`，否则 `main`
- push 当前分支并调用已登录的 `gh pr create`
- `--dry-run` 只打印计划，不联网
- 项目网络策略为 `deny` 时需要显式 `--allow-network`
- 不在 `main`/`master` 上创建 PR，避免直接把主分支当 head

### 检查更新

```powershell
.\.venv\Scripts\python.exe -m scidev.cli update --workspace . --json
.\.venv\Scripts\python.exe -m scidev.cli update --fail-if-available
```

从 `origin` 推断 GitHub 仓库（可用 `--repo owner/repo` 或 `SCIDEV_UPDATE_REPO` 覆盖），读取 latest GitHub Release 的 tag、页面、说明和 HTTPS 安装包地址，并报告名称、大小和 `digest`；只提示、不安装。桌面端命令面板输入 `> update`/`> 检查更新` 会显示可取消的后台检查进度，完成后打开同一检查结果对话框，可打开 Release 页面，或选择保存路径后在后台下载并校验 SHA-256，下载中可取消；下载完成只显示文件路径，不执行安装包。`--download PATH` 会把安装包下载到 PATH（或已存在目录）并以 `digest` 或显式 `--sha256` 校验，声明大小、重定向到 HTTP、超限、摘要不符或空文件都会拒绝；`--install` 必须与 `--download` 一起用，下载校验后再校验一次摘要并启动安装器 UI（不静默安装、不提权）；不传 `--install` 时只报告路径和摘要，不执行安装包。`--fail-if-available` 在发现新版本时返回 1，便于脚本检查。也可调用 `scidev.integrations.update.verify_downloaded_asset(path, sha256)` 校验已有文件。

### 崩溃报告

桌面端启动后安装本地异常钩子。未捕获异常写入 `.research/crashes/crash-*.json`，包含版本、Python、平台、线程、异常类型和有界 traceback；API key/token/password 和用户主目录会先脱敏。保留最近 20 份，不上传网络。

### 错误分类

工作线程和 CLI 失败会附带稳定分类：

- `auth`：认证/权限
- `rate_limit`：限流/额度
- `timeout`：超时
- `network`：网络
- `approval`：需要审批但当前界面/模式无法批准
- `tool`：工具参数/补丁
- `verification`：测试/lint/typecheck
- `workspace`：路径/文件/Git
- `internal`：未知内部错误

事件和 CLI JSON 含 `error_kind`、`user_message`、`retryable`、`detail`；桌面失败气泡会显示非 internal 分类前缀。

### 会话与版本迁移

```powershell
.\.venv\Scripts\python.exe -m scidev.cli migrate --workspace . --dry-run --json
.\.venv\Scripts\python.exe -m scidev.cli migrate --workspace .
```

迁移 `.research/settings.json`、会话 JSON 和事件 SQLite 的 schema：

- 写入 `schema_version`
- 规范 execution/network/allowlist 设置
- 补全会话的 `messages`/`attachments`/`status`
- 设置事件库 `PRAGMA user_version`
- 修改前备份到 `.research/backups`，每个文件最多保留 5 份
- 幂等；第二次运行不产生改动
- 坏 JSON 只报告错误，不覆盖原文件

列出最近会话，再用同一个 `--session-id` 继续该会话的上下文：

```powershell
.\.venv\Scripts\python.exe -m scidev.cli sessions --workspace "D:\Projects\my-project" --json
.\.venv\Scripts\python.exe -m scidev.cli exec --workspace "D:\Projects\my-project" --session-id <session_id> --task "继续修复剩余失败测试"
```

## 上下文、规划与验收

### 项目上下文与环境核验

发送任务后自动生成项目概况与相关文件片段；继续任务、网络重试和 Agent 修改文件后刷新同一份上下文，不累积旧的目录快照。当前编辑器文件优先检索，选区仍随任务附上；敏感或内部文件不自动引用。未保存的打开文件会以有界只读快照进入检索与 `read_file`；预算外的草稿会从检索中排除，也不会回退读取磁盘旧版本。Agent 不会写入任务开始时有未保存修改的文件，保存后重新发起即可修改。

项目上下文会静态推断 Python pytest/unittest、Node 根/子 package 与 `test:*` 脚本、嵌套 Go/Rust/Maven/Gradle/.NET 项目，以及 tox/nox、Makefile、Justfile 中明确的测试入口；候选附证据并限定到对应子项目，本地普通 `.venv` 解释器存在时优先引用。对 `.venv`/`venv`/`env` 的 `pyvenv.cfg` 只读取 Python 创建版本及共享系统包标志，不执行解释器或泄露配置中的绝对路径；这不等于实际解释器/依赖核验。上下文页“核验环境”可在用户主动点击并批准一次后，检查项目 Python venv 和根清单所声明的 Node/Rust/Go 工具链版本，并在本机可用时探测 NVIDIA/CUDA 工具；全程不联网、不安装/解析依赖、不运行项目脚本、构建或测试，也不会随打开项目、生成上下文或编码任务自动触发。Python 不回退到 Harness 解释器；其他工具只运行固定版本参数。`pip check` 不证明项目声明依赖齐全/可导入；Node/Rust/Go 版本可用不代表工程可构建；GPU/CUDA 工具存在也不代表框架能实际使用 GPU。目前只检查根清单，未发现嵌套 monorepo 的运行时配置。普通测试发现仍为静态候选；执行时按当前执行策略处理：工作区可写会自动批准可识别的验证命令，手动审批和完全访问模式分别逐条确认或全部自动放行。当模型结束任务但代码改动尚未验证时，Harness 会在工作区可写/完全访问模式下自动选择第一个安全的 test/lint/typecheck 候选运行一次，把退出码和结构化诊断加入后续修复轮次；同一编辑版本不重复自动运行，最多 3 次。无法安全引用路径、清单冲突、文件或输出预算耗尽时会省略候选并标记范围受限；未知入口仍可能漏检，候选命令和覆盖范围都未实际验证。

`search_code` 是字面文本检索，可指定 `path`、`glob`、大小写和相邻行；例如查 `InventoryManager`，或限定 `path="src"`、`glob="**/*.py"`。`get_project_context` 可按新关键词和准备修改的路径获取概况/目录指令。读取源文件时也会附上所在目录的 `AGENTS.md`；下级指令只适用于该目录及子目录。

编辑器快照最多 8 个文件、合计 1 MB；超预算文件标为只读并从自动检索/读取中排除，避免误用旧磁盘内容。快照不会自动写入工作区源文件。为支持队列/重启重试，待处理或可重试任务的原文快照会暂存在本机 `.research/runs.db`；创建事件不记源码正文，只记快照路径和字节数，任务成功后从队列记录中清除。最终失败但仍可手动重试的任务会保留快照。项目上下文片段和工具读取结果仍按现有会话机制保存在 `.research/sessions/`。项目清单若有未保存修改，也会用于运行时和依赖摘要。

本地缓存按文件时间和大小失效，无需额外模型或下载。默认概况最多 18000 字符，最多发现 5000 个文件、扫描 8 MB 文本，文本源文件上限 2 MB；局部读取最多 400 行/25000 字符，达到预算会提示继续行号。检索截断时可缩小路径或模式。Git 项目使用 Git 自身的忽略规则，非 Git 项目兼容常见 `.gitignore` 模式。当前是关键词/标识符检索，不含向量语义检索或完整依赖图；依赖声明不代表已安装。

### 跨会话记忆参考

新任务可从本项目已完成会话的总结中检索少量相关记录；索引保存在本地 `.research/memory.sqlite3`，不跨项目共享、不额外调用模型或网络。聊天栏 Agent 设置可关闭检索，或调整每次引用的总结条数和上下文长度。检索基于关键词而非向量语义；常见凭据格式会在索引前脱敏，但不能保证识别所有私有凭据。

### 计划的保存与恢复

右侧计划面板可切换“自动规划 / 必须先规划 / 关闭”。每个任务最多 12 个计划步骤，Agent 会更新待处理、进行中、已完成、受阻或已跳过状态。计划随会话保存；网络重试及任务历史重试会恢复原计划，同一会话的新任务会归档上一计划。若最终回答时计划仍未完成，Harness 对连续未完成的回答最多要求继续两次；取得计划进度后重新计数。仍未完成会失败并保留计划，不自动提交。步骤完成状态由模型报告，不替代实际测试或独立验收；暂不支持逐步人工批准和 DAG 拖动。

### 独立语义验收参考

聊天区“Agent 设置”中可选“关闭 / 建议 / 必需”，默认关闭，不会悄悄增加模型调用。启用后会向配置的验收模型发送用户任务、编码模型的最终说明、最多 40 个安全文本变更文件的完整 diff（总计最多 36,000 字符）、任务计划摘要和最近的检查摘要；审查请求不提供文件/命令工具。请注意：这会把变更源码额外发送到当前配置的模型服务。

验收模型 ID 必须与编码模型 ID 不同。它可以仍使用同一个 API 地址和服务商，因此“独立”指隔离的模型审查步骤，不代表机构或数据源独立，也不是形式化正确性证明。模型必须返回逐项判定和引用的变更文件；格式错误、缺少引用、超预算/敏感/不可读 diff，或审查等待期间代码变化，都不能标为通过。

“建议”模式仅报告结论，不阻止现有自动提交；“必需”模式在需求未满足时最多要求 Agent 修正一次，然后重新审查，若审查不可用、不确定、范围不完整或最终不通过，则保留改动且不自动提交。此功能不替代执行策略或人工审批；模型评审结果只作为额外证据。

## 开发与发布

### 测试与后台截图

```powershell
.\.venv\Scripts\python.exe -B scripts/run_tests_coverage.py
.\.venv\Scripts\python.exe scripts/smoke_dependency.py
.\.venv\Scripts\python.exe scripts/smoke_screenshot.py --output-dir "$env:TEMP\scidev-ui-shots"
.\start_python_client.bat --check-runtime
```

Windows CI 会验证 Qt 窗口与 QML 加载、资源管理器双击打开、Ctrl+S 保存、工作区选择/记忆、多窗口行为、shell 拒绝审批，以及版本树分支拖拽、画布平移、滚轮滚动/Ctrl+滚轮缩放；另在宽屏/窄屏渲染编辑器和版本树截图。每周和手动 CI 会保留一周的截图工件，供视觉复查；每次变更仍会跑单测、依赖/编译检查和 QML lint。

### Windows 独立版打包

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

仓库 Actions 中的“Legacy Python Package Verification”是手动触发的完整构建检查：它会在固定的 Windows Server 2022 runner 初始化 MSVC、构建 standalone 并实际启动 exe 验证 QML；随后安装 Inno Setup 6、构建 per-user 安装器并上传 installer 与 SHA-256 manifest。默认 unsigned；配置仓库 secrets `WINDOWS_SIGNING_PFX_BASE64` 和 `WINDOWS_SIGNING_PFX_PASSWORD` 后，workflow 会导入 PFX、调用 `build_installer.ps1 -SignThumbprint` 签名并在结束时删除证书，manifest 的 `signed` 随之为 true。不上传 standalone 二进制；手动 workflow 不创建 Release。GitHub 默认发行版现由 C# Release workflow 发布。需要在本机构建时，装 Inno Setup 6 后运行 `scripts/build_installer.ps1 -Deployment <staged deployment> -Version <x.y.z>`；它只构建安装器并可选调用签名脚本，不下载依赖、不执行安装器、不自动安装。清单明确标记需人工审核，并不代表许可证合规结论。构建固定使用 `requirements-build.txt` 中的 PySide6-Essentials 6.11.2，Dependabot 每周检查依赖更新，升级后需重新通过打包烟测。公开分发前仍要按实际打包组件补齐 Qt/PySide 第三方许可与声明；Nuitka 编译器受 AGPLv3 约束，其 [Runtime Library Exception](https://github.com/Nuitka/Nuitka/blob/develop/LICENSE-RUNTIME.txt) 对符合条件的编译输出另有许可，二者不能混为一谈。详见 [Qt 官方部署说明](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html) 与 [Nuitka 4.1.1 元数据](https://pypi.org/project/Nuitka/4.1.1/)。以上不是法律意见。

### 发布签名

```powershell
powershell -File scripts/sign_release.ps1 -Path dist -Thumbprint <40位证书指纹>
powershell -File scripts/sign_release.ps1 -Path dist -Thumbprint <40位证书指纹> -VerifyOnly
```

脚本对 `.exe/.dll/.msi` 做 SHA-256 Authenticode 签名和 RFC 3161 时间戳，随后用 `/pa` 验证。证书需事先装入当前用户证书存储；脚本不接受密码或 pfx 文件，也不在 CI 中自动签名。时间戳默认 `http://timestamp.digicert.com`：本机 `signtool` 对 HTTPS URL 报 `Invalid Timestamp URL`，而 TSA 响应本身仍由 `signtool` 校验；本地 `signtool` 支持 HTTPS 时可显式传 `-TimestampUrl https://...`。

查看和审查：

```powershell
.\.venv\Scripts\python.exe -m scidev.cli pr-view --workspace . --number 12 --json
.\.venv\Scripts\python.exe -m scidev.cli pr-review --workspace . --number 12 --action comment --body "请补测试"
.\.venv\Scripts\python.exe -m scidev.cli pr-review --workspace . --number 12 --action approve
```

`pr-view` 读取编号、标题、状态、作者、head/base、review decision 和 mergeable；`pr-diff` 读取有界 diff（默认 30 万字符，最多 100 万）；`pr-review` 支持 `approve`、`comment`、`request-changes`，comment/request-changes 必须提供 body。三者都受网络 deny 约束，需要 `--allow-network` 才放行。桌面命令面板 `> pr` 可加载 PR 元数据和 diff、查看 review 状态并提交 review。

### 许可证与第三方组件

- 项目代码采用 [Apache-2.0](../LICENSE)；Qt/PySide6 与可选模型权重使用各自许可证，模型权重不随仓库分发。
- 运行时依赖只选 PySide6-Essentials（项目使用的 Qt Widgets/QML/QuickWidgets），不安装未使用的 PySide6-Addons；两者是独立发行组件，详见 [Qt for Python 包结构](https://doc.qt.io/qtforpython-6/package_details.html)。
- 源码仓库不含 Qt/PySide6 运行时二进制。Qt for Python 社区版依组件采用 LGPLv3、GPLv3 等许可证，也提供商业授权。
- 若今后分发独立可执行文件，须审核实际打包组件并随包提供对应版权、许可证和第三方声明；不能把这些运行时组件误标为 Apache-2.0。

详见 [Qt for Python 官方许可说明](https://doc.qt.io/qtforpython-6/commercial/index.html)、[第三方许可证清单](https://doc.qt.io/qtforpython-6/licenses.html) 和 [LGPLv3 说明](https://doc.qt.io/qtforpython-6/overviews/qtdoc-lgpl.html)。以上不是法律意见。

## 能力与技术架构

以下保留能力盘点与实现边界；历史性能和评测数字按原记录保留，后续变更及验收日期以[历史与结果](HISTORY_RESULTS.md)为准。

版本树通过 Qt Quick/QML 与 QQuickWidget 绘制；文件树、编辑器和 Agent 面板使用 Qt Widgets，桌面端不需要额外前端运行时。

### 源码目录

```text
scidev/             Python 应用代码
  agent/            Agent 执行、工具、会话、计划与验证
  desktop/          桌面窗口、编辑器、面板与界面工作流
  workspace/        Git、补丁、检查点、项目检索与环境诊断
  integrations/     模型、浏览器、LSP、MCP、PR 与更新
  runtime/          进程、队列、持久化、配置与运行支持
  evaluation/       评测与结果统计
scripts/            启动准备、测试、冒烟与发布工具
ui/                 QML 与图标资源
tests/             自动化测试
docs/              当前工作、历史结果与能力说明
evals/             评测题集
examples/          示例项目
packaging/         安装器配置
```

源码使用 `scidev.*` 包路径导入。根目录的 `scidev_client.py`、`scidev_cli.py` 是兼容启动入口；也可运行 `python -m scidev` 启动桌面端，或用 `python -m scidev.cli --help` 查看命令行。维护进度见 [当前工作](CURRENT_WORK.md)，已完成记录见 [历史与结果](HISTORY_RESULTS.md)，模块说明见 [能力说明](SCIDEV_HARNESS.md)。

### 能力状态总表

| 能力 | 状态 | 已实现 | 尚缺 / 边界 |
|---|---|---|---|
| Agent 循环 | ✅ 已实现 | 最多 32 轮模型/工具交互；工具结果回传给模型，允许根据报错继续处理；结束时核对 Git diff | 模型仍可能误判；达到轮数上限会失败，不保证一次完成 |
| 会话恢复 | ✅ 基础已实现 | Agent 设置列出最近会话，恢复后复用原 session id 和 messages 继续对话；CLI `sessions` 列出会话，`exec --session-id` 继续；不会自动重放已完成的工具调用 | 恢复的是会话上下文，不是完整 UI 消息回放；不自动恢复已中断的命令/进程；会话文件损坏时拒绝恢复 |
| 任务预算 | ✅ 基础已实现 | Agent 设置可配置单任务最多模型轮次（1–64）和最多 token（0 = 不限）；token 按任务起点扣除历史累计，用尽后失败并保留未提交改动；服务端不返回 usage 时（如 Ollama）按任务内请求/响应 UTF-8 字节数除以 4 估算并继续执行硬限，事件标记 `estimated=true` | 估算不是真实计费；字节/token 比例因语言和 tokenizer 而异；没有货币成本换算；预算用尽只停止任务，不自动回滚已改文件 |
| 代码智能 | 🟡 Python 诊断/补全/跳转已实现，TypeScript 诊断可选 | Python 文件使用常驻 Ruff LSP 进程，异步发送 didOpen/didChange 并把诊断显示在“问题”面板；同一窗口复用进程，输入请求合并；双击跳到行列。TypeScript/JavaScript（`.ts/.tsx/.js/.jsx/.mjs/.cjs`）在找到 `typescript-language-server` 或设置 `SCIDEV_TYPESCRIPT_LSP` 时使用独立常驻进程，Python/TS 互不重启。编辑器 `Ctrl+Space` 用本机 Jedi 显示补全，`Ctrl+F12` 用 Jedi 跳转到工作区内定义；原有 `F12` 符号跳转保留。语法错误仍由 AST 即时显示。`F5` 在集成终端用项目解释器启动标准库 `pdb`，自动在当前光标行设断点并继续，支持交互式单步/继续；“更多 → Pdb 控制”可发送 continue/step/return/quit | Ruff/TS 进程按窗口常驻，不是全局共享；TS 服务器未安装时静默跳过，未做真实 TS 项目 E2E；Jedi 只支持 Python，补全同步执行、首次可能较慢；无重命名/引用/调用层级；pdb 只作为终端便捷入口，不计划做 DAP 图形调试器；Codex 本身也没有内置调试器，VS Code 调试器属于宿主 IDE；Ruff 或 Jedi 未安装时静默跳过；诊断不保证每个版本都送达 |
| 模型接入 | ✅ 基础已实现 | Agent 设置内可填写 API Base URL、模型、推理强度、回退模型和 API Key；Key 存入 Windows 凭据管理器，非敏感配置存 `.research/provider.json`；环境变量作为后备；内置 OpenAI、DeepSeek、Anthropic Claude（兼容层）、OpenRouter、Ollama/LM Studio/llama.cpp 预设与输入框模型下拉，本地服务自动探测；CLI 显式 provider 参数强制覆盖；可异步测试 `/models` 连通性，并用一次极小 ping 工具调用探测工具调用/上下文/视觉元数据；HTTP 404/模型不存在 400 时自动用回退模型重试一次 | 不是 OpenAI/ChatGPT 账号登录或 OAuth；Key 仍可被同一 Windows 用户下的进程读取；只支持 OpenAI-compatible Chat Completions；能力探测依赖 endpoint 报告且会消耗少量 token；回退只覆盖模型解析错误，不覆盖认证/限流/网络错误 |
| 编码工具 | ✅ 核心已实现 | `search_code`、`get_project_context`、`list_files`、`read_file`、`write_file`、`apply_patch`、精确/批量 `replace_in_file`、`run_command`、`git_diff`；按用户意图启用网页搜索/打开/点击/截图；可选视觉制品检查 | 网页操作逐次审批且不复用登录态；截图只本机保存；符号提取不是完整语言服务器 |
| MCP 扩展 | 🟡 stdio/HTTP 工具已接入，另有只读服务端 | `.research/mcp.json` 显式启用后启动 stdio server 或 HTTP server，`initialize`/`tools/list`，把工具以 `mcp__<server>__<tool>` 加入模型工具列表；`tools/call` 结果回传；支持 JSON/SSE 响应和 `Mcp-Session-Id`；CLI `mcp` 和 GUI `> mcp` 可列出 tools/resources/prompts、读取资源、渲染 prompt 并显式插入输入框；每次工具调用仍走命令审批；只读模式禁用；网络策略 deny 拒绝 HTTP server；失败只记录错误；CLI `mcp-serve` 可按 MCP stdio 只读暴露 search_code/read_file/list_files/get_project_context/git_diff | 不支持 WebSocket MCP；资源/prompt 不会自动进入模型上下文；无 MCP 采样；无配置向导；HTTP 只实现 POST 请求/响应，不是完整流式会话；服务配置由用户文件信任；mcp-serve 只读、无认证、仅 stdio |
| 生命周期 hooks | 🟡 基础已实现 | `.research/hooks.json` 显式启用后可配置 `task_start`/`before_tool`/`after_tool`/`before_commit`；不经 shell、工作区内、超时和输出有界；前三个事件非零退出阻止继续，`after_tool` 只记录；事件与有界 JSON 上下文经环境变量传入 | 只有固定四个事件；无异步/并行 hooks；无 hooks 的 UI 配置或权限声明；hooks 命令由项目配置文件信任，不是 OS 沙箱 |
| 项目上下文 | ✅ 核心已实现 | 本地增量缓存、代码/标识符检索、任务相关片段与符号、中英术语及复合短语加权（含旋转/碰撞等游戏机制）、环境/依赖/目录指令；未保存编辑器缓冲参与预览、搜索和读取，Agent 对这些文件只读；任务/重试/编辑后刷新 | 有界关键词检索，不含向量索引或完整依赖图；快照最多 8 个文件/1 MB，超预算的草稿排除且禁止读取旧磁盘内容；扫描/输出有界；缓存仅在当前进程内 |
| 任务规划 | ✅ 核心已实现 | Agent `update_plan` 工具、自动/必须/关闭三种模式、步骤状态、右侧计划面板、会话持久化与重试恢复；新任务会归档上一计划 | 没有逐步人工批准、可拖动 DAG 或独立验证模型报告的完成状态；科研实验仍不在范围 |
| 记忆与过程记录 | ✅ 核心已实现 | SQLite 任务/会话索引、JSONL 事件、会话 transcript 与最终总结；新任务可从本项目旧总结中词法检索，限制数量/长度，重试复用快照 | 非向量语义检索；失败且未生成最终总结的尝试不会成为记忆；密钥脱敏仅覆盖常见格式 |
| 可观测性 | 🟡 已实现 | `> errors` 打开只读错误趋势：统计 1–90 天内任务开始/完成/失败、成功率、按 `error_kind` 分布、可重试标记和每日失败 sparkline；数据直接读 `.research/runs.db` | 只统计本机本地事件；不区分模型/任务类型；旧事件无 `error_kind` 时按错误文本重新分类；没有导出/告警/远程遥测 |
| 执行与验证反馈 | 🟡 核心证据闭环已实现 | 静态发现根/嵌套项目测试命令、Node 自定义测试脚本、tox/nox 与 Make/Just 入口；经批准命令记录状态并绑定当前代码指纹；代码改动可要求当前代码检查通过；执行策略支持只读/手动审批/工作区可写/完全访问，工作区可写自动批准可识别的验证命令；网络策略 deny/ask/allow 对 curl/wget/ssh 等命令硬阻断或按策略放行；项目命令白名单按精确前缀自动批准；pytest/ruff/mypy/tsc 输出解析为文件、行号、错误码的结构化诊断并回传模型；任务结束时 Harness 可按 test/lint/typecheck 类别各选一个安全候选自动运行，失败诊断进入后续修复轮次；CLI 无审核界面时默认自动应用已校验补丁块（`--patch-approval reject` 可关闭）；弱模型只输出完整 `*** Begin Patch` 文本时会转成 `apply_patch` 调用，仍走原路径校验和审核；可选 Docker 后端把命令放进只挂载工作区、默认断网、cap-drop/资源受限的容器 | 候选不是已验证命令；自动批准只覆盖单条验证命令，不自动安装依赖；网络识别基于命令词法，间接联网（测试代码、包管理器）无法拦截；白名单只做前缀匹配，用户需自行确认前缀范围；未知运行器/脚本约定仍可能漏检；退出码不证明目标语义正确；文本补丁恢复只接受完整且有文件头的块；默认 local 后端不是 OS 沙箱；Docker 后端要求 daemon/镜像，未做真实项目 E2E |
| 独立语义验收 | 🟡 可选核心流程已实现 | 可关闭/建议/必需；向不同模型 ID 发送任务、完整受限 diff、验证摘要；结构化标准须逐项引用变更文件；必需模式不通过不自动提交，并可有限修复后复核；复核期间 diff 变化会作废结论 | 默认关闭且会增加一次 LLM 调用；同一服务商的不同模型 ID 不等于机构独立；模型判断不是形式化正确性证明；不完整/超限 diff 不调用模型且必需模式阻止提交 |
| 环境感知 | 🟡 项目上下文与按需工具链核验已实现 | 区分 Harness 与项目环境；识别 OS/架构、Python/Node/Rust/Go 版本约束、依赖清单/锁文件；有界读取 `pyvenv.cfg` 安全元数据；上下文页“核验环境”按根清单检查 Python 项目虚拟环境、Node、Rust、Go 版本，附加检查可用的 NVIDIA/CUDA 工具；整组一次审批、异步执行并记录结果 | Python 不回退到 Harness 解释器；不联网、不安装/解析依赖、不运行项目脚本或构建/测试；`pip check` 不验证清单声明依赖齐全/可导入，工具链版本不证明项目可编译/运行；目前只发现根清单，未支持嵌套 monorepo 运行时发现 |
| 精确代码修改 | ✅ 核心已实现 | `apply_patch` 解析 Codex 风格补丁并在聊天中逐块预览/勾选；可打开“补丁 diff”编辑器标签，彩色显示增删行并在其中勾选/应用/拒绝；路径、上下文唯一性、文件摘要与大小受校验；Agent 开始时已存在的文件强制走此审核，新建文件可在任务内迭代 | 补丁受文件数、块数和字节预算限制；编辑器 diff 标签只读，不是主编辑器内联逐行编辑；新建文件的后续内容仍可直接迭代 |
| 终端 | 🟡 ConPTY 已实现 | Windows 优先用 `pywinpty` 的 ConPTY 运行终端命令，保留交互式 stdin；输出做 ANSI 清理；命令运行中可继续输入；ConPTY 不可用时回退隐藏窗口管道；显示退出码 | 每条命令一个 ConPTY，不是持久 shell；无完整 VT 仿真/颜色/光标；不是 OS 沙箱；pywinpty 需安装，失败自动回退 |
| 多模态输入 | 🟡 图片附件已实现 | 聊天“图片”或 CLI `--image` 附加 PNG/JPEG/GIF/WebP；最多 4 张、单张 5 MB、总计 12 MB；校验 MIME/魔数并转成 data URL，作为 `image_url` 随任务发送；会话保存附件元数据 | 只支持图片，不支持 PDF/音频/视频；模型必须支持视觉；会话 JSON 会保存 base64 图片；无图片缩放/压缩或 OCR |
| 评测与回归 | 🟡 固定任务 harness 已实现 | `scidev_cli.py eval` 读取 suite JSON，在隔离副本中跑固定任务，确定性 check 判成功；支持 `--jobs` 并行；suite 可用 `environment` 设置受限的 `SCIDEV_*` 变量；报告完成率、平均轮数、prompt/completion token、耗时、按定价估算的成本和相对上次 pass rate 的回归；`evals/smoke/` 3 项、`evals/standard/` 20 项，`evals/local/` 为弱本地模型基线（3 项 1/3；standard 20 项首轮 1/20，开启无头补丁自动应用后 2/20），测试验证标准任务修复前均失败 | 默认调用真实 CLI/模型；check 是外部命令，不能证明语义完全正确；成本依赖用户填写价格；本地模型常不返回 usage；基线样本仍小且强依赖模型能力；suite 无 Web UI |
| 网络重试与省流 | 🟡 部分实现 | Provider 显式直连优先；只有公网主机 DNS 失败或明确 TCP 拒绝连接（请求体尚未发出），且已配置代理并未被 `NO_PROXY` 排除时，才单次代理回退；超时、其他网络错误、HTTP 错误、本机地址不回退；有持久任务队列、429/5xx 的 `Retry-After`（秒/HTTP 日期，最多等待 1 小时）与指数退避；请求幂等键按任务/阶段/轮次区分并绑定 JSON 请求体摘要；工具调用有 pending/started/completed 持久化及重启恢复，避免自动重放未决副作用；按会话统计请求/响应体字节、尝试/重试/失败、代理回退、耗时和 token；成功阶段总结压缩后续上下文 | 重试本身仍会增加流量；服务端可能不支持/不遵守幂等键，传输中断仍可能被计费；不承诺外部副作用 exactly-once，未知结果需检查现状，模型仍可能产生新的调用；模型请求未缓存；总结只在短 `Retry-After` 内等待，较长时跳过本次辅助总结重试；字节不含 HTTP/TLS 头，完整 transcript 保存在本地 |
| Git 与版本追溯 | ✅ 已实现 | 记录基线/结果 SHA；任务结束可安全地自动提交本次改动；Git 面板支持查看、暂存和手动提交；开发树记录成功/失败/取消尝试；每次任务写入前保存文件检查点，可回滚本次任务写过的文件并删除新文件 | 不自动 push；开发树的尝试分支是 Harness 记录，不等于真实 Git 分支；检查点只保留最近 10 个，回滚会覆盖任务开始后的人工修改 |
| IDE 客户端 | 🟡 核心已实现 | 原生桌面文件树、代码编辑器、多标签、搜索、集成终端、问题面板、Git 视图和 Agent 对话 | 不是完整 VS Code：语言服务器级 IntelliSense 仍有限；不计划内置 DAP 调试器，调试仅保留 pdb 终端入口（Codex 同样依赖宿主 IDE 调试器） |
| 无界面/CI | 🟡 基础已实现 | `scidev_cli.py exec` 复用 Agent、执行策略、Git 自动提交与审计；支持 JSON 输出、退出码、临时 provider 参数、`--no-verify`、`--restore-on-failure`、`--allow-no-changes`、`--worktree` 隔离运行、`--patch-approval auto|reject`、`--sandbox docker`、`--max-turns`/`--max-tokens` 本次任务预算和 `--result-file` 原子结果落盘；`sandbox-check` 报告后端是否就绪及是否 OS 沙箱；`doctor` 离线检查运行时/provider 密钥来源/Git/沙箱/LSP，`--online` 才做最小模型能力探针；默认应用已校验补丁块，无审核界面也能修改已有文件；默认“完成但无改动”按失败处理；自动读取桌面端保存的模型配置 | 无流式 UI/交互审批；手动审批模式在 CLI 中无法批准命令；worktree 只保证文件隔离，不限制网络/进程；Docker 后端依赖 daemon/镜像；worktree 默认保留，需要手动清理；结果文件限制在工作区内 |
| GitHub PR | 🟡 CLI + GUI 已实现 | `scidev_cli.py pr` 从当前任务分支 push 并调用已登录的 `gh pr create`；`pr-view` 读取 PR 元数据；`pr-diff` 读取有界 diff（最多 100 万字符，默认 30 万）；`pr-review` 支持 approve/comment/request-changes；桌面 `> pr` 可加载元数据和 diff、查看 review 状态并提交 review；支持 base/title/body/draft、`--dry-run`、JSON 输出；网络 deny 项目需 `--allow-network` | 需要本机 `gh` 已认证且有推送/审查权限；不创建 GitHub App/OAuth；不自动修复 review finding；不管理团队权限；失败后不自动清理远端分支；diff 是纯文本，不支持逐行评论或本地 hunk 联动 |
| 分发与签名 | 🟡 脚本已实现 | `scidev_cli.py update` 检查 GitHub latest Release，只报告 HTTPS 安装包地址、名称、大小和 `digest`；`--download PATH` 流式下载到同目录临时文件，强制 HTTPS/大小上限/Release 大小/SHA-256 校验后原子改名；`--install` 必须配合 `--download`，启动前再校验一次摘要并打开安装器 UI（不静默、不提权）；`scidev.integrations.update.verify_downloaded_asset()` 可校验已有文件；桌面端 `> update` 显示 Release 结果、可打开页面，或后台下载并校验（可取消），不执行；`scripts/sign_release.ps1` 用证书存储中的 SHA-1 thumbprint 对 .exe/.dll/.msi 做 SHA-256 Authenticode 签名 + RFC 3161 时间戳（默认 HTTP 端点，因部分 signtool 拒绝 HTTPS），随后 `/pa` 验证；`scripts/build_installer.ps1` + `packaging/windows/scidev.iss` 从 staged deployment 构建 per-user Inno Setup 安装器，可选签名并输出 SHA-256；手动 Legacy Python Package Verification workflow 会安装 Inno Setup、构建并上传 installer/manifest；配置 PFX secrets 时导入证书、签名并在结束时删除 | 仓库没有签名证书，默认 CI/Release 产物 unsigned；只有配置 `WINDOWS_SIGNING_PFX_BASE64`/`WINDOWS_SIGNING_PFX_PASSWORD` 才签名；未在真实证书/远端 runner 上验证签名；桌面端检查在后台 QThread 执行并显示可取消进度，取消只忽略结果、不中断阻塞中的 HTTP；没有下载安装器的自动执行；没有 MSIX/MSI 打包；没有跨平台；zip 内二进制未被单独签名 |
| 工程化迁移 | 🟡 已实现 | `scidev_cli.py migrate` 迁移 settings/session/事件库 schema；写入 `schema_version` 与 SQLite `user_version`；规范化执行/网络/白名单设置；修改前备份，幂等，坏 JSON 不覆盖；`scidev_cli.py update` 检查 GitHub latest Release 并给出安装包地址，不自动下载/安装；桌面端未捕获异常写本地脱敏崩溃报告 | 只迁移已知字段；不迁移旧二进制/数据库结构；备份保留 5 份；更新检查需要 GitHub API 可达且仓库有 Release；无自动下载、签名校验或自动安装；崩溃报告不压缩、不上传、不收集局部变量 |
| 错误分类 | 🟡 已实现 | 工作线程 `task_failed`/`task_retry` 和 CLI 失败附带 `error_kind`（auth/rate_limit/timeout/network/approval/tool/verification/workspace/internal）、`user_message`、`retryable`、有界 `detail`；桌面失败气泡显示分类前缀；支持中英文关键词 | 基于异常类型和消息词法，可能误分类；分类不改写 provider 的原始错误；不自动修复/重试策略仍由队列统一决定；`detail` 仍可能含路径 |
| 对话总结配置 | ✅ 已实现 | 可配置启用、模型、上下文长度、输出 token、重试次数、总结指令和每 N 轮频率；会话结束总结 | 总结质量依赖所选模型；只有成功保存的最终总结会进入项目记忆 |
| 命令与文件安全 | 🟡 部分实现 | 文件工具限制在项目内并过滤敏感路径；执行策略分只读/手动审批/工作区可写/完全访问，工作区可写自动批准窄范围验证命令，其他命令逐条审批；拦截破坏性命令和 Harness 自有 Git 操作；可选 Docker 后端只挂载工作区，默认 `--network none`、`--cap-drop ALL`、`no-new-privileges` 和资源限制，daemon/镜像不可用时拒绝执行而不回退；桌面端 Agent 设置和 CLI/环境变量均可切换；同一项目任务有跨进程文件锁，第二个进程失败并建议 worktree | 默认 local 后端的命令仍以当前 Windows 用户权限运行；Docker 后端依赖用户安装的 daemon/镜像，未做真实项目 E2E；自动批准是策略而非网络/文件系统隔离 |
| 浏览器 Agent | ✅ 核心已实现 | 可见外部 Edge + 本机 CDP；显式网页意图才声明工具；搜索、打开、CSS 点击、页面文字与截图；逐次用户审批与事件记录；下载默认拒绝 | 使用独立临时配置，不共享 Edge 登录态；只允许 HTTP/HTTPS；拒绝单段、本机/局域网常见后缀主机名及非公网 IP 字面值；不做 DNS 解析钉扎，故不是网络沙箱；页面文字最多 12000 字符；截图最多 8 MB，每项目最多 20 张，达到上限需手动清理；截图不上传给模型 |
| 多 Agent 协作 | 🟡 部分实现 | Manager 可委派架构/测试/安全调查、受范围限制的编码专员和 Reviewer；2–3 个互相独立的只读调查可并行（最大并发 2）；批次前后核对工作区指纹，变更时报告标为过期且不缓存；Coder 由 Harness 按 `focus_paths` 预取脱敏上下文，首轮只声明必需 `apply_patch`；补丁范围校验、逐块人工审核、主 Agent 上下文刷新/验证/提交前 diff；报告、状态和事件持久化并在聊天显示；未真正应用审核补丁时标记未完成；本机 Qwen2.5-Coder-7B 单文件 E2E 成功 | 默认关闭；每任务最多 3 个子任务、每子 Agent 最多 5 轮/8 次工具调用；并行批次拒绝 Coder，所有写入仍串行；没有独立工作区分支或自动冲突合并；子 Agent 不执行测试/命令、不浏览网页或操作 Git；Qwen 仅验证一个简单单行改动，不能外推复杂任务/其他模型；不同模型 ID 不代表独立机构 |
| 科研实验执行器 | ⬜ 当前不在范围 | 会记录编码会话及代码版本，可用于回溯 Agent 改动 | 不运行训练/论文实验，不管理数据集、实验指标或科研实验 DAG；这是有意的产品边界 |

**当前定位：**带有项目检索、结构化任务计划、跨会话项目记忆、静态测试命令发现、可选独立语义验收、受控浏览器访问、受审批门控的多 Agent 编码协作、会话记录和 Git 可追溯能力的本地 Coding Agent / IDE。多 Agent 只读调查支持有限并行；GUI/CLI 可创建独立 worktree 并行跑任务，GUI 可列出/打开/删除 worktree；Coder 写入仍串行，无冲突自动合并；科研实验执行器仍不在第一阶段范围。

### 1. 核心闭环

核心对象定义为 `Coding Session`：

```text
用户任务
  → 项目文件树和指令
  → LLM 分析
  → 工具调用
  → 读取/修改代码
  → 测试或静态检查
  → Git diff
  → 可选独立语义验收
  → 自动提交
```

Agent 不能只返回一段代码，而要真正操作当前项目。工具执行结果会回传给模型，模型可以根据报错继续修改，直到任务完成或达到步数上限。

### 2. LLM 与网络层

客户端使用 OpenAI-compatible Chat Completions 接口，要求模型支持 function/tool calling。接口地址、密钥和模型通过环境变量配置，因此可以接入 OpenAI、国内兼容网关或本地模型服务。

```text
用户任务
  → 本地 SQLite 任务队列
  → LLM 请求
  → 成功 / 暂时失败 / 永久失败
```

网络策略：

- 请求和任务状态持久化，客户端崩溃后可以恢复。
- 网络错误、超时、429、5xx 使用持久队列和指数退避；429/5xx 的 `Retry-After` 秒数或 HTTP 日期优先于较短退避，等待最多 1 小时。
- 认证、参数、权限错误立即失败，不无限重试。
- Provider 显式忽略环境代理先直连；仅公网目标发生 DNS 解析失败或明确 TCP 拒绝连接时，才使用系统/环境里已配置的代理尝试一次。遵循 `NO_PROXY`；超时、其他网络错误、HTTP 响应（包括 429/5xx）和 localhost/单段内网名/私有 IP/常见本地域名不走代理。代理失败后由同一持久队列进行有上限的重试，不并发探测或下载。
- 每个逻辑任务/轮次/总结阶段有独立请求 ID；HTTP `Idempotency-Key` 由该 ID 和精确 JSON 请求体摘要生成。同一请求重试保持 key，任务/会话内容改变不会复用旧 key；此机制只有服务端支持时才能避免重复处理或计费。
- 会话 transcript 持久化；模型请求失败后从上一次工具结果继续，不重新发送整个任务。
- 通过项目树、相关文件、Git diff 和工具结果分层提供上下文，不把整个项目反复塞给模型。

“省流量”主要依靠上下文控制：

- 先发文件树，再按需读取文件。
- 文件按行号局部读取，限制单次输出大小。
- 局部修改优先用 `apply_patch` 并逐块审核；精确替换工具仍可用于窄范围变更，避免重复发送整个大文件。
- 工具输出截断并保留最后的错误和结果。
- 成功的阶段总结会压缩后续模型请求携带的历史轮次；后续 checkpoint 总结只合并上次摘要与压缩点之后的新消息，避免重复发送已总结的旧 transcript。损坏的压缩游标会退回有预算限制的全量来源；完整 transcript 与工具输出仍保存在本地，重试沿用持久化上下文。
- 总结是辅助流程：短服务端退避最多等待 5 秒；若 `Retry-After` 更长则放弃本次立即重试并记录失败，不拖住已经完成的编码任务。
- 会话用量芯片显示请求体/客户端读取的响应体字节、尝试/重试/失败、代理回退次数、耗时及接口报告的 token；不保存提示词/响应内容或代理地址，且不计 HTTP/TLS 头。

本机省流实测（2026-10-05）：用 Qwen2.5-Coder-7B 对两份独立的 10 轮合成会话，比较真实 Provider JSON 请求和 Ollama 返回的 usage。下一次请求体从 21,976 B 降至 2,945/3,097 B（85.9–86.6%）；prompt tokens 从 5,051 降至 642/686（86.4–87.3%）。生成总结消耗 4,646/4,689 tokens；按请求体字节或 prompt tokens 分别约两次后续调用回本。该结果仅代表本机模型和合成输入，不能外推云端/其他模型；字节统计不含 HTTP/TLS 头。回归测试同时断言压缩后的 Provider 请求体确实更小。

### 3. Coding Agent 工具

当前 Agent 提供以下工具：

```text
search_code      搜索代码，返回路径、行号和相邻行
get_project_context  刷新项目概况、相关片段与路径指令
list_files       查看项目结构
read_file        按行读取文本文件
write_file       创建或完整写入文件
replace_in_file  精确替换已有代码
apply_patch     预览补丁并逐块选择应用
run_command      执行测试、检查或构建命令
git_diff         查看工作区状态和代码差异
update_plan      创建/更新本任务的可见步骤与状态
delegate_research  将代码调查委派给只读架构/测试/安全子 Agent
delegate_parallel_research  并行委派 2-3 个只读调查（最多同时 2 个）
browser_search   经用户批准，在 Bing 搜索并读取有限页面文字
browser_open     经用户批准，打开指定 HTTP/HTTPS 公网页面
browser_click    预览目标后经用户批准，点击 CSS 选择器
browser_screenshot 经用户批准，将当前可视区域截图保存在本机状态目录
```

只有任务明确要求网页搜索/浏览时才把浏览器工具加入模型请求，普通编码任务不会承担额外 token 开销。浏览器由 `scidev/integrations/browser.py` 使用标准库通过本机 CDP 控制系统 Edge；每次启动用独立临时 profile，不触碰用户原有 Edge 会话。搜索、导航、点击和截图分别等待一次 UI 审批，拒绝后本轮移除浏览器工具，避免重复弹窗/隐式重试；页面文字作为不可信数据交给模型。只允许 HTTP/HTTPS，拒绝单段主机名、`localhost`/`.local`/`.lan`/`.internal`/`home.arpa`/`.test` 等本机常见名称及非公网 IP 字面地址；CDP 监听回环地址、随机端口，下载策略为拒绝。截图限 8 MB，每项目最多 20 张于 `.research/browser/`；达到上限后提示手动清理，不会自动删除用户文件；截图不上传给 LLM，也不自动纳入代码自动提交。此策略不钉扎 DNS 解析结果，也不是网络沙箱：域名重绑定、重定向、脚本子请求及用户明确批准的点击仍可能访问局域网/外网或改变远端状态。

多 Agent 通过设置启用，默认关闭以避免额外模型调用。主 Agent 承担 Manager/Coder 职责：`delegate_research` 单项委派架构、测试、安全、coder 或 reviewer；`delegate_parallel_research` 可一次委派 2–3 个互相独立的架构、测试、安全或 Reviewer 只读调查，线程池最多同时运行 2 个。并行批次不接受 Coder；所有文件写入继续串行，不会让子 Agent 争抢同一工作区。每任务最多 3 个子 Agent 调用，每个子 Agent 最多 5 轮、8 次工具调用。批次开始和结束时检查工作区指纹；可完整核验且期间变化时，报告标记为过期并禁止缓存；范围不完整时报告会提示无法保证版本稳定，也不会缓存。架构/测试/安全/Reviewer 只能调用 `search_code`、`get_project_context`、`list_files`、`read_file`。Coder 必须指定现有文件/目录作为 focus path；Harness 在本地按该范围预取脱敏上下文，子 Agent 只声明一次 `apply_patch`，并从首轮强制工具调用，不允许子模型以重复检索替代补丁。补丁目标仍逐一校验是否位于 focus 范围，聊天逐块人工审核；拒绝时立即停止且不得回退其他写入工具，部分批准只保留获批块并阻止自动提交。改动路径并入主 Agent 的会话、工作区上下文刷新、验证指纹和提交前 diff。Coder 若只输出文字 diff 或未成功应用经审查的补丁，会记录为未完成，不将其当作成功结果。子 Agent 不运行测试/命令、不浏览网页或操作 Git；测试命令仍由主 Agent 逐条申请用户批准。会话保存调用状态、报告与修改路径，JSONL 记录开始、工具、成功/失败、复用和限额事件，客户端实时展示进度与结果。同一完整工作区版本、问题、角色、范围和模型下只复用成功报告；失败/不可用不会被缓存成成功。并行子任务的会话状态/用量由主线程顺序持久化，结果按用户请求顺序返回；缓存只复用相同且完整的工作区版本。协作模型可单独指定，留空跟随主模型；不同模型 ID 仍可能属于同一服务商，报告需主 Agent 核验。本机 Qwen2.5-Coder-7B 已在隔离临时项目验证简单单行补丁；复杂任务和其他模型未验收。当前没有并行编码、独立工作区分支或冲突自动合并。

`apply_patch` 使用 `*** Begin Patch` / `*** Update File` / `@@` 格式；桌面客户端默认不勾选任何修改块，用户可逐块批准或全部拒绝。拒绝、部分批准、审核界面不可用或预览后文件已变化时，Agent 停止且不以其他写文件工具绕过；通过审核的文件先全部校验，再以临时文件替换，应用失败会尝试回滚。操作受项目路径、单文件/总字节数、文件数与修改块数限制，并写入事件账本。传统 `write_file` 和 `replace_in_file` 仍可用于相应场景，故现阶段不宣称每种写操作都必经补丁审核。

项目上下文由独立 `scidev/workspace/context.py` 提供：按任务与当前文件/目录检索、提取符号、读取项目清单与适用指令，指定目录会优先选择其后代文件；中文查询可离线扩展为有限的常见英文软件标识符，直接查询词权重更高，已知复合短语加权参与排序，不是通用翻译或语义检索。合成中英/标识符基准为 Top-1 11/11、Top-5 11/11；真实仓库 7 类工作流回归均在 Top-5 命中预期目标（含 Tetris 引擎和测试）。没有关键词命中时，聚焦文件仍提供少量代码行。快照最多 18000 字符。扫描限制为 5000 文件/8 MB 文本/单文件 2 MB，达到限制会提示缩小检索范围；大文件不会静默标成完整结果。局部读取最多 400 行/25000 字符。源文件按时间和大小增量缓存；任务、重试、修改后替换当前快照并写入会话记录，旧版内嵌目录快照会迁移移除。发给模型的快照只标记“当前工作区”，不发送本机绝对路径。客户端 `@` 菜单和底部“上下文”页提供异步预览/刷新，不调用 LLM。

提交任务或预览上下文时，会捕获最多 8 个未保存编辑器文件、合计不超过 1 MB；这些快照参与上下文片段、`search_code`、`get_project_context` 和 `read_file`，项目清单也按缓冲区版本解析。快照被明确标成数据而非指令，并限制为只读：`write_file`、替换及补丁工具不能修改这些文件，避免磁盘旧版本覆盖编辑器草稿。超预算的脏文件不会按旧磁盘版本参与检索或读取，Agent 会要求先保存后重试。快照不写回源文件。为支持断网/重启续跑，待处理及可重试任务的原始快照暂存在本机 `.research/runs.db`；任务创建事件不记录快照源码正文，只记录路径和字节数，任务成功后清除队列副本，最终失败但仍可手动重试的任务保留快照。上下文片段与 `read_file` 工具结果按既有会话机制保存在 `.research/sessions/`；上下文页也会保留有限摘要供查看。

环境快照区分 Harness 当前进程解释器和项目自己的解释器：显示 OS/架构、Python 版本/实现、解释器是否位于项目内，以及 `.venv`、`venv`、`env` 中 Python 可执行文件的存在状态（普通上下文生成不运行它们）。对项目环境的 `pyvenv.cfg` 最多读取 4 KB，只提取创建 Python 版本和 `include-system-site-packages`；链接/越界/超限配置不读取，也不向模型暴露 `home`、`executable` 等绝对路径。该版本仅为配置元数据，不证明当前解释器真实版本或可启动。从 `pyproject.toml`、根目录 `requirements*.txt`、`package.json`、`Cargo.toml`、`go.mod` 和常见锁文件提取声明版本/依赖名；依赖摘要不发送 PEP 508 条件或直接 URL。发给模型的项目上下文/检索副本会遮盖 URL 账号信息、常见密钥字段字面量、若干常见 Token 格式及 PEM 私钥块，不修改源文件；这是启发式遮盖，不是完整秘密扫描。显式 `read_file` 仍按项目内可读路径提供原文。

上下文页的“核验环境”是独立的用户主动操作，不会随打开项目、预览上下文或发送编码任务触发。Python 只选择项目内 `.venv`/`venv`/`env` 解释器，不回退到 Harness Python；拒绝虚拟环境目录链接和越界解释器，Unix venv 的标准解释器软链接则按 venv 入口路径启动。根目录声明 `package.json`/`Cargo.toml`/`go.mod` 时，分别只探测 `node --version`、`rustc/cargo --version`、`go version`；不运行 npm/pnpm/yarn、Cargo 构建或 Go 模块命令。系统 PATH 可用时另探测 `nvidia-smi`/`nvcc`。整组只请求一次审批，Python 使用 `-I -B -S`，所有进程均 `shell=False`，45 秒总超时；不联网、不安装/解析依赖、不运行项目脚本/构建/测试，运行期间暂停新的编码任务和重试。结果显示在上下文页并写入本地事件账本。`pip check` 只核验已安装 Python 分布元数据冲突，不证明清单声明依赖均已安装或可导入；Node/Rust/Go 只证明版本命令可启动，不证明依赖或工程可构建；没有 `nvidia-smi`/`nvcc` 不足以证明没有 GPU/CUDA，检测到它们也不代表框架可用 GPU。当前仅识别根清单，未做嵌套 monorepo 运行时发现。

工具执行限制：

- 所有文件路径限制在项目根目录内。
- 禁止访问 `.git`、`.research`、虚拟环境和常见凭据路径（`.env`、`.ssh`、`.aws`、`.npmrc`、`secrets/` 等）；模板 `.env.example` 可读。
- 执行策略分四档：只读（禁止写入和命令）、手动审批（每条命令确认）、工作区可写（自动批准窄范围验证命令，其他命令确认）、完全访问（自动批准所有命令）。
- 自动批准只接受单条、无 shell 链接/管道/重定向、无绝对路径或上级目录、无网络下载的 pytest/unittest、npm/pnpm/yarn test、ruff/eslint、mypy/pyright、`tsc --noEmit` 等验证命令。
- 测试/lint/typecheck 输出会解析为文件、行号、错误码的结构化诊断，附加在工具结果后供模型继续修复。
- 手动审批和完全访问以外的命令缺少审批界面或用户拒绝时命令不执行；批准后的命令仍以当前 Windows 用户权限运行，这不是操作系统级沙箱。
- 默认不运行长时间科研训练、实验或大文件下载。
- 屏蔽 `git reset --hard`、`git clean`、递归删除等破坏性命令。
- 命令只在当前项目根目录执行，并有超时限制。

典型交互：

```text
用户：给这个项目加一个配置文件读取功能

Agent：list_files
Agent：read_file
Agent：apply_patch（聊天内逐块预览/批准；也可按场景使用 write_file / replace_in_file）
Agent：run_command（测试）
Agent：git_diff
Agent：总结修改并自动提交
```

### 4. 科研过程记录

虽然第一阶段不运行科研实验，但所有编码过程已经按科研可追溯方式记录：

```text
coding_session_started
task_created
model_call_started
tool_called
command_execution_recorded
file_changed
tool_result
verification_recorded
verification_report_recorded
task_retry_scheduled
tool_call_started
tool_call_recovered
coding_session_completed
git_commit_created
```

每个会话保存一份 transcript：

```text
.research/
├── events.jsonl             # append-only 事件日志
├── runs.db                  # SQLite 查询索引
└── sessions/
    └── session_xxx.json     # 消息、工具调用、验证证据和 Git SHA
```

工具调用恢复以本地 SQLite journal 为准：完整结果先持久化、再写入会话 transcript；进程在执行前退出的 pending 调用会明确标记为未运行，已启动但结果缺失的调用标记为结果不确定且不自动重放。旧会话没有 journal 时也不猜测其是否执行。会话 JSON 通过同目录临时文件与原子替换保存，减少崩溃产生截断 JSON 的风险。该机制防止 Harness 自动重放同一调用，不是任意外部操作的 exactly-once 保证；模型后续仍可能生成新调用，`run_command` 仍须逐条审批；恢复到可能改代码的调用会要求验证后才能自动提交。

`get_project_context` 静态检查可读文件和配置，给出 Python pytest/unittest、Node 根/嵌套 package（含 `test:*`）、Go、Rust、Maven/Gradle、.NET、tox/nox、Makefile 与 Justfile 的候选命令；子项目命令会限定项目路径，Python 项目若有普通 `.venv`/`venv`/`env` 解释器文件则优先使用该路径。Node 会参考 `packageManager` 与最近的锁文件；tox/nox、Make/Just 只接受明确包含测试语义的入口。候选来自有限约定/文本证据，不探测解释器/依赖是否可用，也不会执行或安装；未知脚本约定、路径含 shell 元字符、配置冲突或读取预算耗尽时可能漏掉候选。发现过程拒绝路径越界/链接并受文件数、单文件和总字节上限限制。候选会出现在任务上下文和桌面“上下文”预览中，实际命令仍须逐条批准。

`run_command` 仍逐条等待用户批准；批准后保存命令状态（通过/失败/超时/启动失败）、退出码和耗时，拒绝或审批不可用则明确记为未运行。常见测试、lint、类型检查、编译、构建命令会被分类。检查前后对 Git 变更路径做有上限的内容指纹，并纳入 HEAD SHA；无 Git 提交基线时由项目上下文安全清单枚举源码路径。后续修改或检查期间工作区变化时，旧通过结果不再视为当前验证。

桌面 Agent 默认启用完成门槛：本次改动涉及代码或配置时，必须有绑定当前代码的成功检查才允许自动提交；最多反馈续作两轮。用户拒绝命令、检查失败或证据范围受限时立即停止，不拆分/改写命令规避审批，不自动提交，改动保留在工作区供用户检查。无 Git、安全路径受限或文件数/体积超预算会显示“范围受限”。客户端在聊天中显示每次检查及最终状态。

任务完成前的可选独立语义验收是额外 LLM 调用，默认关闭。它只证明另一个模型给出了基于所提供 diff 的判断，不是形式化证明，也不保证不同模型处于不同服务商。完整配置和数据发送边界见 README 的“独立语义验收”。

`run_command` 的退出码和源码指纹是执行证据，不是目标达成的证明。可选独立语义验收会在用户明确配置后，使用不同模型 ID、无工具调用的单独请求审查任务、受限完整 diff 和检查摘要；它要求结构化标准及变更文件引用，并在请求结束后重新核对 diff。必需模式下，未满足的需求可触发一次有限修正；审查不可用、证据不完整或最终不通过时保留工作区且不自动提交。

因此可以查询：

- 用户当时提出了什么任务。
- Agent 读取和修改了哪些文件。
- 哪些工具调用失败过，后来如何修复。
- 哪次 LLM 请求因为网络失败而重试。
- 最终代码对应哪个 Git commit。

### 5. Git 与文件树

Git 默认采用“编码任务完成后自动提交，推送由用户决定”：

- 客户端显示当前分支、工作区状态和提交记录。
- 每次编码会话完成后自动生成 `[codex] ...` commit。
- 自动提交只纳入任务开始时干净、且在本会话内变化的路径；已有暂存和未提交内容保留在原位，不混入 Agent commit。
- 会话提交使用隔离的临时 Git index，提交后只刷新本次安全路径的真实 index 项。
- 会话记录 `git_base_sha` 和 `git_result_sha`。
- Agent 自动 diff 隐藏常见凭据/内部路径；自动提交跳过敏感路径、链接路径、超过 240,000 字节的文件及二进制/非 UTF-8 文件，跳过项保留在工作区并通过对话提示。敏感文件重命名时，来源与目标一并跳过。
- Git 面板手动 diff/提交仍由用户显式操作；Agent 的自动筛选不会拦截手动审核。
- 不自动 push、不自动合并、不删除用户分支。
- `.env`、`.research` 数据库和缓存默认进入 `.gitignore`。

客户端采用 VSCode + Codex 的布局：左侧为活动栏和项目文件树，中间为代码编辑工作区，右侧为固定的 Agent 对话栏；开发版本树是主工作区入口，集中展示主线提交、开发尝试和任务历史。

### 6. 技术架构

```text
原生桌面客户端 PySide6 / Qt Widgets
  ├─ 编码对话
  ├─ 项目文件树
  ├─ 任务队列
  ├─ 多标签代码编辑器
  ├─ Git / 开发版本树
  └─ Agent 对话与实时日志

Qt Quick / QML 通过 QQuickWidget 承载可拖拽、缩放的 2.5D 开发版本树；Git、会话、工具权限与持久化仍由 Python 核心管理。

应用核心
  ├─ Coding Agent Loop
  ├─ Request Broker / Retry Queue
  ├─ Coding Toolbox
  ├─ Git Manager
  └─ Event Ledger

本地存储
  ├─ SQLite：任务、会话索引
  ├─ JSONL：不可变事件日志
  ├─ JSON：会话 transcript
  └─ Git：代码版本和 diff
```

当前实现使用 Python、PySide6/Qt、Qt Quick/QML、SQLite、Git CLI 和 OpenAI-compatible HTTP 接口；版本树通过 QQuickWidget 嵌入桌面客户端，不新增前端运行时依赖。后续再完善 Windows 安装包和语言服务集成。

2026-11-17 · 维护入口：

| 修改内容 | 主要模块 |
|---|---|
| 窗口状态、生命周期、跨面板连接 | `scidev/desktop/client.py` |
| 窗口构建、缩放与响应式布局 | `scidev/desktop/window_builders.py`、`scidev/desktop/window_layout.py` |
| Agent 面板、模型与任务设置 | `scidev/desktop/agent_panel.py`、`scidev/desktop/agent_settings.py` |
| 编辑器、资源管理、Git、终端、对话交互 | `scidev/desktop/editor_workspace.py`、`scidev/desktop/explorer_workspace.py`、`scidev/desktop/git_workspace.py`、`scidev/desktop/terminal_workspace.py`、`scidev/desktop/chat_workflow.py` |
| Agent 装配、项目上下文、计划和会话适配 | `scidev/agent/core.py` |
| 任务准备与请求/响应/完成阶段 | `scidev/agent/run.py`，阶段共用单次任务的 `CodingRunState` |
| 工具批次、验收收尾、会话总结 | `scidev/agent/tools.py`、`scidev/agent/completion.py`、`scidev/agent/summary.py` |
| 模型通信、持久化、文件/命令工具 | `scidev/integrations/model.py`、`scidev/agent/session.py`、`scidev/agent/toolbox.py` |

窗口与 Agent 统一持有状态和服务；工作流 mixin 不初始化状态、不反向导入宿主入口。修改时优先进入对应职责模块，原入口保留已有导出兼容；审批、验证和提交仍走原有流程。

### 7. 后续科研能力

实验运行、数据集、指标、图表和实验 DAG 不放在当前第一阶段。等 Codex 式编码闭环稳定后，再把它们作为独立的科研插件接入，避免编码 Agent 和实验执行器一开始互相干扰。

### 8. 客户端功能补充

#### 任务规划

聊天栏内置轻量计划面板；计划模式可选“自动规划”“必须先规划”或“关闭”，选择保存在客户端设置中。自动模式下，模型判断为多步骤任务时可调用 `update_plan`；必须模式下，首轮只提供计划工具并要求先创建步骤，再开放编码工具。计划最多 12 步，每步为待处理、进行中、已完成、受阻或已跳过，同一时刻最多一个进行中步骤；跳过、删除未完成步骤或重开已完成步骤须给出原因。

计划每次修订先写入 `.research/sessions/<session>.json`，随后把完整步骤快照记入 `.research/events.jsonl` 并通知界面；网络重试/队列恢复时恢复同一份计划。同一会话的新任务会把旧计划归档（最多保留 20 份）。Agent 返回最终答复但仍有未完成步骤时，Harness 对连续未完成的答复最多给两次继续机会；计划取得进度后重新计数。仍未完成则标记失败、保留计划且不自动提交，用户可从任务历史重试。计划状态由模型报告，不是独立的验收证明；明确要求的测试仍须实际运行。当前没有逐步人工批准或图形 DAG。

#### 对话总结

Agent 可每隔 N 轮发起一次轻量总结请求，并在会话结束时生成最终总结。总结包含精简 transcript、当前 Git SHA 和配置的总结指令，展示在对话面板并保存为独立记录：

```text
.research/summaries/<session>_<summary_id>.json
```

成功的阶段总结会进入后续模型请求的系统上下文，并从请求消息列表中省略已总结轮次；后续 checkpoint 总结只携带上一份摘要与压缩点后的新消息，不再重复打包已总结的旧对话。无效压缩游标会安全退回全量 transcript 摘要，并严格限制在配置的字符预算内。完整 transcript 与工具调用仍留在本地会话文件。总结失败不会改变已完成编码任务的状态；每次会话单独保存，不覆盖旧总结。聊天栏可配置启用状态、总结模型、最大输出 token、transcript 上下文长度、重试次数、每 N 轮总结（`0` 表示仅最终总结）和自定义总结指令。设置写入被 Git 忽略的 `.research/settings.json`，也可用 `SCIDEV_SUMMARY_ENABLED`、`SCIDEV_SUMMARY_MODEL`、`SCIDEV_SUMMARY_INTERVAL_TURNS` 环境变量覆盖。

#### 跨会话项目记忆

每个新任务从当前项目 `.research/summaries/` 的最终总结中检索相关记录，采用本地中英文词项倒排与相关度排序；不生成 embedding、不下载模型、不发额外网络请求。当前会话会被排除，瞬时重试沿用首次检索快照；历史总结会先做常见 API key、Bearer、GitHub/AWS token 与 URL 密码格式脱敏。原始会话/总结只留在项目本地，不跨项目共享。

索引保存在 Git 忽略的 `.research/memory.sqlite3`，最多保留 1000 条；启动后按上限回填最近 400 个总结文件/12 MB。每次任务默认最多注入 3 条、6000 字符，可在聊天栏“总结与项目记忆”设置中调整开关、条数和长度。无法读取记忆库只记事件，不会阻断编码。它是关键词检索，不保证语义召回；通用密钥脱敏不能保证识别所有私有凭据，历史内容会作为不可信背景而非指令提供给模型。

#### 2.5D 开发版本树

Git 入口打开主工作台。竖直主干表示已完成的编码工作；侧边节点表示已取消、失败、运行中或等待中的尝试方向，它们不是 Git 分支。

- 左键拖动尝试卡片可移动分支，连接线随卡片更新；拖动空白区平移画布；滚轮以光标位置为中心缩放。
- 点击卡片查看详情；失败和等待节点可从详情面板重试。
- 卡片用阴影层、高光边缘、深度偏移和状态强调色表现层次。
- 卡片位置保存到 `.research/tree_layout.json`，项目重开后恢复。

#### 编辑器工作流

- 文件树单击打开临时预览标签，双击固定为正式标签；多个文件可同时打开，标签可移动或关闭。
- 有未保存修改的标签显示圆点，并在关闭前保护；`Ctrl+S` 保存并记录 `file_changed` 事件。
- `+` 创建项目内文件；`@` 将当前文件和选中代码加入下一条 Agent 提示词；Agent 完成后，干净的已打开编辑器会从磁盘刷新。
- `Ctrl+P` 或顶部搜索打开匹配文件；`>` 命令模式可切换终端、问题面板和替换视图。
- `Ctrl+F` 搜索当前文件；`Ctrl+H` 支持替换、全部替换、大小写和全词选项；`Ctrl+`` 切换项目根目录下的集成终端。
- 问题面板保留任务和终端错误；状态栏展示光标位置、编码、语言和 Git 状态；差异以只读编辑器标签打开。
- Git 页面支持变更文件列表、双击差异、选择/全部暂存、取消暂存、工作区状态和手动提交。
- `Ctrl+Shift+F` 搜索项目可读文件并跳转结果；`Ctrl+Shift+O` 显示 Python 类与函数大纲；`F12` 跳转定义，`Shift+F12` 查找全词引用，`Alt+Left/Right` 浏览位置历史。
- `Shift+Alt+F` 清理行尾空格并规范文件末尾换行；编辑器上下文菜单提供导航、重命名和格式化。
- 工具栏可打开并排编辑组；各组拥有独立标签，共享保存、差异、诊断和未保存保护。
- `Ctrl+Space` 提供本地确定性补全；语义 IntelliSense 仍需语言服务器支持。
- Python 语法检查支持双击跳转到错误行列；文件树上下文菜单可创建文件/目录、重命名、删除项目内资源，或在指定目录打开终端。
- `Ctrl+Shift+P` 打开命令面板，可切换终端、问题、搜索和替换，无需额外弹窗。

整体遵循 VS Code 工作台模型：左侧文件树、中间编辑器标签、右侧 Agent 对话栏，底部终端与问题面板。
