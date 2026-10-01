# SciDevHarness 设计说明

第一阶段先做一个仿 Codex 的原生桌面编码客户端。核心不是自动跑论文实验，而是让 LLM 能在本地项目中可靠地读代码、改代码、做检查，并留下完整过程记录。

## 1. 核心闭环

核心对象定义为 `Coding Session`：

```text
用户任务
  → 项目文件树和指令
  → LLM 分析
  → 工具调用
  → 读取/修改代码
  → 测试或静态检查
  → Git diff
  → 自动提交
```

Agent 不能只返回一段代码，而要真正操作当前项目。工具执行结果会回传给模型，模型可以根据报错继续修改，直到任务完成或达到步数上限。

## 2. LLM 与网络层

客户端使用 OpenAI-compatible Chat Completions 接口，要求模型支持 function/tool calling。接口地址、密钥和模型通过环境变量配置，因此可以接入 OpenAI、国内兼容网关或本地模型服务。

```text
用户任务
  → 本地 SQLite 任务队列
  → LLM 请求
  → 成功 / 暂时失败 / 永久失败
```

网络策略：

- 请求和任务状态持久化，客户端崩溃后可以恢复。
- 网络错误、超时、429、5xx 使用指数退避和定时重试。
- 认证、参数、权限错误立即失败，不无限重试。
- 每轮模型请求使用固定 `Idempotency-Key`，降低重复请求风险。
- 会话 transcript 持久化；模型请求失败后从上一次工具结果继续，不重新发送整个任务。
- 通过项目树、相关文件、Git diff 和工具结果分层提供上下文，不把整个项目反复塞给模型。

“省流量”主要依靠上下文控制：

- 先发文件树，再按需读取文件。
- 文件按行号局部读取，限制单次输出大小。
- 修改已有文件优先使用精确文本替换，不重复发送整个大文件。
- 工具输出截断并保留最后的错误和结果。
- 会话完整保存到本地，重试时复用已有上下文。

## 3. Coding Agent 工具

当前 Agent 提供以下工具：

```text
list_files       查看项目结构
read_file        按行读取文本文件
write_file       创建或完整写入文件
replace_in_file  精确替换已有代码
run_command      执行测试、检查或构建命令
git_diff         查看工作区状态和代码差异
```

工具执行限制：

- 所有文件路径限制在项目根目录内。
- 禁止访问 `.git`、`.research`、虚拟环境和常见凭据路径（`.env`、`.ssh`、`.aws`、`.npmrc`、`secrets/` 等）；模板 `.env.example` 可读。
- Agent 发起的每条 shell 命令都在 UI 中逐次等待用户批准；缺少审批界面或用户拒绝时命令不执行。
- 用户批准的 shell 命令仍以当前 Windows 用户权限运行；这不是操作系统级沙箱。
- 默认不运行长时间科研训练、实验或大文件下载。
- 屏蔽 `git reset --hard`、`git clean`、递归删除等破坏性命令。
- 命令只在当前项目根目录执行，并有超时限制。

典型交互：

```text
用户：给这个项目加一个配置文件读取功能

Agent：list_files
Agent：read_file
Agent：write_file / replace_in_file
Agent：run_command（测试）
Agent：git_diff
Agent：总结修改并自动提交
```

## 4. 科研过程记录

虽然第一阶段不运行科研实验，但所有编码过程已经按科研可追溯方式记录：

```text
coding_session_started
task_created
model_call_started
tool_called
file_changed
tool_result
task_retry_scheduled
coding_session_completed
git_commit_created
```

每个会话保存一份 transcript：

```text
.research/
├── events.jsonl             # append-only 事件日志
├── runs.db                  # SQLite 查询索引
└── sessions/
    └── session_xxx.json     # 消息、工具调用、结果和 Git SHA
```

因此可以查询：

- 用户当时提出了什么任务。
- Agent 读取和修改了哪些文件。
- 哪些工具调用失败过，后来如何修复。
- 哪次 LLM 请求因为网络失败而重试。
- 最终代码对应哪个 Git commit。

## 5. Git 与文件树

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

## 6. 技术架构

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

## 7. 后续科研能力

实验运行、数据集、指标、图表和实验 DAG 不放在当前第一阶段。等 Codex 式编码闭环稳定后，再把它们作为独立的科研插件接入，避免编码 Agent 和实验执行器一开始互相干扰。
