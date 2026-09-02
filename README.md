# SciDevHarness

SciDevHarness 是一个原生 Windows 桌面编码客户端，第一阶段仿照 Codex，重点是让 LLM 直接读取项目、修改代码、执行必要检查并查看 Git diff；暂不包含“运行科研实验”流程。

客户端使用 PySide6/Qt，采用 VSCode + Codex 风格的左侧资源管理器、中间编码工作区和右侧 Agent 对话栏。

## 启动

推荐直接双击 `start_client.bat`，它会自动使用项目自己的 `.venv`；只有虚拟环境或依赖不存在时才自动安装。

```powershell
.\start_client.bat
```

如果已经完成初始化，也可以直接使用虚拟环境启动：

```powershell
.\.venv\Scripts\python.exe scidev_client.py
```

客户端包含：

- 编码会话：输入任务，Agent 自主读取文件、写代码、修复错误
- 工具调用：`list_files`、`read_file`、`write_file`、`replace_in_file`、`run_command`、`git_diff`
- 本地任务队列：网络失败时指数退避并定时重试，进程重启后可恢复会话
- Git 集成：查看状态和提交记录，编码任务完成后自动提交
- 过程记录：`.research/events.jsonl`、`.research/runs.db`、`.research/sessions/`

## LLM 配置

使用 OpenAI-compatible Chat Completions 接口，并要求模型支持 function/tool calling：

```powershell
$env:SCIDEV_API_BASE="https://your-endpoint/v1"
$env:SCIDEV_API_KEY="your-key"
$env:SCIDEV_MODEL="your-model"
.\start_client.bat
```

也支持 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。如果只配置 `OPENAI_API_KEY`，地址默认使用 `https://api.openai.com/v1`，模型默认使用 `gpt-5`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前版本先把 Codex 式编码闭环跑通：

```text
用户任务 → 项目上下文 → LLM → 工具调用 → 文件修改/检查 → Git diff → 自动提交
```
