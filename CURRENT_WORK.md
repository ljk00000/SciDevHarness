# 当前工作

## 2026-11-10 08:10（Asia/Shanghai）· 安装路径与重复安装
- CI 安装 E2E 的目录改为含空格路径（`scidev install e2e`），并在 smoke 前重复运行同一安装包，验证覆盖安装/幂等路径；随后仍卸载并检查残留。
- 首轮失败：`Start-Process -ArgumentList` 数组不会给含空格路径加引号，`/DIR` 被拆成两个参数；改为单个带引号参数串 `/DIR="..."`。本地 bootstrap 断言通过；package workflow 待重跑。

## 2026-11-10 07:50（Asia/Shanghai）· 安装包安装/卸载 E2E
- package workflow 新增真实静默安装 → 运行已安装 exe 的 `--smoke-test` → 静默卸载 → 检查 exe 已删除；这样安装 E2E 在干净 runner 上执行，不依赖本机策略。
- 实际 run `42722815742` 全绿：build、smoke、审计、installer、安装/卸载全部通过；Windows CI `42722814737` 三版本成功。全量 771 项 0 失败、14 跳过。

## 2026-11-10 06:50（Asia/Shanghai）· LSP 回调与安装器实测
- 全量日志中出现 `RuntimeError: Signal source has been deleted`：窗口销毁后 resident LSP 线程仍回调桥接对象。`ResidentLSPBridge._deliver` 现在捕获该 RuntimeError 并丢弃过期诊断；LSP 7 项测试通过。
- 下载真实 unsigned 安装包做静默安装实测；本机 `Start-Process` 后未出现安装进程、目标目录或日志，判定为本地策略/环境阻塞，未当作安装通过。全量 771 项 0 失败、14 跳过、无重试目标；远端 CI `42721031393` 三版本成功。

## 2026-11-10 06:25（Asia/Shanghai）· 测试临时目录清理
- 全量回归偶发 `PermissionError [WinError 32]`：`ClientWindow` 仍被局部变量引用，`QFileSystemWatcher` 在 `TemporaryDirectory.cleanup()` 后才销毁。
- `test_client` 的重命名测试和 `test_settings` tearDown 在 close 后显式 `deleteLater()` + 处理事件；两项连续 5 轮通过。全量 771 项 0 失败、14 跳过、无重试目标；远端 CI `42712635724` 三版本成功。

## 2026-11-10 04:20（Asia/Shanghai）· 打包显式设置 VIRTUAL_ENV
- 实际触发 `Windows Package Verification`（run `42689172205`）发现 `pyside6-deploy` 只认 `VIRTUAL_ENV` 环境变量；workflow 虽从 `.venv` 启动，仍被判定“not using a virtual environment”并等待 stdin，最终 `EOFError`。
- 在构建 step 中显式 `set "VIRTUAL_ENV=%GITHUB_WORKSPACE%\.venv"`；本地 `pyside6-deploy --dry-run` 退出 0。新增 workflow 断言；全量 770 项 0 失败、14 跳过。
- 修复后真实 Nuitka 构建成功，但 Nuitka 在非交互环境拒绝自动下载 Dependency Walker，`deployment` 未生成、smoke 失败。spec 增加 `--assume-yes-for-downloads`，并新增断言。
- 再次重跑后构建完成，但 `pyside6-deploy` 默认在结束时清理 `deployment`，smoke 仍找不到目录。命令增加 `--keep-deployment-files`。
- 第三次重跑找到 2 个 `scidev_client.exe`（仍在 `.dist` 过滤范围内）；改为直接使用 `deployment\SciDevHarness.dist\scidev_client.exe`。
- 第五次重跑 smoke 的 `$LASTEXITCODE` 为空：GUI 子系统 exe 用 `&` 启动不会等待。改用 `Start-Process -PassThru -Wait`，捕获 stdout/stderr 和进程退出码。
- smoke 已通过，但 `audit_standalone.py` 仍把中间产物算作第二个 exe；现在优先使用 `deployment\SciDevHarness.dist`（实际同时存在 `scidev_client.dist` 和 `SciDevHarness.dist`），错误信息列出路径。新增审计测试（6 项）。
- 第八次重跑 smoke/审计通过，但 installer 仍把 deployment 根目录当源码目录，根目录没有 exe；改为传 `SciDevHarness.dist`。
- 最终重跑 `42707785249` 成功：Nuitka standalone、smoke、build manifest、Inno Setup 安装包全部通过；产物见 `SciDevHarness-Windows-x64-installer`。全量 771 项 0 失败、14 跳过。

## 2026-11-10 04:10（Asia/Shanghai）· doctor 扫描排除受保护目录
- `doctor --tests` 的 `ProjectContext` 不再用恒 false 的 protected 回调，改为排除 `.git/.research/.venv/venv/node_modules/secrets`，避免把本地状态或密钥目录纳入静态扫描。
- CLI 30 项测试、ruff、mypy 通过；全量待验证。

## 2026-11-10 03:35（Asia/Shanghai）· showEvent 不再假定窗口句柄可提供屏幕
- `tests.test_ui_polish` 偶发 `windowHandle()` 返回无 `screen()` 的 `QWidgetItem`，触发 `showEvent` AttributeError。改为优先 `self.screen()`，仅在为空时才尝试 handle，并新增无 `screen()` 句柄的回归测试。
- `tests.test_ui_polish` 连续 10 轮通过；全量 770 项 0 失败、14 跳过、无重试目标。远端 CI `42687016853` 三版本成功。

## 2026-11-10 03:15（Asia/Shanghai）· 命令补全惰性创建
- 命令搜索框不再在窗口构造时创建 `QCompleter`，改为首次输入 `>` 时才创建；测试断言启动时无补全器、输入后出现。减少每个窗口的 Qt 对象和 offscreen 原生崩溃暴露面。
- 定向 UI 测试通过；全量 769 项 0 失败、14 跳过（`tests.test_ui_polish` 重试通过）。远端 CI `42683199600` 三版本成功。

## 2026-11-10 02:55（Asia/Shanghai）· 取消等待时间阈值
- 3.13 CI 的 `test_cancel_interrupts_an_in_flight_model_stream` 在 5.25s 完成取消，超过原 5s 断言；取消语义正确，阈值对慢 runner 过紧。放宽到 8s，仍保留 join/10s 与“任务已取消”断言。
- 定向测试连续 3 轮通过；全量 769 项 0 失败、14 跳过（`EditorSplitInteractionTests` 重试通过）、无新增测试。远端 CI `42679272212` 三版本成功（3.13 耗时 11m33s）。

## 2026-11-10 02:15（Asia/Shanghai）· `doctor --tests` 静态入口发现
- `doctor --tests` 复用 `ProjectContext` 的路径清单和 `TestCommandDiscovery`，只推断 test/lint/typecheck 候选命令、不执行；输出数量与首个候选，便于用户先确认自动验证入口。
- 新增 1 项 CLI 集成测试（30 项）。全量 769 项 0 失败、14 跳过、无重试目标；`scidev_cli` 64%。

## 2026-11-10 01:50（Asia/Shanghai）· 显式保存指定编辑器
- 3.13 CI 偶发 `EditorSplitInteractionTests` 保存后文件未变；`_save_editor` 之前通过全局 active editor 间接保存。现在把目标 editor 直接传给 `save_current_file(editor)`，消除分栏/焦点切换的竞态。
- 定向测试连续 3 轮通过；ruff/mypy 通过。全量 768 项 0 失败、14 跳过、无重试目标。远端 CI `42670148888` 三版本成功。

## 2026-11-10 01:20（Asia/Shanghai）· 强化补丁提示
- 系统提示新增：`apply_patch` 的 hunk 必须逐字使用 `read_file` 的真实行，schema 的 `old line`/`new line` 不能复制；提示长度上限从 700 调到 850。
- 本地 Qwen 仍未能完成单行修改（继续追问/抄占位符），记录为模型限制。全量 768 项 0 失败、14 跳过（`UIProfileTests` 重试通过）；无新增测试。

## 2026-11-10 00:55（Asia/Shanghai）· 补丁失败反馈实际行
- `apply_patch` 上下文失败时，错误信息现在明确提示 `old line/new line` 只是 schema 占位符，并附带当前文件前 3 行的真实内容（最多 600 字符），减少弱模型反复询问或盲猜。
- 新增断言；本地 Qwen 仍无法完成单行修改（会追问或耗尽 5 步），记录为模型限制。全量 768 项 0 失败、14 跳过、无重试目标；`scidev_patching` 83%。远端 CI `42660336418` 三版本成功。

## 2026-11-10 00:30（Asia/Shanghai）· 本地 Qwen 探针与 fallback 提示
- 启动本机 Ollama，用 `doctor --online` 对 `scidev-qwen2.5-coder-7b:q4_k_m` 实测：模型声明 `tools`，但探针返回文本而非原生 tool_call；doctor 现显示 `text_fallback` 状态，并在 loopback endpoint 建议 `SCIDEV_TEXT_TOOL_CALL_FALLBACK=true`。
- 打开 fallback 后跑真实 `scidev exec` 单行任务，模型仍生成无效补丁并最终无改动；记录为模型能力限制，不伪造成功。新增 1 项 CLI 测试（29 项）；全量 767 项 0 失败、14 跳过、无重试目标；`scidev_cli` 覆盖率 64%。远端 CI `42656127420` 三版本成功。

## 2026-11-09 23:40（Asia/Shanghai）· `doctor --online` 能力探针
- `scidev doctor --online` 在本地检查后显式请求 provider `/models` 并做一次最小 tool-call 探针；报告 tools/context/vision，缺 Key/网络/HTTP 失败只告警。
- 默认不加 `--online` 不产生任何模型请求；新增 1 项无 PySide6 回归测试，Git CLI 缺失单独告警。CLI 28 项测试通过；全量 766 项 0 失败、14 跳过（`UIResolutionScalingTests` 重试通过）；`scidev_cli` 覆盖率 64%。远端 CI `42651881950` 三版本成功。

## 2026-11-09 23:00（Asia/Shanghai）· CLI `doctor` 环境诊断
- 新增 `scidev doctor --workspace . --json`：离线检查运行时/PySide6、provider 模型与密钥来源（不打印密钥）、Git 分支/HEAD、local/docker 边界、项目 `.venv`/PATH 中的 LSP 工具。
- 有 `error` 时返回 1；缺失工作区返回 2；provider 配置无效计为 error；缺少 API Key、非 Git、local 后端、缺少 PySide6（CLI 仍可用）或 LSP 计为 warn。新增 3 项 CLI 测试；全量 764 项 0 失败、14 跳过（`WorkspaceSearchTests` 重试通过）；`scidev_cli` 覆盖率 64%。远端 CI `42644493899` 三版本成功。

## 2026-11-09 22:20（Asia/Shanghai）· 拆分 GitManager 与路径策略
- `GitManager`/`GitStatusEntry`/`parse_porcelain_z` 移到 `scidev_git.py`；敏感/排除路径判定移到 `scidev_paths.py`。
- `scidev_core.py` 从 7922 行降到约 7215 行；旧导入路径保留 re-export；`pyproject` 打包与 mypy 列表加入两个新模块（mypy 32 个文件）。core 159 项测试通过；全量 761 项 0 失败、14 跳过、无重试目标；`scidev_git` 覆盖 85%，`scidev_paths` 100%。远端 CI `42636505682` 三版本成功。

## 2026-11-09 21:45（Asia/Shanghai）· 拆分 RetryQueue 与稳定异常
- `RetryQueue`（约 108 行）移到 `scidev_queue.py`；`PermanentError`/`TaskCancelled`/`TaskAborted` 移到 `scidev_errors.py`，`scidev_core` 保留 re-export。
- `scidev_core.py` 从 8042 行降到 7922 行；`pyproject` 打包与 mypy 列表加入新模块（mypy 30 个文件）。core/errors 162 项测试通过；全量 761 项 0 失败、14 跳过（2 个 GUI 目标自动重试后通过）。远端 CI `42629420605` 三版本成功。

## 2026-11-03 01:20（Asia/Shanghai）· 修复 Authenticode 时间戳端点
- 用自签代码签名证书实际调用 `sign_release.ps1`，发现本机 signtool 对 `https://timestamp.digicert.com` 报 `Invalid Timestamp URL`；改为默认 HTTP，接受 HTTP/HTTPS 覆盖。
- 签名本身成功；`/pa` 对未信任自签证书按预期失败，真实受信证书路径未验证。新增脚本静态断言。全量 761 项 0 失败、14 跳过、无重试目标。远端 CI `41490858500` 三版本成功。

## 2026-11-03 01:10（Asia/Shanghai）· 测试超时清理进程树
- 超时目标改用 `Popen` + `terminate_process_tree`，同时清理测试子进程启动的孙进程；等待 10 秒后仍不退则 `kill()`。
- 超时单元测试覆盖“任务树终止 + 不完整结果”；全量 761 项 0 失败、14 跳过（`UIProfileTests` 自动重试后通过）。远端 CI `41486206537` 三版本成功。

## 2026-11-03 00:45（Asia/Shanghai）· 测试运行器单目标超时
- `scripts/run_tests_coverage.py` 为每个目标设置默认 300 秒超时（`SCIDEV_TEST_TARGET_TIMEOUT` 可覆盖）；超时返回 `timed_out` 不完整结果，父进程按既有重试/崩溃逻辑处理，不再无限等待。
- 新增 1 项超时逻辑测试；全量 761 项 0 失败、14 跳过（`test_ui_polish` 命中 300 秒超时，重试通过；`WorkspaceSearchTests` 也自动重试通过）。远端 CI `41482318437` 三版本成功。

## 2026-11-02 23:50（Asia/Shanghai）· 修复命令取消竞态
- 远端 3.14 CI 失败定位：`tool_called` 已写入、但 `Popen` 尚未启动时用户取消，`_command_cancel` 虽已置位仍会 spawn 子进程。
- `run_command` 在 spawn 前再次检查取消；`Popen` 后立即检查并终止进程树。取消测试改为等待 `tool_called` 事件后取消；连续 5 轮通过。全量测试 760 项 0 失败、14 跳过、无重试目标。远端 CI `41478009195` 三版本成功。

## 2026-11-02 23:20（Asia/Shanghai）· CLI `sandbox-check`
- 新增 `scidev sandbox-check [--backend local|docker] [--image ...] [--json]`：local 返回 `ready=true/os_sandbox=false` 并明确说明当前用户权限；docker 查询 daemon/镜像，失败返回 1。
- 新增 2 项 CLI 测试；README/能力表补充。全量 760 项 0 失败、14 跳过（1 个 GUI 目标自动重试后通过）；过程中 `test_ui_polish` 子进程在输出 OK 后未退出，已手动终止，需给测试运行器加单目标超时。

## 2026-11-02 22:50（Asia/Shanghai）· 编辑器补全器懒加载
- `CodeEditor` 不再在构造时创建 `QCompleter`/`QStringListModel`，首次请求补全时才创建；减少编辑器实例的 Qt 对象和测试期原生压力。
- 新增 1 项懒加载测试；`tests.test_client` 连续 3 轮通过；全量 758 项 0 失败、14 跳过（1 个 GUI 目标自动重试后通过）。远端 CI `41466839750` 三版本成功。

## 2026-11-02 22:10（Asia/Shanghai）· 拆分 EventLedger 与 runtime helpers
- `EventLedger`（约 422 行）移到 `scidev_ledger.py`；共享锁/`now_iso`/`new_id`/任务锁路径/重试常量移到 `scidev_runtime.py`。`scidev_core.py` 由 8491 行降到约 8023 行，`scidev_core` 继续 re-export 旧名字。
- `pyproject.toml` 的两个模块列表已加入新文件；core/context/cli 190 项测试通过，全量 757 项 0 失败、14 跳过、无重试目标。远端 CI `41459680128` 三版本成功。

## 2026-11-02 21:40（Asia/Shanghai）· 更细的下载分块
- `download_asset` 从 1 MiB 改为 64 KiB 分块读取：进度更新更及时，取消检查间隔更短；大小/摘要校验不变。更新/下载测试与 lint 通过；全量 757 项 0 失败、14 跳过、无重试目标。远端 CI `41455453012` 三版本成功。

## 2026-11-02 21:20（Asia/Shanghai）· 下载进度回传
- `download_asset` 新增可选 `progress(done,total)` 回调；桌面下载线程转发为 Qt 信号，状态栏显示百分比/字节数。
- 新增 1 项 UI 进度测试，并在下载测试中断言最终进度；全量 757 项 0 失败、14 跳过（1 个 GUI 目标自动重试后通过）。远端 CI `41451760796` 三版本成功。

## 2026-11-02 20:50（Asia/Shanghai）· 跨进程任务锁
- `CodingAgent.run` 在进程内线程锁之外增加 `.git/scidev-task.lock` 跨进程锁；第二个进程直接失败并提示改用 worktree。
- 非 Git 项目回退 `.research/locks/task.lock`；新增 3 项路径/进程互斥/释放测试。全量 756 项 0 失败、14 跳过（1 个 GUI 目标自动重试后通过）。远端 CI `41448043884` 三版本成功。

## 2026-11-02 20:30（Asia/Shanghai）· 异步更新对话框路径测试
- 新增 1 项测试：真实启动 `_CheckWorker`，经过事件循环后打开结果对话框；全量 753 项 0 失败、14 跳过、无重试目标。远端 CI `41444157509` 三版本成功。

## 2026-11-02 19:45（Asia/Shanghai）· 后台检查更新
- 新增 `_CheckWorker` 与 `UpdateDialog.show_for_workspace_async`：命令面板 `> update` 不再在 UI 线程阻塞网络检查，显示可取消进度；结果/失败回到主线程。
- 新增 2 项 worker 测试；取消只忽略结果，不中断阻塞中的 HTTP。全量 752 项 0 失败、14 跳过、无重试目标。远端 CI `41439594662` 三版本成功。

## 2026-11-02 19:20（Asia/Shanghai）· Pdb 控制命令测试
- 新增 `tests/test_debug_ui.py`：验证控制命令写入活动 PTY、无 pdb 时只提示；全量 750 项 0 失败、14 跳过、无重试目标。远端 CI `41435693025` 三版本成功。
- 首次全量在 `test_settings` 出现 Qt 原生崩溃，单测与重跑均通过。

## 2026-11-02 18:45（Asia/Shanghai）· 本机 fixture 安装器重建
- 用当前 stage_release.ps1 暂存 63 个文件（含全部新模块），加一个 dummy `scidev_client.exe` 后用 Inno Setup 构建 fixture 安装器。
- 产物 2,381,679 字节，SHA-256 `99b76372...7fb0`，unsigned；验证的是暂存/安装器脚本，不是真实 Nuitka 应用。

## 2026-11-02 18:30（Asia/Shanghai）· Release tag/版本一致性
- `windows-package.yml` 在 tag 触发时校验 `GITHUB_REF_NAME == v + APP_VERSION`，不一致直接失败，避免发布错版本。
- 新增静态断言；普通 push/manual dispatch 跳过该步骤。全量 748 项 0 失败、14 跳过、无重试目标。远端 CI `41430517645` 三版本成功。

## 2026-11-02 18:00（Asia/Shanghai）· Tag 触发的 Release workflow
- 新增 `.github/workflows/release.yml`：`v*` tag 触发，`workflow_call` 复用 Windows 打包 job，`gh run download` 取回 installer/manifest，`gh release create` 发布。
- `windows-package.yml` 增加 `workflow_call`；新增 1 项静态断言。未创建 tag/未实际发布。全量 748 项 0 失败、14 跳过（1 个 GUI 目标自动重试后通过）。远端 CI `41427247710` 三版本成功。

## 2026-11-02 17:20（Asia/Shanghai）· Pdb 控制菜单
- 编辑器“更多”菜单新增“Pdb 控制”：继续、单步跳过、单步进入、跳出函数、退出调试；通过现有 PTY 发送 `c/n/s/r/q`，pdb 未运行时只提示。
- 全量 747 项 0 失败、14 跳过（1 个 GUI 目标自动重试后通过）；未新增自动化测试。远端 CI `41422215761` 三版本成功。

## 2026-11-02 17:00（Asia/Shanghai）· 下载线程成功/失败测试
- 为 `_DownloadWorker` 增加成功路径和失败路径测试，验证 `completed`/`failed` 信号内容；全量 747 项 0 失败、14 跳过、无重试目标。远端 CI `41417336773` 三版本成功。

## 2026-11-02 16:35（Asia/Shanghai）· 稳定关闭确认测试
- `UnsavedCloseTests` 的 `QTimer` 轮询在“继续编辑”取消关闭后仍会继续，可能点击下一次确认框；增加 `active` 停止标志。
- 连续 3 轮 5 项关闭测试通过；全量 745 项 0 失败（1 个无关 GUI 目标自动重试后通过）。远端 CI `41413999895` 三版本成功。

## 2026-11-02 15:50（Asia/Shanghai）· 可取消的更新下载
- `download_asset` 新增 `cancel` 回调，每个 1 MiB chunk 前检查；取消抛 `UpdateError` 并清理 `.part` 临时文件。
- `UpdateDialog` 增加“取消下载”按钮，取消后下载线程正常结束并恢复按钮；新增 1 项取消测试。首次全量在 `UnsavedCloseTests` 出现 Qt 原生崩溃，单独与重跑均通过；全量 745 项。远端 CI `41410933383` 三版本成功。

## 2026-11-02 15:30（Asia/Shanghai）· 桌面端后台下载更新包
- `UpdateDialog` 新增“下载并校验安装包”：选择保存路径后由 `QThread` 调用 `download_asset`，校验 SHA-256；完成只显示路径，不执行。
- 下载中禁用关闭/按钮；不支持没有资产地址的 Release；新增 1 项按钮状态测试。远端 CI `41407215712` 三版本成功。

## 2026-11-02 14:30（Asia/Shanghai）· 跨进程 Git 写锁
- 新增 `scidev_filelock.FileLock`（Windows `msvcrt` / POSIX `flock`），10 秒等待；`GitManager` 只对 add/commit/reset/checkout 等写命令加 `.git/scidev-git.lock`（worktree fallback `.research/locks`）。
- 锁超时转为明确的 `PermanentError`；读命令不加锁，避免阻塞 UI 状态刷新。
- 新增 2 项测试：子进程持锁时父进程超时、写命令分类；`commit_paths` 现在用可重入 mutation session 把 read-tree/add/commit/reset 整段锁住，仍未做双进程真实任务 E2E。远端 CI `41404577147` 三版本全绿。

## 2026-11-02 14:10（Asia/Shanghai）· 修复 Windows CI 发布暂存失败
- `896ce5c` 的 Windows CI 三版本均失败：`scripts/stage_release.ps1` 硬编码模块清单漏掉 `scidev_sandbox.py`，导致 staged client/deploy dry-run `ModuleNotFoundError`。
- 暂存脚本改为自动收集 `scidev_*.py`；release staging 测试的期望集合也改为动态，避免新增模块后再次漏测。
- `test_debug` 在 CI 短路径/解析路径下比较原始字符串失败，改为比较 `Path.resolve()`，只断言 `-m pdb <relative>` 尾部。
- CI 编译步骤改为动态收集根目录/tests/scripts 的 Python 文件，新模块不会再漏编译。
- 远端 `31a4fe9` 的 Windows CI run `41397606334` 已在 Python 3.12/3.13/3.14 全部通过。

## 2026-11-02 14:00（Asia/Shanghai）· 工具注册表
- 新增 `scidev_tool_registry.ToolRegistry`：集中 name → handler 绑定和只读元数据；`CodingToolbox.execute_tool` 通过注册表取 handler。
- MCP 服务端复用 `READ_ONLY_TOOL_NAMES`；新增 1 项注册表测试。
- 执行逻辑仍在 `CodingToolbox`，这是绑定表而不是插件系统。

## 2026-11-02 13:45（Asia/Shanghai）· Docker 超时清理与 Windows-only 声明
- Docker 容器加 `--name scidev-<随机>`；超时/取消时调用 `docker rm -f` 清理，正常结束仍依赖 `--rm`。
- 新增 1 项 cleanup 测试；`pyproject.toml` 明确 Windows classifier，README 改为“仅支持 Windows 桌面”。

## 2026-11-02 13:30（Asia/Shanghai）· 只读 MCP stdio 服务端
- 新增 `scidev_mcp_server.MCPServer` 和 CLI `mcp-serve --workspace`：JSON-RPC initialize/tools/list/tools/call/ping/shutdown。
- 只暴露 search_code/read_file/list_files/get_project_context/git_diff；write_file/apply_patch/run_command 等直接拒绝。
- 新增 3 项服务端测试；已用 CLI 管道实测 initialize 返回。

## 2026-11-02 13:12（Asia/Shanghai）· 内置工具 schema 拆分
- 将 `CodingToolbox.definitions` 的 9 个内置工具 schema 提取到 `scidev_tool_schema.py`，core 减少约 180 行；browser/visual 条件工具仍由参数控制。
- 新增 1 项 schema 完整性/条件开关测试；执行逻辑仍在 `CodingToolbox`，这不是完整工具注册表。

## 2026-11-02 13:20（Asia/Shanghai）· 桌面端检查更新对话框
- 新增 `scidev_update_ui.UpdateDialog`：显示当前/最新版本、Release 日期、URL、资产名/大小和说明；只提供“打开 Release 页面”，不下载/执行。
- 命令面板 `> update`/`> 检查更新` 打开对话框；检查失败弹警告。
- 新增 4 项 Qt 测试；检查是同步网络调用，可能短暂阻塞 UI。

## 2026-11-02 13:00（Asia/Shanghai）· CI 可选 PFX 签名
- Windows Package Verification 新增可选签名：读取 `WINDOWS_SIGNING_PFX_BASE64`/`WINDOWS_SIGNING_PFX_PASSWORD`，导入 CurrentUser\My，传给 `build_installer.ps1 -SignThumbprint`，结束时删除证书；无 secret 时保持 unsigned。
- manifest `signed` 变为按实际签名状态输出；新增静态断言，未在真实 PFX/远端 runner 上执行。

## 2026-11-02 12:45（Asia/Shanghai）· Pdb 光标行断点
- `breakpoint_commands()` 生成 `b <相对路径>:<行>\nc`；F5 启动 pdb 后延迟写入终端，自动停在当前光标行并继续运行。
- 新增 1 项断点命令测试；GUI 端到端仍只做启动逻辑，未自动化验证 pdb 实际停点。

## 2026-11-02 12:20（Asia/Shanghai）· F5/Pdb 调试入口
- 新增 `scidev_debug.py`：选择项目 `.venv` 解释器，校验 `.py` 和工作区边界，生成 `python -m pdb <相对路径>`。
- 桌面端 F5、编辑器“更多”菜单和命令面板 `> debug`/`> 调试` 在集成终端启动 pdb；终端忙时拒绝，未保存文件提示先保存。
- 新增 3 项纯逻辑测试；这是终端式 pdb，不是 DAP 图形调试器。全量 729 项 0 失败、14 跳过、覆盖率 78%；顺带修复 `showEvent` 在无 `screenChanged` 的 window handle 上抛 `AttributeError` 的问题。

## 2026-11-02 12:00（Asia/Shanghai）· CLI 结果原子落盘
- `exec --result-file PATH` 把最终 JSON 原子写入工作区内文件；路径解析后必须位于工作区，越界拒绝并使成功任务返回 1。
- 成功/失败/取消都写同一份结果；写入失败打印错误，不掩盖原任务状态。
- 新增 1 项 CLI 测试，覆盖正常写入、无残留临时文件与越界拒绝。

## 2026-11-02 11:35（Asia/Shanghai）· 沙箱设置进入桌面前端
- `SummarySettings` 新增 `sandbox_backend`/`sandbox_image` 并持久化；Agent 构造和运行中切换都读取该配置。
- Agent 设置新增“命令沙箱/沙箱镜像”：本机或 Docker；Docker 镜像为空时拒绝保存，切换后对新命令生效。
- 新增 1 项设置往返/非法后端回退测试；未做 Docker 真实项目 E2E。

## 2026-11-01 15:25（Asia/Shanghai）· CLI 任务预算参数
- `exec --max-turns N`（1–64）和 `--max-tokens N` 覆盖本次任务的轮次/token 上限，不写回项目设置。
- 新增 1 项 CLI 预算贯通测试。

## 2026-11-01 15:10（Asia/Shanghai）· 可选 Docker 命令沙箱
- 新增 `scidev_sandbox.py`：`DockerSandbox` 只挂载工作区到 `/workspace`，默认 `--network none`、`--cap-drop ALL`、`no-new-privileges`、pids/内存/CPU/tmpfs 限制；Docker CLI/daemon 不可用时抛 `SandboxError`，不静默回退。
- `CodingToolbox.run_command` 在 `SCIDEV_SANDBOX_BACKEND=docker` 或 CLI `--sandbox docker --sandbox-image ...` 时改用容器 argv；默认 local 行为不变。CLI 参数已贯通到 `CodingAgent`。
- 新增 8 项测试（7 项 sandbox + CLI 贯通）；全量 723 项 0 失败、14 跳过、覆盖率 78%；未在本机 Docker daemon 上做真实项目 E2E，镜像需自带项目工具链。

## 2026-11-01 14:50（Asia/Shanghai）· 下载后显式启动安装器
- `scidev_update.launch_installer()`：只接受 `.exe/.msi` 和 Windows；启动前重新校验 SHA-256；不静默安装、不提权，MSI 用 `msiexec /i`。
- CLI `update --install` 必须与 `--download` 同用；启动成功返回 `installer_pid`，失败保留已下载文件并返回 1。
- 新增启动前校验测试与 CLI `--install` 测试。

## 2026-11-01 14:25（Asia/Shanghai）· Inno Setup 安装器脚本
- 新增 `packaging/windows/scidev.iss`：per-user 安装到 `{localappdata}\Programs\SciDevHarness`，包含完整 deployment、开始菜单/可选桌面快捷方式、卸载信息和安装日志。
- 新增 `scripts/build_installer.ps1`：校验 deployment/版本/签名指纹/HTTPS 时间戳，查找 Inno Setup 6，调用 ISCC 构建，可选调用 `sign_release.ps1`，输出路径/字节数/SHA-256；不下载依赖、不执行安装器、不自动安装。
- 新增 3 项静态测试并做 PowerShell 语法解析；全量 715 项 0 失败、14 跳过、覆盖率 78%。本机安装 Inno Setup 6.7.1 后用 notepad 作为 `scidev_client.exe` fixture 实际构建成功：`SciDevHarness-Setup-0.1.0.exe` 2,182,836 字节，SHA-256 `0b1f39a0d76ae402137c6491a179720a0dbd7aea2ed9793c835e0ff67adbb838`，未签名；fixture 只证明打包链路，不代表正式二进制。Windows Package Verification 现在安装 Inno Setup、构建并上传 unsigned installer/manifest；尚未在远端触发实际运行。

## 2026-11-01 14:10（Asia/Shanghai）· 可校验的更新包下载
- `scidev_update.download_asset()`：只接受 HTTPS，流式写入同目录临时文件，校验 Content-Length/Release 大小/上限/SHA-256 后原子改名；拒绝空文件、超限、HTTP 重定向和摘要缺失。
- CLI `update --download PATH [--sha256 HEX]` 下载并报告 `path/sha256/size`，不执行安装包；摘要优先取 Release digest，缺失时必须显式 `--sha256`。
- 新增 1 项下载成功/失败测试与 1 项 CLI 下载测试；更新文档。尚未提供 Inno Setup 安装器或自动安装。

## 2026-11-01 13:55（Asia/Shanghai）· TypeScript LSP 与双语言常驻
- `scidev_lsp.py` 新增 LanguageSpec/语言路由：Python 用 Ruff；`.ts/.tsx/.js/.jsx/.mjs/.cjs` 用 `typescript-language-server --stdio`，可用 `SCIDEV_TYPESCRIPT_LSP` 覆盖命令。
- `ResidentLSP` 改为按语言持有多个客户端；Python/TS 进程互不重启，仍合并连续输入并复用 didOpen/didChange。
- 编辑器对支持的后缀统一调度 LSP；TS 服务器缺失时静默跳过，诊断错误不阻断编辑。
- 新增 TS 命令解析与双语言客户端测试；LSP 专项 7 项通过；全量套件 709 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 13:30（Asia/Shanghai）· 无头补丁与多类验证闭环
- CLI `exec` 新增 `--patch-approval auto|reject`：默认 `auto` 在没有审核界面时应用全部已校验补丁块，仍受工作区路径限制；`reject` 保持旧文件不变。修复 CLI/eval 对已有文件的修改一律被“无审核界面”拒绝的问题。
- Harness 自动验证从“只跑首个候选”改为按 test/lint/typecheck 各选一个可自动批准命令；每个结果附结构化文件/行号诊断，再进入有界修复轮次。
- 标准 20 项本地 Qwen2.5-Coder-7B 基线：修复前 1/20（开启无头补丁前）、开启后 2/20，平均 4.0 轮，464.5 秒；摘要基线在 `evals/baselines/standard-20-qwen2.5-coder-7b.json`，完整报告在 `.research/evals/standard-20-auto-patch.json`，用于后续强模型对照。
- 新增 CLI 补丁审核测试、候选分类测试和文本补丁恢复测试；全量套件 707 项 0 失败、14 跳过、覆盖率 78%；ruff/mypy 通过。修复 `run_tests_coverage.py` 在 `coverage combine` 无新数据时误返回 1 的问题。

## 2026-11-01 21:00（Asia/Shanghai）· PR diff 与 review 桌面联动
- `scidev_pr.py` 新增 `pull_request_diff()`：有界 `gh pr diff`（默认 30 万字符、最多 100 万）、网络策略门控。
- CLI 新增 `pr-diff`；桌面 `> pr` 打开 PR 对话框，可加载元数据/diff、填写备注并提交 approve/comment/request-changes。
- 新增 1 项 PR diff 核心测试、2 项 PR UI 测试、1 项 CLI 测试；全量套件 704 项 0 失败、14 跳过、覆盖率 78%。
- 标准 20 项本地模型基线已完成：修复前 1/20，开启无头补丁自动应用后 2/20，见 13:30 节。

## 2026-11-01 20:00（Asia/Shanghai）· 发布签名与安装包校验
- 新增 `scripts/sign_release.ps1`：显式 40 位证书指纹、HTTPS RFC 3161 时间戳、SHA-256 Authenticode 签名、`/pa` 复验；不接受密码/pfx。
- `scidev_update` 只报告 HTTPS 安装包，并返回名称/大小/`digest`；新增 `verify_downloaded_asset()` 按 SHA-256 和大小上限校验。
- 新增 2 项签名脚本测试与 6 项更新/校验测试；全量套件 700 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 19:00（Asia/Shanghai）· 错误趋势面板
- 新增 `scidev_analytics.py`：只读 `.research/runs.db`，统计 1–90 天任务开始/完成/失败、成功率、按 `error_kind` 分布、可重试标记和每日失败。
- 新增 `scidev_analytics_ui.py`；命令面板 `> errors` 打开面板，含 sparkline 与表格。
- 新增 4 项 analytics/UI 测试；mypy 扩到 19 个文件；全量套件 696 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 18:00（Asia/Shanghai）· 无 usage 模型的 token 预算
- 服务端不返回 usage 时（Ollama 等），按任务内请求/响应字节数 `/4` 估算 token，并让 `max_task_tokens` 继续生效。
- `task_token_budget_exhausted` 事件新增 `estimated` 标记；新增 1 项估算测试；全量套件 692 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 17:00（Asia/Shanghai）· MCP 图形浏览器
- 新增 `scidev_mcp_ui.py`：树形显示工具/资源/提示词，读取资源、渲染 prompt、预览并显式插入输入框。
- 命令面板新增 `> mcp`；管理器随对话框启动，关闭时释放；资源/prompt 不自动进入模型上下文。
- 新增 2 项 fake-manager UI 测试；mypy 扩到 17 个文件；全量套件 691 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 16:00（Asia/Shanghai）· 常驻 Python LSP
- `scidev_lsp.py` 新增 `ResidentLSP`：每个窗口复用一个 Ruff 进程，合并连续输入，首次 didOpen、后续 didChange，回调可携带 generation。
- 桌面端用 `ResidentLSPBridge` 把后台诊断转成 Qt 信号；关闭窗口时停止 worker 和 Ruff 进程。
- 新增 1 项 fake-client 测试（复用单进程、didOpen→didChange、关闭）；mypy 扩到 16 个文件；全量套件 689 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 15:00（Asia/Shanghai）· CLI 无改动语义
- CLI `exec` 默认把“完成但 `no_changes_detected`”判为失败：`success=false`、`status=no_changes`、`error_kind=no_changes`、退出码 1。
- 新增 `--allow-no-changes`，供问答/分析类任务显式放行。
- 新增 CLI 测试：默认失败与显式放行；修复弱模型只回复计划却被当成成功的问题。全量套件 688 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 14:00（Asia/Shanghai）· 真实本地模型基线
- 用本机 Ollama `scidev-qwen2.5-coder-7b:q4_k_m` 跑 3 项 smoke：无 fallback 0/3，开启 `SCIDEV_TEXT_TOOL_CALL_FALLBACK=1` 后 1/3（`create-notes` 通过），平均 4.33 轮。
- eval suite 新增受限 `environment`（只允许 `SCIDEV_*`），任务子进程会继承；新增 `evals/local/suite.json`（fallback + 8 轮）和 `evals/baselines/local-qwen2.5-coder-7b.json`。
- 新增 1 项 suite 环境测试；全量套件 687 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 13:00（Asia/Shanghai）· 结构化错误分类
- 新增 `scidev_errors.py`：把失败分为 auth/rate_limit/timeout/network/tool/verification/workspace/internal，附用户消息、是否可重试和有界 detail。
- RetryQueue 的 `task_failed`/`task_retry` 事件、CLI 失败 JSON 都带 `error_kind`、`user_message`、`retryable`、`detail`；桌面失败气泡显示分类前缀。
- 新增 3 项分类测试；mypy 扩到 15 个文件；全量套件 686 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 12:00（Asia/Shanghai）· MCP 资源与提示
- `MCPClient`/`MCPHTTPClient` 新增 `resources/list`、`resources/read`、`prompts/list`、`prompts/get`。
- `MCPManager` 聚合多 server 的 resources/prompts，按 server 读取资源、渲染 prompt；结果有界。
- CLI 新增 `mcp`：默认列出 tools/resources/prompts，`--read SERVER URI`、`--prompt SERVER NAME --arg key=value`、`--json`。
- 新增 manager 与 CLI 测试；全量套件 683 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 11:30（Asia/Shanghai）· Worktree 管理 UI
- 新增 `scidev_worktree_ui.py`：列表显示分支、路径、HEAD，支持打开、删除、强制删除。
- 命令面板新增 `> worktrees` / `> worktree list` / `工作树`；打开选中 worktree 会切到独立窗口。
- 删除默认拒绝未提交改动；强制删除忽略改动并清理分支；当前窗口所在 worktree 不允许删除。
- 新增 1 项对话框测试；mypy 扩到 14 个文件；全量套件 681 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 11:05（Asia/Shanghai）· 本地崩溃报告
- 新增 `scidev_crash.py`：安装未捕获异常/线程异常钩子，写 `.research/crashes/crash-*.json`，含版本、Python、平台、线程、异常类型、有界 traceback。
- 写盘前脱敏主目录和 API key/token/password；保留最近 20 份；不联网、不收集局部变量；KeyboardInterrupt/SystemExit 不记录。
- 桌面 `main` 启动后安装；新增 3 项测试；mypy 扩到 13 个文件；全量套件 680 项 0 失败、14 跳过、覆盖率 79%。
- 同时移除 LSP 线程内重复的 `ast.parse`：语法状态由主线程 code-intel 缓存复用，避免一次输入触发两次解析；outline 防抖测试连续 3 次稳定通过。

## 2026-11-01 10:45（Asia/Shanghai）· GUI 隔离 worktree
- 命令面板新增 `> new worktree` / `新建 worktree` / `隔离工作区`：从当前 Git 仓库创建 `scidev/agent-gui-*` 分支，并自动打开独立窗口。
- 多窗口可并行跑任务；每个窗口有自己的仓库副本、编辑器、Git 面板和 Agent；worktree 默认保留。
- 新增 GUI 测试：创建 worktree、打开非原窗口的子窗口、清理测试 worktree；全量套件 677 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 10:20（Asia/Shanghai）· 更新检查
- 新增 `scidev_update.py`：从 `origin` 或 `--repo`/`SCIDEV_UPDATE_REPO` 推断仓库，读取 GitHub latest Release 的 tag、页面、说明、安装包 URL；只提示，不下载/安装。
- 语义版本比较、`--json`、`--fail-if-available`；CLI `--version` 改用统一 `APP_VERSION`。
- 新增 5 项模块测试和 CLI 测试；mypy 扩到 12 个文件；全量套件 676 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 10:00（Asia/Shanghai）· 扩大 mypy 覆盖
- `pyproject.toml` 的 mypy 文件从 2 个扩到 11 个：process、usage、policy、pr、migrations、eval、worktree、mcp、settings、intel、credentials。
- 修复新增类型错误：PR 编号解析、eval usage 字典/回归计算、MCP HTTP/stdio client 联合类型、settings 滚动区与 spinbox 值。
- `python -m mypy` 对 11 个文件通过；全量套件 670 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 09:35（Asia/Shanghai）· PR 查询与审查
- `scidev_pr.py` 新增 `view_pull_request`、`review_pull_request`：调用已登录 `gh` 读取 PR 元数据，提交 approve/comment/request-changes。
- CLI 新增 `pr-view`、`pr-review`；支持 `--json`、`--allow-network`，comment/request-changes 强制 body，校验编号与 action。
- 网络 deny 默认拒绝；新增测试覆盖查询、审查命令、空 body 和非法 action；全量套件 670 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 09:15（Asia/Shanghai）· Eval 并行与成本
- `EvalRunner` 新增 `jobs`（1–8），用线程池并行跑隔离任务；CLI `eval` 新增 `--jobs`。
- 结果记录 prompt/completion token，按 suite `pricing` 或 `SCIDEV_EVAL_INPUT_PRICE`/`SCIDEV_EVAL_OUTPUT_PRICE` 估算美元成本。
- 报告新增 `jobs`、`total_prompt_tokens`、`total_completion_tokens`、`estimated_cost_usd`、`pricing`。
- 新增并行与成本测试；全量套件 669 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 08:55（Asia/Shanghai）· 20 项固定 eval 套件
- 新增 `evals/standard/suite.json`：20 个固定任务，覆盖 bug 修复、文件创建/重命名、unittest/pytest、JSON/CSV/配置解析、dataclass、argparse、正则、排序/去重/二分、类型标注、async gather。
- 每个任务有独立 setup 和确定性 check；新增 `tests/test_eval_suite.py` 验证 id 唯一、check 非空，并确认每个任务在修复前确实失败；全量套件 668 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 08:30（Asia/Shanghai）· MCP HTTP 传输
- `scidev_mcp.py` 新增 Streamable HTTP 客户端：POST JSON-RPC，支持 JSON/SSE 响应、`Mcp-Session-Id`、超时和 4 MB 响应上限。
- `.research/mcp.json` 可用 `url`/`transport=http` 配置 HTTP 服务；网络策略 deny 时拒绝 HTTP 服务；stdio 行为不变。
- 新增本地 HTTP server 测试：工具发现、tools/call、会话头和网络 deny；全量套件 666 项 0 失败、14 跳过、覆盖率 78%。

## 2026-11-01 08:00（Asia/Shanghai）· 设置/会话 schema 迁移
- 新增 `scidev_migrations.py`：把 `.research/settings.json`、`sessions/*.json`、`events.db` 迁移到 schema v2。
- 规范化 execution/network/allowed_commands/turn/token 字段；补全会话 `messages`/`attachments`/`status`；设置 SQLite `user_version`。
- 修改前备份到 `.research/backups`，每个文件最多 5 份；dry-run 不写盘；坏 JSON 只报错不覆盖；幂等。
- CLI 新增 `migrate --workspace ... [--dry-run] [--json]`；新增 2 项迁移测试；全量套件 664 项 0 失败、14 跳过、覆盖率 79%。

## 2026-11-01 07:45（Asia/Shanghai）· GitHub PR
- 新增 `scidev_pr.py`：解析 GitHub `origin`（SSH/HTTPS），确认仓库根和功能分支，默认 base 为 `origin/HEAD` 或 `main`，拒绝从 `main`/`master` 直接建 PR。
- CLI 新增 `pr`：`--base/--title/--body/--draft/--dry-run/--json/--allow-network`；先 push 当前分支，再调用已登录的 `gh pr create`。
- 项目网络策略为 deny 时，PR 创建默认拒绝，需显式 `--allow-network`。
- 新增 4 项 PR 模块测试和 CLI dry-run 测试；全量套件 662 项 0 失败、14 跳过、覆盖率 79%。

## 2026-11-01 06:35（Asia/Shanghai）· Worktree 隔离
- 新增 `scidev_worktree.py`：校验 Git 仓库根，按任务 id 创建 `.research/worktrees/<id>` 与 `scidev/agent-<id>` 分支，返回路径/分支/base SHA；支持列表、dirty 检查、force 删除和 prune。
- CLI `exec` 新增 `--worktree`、`--worktree-id`；Agent 在隔离 worktree 内运行，JSON/人类输出包含 worktree 路径和分支。
- 新增 3 项 worktree 测试和 1 项 CLI 隔离运行测试；全量套件 657 项 0 失败、14 跳过、覆盖率 79%。

## 2026-11-01 05:55（Asia/Shanghai）· 命令白名单
- `scidev_policy.py` 新增命令前缀规范化与白名单：`;`/换行分隔，最多 50 条，每条最长 200 字符，拒绝 shell 组合、命令替换、环境变量赋值、绝对路径和上级目录。
- 白名单命中采用精确前缀/空格边界匹配，仅在工作区可写/完全访问下自动批准；手动审批仍逐条确认，网络 deny 优先于白名单。
- Agent 设置新增“命令白名单”；随项目设置持久化；`SCIDEV_ALLOWED_COMMANDS` 可覆盖。
- 新增白名单解析、边界、网络优先、Agent 自动执行、设置持久化测试；全量套件 653 项 0 失败、14 跳过、覆盖率 79%。

## 2026-11-01 04:55（Asia/Shanghai）· 网络策略
- `scidev_policy.py` 新增 `NetworkPolicy`（deny/ask/allow），`curl/wget/ssh/scp` 等命令归类为 `network`，默认不会自动批准。
- `deny` 在审批前硬阻断，即使完全访问也不执行；`ask` 仅允许手动批准；`allow` 允许工作区可写/完全访问自动执行。
- Agent 设置新增“网络策略”，随项目设置持久化；`SCIDEV_NETWORK_POLICY` 可覆盖；CLI/Agent 复用同一策略。
- 新增网络策略解析、自动批准、硬阻断、设置持久化测试；全量套件 651 项 0 失败、14 跳过、覆盖率 79%。
- 修复取消竞态：`run_command` 不再在开始时清空取消标记，改为新任务启动时统一重置；停止发生在命令启动前也不会再睡满超时。

## 2026-11-01 03:40（Asia/Shanghai）· Python 补全与跳转
- 新增 `scidev_intel.py`：基于本机 Jedi 的 Python 补全/定义查找，限制候选数量，定义只返回工作区内路径；缓存目录放 `.research/jedi-cache`，不可写时回退临时目录。
- 编辑器新增 `Ctrl+Space` 补全弹窗和 `F12` 跳转；补全只对 `.py` 文件生效，插入当前候选；无候选/不可用时写入日志。
- 依赖新增 `jedi>=0.19`；新增 2 项 intel 测试和 1 项 UI 补全测试；全量套件 649 项 0 失败、14 跳过、覆盖率 79%。退出时显式终止 Jedi 编译子进程，避免解释器退出崩溃。

## 2026-11-01 03:10（Asia/Shanghai）· 真实仓库 eval
- 新增 `scidev_eval.py`：读取 suite JSON，把 workspace 模板复制到临时目录，应用 setup 文件，运行 CLI/自定义 runner，再用确定性 check 命令判定成功。
- 报告包含 total/passed/failed、pass rate、平均轮数、总/平均 token、耗时、相对上次 pass rate 的 regression；可指定稳定 `--output` 比较。
- Agent/CLI 返回新增 `turn_count` 和 `usage`；新增 `scidev_cli.py eval --suite ... --output ... [--json]`。
- 示例 `evals/smoke/suite.json` 3 个任务；新增 2 项 eval harness 测试和 CLI 缺失 suite 测试；全量套件 646 项 0 失败、14 跳过、覆盖率 79%。

## 2026-11-01 02:40（Asia/Shanghai）· 图片任务附件
- 新增 `scidev_attachments.py`：校验 PNG/JPEG/GIF/WebP 魔数、MIME、大小和数量；单张 5 MB、最多 4 张、总计 12 MB；文件名去路径。
- Agent 会话创建时把图片作为 `image_url` 内容块加入用户消息；已有会话的新任务附加到最后一条用户消息；会话保存附件元数据。
- 聊天输入区新增“图片/清除”；CLI `exec` 支持重复 `--image`；无效图片在启动 Agent 前拒绝。
- 新增 5 项附件测试和 UI/CLI 测试；全量套件 643 项 0 失败、14 跳过、覆盖率 79%。

## 2026-11-01 02:00（Asia/Shanghai）· ConPTY 终端
- 新增 `scidev_pty.py`：Windows 优先用 `pywinpty` 的 ConPTY 启动命令，后台线程读取输出，非阻塞 `drain`，支持 `write` 交互输入、`setwinsize` 和退出码。
- 终端命令写入临时 `.cmd` 文件后再由 `cmd.exe /d /c` 执行，保留用户命令中的引号；输出清理 ANSI/OSC 和异常长空白；命令结束删除临时文件。
- 客户端终端优先 ConPTY；运行中再次回车会作为 stdin 发送；ConPTY 不可用或启动失败时回退隐藏窗口管道。
- `pywinpty>=3.0` 加入 Windows 运行依赖；测试更新为同时等待 pipe/PTY 结束。
- 新增 4 项 PTY 测试；全量套件 636 项 0 失败、14 跳过、覆盖率 79%。新增测试文件触发的项目上下文排序回归，已让 `tests/` 同分时权重降为 0.8。

## 2026-11-01 01:35（Asia/Shanghai）· 生命周期 hooks
- 新增 `scidev_hooks.py`：读取 `.research/hooks.json`（必须 `"enabled": true`），支持 `task_start`、`before_tool`、`after_tool`、`before_commit`。
- 命令不经 shell，限制在工作区内，超时 0.5–60s、输出 8KB；事件和 16KB 上下文通过环境变量传入；前三个事件非零退出阻止继续，`after_tool` 只记录。
- Agent 在任务开始、每次工具调用前后、自动提交前触发 hooks；`before_commit` 阻止提交时任务失败并保留改动。
- 新增 4 项测试；全量套件 632 项 0 失败、13 跳过、覆盖率 79%。

## 2026-11-01 00:50（Asia/Shanghai）· MCP stdio 工具
- 新增 `scidev_mcp.py`：读取 `.research/mcp.json`（必须 `"enabled": true`），启动 stdio MCP server，完成 initialize/tools/list，把工具以 `mcp__<server>__<tool>` 暴露给模型；tools/call 文本结果回传。
- `CodingToolbox.all_definitions()` 在静态工具后追加 MCP 工具；`execute` 对 MCP 调用要求命令审批，只读模式禁用；服务启动失败只记录错误。
- 新增 4 项测试：禁用不启动、工具列表/调用、Toolbox 审批执行、无审批回调拒绝；全量套件 628 项 0 失败、13 跳过、覆盖率 79%。
- 边界：只支持 stdio，无 resources/prompts/sampling、无 UI 配置向导、无 hooks；MCP 服务命令由项目配置文件信任。

## 2026-11-01 00:00（Asia/Shanghai）· 编辑器补丁 diff
- 新增 `PatchDiffPanel`：编辑器标签内显示彩色 unified diff，左侧逐 hunk 勾选，可应用已选或拒绝全部。
- `PatchReviewCard` 新增“在编辑器中审核”，打开同一待审核请求的 diff 标签；决定后关闭标签并解析原请求。
- `UnifiedDiffHighlighter` 区分新增/删除/hunk 头；拒绝或部分批准仍停止任务。
- 验证：PatchReviewUiTests 5 项通过；全量套件 624 项 0 失败、13 跳过、覆盖率 79%。CI #134 暴露 LSP UI 测试的时序依赖，已改为直接渲染缓存诊断，重跑全量通过。

## 2026-10-31 23:10（Asia/Shanghai）· Ruff LSP 诊断
- 新增 `scidev_lsp.py`：最小 JSON-RPC stdio 客户端，启动本机 `ruff server`，初始化、didOpen、等待 `publishDiagnostics`、关闭；支持超时和进程清理。
- 编辑器打开/修改 Python 文件时，防抖后台线程收集诊断；结果写入“问题”面板，双击跳转到行列。
- 语法错误仍由 AST 即时显示；LSP 不可用或超时时静默跳过，不阻塞编辑器。
- 验证：新增 `tests/test_lsp.py` 4 项（真实 Ruff LSP 语法诊断、干净文件、后缀拒绝、URI）；UI 诊断渲染测试；全量套件 622 项 0 失败、13 跳过、覆盖率 79%。

## 2026-10-31 22:50（Asia/Shanghai）· 回退模型
- 模型接入新增“回退模型”字段；保存到 `.research/provider.json`，支持 `SCIDEV_FALLBACK_MODEL`。
- 主模型返回 HTTP 404、`model_not_found` 或模型不存在的 HTTP 400 时，本次任务自动用回退模型重试一次；只回退一次，其他错误不回退。
- 新增 `provider_fallback_started/completed` 事件；请求 ID 加 `-fallback`，避免与主模型请求复用幂等键。
- 验证：provider 配置/触发条件测试；全量套件 617 项 0 失败、13 跳过、覆盖率 79%。

## 2026-10-31 22:25（Asia/Shanghai）· 推理强度与能力探测
- 模型接入新增“推理强度”：默认/none/low/medium/high/max，保存到 `.research/provider.json`，随主模型请求发送。
- 新增“探测能力”：异步请求 `/models`，再发送一次极小的 `ping` 工具调用，显示工具调用支持、上下文长度、视觉元数据和模型数。
- `ProviderConfig` 增加 `reasoning_effort`；Agent 保存配置时记录该值，并在编码请求中使用。
- 验证：provider 测试新增能力探测；UI 测试覆盖推理强度保存和探测按钮；全量套件 616 项 0 失败、13 跳过、覆盖率 79%。

## 2026-10-31 21:30（Asia/Shanghai）· 会话恢复
- Agent 设置新增“会话恢复”：列出最近会话，显示任务摘要/状态，可恢复选中会话。
- 恢复后设置 `current_session_id`；下一条消息复用原 session id 和 messages 继续对话，不自动重放已完成的工具调用。
- CLI 新增 `sessions` 子命令（`--workspace`、`--json`、`--limit`）；可用 `exec --session-id` 继续指定会话。
- `EventLedger.list_sessions` 改为按 `updated_at` 倒序。
- 验证：UI 恢复测试、CLI sessions 测试；全量套件 615 项 0 失败、13 跳过、覆盖率 80%。

## 2026-10-31 20:55（Asia/Shanghai）· 任务轮次与 token 预算
- `SummarySettings` 新增 `max_agent_turns`（1–64，默认 32）和 `max_task_tokens`（0 = 不限）。
- Agent 循环使用配置的轮次上限（仍受内置 32 轮硬上限约束）；每个任务记录 `task_usage_baseline`，token 预算只按本任务新增的服务端 usage 计算。
- 预算用尽时记录 `task_token_budget_exhausted`，任务失败并保留未提交改动。
- Agent 设置新增“任务预算”区；支持环境变量 `SCIDEV_MAX_AGENT_TURNS` / `SCIDEV_MAX_TASK_TOKENS`。
- 验证：新增 `tests/test_budget.py` 4 项；UI 设置持久化断言补充；全量套件 613 项 0 失败、13 跳过、覆盖率 80%。

## 2026-10-31 20:10（Asia/Shanghai）· 应用内模型接入与密钥链
- Agent 设置新增“模型接入”：API Base URL、模型、API Key、保存与异步测试连接。
- 新增 `scidev_credentials.py`：Windows Credential Manager（CredWriteW/CredReadW/CredDeleteW），Key 不进入项目 JSON 或审计日志。
- 新增 `scidev_provider.py`：非敏感配置 `.research/provider.json`、环境变量后备、配置校验、`/models` 连通性测试。
- `CodingAgent._build_provider` 优先读取保存配置；CLI 显式 `--api-base`/`--api-key`/`--model` 强制走环境变量路径。
- 验证：新增 `tests/test_provider.py` 10 项（含真实凭据管理器往返）；UI/CLI 测试补充；全量套件 609 项 0 失败、13 跳过、覆盖率 80%。

## 2026-10-31 19:15（Asia/Shanghai）· 任务检查点与回滚
- 新增 `scidev_checkpoint.py`：Agent 写文件前保存原始字节到 `.research/checkpoints/<id>/`；新文件记录为“任务前不存在”。
- `CodingToolbox` 在 write_file/replace/apply_patch 写入口自动 capture；`CodingAgent` 每次任务创建检查点，失败/取消后保留，最近 10 个自动清理。
- `CodingAgent.restore_last_checkpoint()` 恢复原文件、删除新文件并记录 `checkpoint_restored`；任务运行中拒绝回滚；Agent 设置增加“回滚上次任务改动的文件”按钮。
- CLI 增加 `--restore-on-failure`，失败时自动回滚并在 JSON 中返回 `checkpoint_restore`。
- 验证：新增 `tests/test_checkpoint.py` 7 项；CLI/UI 测试补充；全量套件 599 项 0 失败、13 跳过、覆盖率 80%。

## 2026-10-31 18:55（Asia/Shanghai）· Harness 自动验证
- 当模型结束任务但代码改动尚未验证时，Harness 会在工作区可写/完全访问模式下从静态发现的候选里选择第一个安全 test/lint/typecheck 命令自动运行。
- 自动运行结果记录为正式 verification run，绑定当前编辑版本和代码指纹；通过则允许完成/提交，失败则把退出码和结构化诊断加入后续修复提示。
- 同一编辑版本同一命令只自动运行一次，每次任务最多 3 次；用户拒绝过命令、只读/手动审批、单轮提示模式不会自动运行。
- 新增 `verification_auto_started/completed/failed/passed` 事件；`VerificationTracker` 记录增加 `approval_source`。
- 验证：全量套件 590 项 0 失败、13 跳过、覆盖率 80%；新增 workspace-write 无审批回调自动跑 pytest 的回归测试。

## 2026-10-31 17:55（Asia/Shanghai）· 无界面 CLI
- 新增 `scidev_cli.py exec`：不导入 PySide6，复用 `CodingAgent`、执行策略、Git 自动提交和 `.research` 审计。
- 支持 `--workspace`、`--task`、`--json`、`--execution-mode`、`--no-verify`、`--session-id`、`--api-base`、`--api-key`、`--model`、`--request-timeout`。
- 退出码：成功 0，任务失败 1，参数/工作区错误 2，取消 130；JSON 失败也输出结构化 `success=false`。
- 发布 staging 和测试期望已加入 CLI；新增 `tests/test_cli.py` 6 项，覆盖策略传递、JSON 结果、失败输出、工作区校验和 provider 环境覆盖。
- 验证：全量套件 589 项 0 失败、13 跳过、覆盖率 80%；CLI 本地 Qwen 烟测成功；CI #121 三套 Python 全绿。
- 下一步：让 CLI 支持自动验证命令选择/重试闭环，并把 checkpoint、模型配置继续补齐。

## 2026-10-31 17:15（Asia/Shanghai）· 执行策略与结构化验证诊断
- 已实现 `scidev_policy.py`：四档执行策略（只读 / 手动审批 / 工作区可写 / 完全访问）、窄范围验证命令识别、shell 链接/管道/重定向/绝对路径/上级目录/网络命令排除。
- `CodingToolbox` 在只读模式拒绝 write_file / replace_in_file / replace_many_in_file / apply_patch 和所有命令；工作区可写自动批准 pytest/unittest、npm/pnpm/yarn test、ruff/eslint、mypy/pyright、tsc --noEmit 等；完全访问自动批准其他命令，但仍拦截破坏性命令和 Harness 自有 Git 操作。
- `run_command` 记录 `approval_source=policy/user`、命令类别和诊断数量；pytest/ruff/mypy/tsc 输出解析为文件、行号、列、错误码，附加到工具结果。
- Agent 设置新增执行策略下拉框，项目 `.research/settings.json` 持久化 `execution_mode`，切换后立即更新当前 toolbox；验证门在只读且无审批通道时快速失败，工作区可写无审批通道仍可跑验证。
- 验证：全量隔离套件 583 项 0 失败、13 跳过、覆盖率 83%；ruff/mypy 通过。新增 `tests/test_policy.py` 覆盖策略、自动批准、只读写保护和诊断解析；7B 探针 `flagged=[]`；CI #118 三套 Python 全绿。
- 下一步：让 Agent 自动选择已发现验证命令并形成“失败→修复→重跑”闭环；任务级 checkpoint/回滚；应用内模型配置与密钥管理。

## 2026-10-31 11:50（Asia/Shanghai）· 本地 Qwen 第七轮（工具参数与文件名边界）
- 对全部文件/检索工具做参数类型 fuzz，并用官方本地模型 smoke 复测；修复 7 项：
  1. 零参数工具（如 git_diff）收到额外参数时跳过签名校验，直接抛 TypeError；现在零参数也会给出“不支持参数：（无）”。
  2. read_file/list_files 对 []/{} 等假值参数静默回退默认值；现在 start_line/end_line/depth 严格校验，path 必须是非空字符串。
  3. run_command 的数字/数组命令、bool/空值超时被静默转换；现在非字符串命令、布尔或非法超时明确拒绝，不启动进程。
  4. replace_in_file 批量编辑的 5 条校验错误仍是英文；现在统一中文，弱模型能直接理解。
  5. 含 NUL 的路径触发 atomic_write_bytes 清理二次异常，掩盖原始“embedded null character”；现在清理不再掩盖，`_resolve` 统一拒绝 NUL 路径。
  6. 300 字符文件名等写盘错误直接抛 OSError；现在文件名长度、目录创建、原子写入失败都转成 PermanentError，并保留原始原因。
  7. 新增 `_write_file_bytes` 统一 write_file/replace_in_file/批量替换的写盘错误处理。
- 验证：工具 fuzz 无未捕获异常；官方 Ollama smoke 通过（4 个编码任务 + 修 bug + 自动提交 + 总结）；真实 7B 探针（36 + 并发）flagged=[]、coverage_gaps={}；全量隔离套件 571 项 0 失败、13 跳过、覆盖率 79%；ruff 通过。
- CI：首次 #111 仅 3.12 在 RetryQueue 测试的临时 `.research/runs.db` 清理上报 WinError 32；给 5 个队列测试加 `ignore_cleanup_errors=True` 后，#112 三套 Python 全绿（9 分 26 秒），断言未减弱。

## 2026-10-31 02:25（Asia/Shanghai）· Windows CI 3.12/3.13 时序修复（已完成）
- CI #106 只有 Python 3.12 失败：流式取消测试用 8 秒总时长断言，CI 线程启动偏慢，8.265 秒误报；改为等流真正开始后再取消，只断言取消反应时间。
- CI #107 又暴露 3.13 的测试清理竞争：被终止的子进程短暂占用临时目录 cwd，`TemporaryDirectory` 清理报 WinError 32；两个取消测试改用 `ignore_cleanup_errors=True`，不把清理竞争当功能失败。
- 验证：CI #108/#109 三套 Python（3.12/3.13/3.14）全部成功（#109 10 分 19 秒）；本地 569 项 0 失败、13 跳过、覆盖率 79%；ruff 通过。

## 2026-10-31 01:05（Asia/Shanghai）· 本地 Qwen 第六轮（审批/审核取消与弱模型错误）
- 探针扩到 36 个场景：新增越界行号、额外参数、真实命令超时、.env 读取、补丁删除、审批中停止、审核中停止、非 Git 目录、非 Git 且不提交、既有脏改动、覆盖已有文件、Add File 已存在、拒绝补丁、部分批准。
- 修复 7 项：
  1. 审批弹窗期间点停止，审批返回后命令仍会执行；现在执行前检查取消标记，命令标记 cancelled 且不启动子进程。
  2. 补丁审核期间点停止，审核返回后仍会应用补丁；现在共享取消事件，审核后先检查，未写入即取消。
  3. 工具参数错误只抛 Python TypeError；现在未知/缺失参数返回可读中文，write_file/replace_in_file/apply_patch 拒绝数组等非字符串内容，不再静默写 repr。
  4. read_file 越界行号显示“空文件”；现在报告真实总行数；start_line/end_line 非整数、list_files depth 非整数给出明确错误。
  5. 补丁粘贴 read_file 行号（如 `1: `）只报“无效行”；现在附带“去掉行号”的针对性提示。
  6. 弱模型把工具失败结果原样当最终答复；现在识别为 model_tool_error_echo，追问一次，仍复述则明确失败。
  7. “不需要提交测试报告”被误判为不要提交代码；现在排除报告/文档等对象；同时补英文“只要示例/不要调用工具”保护。
- 探针自身修复：非 Git 目录按项目契约预期自动初始化并提交、空基线改用 HEAD 记录提交、拒绝/部分补丁的覆盖缺口与 allow_error 归类、场景被替换丢失后补回。
- 验证：真实 7B 全场景探针（36 场景 + 并发）flagged=[]；覆盖缺口仅 manual-commit（模型仍未实际执行 git，拦截逻辑由单测覆盖）；cancel-during-patch-review 本轮已实测进入审核、取消后补丁未落盘；全量隔离套件 569 项 0 失败、13 跳过、覆盖率 79%；ruff/mypy/编译通过。

## 2026-10-30 23:45（Asia/Shanghai）· 本地 Qwen 第五轮（已完成）
- 探针扩到 23 个场景（新增：流式取消、拒绝命令、.git 写入、超时钳制、目录读取、未声明工具、已有文件误用 replace_in_file、并行工具调用、数组参数、补丁冲突、只要示例、不要提交；并发场景仍单列）。
- 修复 7 项：
  1. 停止按钮无法中断正在生成的模型流；现在按 SSE 行检查取消，实测约 2 秒停止且不提交。
  2. 模型复述内部英文提示“The Harness rejected …”被当作最终答复并标记完成；现在识别并重试，超过上限明确失败。
  3. 同一轮多个工具调用（同一个 tool_call 标签或 json 代码块内多个对象）被判 Extra data 拒绝；现在逐个校验，全部合法才整批执行；7B 双文件创建实测通过。
  4. 用户明确“只要示例 / 不要调用工具”时模型仍调用 write_file 并自动提交；现在拒绝所有写操作，重复调用则停止并说明未修改文件。
  5. 用户“先不要提交”仍自动提交；现在保留改动在工作区并提示手动提交，不再误报“没有文件改动”。
  6. 命令输出中文乱码（cmd 默认 OEM 代码页）；Windows 改用 UTF-8 代码页启动并注入 PYTHONIOENCODING，解码按 UTF-8 → OEM/本地编码回退。
  7. read_file 把目录报成“文件不存在”；现在明确提示路径是目录并引导 list_files。
- 探针自身修复：阻塞命令改用 sleep、取消断言只在真实取消后生效、manual-commit 未触发自动重试、超时场景不再误报缺提交、数组/补丁场景误报修正、Git 路径 quotepath、事件账本读取重试；未触发路径单独记为 coverage_gaps。
- 验证：真实 7B 全场景探针 flagged=[]（23 场景 + 并发；manual-commit 模型未实际尝试 git，仅记为覆盖缺口，拦截逻辑由单测覆盖）；全量隔离套件 558 项 0 失败、13 跳过、覆盖率 79%；ruff/mypy/编译通过；官方 Ollama smoke 重跑通过（首次 7B 被拒 replace_in_file 后放弃，属模型随机性）。

## 2026-10-30（Asia/Shanghai）· 本地 Qwen 第四轮（已完成）
- 新增可重复的真实模型探针 `scripts/probe_weak_model.py`：11 个对抗场景（问答、子目录创建、路径越界、阻塞命令取消、改 bug、中文路径、手动提交、删除已跟踪文件、超窗、4B 小窗口、同项目并发），输出 `.research/weak_model_probe.json`。
- 修复 7 项：
  1. 原生工具参数非法 JSON 触发 `UnboundLocalError` → 改为工具错误回传模型并继续。
  2. Ollama 窗口探测不再信任架构 `context_length`（qwen3.5:4b 实际 4096，曾被当作 262144）；顺序改为显式 `num_ctx` → `/api/ps` 运行时 → 默认 4096。
  3. 中文提示 token 估算改为 1 字≈1 token（原先 `len//4` 低估约 4 倍）。
  4. 输出预算超出剩余窗口时自动收敛（4B 实测 12000→约 600）；提示词本身超窗仍显式失败。
  5. 禁止模型手动 `git add/commit/reset`（含 `--no-pager`/`-c` 变体），避免绕过按路径提交与敏感文件过滤；运行期外部提交报告真实 SHA 并告警。
  6. 创建后又删除的未跟踪文件不再让整次 `git add` 失败；无实际 diff 时提示“没有产生可提交的改动”。
  7. 纯 `<tool_response>` 等无效工具标记不再被当作最终回答，改为退回模型重试。
- 测试运行器为每个子进程隔离临时目录并设置 `GIT_CEILING_DIRECTORIES`，避免系统临时目录不可写时把测试仓库建进当前 checkout。
- 已验证：核心 136 项通过；官方 Ollama smoke 通过；真实 7B 探针连续两轮 10/10 场景无新 Harness 缺陷；全量隔离套件 546 项 0 失败、13 跳过、覆盖率 79%；ruff/mypy/编译通过。
- 下一步：跨进程（多开客户端）同项目互斥仍只有进程内锁，可加文件锁；Qt/PySide 离屏崩溃仍是原生层问题（已隔离+单次重跑）。

## 2026-10-29（Asia/Shanghai）· 第三轮遗留修复（已完成）
- 并发隔离：同项目任务锁，第二个任务快速失败，避免混提交。
- 无改动完成：提示词明显要求改动但没有文件变化时，UI/结果/审计均给出“未检测到文件改动”警告。
- Ollama 上下文：探测本机 `num_ctx`，超窗请求直接报错并给出设置方法，避免静默截断。
- Qt 崩溃：测试按模块/类进程隔离 + 崩溃目标单次重跑；本地全量 535 项 0 失败、225 秒。
- 下一步：如需跨进程（多开客户端）互斥，可在项目锁上增加文件锁。

## 2026-10-29（Asia/Shanghai）· CI 失败邮件（已完成）
- Windows CI 已从“每次 push 必失败”修到 #98 成功；根因是 ruff 未使用变量、测试依赖未提交的 UI/打包改动、Windows 短路径/重命名/超时/截图断言差异。
- 增强：测试运行器记录失败测试名与部分结果，CI 失败时输出注解；路径统一 `resolve()`，重命名 rebase 规范化，staging 超时 120 秒，截图断言与产品布局对齐。
- 后续：若仍收到 GitHub 邮件，检查 Settings → Notifications → Actions 或 Dependabot 通知。

## 2026-10-29（Asia/Shanghai）· 本地 Qwen 第三轮（已完成）
- 修复：停止/关窗时未终止正在运行的命令（进程树可能继续跑到超时）。新增 `CodingToolbox.cancel_running_command()`，取消即杀进程树并标记 `cancelled`；单测 + 真实 Qwen 验证（0.12 s 生效）。
- 已记录未改：同项目两个窗口并发任务会混提交（后完成者提交两者）；无改动请求被弱模型“口头完成”时仍标记完成。前者建议串行化/限制单项目任务，后者需意图校验策略。
- 已验证：危险命令拦截、中文文件名、同会话连续任务隔离、无改动不提交、provider 不可达会话 failed。
- 回归：529 项测试 516 通过、4 失败、1 错误、8 跳过（5 项沙箱限制）；ruff/mypy/编译通过；另有一次已知 Qt 离屏崩溃。

## 2026-10-28（Asia/Shanghai）· 本地 Qwen 第二轮（已完成）
- 修复 6 项：空回复假成功、provider 报错后会话停留 active、关窗不取消任务、缺少停止入口、重复失败工具跑满 32 轮、取消后旧转录污染新任务（导致提交错文件）。
- 新增：`CodingAgent.request_cancel()` + 取消检查点；“停止”按钮；取消后 UI 显示“已停止”；重复工具熔断；取消后的新任务使用干净上下文。
- 实测：本地 Qwen 停止按钮/关窗取消均不再写文件或提交；取消后第二个任务只提交自己的文件；527 项测试 522 通过、5 项沙箱限制失败。
- 遗留：Qt 离屏测试间歇崩溃仍存在（约 1/3，QCompleter 创建处，疑似 PySide6/Qt 层）；本地 Ollama 需自行设置 `num_ctx`，否则超长提示被静默截断。

## 2026-10-28（Asia/Shanghai）· 本地 Qwen 实测修复（已完成）
- 用 `scidev-qwen2.5-coder-7b:q4_k_m` 跑通项目自带 smoke 与真实客户端：4 个编码任务 + 修 bug（apply_patch）+ 自动提交 + 总结全部通过；单任务约 4.7 s。
- 修复 4 项：无审批通道时验证门槛白跑并给误导原因（改为立即失败并说明）、smoke 的验证/补丁审核配置自相矛盾（关闭验证门槛 + 自动批准补丁）、空参数工具气泡显示 `{}`（改“无需参数”）、跳过自动提交时完成气泡误显示“无新提交”（改“未自动提交”）。
- 遗留：`tests.test_client` 约 1/3 运行出现 Qt 原生访问冲突（QCompleter 创建处，PySide6 6.11.2 + Python 3.14 + offscreen），已记录证据，待 Qt/PySide 层定位。
- 下一步：优先调查该间歇崩溃；可用 `WA_DeleteOnClose`/进程隔离做对照实验，但需先修复受影响用例。

## 2026-10-28（Asia/Shanghai）· 第二轮 Bug 修复（已完成）
- 修复 6 项：同仓库多 `GitManager` 的 index.lock 冲突（共享锁 + 重试）、审计库跨实例并发与索引降级、敏感重命名检测失败关闭、`.research` 私有状态自动忽略、提交后版本树 HEAD 陈旧、错误可观测字段。
- 验证：515 项测试 510 通过、4 失败、1 错误、8 跳过（5 项为沙箱限制）；ruff/mypy/编译通过；改动已提交并推送。
- 下一步：可继续覆盖浏览器工具（当前覆盖率 49%）与视觉复查（66%）的未测路径。

## 2026-10-28（Asia/Shanghai）· 十项关键问题修复（已完成）
- 10 项全部完成：Git 异步+超时+单次查询、原子覆盖写、测试隔离、模块入库、按键防抖、巨型函数分阶段、进程树终止+命令策略、单一 porcelain 解析、静默异常可观测、静态门禁与仓库清理。
- 第 6 项拆出 `_prepare_coding_run`、`_execute_tool_calls`、`_enforce_verification_gate`、`_run_acceptance_stage`、`_finish_coding_session`；`_run_with_editor_context` 1383→705 行。
- 验证：508 项测试 503 通过、4 失败、1 错误、8 跳过（5 项均为沙箱限制）；ruff/mypy 通过；覆盖率 79%。下一步：可按需继续拆分模型调用段，并为各阶段补独立契约测试。

## 2026-10-28（Asia/Shanghai）· 关键问题清单（已完成排查）
- 体检出 10 项关键问题；修复结果见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。

## 2026-10-28（Asia/Shanghai）· 审计缺陷修复（已完成）
- 已修复：Git 工作区边界（不再把外层仓库当成本项目）、内置终端子进程控制台、899px 窄窗编辑器被挤压、Agent 设置面板横向溢出、版本树折叠详情后仍占宽、用量芯片窄屏独立成行、设置页断点常量统一。
- 回归：493 项测试中 480 通过、4 失败、1 错误、8 跳过；剩余 5 项均为本机沙箱限制（发布暂存需在源码树外、符号链接需特权），不是代码缺陷。已用目录联接验证暂存脚本本身可用（27 个文件）。
- 说明：过程中曾因临时目录回退导致测试污染真实 Git 历史，已恢复到 `ee229b4` 且 ahead=0，工作区文件未丢失。详见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。未提交 Git。

## 2026-10-08（Asia/Shanghai）· Git 失败提示修复（已完成）
- 后续：重启客户端后，可从历史重试失败会话，确认任务是否正常完成。

## 2026-10-08（Asia/Shanghai）— 对话执行过程折叠（已完成）
- 每条用户任务的模型中间回复、工具调用/结果及 Harness 过程默认收进可展开区；最终答复常显，补丁审核等需用户操作的内容不折叠。窄屏与深浅主题已做后台截图复核。
- 点击展开、再次点击收起已用合成界面事件确认；源码编译与差异检查完成，未连接模型、未提交 Git。下一项继续审视 monorepo/嵌套项目清单发现与适用范围。

## 2026-10-08（Asia/Shanghai）— 上下文用量与半屏 Agent 布局（已完成）
- 对话区常驻显示上下文估算/接口实报、窗口占比和输入草稿增量；模型上下文窗口可在 Agent 设置中配置。默认 Studio/Paper 为资源栏 20%、编辑器 30%、Agent 50%，窄屏自适应；截图见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。
- 编译、差异检查及 6 张后台 Qt 截图完成，未运行测试、未调用真实 LLM、未提交 Git。下一项继续审视 monorepo/嵌套项目清单发现与适用范围。

## 2026-10-08（Asia/Shanghai）— 十二项界面问题修整（已完成）
- 已优化聊天/资源栏响应式、状态对比、版本树字号/画布/详情区主题色、固定 Git 提交区、顶栏/欢迎页、专注设置与多行 Agent 指令、输入区、状态栏和分隔线。
- 已完成三套主题、720p/1080p/2K/4K、780×480 紧凑窗口及 14pt 字号的 62 张后台 Qt 渲染截图（含点击“适应全图”后的版本树）；Python 编译和 `git diff --check` 通过。未运行测试、未联网/调用 LLM、未提交 Git；截图见 `.research/ui-polish-20261008/`。
- 下一项按既有计划审视 monorepo/嵌套项目清单发现与适用范围；项目脚本和测试仍需逐项批准，不自动运行。

## 2026-10-07（Asia/Shanghai）— 后台进程不闪控制台
- 修复及专项验证已完成，详见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)；源码客户端重启后生效，不需安装依赖。
- 当时的下一项界面收尾已于 2026-10-08 完成；本项未自动提交或重打包 exe。

## 2026-10-07（Asia/Shanghai）— 文件右键外部打开
- 文件树与编辑器标签的外部打开入口已实现，专项检查与后台菜单截图已完成；验证结果见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。不会自动保存未保存的代码，不增加弹窗。
- 下一项仍为下列界面评估待办，需按后续需求处理；本轮不自动提交 Git 或重打包 exe。

## 2026-10-07（Asia/Shanghai）— 后台截图外观评估
- 已打开源码客户端，生成 110 张 Qt 后台截图，人工复查 23 张代表状态，覆盖 720p/1080p/2K/4K；评估清单及证据见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。Windows 原生窗口捕获返回 SetIsBorderRequired / E_NOINTERFACE，截图采用 Qt Windows 隐藏窗口渲染与离屏渲染。
- 待优化重点：窄屏对话区、大字号资源栏截字、浅色计划状态对比度、版本树字号/空间与 Git 操作层级。当前请求为评估，本轮仅记录清单和截图。
- 计划占位文字与旧步骤重叠在完整事件循环中未复现，确认为截图流程的延迟删除残影，不列入产品问题。

## 2026-10-07（Asia/Shanghai）— 独立可搜索设置页
- 可搜索设置页已完成：左下角齿轮 / Ctrl+, / > settings；17 项外观与编辑器设置即时应用并保存，支持重置和 Agent 配置入口。全量 479 项运行成功（3 项条件跳过），详见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。
- 本轮复查发现并修复 1 项回归测试隔离问题：分辨率测试曾读取本机已保存的字体偏好；改为临时 INI 后，全量 479 项通过（3 项跳过），截图布局冒烟通过。产品按偏好缩放的逻辑未改。
- 下一步结合真实使用反馈复查多显示器字体和大字号布局；本轮后台截图已复核，不自动提交 Git 或重打包 exe。
- 保留下面的嵌套项目上下文待办；本轮没有自动提交 Git。

## 当前工作：嵌套项目上下文与运行时发现（2026-10-05，Asia/Shanghai）
- 已扩展“核验环境”支持根清单对应的 Node/Rust/Go 工具链版本；Python、工具链及 GPU/CUDA 结果只在用户点击并一次批准后检查，详情见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。
- 默认俄罗斯方块样例已改为纯网页 `examples/tetris/index.html`，无服务/依赖，双击即可离线游玩；打包、复制脚本及上下文检索已同步，见 [HISTORY_RESULTS.md](HISTORY_RESULTS.md)。
- 下一步评估 monorepo/嵌套项目清单发现与适用范围呈现；继续禁止项目打开时自动运行，禁止联网、安装依赖、构建/测试或执行项目脚本，清楚区分根清单静态信息与实际检查结果。
- 多 Agent 的只读并行能力已完成记录；编码专员写入仍串行，没有工作区分支隔离或冲突自动合并。
## 2026-10-05 08:54 (Asia/Shanghai) - 上下文压缩省流收益实测
- 本机 Qwen2.5-Coder-7B、合成 10 轮会话独立复测两次：压缩前后续请求体 21,976 B → 2,945/3,097 B（降 86.6%/85.9%）；prompt tokens 5,051 → 642/686（降 87.3%/86.4%）。总结调用分别消耗 4,646/4,689 tokens；请求体与 token 均约 2 次后续调用回本。
- 增加 Provider 实际 JSON 请求体缩减回归；专项通过，全量 385 项通过。仅本机 localhost 与合成数据；未联网、未改用户项目代码、未下载模型、未提交 Git。字节数不含 HTTP/TLS 头。

## 2026-10-05 08:38 (Asia/Shanghai) - 项目上下文能力复核
- 确认现有实现覆盖自动任务上下文、本地相关文件/符号检索、目录级 `AGENTS.md`、依赖与环境摘要、Agent 按需查询、未保存编辑器只读快照和桌面异步预览；真实仓库 smoke 为 Tetris 任务命中引擎与测试文件。
- 上下文专项 30 项、桌面上下文专项 5 项及全量 385 项通过；Python 编译、`git diff --check` 通过。检索仍为有界词法排序；未调用 LLM/联网/安装依赖，未提交 Git。

## 2026-10-05 08:23 (Asia/Shanghai) - 低流量请求身份与服务端退避
- 编码请求 ID 现在按 session/task/轮次隔离；总结 ID 按 session/task/阶段/轮次隔离。HTTP 幂等键使用逻辑 ID 与精确 JSON 请求体 SHA-256，完全相同的重试稳定复用，恢复后内容变化不会撞用旧 key。
- 429/5xx 解析 Retry-After 秒数或 HTTP 日期，持久队列等待时间不短于指数退避，服务端提示最多等待 1 小时；辅助总结仅等待最多 5 秒，更长提示时跳过本次重试，不阻塞编码任务。修复 HTTPError 响应流未关闭，并记录总结实际尝试数。
- 新增 7 项回归；重试/幂等专题 8 项、全量 385 项通过；ResourceWarning 严格模式专项、Python 编译和 `git diff --check` 通过。真实服务是否支持 Idempotency-Key 未验证；没有真实 LLM/联网请求，也未提交 Git。下一步量化上下文压缩收益。

## 2026-10-05 07:52 (Asia/Shanghai) - 非标准测试入口与 monorepo 发现补强
- 静态发现器按嵌套清单生成 Node/Go/Rust/Maven/Gradle/.NET 子项目候选；支持 npm `test:*`、tox/nox 测试入口及 Makefile/Justfile 明确 test/check 目标。Node 优先 packageManager/最近锁文件；候选附项目证据且不读取/展开脚本正文。
- 新增 9 项发现器正反例，覆盖无测试证据、未知包管理器、npm 默认占位脚本、Shell 元字符路径和嵌套工程；验证模块 30 项、全量 378 项通过。当前项目上下文只读 smoke 正确列出 `.venv` 和 Tetris/主 tests 两个 unittest 候选；Python 编译、`git diff --check` 通过。
- 发现保持静态且有界；候选仍需逐条批准执行，不安装依赖，未知配置可能漏检。未运行候选测试命令、未调用真实 LLM/联网、未提交 Git。下一项审查低流量请求/重试边界。

## 2026-10-05 07:28 (Asia/Shanghai) - 独立语义验收
- Agent 设置新增默认关闭/建议/必需三种模式。启用后用不同模型 ID、无工具权限的复核请求审查任务、最终说明、最多 40 个安全文本文件的完整 diff（总计 36,000 字符）和检查摘要；逐条标准必须引用变更路径，结果写入会话、事件账本及用量统计。
- 必需模式在明确未满足时最多触发一次修正并复核；模型不可用、响应不确定、范围不完整/过限或最终未通过时不自动提交。复核后重读 diff，等待期间工作区变化会使结论失效；手动重试保留待审改动。不同模型 ID 仍可能是同一服务商，不是形式化证明。
- 验收专项 15 项、设置 UI 2 项、发布暂存 7 项及全量 369 项通过；Python 编译、`git diff --check` 通过。未调用真实 LLM/联网/安装依赖，未提交 Git。下一项继续补强非标准测试栈发现。

## 2026-10-05 06:52 (Asia/Shanghai) - 项目上下文端到端复核
- 用户提出的项目上下文能力已在当前工作区实现：自动任务注入、相关文件/符号检索、目录级 `AGENTS.md`、环境与依赖摘要、按需工具查询、未保存编辑器只读快照及异步预览；本轮复核未重复改写已有功能。
- 上下文专项 30 项、桌面 UI 专项 5 项、全量 354 项通过；`git diff --check` 通过。未调用 LLM/联网/安装依赖，未提交 Git。

## 2026-10-05 06:36 (Asia/Shanghai) - 静态测试命令发现
- `TestCommandDiscovery` 从可读文件、根清单及测试源码模式推断候选；覆盖 pytest/unittest、Node test script、Go、Cargo、Maven/Gradle、.NET。识别独立测试根目录，存在未链接的项目虚拟环境解释器时优先列出；结果显示在上下文摘要/桌面预览，并明确不代表命令可运行或测试已通过。
- 候选发现不运行 shell、不安装依赖；静态读取有文件数/字节上限，路径拒绝越界与链接；未知运行器会提示无法推断。每条真实命令仍由 `run_command` 请求批准。
- 全量 354 项、验证器/上下文/客户端专项、项目 `.venv` UI smoke、编译及 `git diff --check` 通过。未执行发现出的测试候选、未调用真实 LLM/联网/安装依赖、未提交 Git。

## 2026-10-05 06:04 (Asia/Shanghai) - 项目上下文检索排序补强
- 基准发现“重复支付请求”会把通用扣款代码排在幂等实现之前；加入复合概念权重后，11 个中英/标识符场景 Top-1 与 Top-5 全命中，陌生主题不返回无关文件。本仓库另抽查 7 个任务，目标实现均进入 Top-5。
- 暂维持无模型、无依赖的本地词法检索；暂不引入向量索引，待真实使用中出现漏召回再评估。
- 全量 342 项、项目虚拟环境依赖/UI smoke、编译与 `git diff --check` 通过。未调用真实 LLM、未联网/安装依赖、未提交 Git。

## 2026-10-05 05:50 (Asia/Shanghai) - 未保存编辑器缓冲接入项目上下文
- 任务提交与上下文预览采集最多 8 个打开的脏文件、合计 1 MB；上下文检索、搜索和读取统一使用编辑器快照，依赖清单也按未保存版本解析。超预算文件从搜索排除，Agent 不读取旧磁盘内容。
- 任务期间所有脏缓冲只读；为重试保留的快照暂存于本机 `.research/runs.db`，任务创建事件不记录源码正文，只记快照路径/字节数；成功任务清除队列副本，最终失败但仍可手动重试的任务保留。上下文片段/工具读取结果仍保存在本地会话记录。
- 专项 34 项、全量 341 项通过；桌面依赖 smoke、编译和 `git diff --check` 通过。未连接真实 LLM、未联网/安装依赖、未提交 Git。下一步继续评估词法排序质量与语义索引的必要性。

## 2026-10-05 02:05 (Asia/Shanghai) - 跨会话项目记忆已接入
- 新任务会从同项目已完成会话总结中检索最多 3 条/6000 字符，展示记忆引用提示；设置面板可关闭并配置条数/长度。SQLite 倒排索引本地保存，最多 1000 条；同会话排除、网络重试复用首份快照，常见凭据格式索引前脱敏。
- 修复专项测试发现的 SQLite 连接未关闭导致 Windows 文件锁；新增 10 项记忆回归。全量 287 项、依赖 smoke、发布暂存、Python 编译与 `git diff --check` 通过。剩余边界：词法检索、只索引最终总结，常见格式脱敏不等于完整秘密识别；未接真实 LLM。下一项可补强验证反馈闭环。

## 2026-10-05 01:45 (Asia/Shanghai) - 项目上下文能力复核
- 当前工作区已实现本地项目索引、任务相关片段/符号检索、依赖与目录指令、Agent 工具调用、编辑/重试刷新及客户端异步预览；确认进入发布文件清单。本轮未发现需修复的缺陷。
- 定向测试 15 项、全量测试 277 项、Python 编译和 `git diff --check` 通过。边界仍是关键词检索、进程内缓存，不含向量索引或完整环境探测；下一缺口为跨会话长期记忆检索。

## 2026-10-05 01:36 (Asia/Shanghai) - 结构化任务规划已接入
- Agent 新增 `update_plan` 工具与自动/必须/关闭模式；计划修订先落盘，再将完整步骤快照追加到事件账本，任务重试可恢复，新任务会归档最近 20 份计划。必须模式首轮只允许创建计划；有未完成步骤时有限续作，最终失败不自动提交。
- 客户端右侧计划卡片可折叠、显示进度并配置模式；发布暂存和三主题宽/最窄布局截图纳入回归。全量 277 项、依赖 smoke、编译、发布暂存、离屏响应式截图通过。计划完成状态仍由模型报告，不等于独立验收；下一步可补跨会话历史记忆检索。

## 2026-10-05 01:11 (Asia/Shanghai) - 项目上下文已接入
- 代码检索、相关片段、项目依赖/目录指令与上下文刷新已接入 Agent；客户端 `@` →“查看项目上下文”可异步预览。
- 本轮完成时全量 266 项、依赖 smoke、编译、差异检查和深浅/紧凑离屏截图通过。

## 2026-10-05 00:48 (Asia/Shanghai) - 编辑器路径状态 Bug 修复
- 修复无符号文件大纲不显示占位项，以及重命名文件/目录后导航、搜索结果、面包屑、终端目录和共享编辑器路径未同步。
- 全量 248 项测试通过；下一步按用户反馈或常规回归继续检查其他 IDE 边界。

## 2026-10-05 00:33 (Asia/Shanghai) - Harness 能力状态盘点
- 已在 `SCIDEV_HARNESS.md` 增加实现状态表，区分 Agent 循环、工具、上下文、规划、记忆、验证、Git/IDE 与尚未实现的浏览器、多 Agent、科研实验执行器。
- 下一步按缺口优先级讨论是否补项目检索、结构化计划或长期记忆；不把当前未实现能力写成既成事实。

## 2026-10-05 00:27 (Asia/Shanghai) - 默认俄罗斯方块项目
- `examples/tetris` 已成为首次启动默认工作区；保留显式/已选项目，并迁移旧的仓库根目录默认值。样例无需第三方依赖，源码随独立版放在 exe 旁。
- 规则 7 项、Tk 界面启动、打包复制和主项目全量回归通过。下一步核对客户端首次打开样例后的文件树与编辑器体验。

## 2026-10-05 00:14 (Asia/Shanghai) - 下一步审查编辑器标签生命周期
- 顶部搜索的单字符展开与 Git 页面切回后的宽泛结果恢复已修复，回归及全量验证通过。
- 下一步审查编辑器标签关闭、重开、重命名后的状态同步；先复现再修复。

## 2026-10-05 00:04 (Asia/Shanghai) - 后续 IDE 交互审查
- 顶部搜索单字符命中目录不展开的问题已修复，验证记录见历史文档。
- 下一步继续审查 IDE 搜索与文件树交互边界；先复现再修复，保持文档精简。

## 2026-10-04 23:19 (Asia/Shanghai) - 编辑器分栏数据一致性审查
- 本轮确认 1 个 Bug：同一文件在左右编辑器组打开时会创建独立缓冲区；临时项目实测左侧保存 `value=2` 后右侧仍是旧 `value=1`，右侧再保存会静默覆盖磁盘为 `value=3`。仅复现，未改产品代码。
- Ctrl+W 关闭标签路径已实测可用，不计问题。下一轮修复同文件跨分栏的一致性并补双向保存/未保存状态回归；原生桌面窗口当前不可见，交互通过离屏 Qt 事件复现。

## 2026-10-04 23:07 (Asia/Shanghai) - 顶部宽泛搜索修复完成
- 搜索命中目录超过 150 个时，改为按可视区域分批展开；滚动时继续展开，避免结果被折叠成“空文件夹”或一次展开整个项目。提交 `0ce2858` 已推送。
- 本机全量 235 项、100%/125% DPI 截图矩阵、依赖 smoke、QML lint 通过；GitHub CI Python 3.12/3.13/3.14 全绿。下一轮继续按“先审查、后修复”检查其他 IDE 边界。

## 2026-10-04 22:43 (Asia/Shanghai) - 宽泛顶部搜索审查
- 本轮确认 1 个 Bug：匹配目录超过 150 个时自动展开逻辑完全跳过；151 个命中目录实测为 151 个可见、0 个展开，文件结果被折叠，仍像空文件夹。仅审查复现，未改产品代码。
- 100%/125% DPI 的三主题标准分辨率矩阵通过；Studio 另测四档随机邻近尺寸，4K 字体缩放有生效。CUA 无原生窗口，本轮截图均为 Qt 离屏渲染。下一轮修复宽泛搜索展开策略并覆盖此边界。

## 2026-10-04 22:32 (Asia/Shanghai) - 顶部搜索目录展开修复
- 搜索逻辑原本会过滤真正没有匹配文件的空目录；匹配文件的父目录却保持折叠，容易看起来像空结果。现在窄范围搜索自动展开匹配路径，超过 150 个目录时不自动展开以保护性能。
- 新增嵌套目录回归测试；全量 234 项、`compileall`、`git diff --check` 通过。下一步继续审查并统计新的 IDE 问题。

## 2026-10-04 22:15 (Asia/Shanghai) - 同屏显示变化信号修复
- 修复上一轮确认的问题：订阅当前屏幕的 `geometryChanged` 与 `availableGeometryChanged`，同屏改变分辨率时重算 UI/普通窗口尺寸，工作区变化时重新夹取窗口；切屏会解绑旧屏幕信号，最大化/全屏状态保留系统几何。
- 新增 3 项可触发 Qt 信号回归；专项 10 项、全量 233 项通过。100%/125% DPI 四档及随机邻近分辨率截图、QML lint、依赖 smoke、compileall 通过。原生桌面不可见，未声称实机迁屏验收。
- 下一轮继续按约定先审查并统计新问题，再进入修复轮。

## 2026-10-04 22:03 (Asia/Shanghai) - 同屏显示模式变更审查
- 本轮确认 1 个待修复问题：客户端只监听 `QWindow.screenChanged`，未监听当前 `QScreen.geometryChanged` / `availableGeometryChanged`；同一屏幕改分辨率或可用工作区变化时，UI 缩放和窗口边界不会自动更新。
- 依据 Qt 官方信号说明核对；100%/125% DPI 四档及随机邻近尺寸截图通过，依赖 smoke 与全量 230 项通过。CUA 当前无可见原生窗口，截图为 Qt 离屏渲染。
- 下一轮修复信号订阅与解绑，并用屏幕几何/可用区域变化回归验证。

## 2026-10-04 21:49 (Asia/Shanghai) - 跨显示器窗口尺寸修复
- 修复迁屏时只缩放 UI、不调整窗口几何的问题：按显示缩放比例调整正常窗口，并约束到目标屏幕可用区域；最大化/全屏与 Qt 离屏画布保持原几何。
- 新增迁屏、往返、负坐标副屏和最大化回归；全量 230 项通过，100%/125% DPI 截图矩阵、QML lint、依赖 smoke 通过。截图为离屏渲染，未完成原生桌面迁屏验收。
- 下一轮继续审查 IDE/Git 交互与响应式边界，按“先统计问题、下一轮修复”节奏进行。

## 2026-10-04 21:41 (Asia/Shanghai) - 多显示器切换尺寸排查
- 本轮确认 1 个实质 UI Bug：跨屏回调只更新 UI 缩放，不调整窗口几何。复现为 720p 的 1228×626 窗口移到 4K 后仍为 1228×626，移回 720p 也不变；高分辨率下窗口偏小，反向切到低分辨率可能超出屏幕。
- 当前只完成定位与复现，不改产品代码；按轮次约定，下一轮修复并验证窗口跨屏尺寸适配。CUA 当前没有可见原生窗口，本结论来自 Qt 离屏窗口对象复现。

## 2026-10-04 21:34 (Asia/Shanghai) - 高分辨率版本树滚动范围复核
- 白盒发现版本树节点放大后内容高度仍按 100% 计算，且向下拖分支不扩展滚动范围；现统一按 UI 缩放重算最小高度，并在节点移动时更新。
- 回归覆盖缩放高度与拖动扩展；全量 227 项通过，100%/125% DPI 截图矩阵、标准及随机邻近尺寸均通过。代码提交 `2cd3a85` 已推送，GitHub CI `37205845659` 在 Python 3.12/3.13/3.14 全绿。
- 截图为 Qt 离屏渲染，当前无原生桌面可验；最小 Qt 本地门禁仍受 venv 中额外的 `PySide6-Addons` 影响，未改环境。下一步继续审计 IDE/Git 交互边界。

## 2026-10-04 21:21 (Asia/Shanghai) - 分辨率自适应与高 DPI 复核
- 已实现按屏幕逻辑分辨率渐进放大界面字号/控件间距、编辑器等宽字体和开发版本树卡片；跨屏时更新缩放，Ctrl+滚轮锚点、紧凑树自动适配按同一缩放因子计算。
- 新增单调/上限、样式尺寸、编辑器与 QML 缩放、缩放锚点回归；修复 Qt offscreen 虚拟小屏把测试窗压进紧凑布局的问题。全量 227 项通过。
- 100% 与 125% DPI 下，720p/1080p/2K/4K 和每档随机邻近分辨率截图矩阵通过；视觉抽查 720p/4K。QML lint、launcher、依赖 smoke、pip check、compileall、diff check 通过。
- 最小 Qt runtime 本地门禁因现有 `.venv` 多装了可选 `PySide6-Addons` 未通过；未卸载或修改环境。提交 `f43b184` / `02aae30` 已推送，GitHub CI `37205423114`、`37205443948` 全绿。

## 2026-10-04 20:51 (Asia/Shanghai) - 顶部快速搜索空目录误显示修复
- 本轮定位 1 个产品 Bug：过滤器原先无条件放行目录，导致空目录和无匹配内容的目录混在搜索结果中。改为按工作区相对路径索引匹配文件，只保留命中文件与祖先目录；文件系统变化/刷新时更新索引。
- 搜索专项 8 项、全量 223 项通过；本机截图 smoke 的 720p/1080p/2K/4K 与紧凑布局检查通过。另修复分栏测试夹具的 Qt 键入方式。代码 `b58a576`、文档 `a8246d1` 已推送；GitHub CI `37203806406` 在 Python 3.12/3.13/3.14 全绿。
- 后续继续审计分辨率邻近尺寸随机回归与屏幕尺寸对应的字体/UI缩放。

## 2026-10-04 20:14 (Asia/Shanghai) - 720p/1080p/2K/4K 实际像素回归
- 截图矩阵现覆盖四档标准物理分辨率，编辑器测三主题、版本树测主主题，并在 100% 与 125% 缩放下都校验截图像素尺寸、窗格和状态栏标签边界/重叠。
- 高分辨率截图采样改为随画面加密，消除旧固定稀疏采样在 2K 的误报；本机 100%/125% 两轮 smoke 全绿，720p 版本树正确切成上下布局。待提交并等 GitHub CI 复核。

## 2026-10-04 20:00 (Asia/Shanghai) - IDE 黑盒交互与截图复核
- 新增真实 Qt 输入回归：Ctrl+P/命令面板/Ctrl+Shift+F/Ctrl+F、资源树双击固定打开，以及 F12 后 Alt+←/→ 导航往返；全量 219 项通过。
- 60 张多主题/多尺寸截图 smoke 通过并人工检查 780×480、820×600、1500×920；启动器运行时、依赖 smoke、compileall 均通过。未复现搜索异常；原生客户端进程仍不可见，继续审计任务重试与编辑器分栏。

## 2026-10-04 19:48 (Asia/Shanghai) - 搜索输入报错排查
- 本机 `.venv`（PySide6 6.11.2）用 Qt 按键事件复测 Ctrl+P、Ctrl+F、Ctrl+Shift+F：输入字母均未触发异常，工作区搜索结果正常；相关 6 项回归测试通过，GitHub 三版本 CI 全绿。
- CUA/进程检查没有发现可操作的客户端窗口或运行中的本项目进程；暂未复现，先不猜测性改代码。待拿到报错原文/截图、出错的搜索框和客户端启动方式后对照实际版本定位。

## 2026-10-04 19:42 (Asia/Shanghai) - Windows CI 路径别名回归夹具修正
- CI `37199469935` 三个 Python 版本都只失败在 junction 测试的 `relative_to`：runner 临时路径一处为 `RUNNER~1`、另一处为 `runneradmin`；产品代码与其余 215 项通过。
- 测试改为比较规范化绝对路径；本机 Windows junction 用例通过，等待重新跑远端矩阵。

## 2026-10-04 19:38 (Asia/Shanghai) - IDE 搜索、重命名与工作区生命周期审查
- 修复 junction 子目录被 Quick Open/全文搜索递归读取、切回已打开工作区不更新启动恢复路径、Shift+F12 结果面板不显示/大小写误匹配，以及 F2 把字符串和注释一起替换的问题。
- F2 现按 Python NAME token 安全改名，覆盖中文标识符、关键字拒绝、f-string 和未完成字符串保护；版本树分支点击详情交互补回归。
- 全量 216 项通过；60 张响应式截图、依赖 smoke、启动器和 `pip check` 通过。原生桌面窗口清单为空，截图为 Qt 离屏渲染；继续做编辑器/任务队列边界审查。

## 2026-10-04 19:16 (Asia/Shanghai) - 搜索兼容修复远端复核
- `6b636ea` 已推送；GitHub Actions `37198044756` 在 Python 3.12/3.13/3.14 全绿。
- 本机 Qt 6.11.2 未能复现原始异常；若用户重启后仍报错，下一步需拿到错误原文和出错的搜索框/客户端版本，避免猜修。

## 2026-10-04 19:13 (Asia/Shanghai) - 搜索输入报错兼容性排查
- 逐键输入 Quick Open 在本机 PySide6 6.11.2 可复现为正常，整项目搜索按回车执行；加入旧 Qt 缺少过滤变更 API 时的行过滤回退及回归测试。
- 全量测试 210 项通过；尚未获得用户运行客户端的具体错误文本/版本，若仍报错需据此确认是否为不同搜索框或旧安装包。

## 2026-10-04 19:07 (Asia/Shanghai) - 总结设置窄窗布局修复
- 截图复现总结面板在 1120/820 宽度下四列表单挤压；按对话栏实际宽度切换纵向两列表单，窄屏压缩标签并隐藏辅助提示。
- 截图 smoke 增加 1500/1120/820 三档总结面板；设置保存、窄屏几何与切回宽屏回归通过。
- 全量 209 项、127 子测试通过；客户端 smoke、启动器预检、依赖检查、QML lint、编译及响应式截图通过。原生桌面仍不可见。

## 2026-10-04 18:50 (Asia/Shanghai) - 聊天输入与终端中文输出修复
- Agent 忙碌时发送会先清空草稿；改为仅在任务成功入队后清空，Ctrl+Enter 回归验证。
- 终端按数据块解码导致 UTF-8 中文乱码；stdout/stderr 独立增量解码，保留旧代码页回退并在结束时刷新尾部字节。
- 本机全量 208 项及 127 子测试通过；依赖 smoke、启动器预检、`pip check`、QML lint 通过。继续审计；当前仍无可见原生桌面会话。

## 2026-10-04 18:35 (Asia/Shanghai) - 总结请求幂等键回归修复
- 周期总结与最终总结原先复用同一幂等键；现按会话、阶段和轮次区分，重试同一请求仍稳定复用该键。
- 回归先复现旧逻辑少发一次总结请求，再验证周期/最终各自重试；全量 205 项通过。
- 离屏 UI 截图矩阵、依赖 smoke、启动器预检、`pip check`、QML lint 通过。原生桌面会话未暴露，继续全面审计其他功能。

## 2026-10-04 18:23 (Asia/Shanghai) - 搜索修复远端复验通过
- `master` 提交 `bdbe774` 的 GitHub Actions #37195090931 在 Python 3.12/3.13/3.14 全绿，含最小 Qt 门禁、205 项测试、依赖 smoke、双 DPI UI 渲染和 QML lint。
- 继续检查真实搜索交互及 Explorer 链接边界；本机原生可见窗口仍不可用，离屏截图不替代实机验收。

## 2026-10-04 18:20 (Asia/Shanghai) - 搜索告警与链接路径修复
- Quick Open 每键过滤改用 Qt 6.9+ 过滤器变更 API；修复项目树暴露 junction、删除误解析到真实目标的风险。
- 本机全量 205/205；依赖 smoke、启动器预检和离屏 UI 截图通过。最小 Qt 门禁被现有 `.venv` 中额外安装的 `PySide6-Addons` 拦截，未卸载。
- 继续核对搜索/Git 边界并推送复验；原生桌面交互仍待可见会话。

## 2026-10-04 18:02 (Asia/Shanghai) - 本地与远端验证通过
- 修复 Quick Open 仅输入行列时误选文件；Windows 短/长路径断言也改为解析后比较。
- 本机全量 202/202；GitHub Actions #37193907703 在 Python 3.12/3.13/3.14 全绿，含依赖 smoke、两档 DPI 截图和 QML lint。
- 继续审查未覆盖的编辑器/Git 边界；原生桌面实机交互仍待可见桌面环境验证。

## 2026-10-04 17:53 (Asia/Shanghai) - Quick Open 与 Windows CI 路径回归
- 修复仅输入 `:行:列` 时误开最短文件的问题；无当前文件则提示先打开文件。补两项 Qt 快捷键回归。
- 本机全量 202/202；远端 CI 揭示内部目录测试受 Windows 8.3 路径别名影响，已改为比较解析后路径，待重跑。
- 继续审查编辑器、Git 与窄窗交互；原生桌面验收仍受当前无可见桌面会话限制。

## 2026-10-04 17:47 (Asia/Shanghai) - 内部目录大小写边界修复
- 已实测 `.RESEARCH` 会漏入项目搜索；资源树、项目搜索、创建/删除统一使用大小写不敏感的内部路径名过滤，删除同时检查所有路径层级。
- 全量测试 200/200、离屏响应式/版本树拖拽持久化 smoke、compileall、QML lint、diff 检查通过；修复待推送并复核三版本 CI。
- 继续审核撤销/重做、文件编辑与 Git 边界；CUA 仍无原生窗口可选，桌面实机验收未完成。

## 2026-10-04 17:39 (Asia/Shanghai) - 查找/替换与快捷键回归
- 通过真实 Qt 按键事件验证 Ctrl+F、Ctrl+H、Ctrl+Shift+F、搜索框焦点保留、整词导航、单项替换和全部替换。
- 本机全量 199/199、离屏响应式/版本树拖拽持久化截图 smoke、compileall、QML lint 和差异检查通过；当前没发现查找/替换功能缺陷。
- 继续审核撤销/重做、键盘编辑路径及文件/Git 边界；桌面自动化仍枚举不到原生窗口，实机截图未验证。

## 2026-10-04 17:26 (Asia/Shanghai) - 三版本 CI 复核通过
- GitHub Actions 在提交 `127020e` 上 Python 3.12/3.13/3.14 全绿，包含全套测试、依赖 smoke、IDE/版本树截图矩阵和 QML lint。
- 本机 197/197、Git 面板工作流及路径别名定向测试通过；继续审核查找替换和其他编辑器边界。原生桌面窗口仍不可见，未声称完成实机验收。

## 2026-10-04 17:22 (Asia/Shanghai) - Git 面板与跨路径回归
- Git 面板端到端验证初始化、选中暂存、取消暂存、暂存全部、提交和干净状态；搜索结果路径断言改用规范化路径，适配 Windows 短/长目录别名。
- 全量测试 197/197 通过，最新搜索/Git 定向回归 2/2、静态检查通过；代码和记录待提交推送并重新核验 CI。
- 后续继续查找/替换、版本树交互与其他 Git 边界；原生窗口实机交互仍待可见桌面验证。

## 2026-10-04 17:16 (Asia/Shanghai) - 标签与项目搜索回归
- 修复已固定标签被预览点击降级、预览复用造成标签替换；工作区搜索先检查大小并限制读取，避免整块载入检查点等大文件。
- 全量测试 196/196、QML lint、compileall、diff 检查和响应式/版本树离屏截图 smoke 通过。最新代码待推送并复核 CI。
- 下一步继续核验查找替换、Git 操作及其他编辑器边界；原生桌面交互仍待可见窗口环境验证。

## 2026-10-04 17:06 (Asia/Shanghai) - Windows CI 路径回归
- 修复 Windows 8.3 短路径别名导致重命名时报错；diff 路径回归改为比较规范化路径。
- 全量测试 194/194、QML lint、compileall、diff 检查和完整离屏响应式/版本树截图 smoke 均通过。
- 继续审核预览/固定标签、查找替换、工作区搜索与 Git 操作；原生桌面交互仍待可见窗口环境验证。

## 2026-10-04 16:55（Asia/Shanghai）· 编辑器与资源管理器功能回归

- 已修复保存时 EOL 被统一成 LF、资源创建模式切换失效、内部目录嵌套路径漏检、同名文件 diff 标签误复用四项问题；全量测试 193/193、截图布局回归、编译与 QML lint 通过。
- 继续审查预览/固定标签、查找替换、工作区搜索与 Git 操作；原生桌面控制当前无可枚举窗口，继续区分离屏证据与实机证据。

## 2026-10-04 16:25（Asia/Shanghai）· 持续 UI 精修与桌面验收

- 当前方向：继续按 VS Code 风格检查布局、交互和外观；优先复核真实桌面窗口与常见操作。CUA 当前没有可见原生窗口，离屏截图不能替代实机验收。
- 本轮已完成的非阻塞通知改动已转记至《历史与结果》；下一步在可见桌面环境复核并继续修复发现项。

## 2026-10-04 16:05（Asia/Shanghai）· 主题化键盘焦点提示

- 已为按钮、活动栏、标签页和资源树增加跟随主题强调色的键盘焦点环；离屏截图实际采样三主题焦点像素，验证 100%/150% 均正确。全量单测 186/186、QML lint 与编译检查通过。
- 继续目标：做完整桌面实机观感/交互验收并按主题反馈精修；CUA 仍无原生窗口，当前截图仅为离屏证据。

## 2026-10-04 15:53（Asia/Shanghai）· 活动栏矢量图标与主题状态

- 已将资源、搜索、版本树导航改为 2× 高 DPI 矢量图标，支持普通/悬停/选中及三主题配色，保留无障碍名称；发布暂存已纳入图标模块。
- 验证：全量单测 185/185；三主题 100%/150% 响应式截图、QML lint 与发布暂存通过。下一步继续打磨其余控件；CUA 仍未提供原生窗口，需桌面观感复核。

## 2026-10-04 15:37（Asia/Shanghai）· 窄窗资源栏响应式优化

- 已完成：窗口宽度低于 900px 时自动收起资源树、保留活动栏；点击资源管理器图标可展开/收起，宽屏默认三栏。下一步待原生窗口可见后继续按用户实际观感精修 Studio / Paper / Focus。
- 本轮验证：完整单测 184/184；三主题 100%/150% 离屏截图覆盖 900/899px 断点、780px 窄窗及版本树交互；`compileall`、QML lint、`git diff --check` 通过。CUA 未提供原生窗口，离屏结果不视作桌面实机验收。

## 2026-10-04 15:20（Asia/Shanghai）· 字体回退与代码等宽显示修复

- 修复字体解析：应用默认改为已安装的 Segoe UI；代码区此前被通用 QSS 覆盖成 Segoe UI 11pt 比例字体，现明确使用 Consolas 12pt 等宽字体；版本树小字也更清晰。
- Studio / Paper / Focus 三套方案在 1920×1080、1366×768、1280×720、1120/1119 断点、1024×768、780×480，100%/150% 缩放下通过离屏布局和截图回归；版本树拖动、缩放、窄窗滚动通过。
- 全量 `unittest` 183/183、`compileall`、QML lint 通过。CUA 没有可操作原生窗口，仍待原生实机验收及用户比较主题后继续精修。
