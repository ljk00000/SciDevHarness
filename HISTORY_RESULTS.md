# 历史与结果

## 2026-10-02 14:13（Asia/Shanghai）· 工具调用诊断与有限修复闭环

- 文本工具解析失败现在记录不含参数内容的字段路径/原因，便于模型按反馈修正；SVG 预检后只开放当前阶段允许的工具，读后重复空转有界停止，整图文本恢复不会绕过局部编辑限制。
- `unittest` 165/165、`compileall`、`git diff --check` 通过。固定提示 `Generate an SVG of a pelican riding a bicycle` 用本机 Qwen2.5-Coder-7B、最多 10 回合/3 轮修复后仍未通过视觉语义校验；失败预览/诊断保存在 `%LOCALAPPDATA%\Temp\SciDevHarness-pelican-turn-budget-20261002`，没有重复下载模型。
- 本地模型仍不足以稳定产出合格图形；当前 Harness 会保留失败证据并拒绝把结构预检冒充视觉成功。独立许可证审计仍为 `manual-review-required`。
- 提交 `13927a1` 已推送至 `master`；[Windows CI](https://github.com/ljk00000/SciDevHarness/actions/runs/36972322392) 在 Python 3.12/3.13/3.14 全通过（测试、依赖、常规/125% DPI 布局及 QML lint）。

## 2026-10-02 13:08（Asia/Shanghai）· 工具安全与响应式细节打磨

- 文本工具调用现在递归校验嵌套参数 schema；定向 SVG 修复若读文件但未编辑，只允许一次补救，仍无改动就失败且不自动提交。回归测试覆盖有效编辑与无改动失败。
- Qt 离屏交互 smoke 覆盖 1500×920 至 780×480；窄屏版本树自动紧凑适配，最小宽度缩放约 97%，节点卡片仍可读，拖拽/平移/缩放/持久化通过。
- 全量 `unittest` 162/162 通过。固定提示 `Generate an SVG of a pelican riding a bicycle` 用本机 Qwen2.5-Coder-7B 复测仍失败：模型生成的图形结构差，修复时复述工具响应而没有编辑；Harness 保留文件、明确失败、不提交。未下载模型。
- 本地 smoke 诊断新增安全的工具名/拒绝原因字段，不保存工具参数或凭据。Windows CI [三版本全部通过](https://github.com/ljk00000/SciDevHarness/actions/runs/36967814078)，含 125% DPI 截图与 QML lint。独立打包许可证状态仍需人工复核。

## 2026-10-02 12:39（Asia/Shanghai）· v5 并入唯一公开主仓库

- `SciDevHarness` 主仓库 `master` 快进至 `40ca5e6bdddb77133513c6863035fc0dad424fc5`，保留 `release/v5`（`3ba6861e5bd542866e1d37fd47816e95880daa95`）。v5 全历史保留。
- 将 `SciDevHarness-Research-IDE[-v2/-v3/-v4/-v5]`、`SciDevHarness-{Client,Public,Open,IDE,Desktop}` 共 10 个重复仓库设为私有，未删除仓库/分支；该项目系列现在仅主仓库公开。主线与 v5 分支 [Windows CI 均通过](https://github.com/ljk00000/SciDevHarness/actions/runs/36965218558)（[v5 CI](https://github.com/ljk00000/SciDevHarness/actions/runs/36965167675)）。
- 工具文本解析现在把不完整/不匹配 schema 的已声明调用安全拒绝并有限重试；不会执行或把无效 JSON 当作完成。全量 `unittest` 160/160 通过；独立打包许可审计仍标记 `manual-review-required`。
- 固定提示 `Generate an SVG of a pelican riding a bicycle` 的 Qwen2.5-Coder-7B 本地 smoke 用 14 次请求、3 轮修复后仍未过视觉检查（实心轮、车架/后轮未连接、身体/头部标识和腿/嘴袋关系不合格）。已保存预览与诊断；没有把模型限制误记为 Harness 成功。
- Qt 离屏 UI 烟测覆盖 1500×920 至 780×480、文件编辑、版本树拖拽/平移/缩放和持久化；检查现有截图布局通过。原生桌面窗口不可枚举，本次不宣称实机截图验收。

## 2026-10-02 11:34（Asia/Shanghai）· 新建并公开 v5 仓库

- 创建公开仓库 [ljk00000/SciDevHarness-Research-IDE-v5](https://github.com/ljk00000/SciDevHarness-Research-IDE-v5)。核心源码提交 `a7370793d899ecbf4e4fadeeaaa88f751e6979ae` 已推送；`main` 远端 SHA 与本地源码提交一致，仓库可见性为 `PUBLIC`。
- 修复 Qwen 本地模型复制 `read_file` 行号后输出 `<tool_response>` 的工具调用解析；编辑失败时明确提示去除显示行号。相同 SVG 结构问题与文件连续不变时提前停止，避免空转。全量 `unittest` 158/158 通过。
- 固定提示 `Generate an SVG of a pelican riding a bicycle` 的最新 7B 实测仍未通过视觉评估：缺两条腿，另有 7 项几何/接触关系问题；没有将其记作成功。
- 发布使用已跟踪项目文件；`.venv`、`.research` 数据库/会话及本地模型权重未上传。当前工作区常见凭据格式扫描无命中，gitleaks 未安装，未声称完成完整历史密钥审计。GitHub API 直连超时后才经代理串行完成建仓与查询；源码推送使用 SSH 直连。

## 2026-10-02 10:41（Asia/Shanghai）· 新建并公开 v4 仓库

- 公开仓库 [ljk00000/SciDevHarness-Research-IDE-v4](https://github.com/ljk00000/SciDevHarness-Research-IDE-v4) 已创建。源码提交 `ef4246ad440146f4f8dd9d39e5d5a8a2a297f77a` 的本地与 `main` SHA 一致；可见性为 `PUBLIC`。此前 v3 未覆盖。
- 修复 SVG 结构预检后的稳定工具调度，并要求读取目标后再局部编辑；全量 `unittest` 156/156 通过。离屏 Qt 截图烟测 1500×920 至 780×480 通过，含版本树拖动/平移/滚动/缩放。
- 发布只包含已跟踪源码和文档；`.venv`、`.research` 与本地权重未上传。常见凭据格式扫描未命中；未安装 gitleaks，不代表完整历史审计。GitHub Windows CI 对源码提交仍在运行。
- GitHub HTTPS 直连超时后，按规则逐个、串行使用已配置代理完成建仓/核验；代码推送走 SSH 直连并设置 10 秒连接超时。

## 2026-10-02 09:32（Asia/Shanghai）· 新建公开仓库并上传当前版本

- 创建公开仓库 [ljk00000/SciDevHarness-Research-IDE-v3](https://github.com/ljk00000/SciDevHarness-Research-IDE-v3)，默认分支 `main`。提交 `78d2c5e8f2f9ddeba2a4f61600f0d68b4bea05bf` 的本地与远端 SHA 一致；没有覆盖已有仓库。
- 上传 SVG 结构校验、修复预算、提示和对应回归测试。全量 `unittest` 151/151 通过；`git diff --cached --check` 通过。
- 发布前工作区凭据格式扫描仅发现 README 的 `your-key` 占位示例；未安装 gitleaks，未声称完成完整历史扫描。独立打包二进制仍需单独审查第三方许可证；本次仅发布源码。

## 2026-10-02 09:22（Asia/Shanghai）· SVG 语义/骑行结构与修复预算

- SVG 几何校验现在覆盖躯干与车座接触、车架座管节点、踏板组包含两个不同圆，以及两条腿分别连接身体和不同踏板；检查也会在其他部件缺失时执行。修正测试插画为双踏板、双腿到不同踏板，新增对应回归。
- 通用系统提示（884 字符）及无文件后的单次恢复提示要求主体和每个关键部件分别对应可见几何元素及唯一 `id`，不能只给共享分组命名；未引入 pelican/bicycle 专项规则。
- 全量 `unittest` 151/151、`py_compile`、`git diff --check` 通过。Qwen3.5 4B、`max_tokens=12000`，原句四次首轮都未通过：两次 16K context、两次默认 4K；第五次最多一轮结构修复仍失败。该会话在修复阶段发出 17 个请求；全程 20 请求响应 903,941 bytes、耗时合计 368.4 秒，仍缺踏板/辐条/头部/嘴袋/伸展翅膀等结构。预览已目视复核，骑乘关系不合格。
- 新增 SVG 修复会话 8 回合上限，一般编码仍最多 32 回合；到限额后显式失败、不完成/不自动提交，已有工作区改动保留。新增无进展循环回归测试通过。临时 Ollama 别名及 Modelfile 已清理，原模型仍在本地；当时尚未提交的改动随后发布，见 09:32 记录。

## 2026-10-02 07:54（Asia/Shanghai）· 工具安全、SVG 修复与 UI 回归

- 定向 SVG 修复时隐藏并拒绝 `replace_all`；`write_file`/精确编辑先校验 SVG XML 与根元素，格式错误或 DTD/entity 不落盘。补充无编辑后的有限提示与缺部件时的几何预检。
- 全量 `unittest` 145/145、`py_compile`、`git diff --check` 通过。离屏 UI 截图与交互回归通过：1500×920 至 780×480，含版本树拖动/平移/滚动/缩放及关闭提示。
- Qwen2.5-Coder-7B 与 Qwen3.5:4b 固定提示结果仍未过验证；后者已生成较完整构图，但若干部件缺失/元素类型不符。未下载模型。
- 原生桌面窗口枚举出两个同标题实例，无法安全确定截图目标；先前截图接口报 `SetIsBorderRequired failed: 不支持此接口 (0x80004002)`，因此不把离屏图说成实机截图。

## 2026-10-02 07:03（Asia/Shanghai）· 创建并验证新公开仓库

- 新仓库 [ljk00000/SciDevHarness-Research-IDE-v2](https://github.com/ljk00000/SciDevHarness-Research-IDE-v2) 已公开，默认分支 `main`。提交 `628af9cfa1b5857235aafbe719b35faec47ddc70` 的 `git ls-remote` SHA 与本地一致；已有仓库未覆盖。
- `unittest` 138/138 通过；Windows CI 仍在排队。凭据常见格式扫描未命中；`gitleaks` 未安装，故不宣称完整凭据审计。
- 上传时按项目规则绕开预设代理、直连 GitHub HTTPS 成功；仅移除单次命令进程代理变量。

## 2026-10-02 06:59（Asia/Shanghai）· 工具调用与 SVG 回归修正

- 精简通用系统提示；工具单一为 `write_file` 时要求工具调用。修复 SVG 工具结果误判、局部编辑提示字段和对比度校验，并限制自动修复轮数。
- 全量 `unittest` 138/138 通过，`git diff --check` 通过。固定 Qwen2.5-Coder-7B SVG smoke 仍产生未通过验证的结果；此项保持失败记录。
- 常见凭据格式扫描未命中；`gitleaks` 未安装，扫描不等同完整凭据审计。

## 2026-10-02 06:01（Asia/Shanghai）· 创建并发布独立公开仓库

- 新建公开仓库 [ljk00000/SciDevHarness-Research-IDE](https://github.com/ljk00000/SciDevHarness-Research-IDE)，默认分支 `main`。提交 `9c91d52e46620691f51f26cd2f17ee24b20ca52b` 已上传，`git ls-remote` 与本地 SHA 一致；原有六个公开仓库未覆盖。
- 全量 `unittest` 134/134 通过，`git diff --check` 通过。常见凭据格式历史扫描无命中；`gitleaks` 未安装，未声称完成完整凭据审计。
- SSH 22 端口连接被 GitHub 重置；仅将新仓库 remote 改为 HTTPS，通过直连 443 与现有 `gh` 登录凭据推送，未启用代理、未更改全局 Git 配置。新仓库 Windows CI 已排队。

## 2026-10-02 05:40（Asia/Shanghai）· 批量文件编辑与固定 SVG 回归

- `replace_in_file` 支持最多 50 项精确批量替换，先校验整批再单次写入；测试覆盖 CRLF 保留、失败不落盘和旧参数兼容。模型实测一次调用成功应用 11 项标签，减少拆分请求。
- 修复标签修复提示未进入局部修复工具权限的问题；安全恢复 SVG 后直接进入本地 diff/自动提交闭环，防止模型重复生成同一文件。系统提示改为文件创建只写一次、不重复输出完整产物。
- 全量 `unittest` 134/134 通过。Qwen3.5 本地 smoke 后续 SSE 缺完成标记；Qwen2.5-Coder-7B 本地 smoke 两次修复后仍产生无效 XML。两者均未通过最终 SVG 验证；没有下载模型，所有调用走本机 Ollama。
- 本轮改动尚未提交或推送；UI 未改，原生桌面截图接口仍不可用。

## 2026-10-02 04:33（Asia/Shanghai）· 固定 SVG 提示迭代与 UI 复核

- 固定用户原句、Qwen2.5-Coder-7B `num_ctx=32768` 和 `max_tokens=12000` 未变；五次本地模型迭代均未达到最终验证。较好的一次 12 请求、约 1.46 MB 回包、81.2 秒模型耗时，曾生成多数已标记部件，但轮胎仍填充且若干部件裁切；最新一次 16 请求、约 1.38 MB、78.7 秒仍失败。所有调用只访问本机 Ollama。
- 改进通用编辑指令，并区分完整重建和局部 SVG 修复工具集；局部修复隐藏 `write_file`，SVG 文本适配器不得绕过该范围。回归单测通过；模型仍会输出错误几何或重写草图，未声称质量达标。
- `smoke_screenshot.py` 新截图覆盖 1500×920、1180×760、940×620、820×600、780×480，并通过标题栏/工作区/树布局、拖动、缩放、滚动和未保存关闭断言。目视检查宽屏、紧凑编辑器、最小版本树可读且无明确 UI 缺陷；Computer Use 返回 `apps=[]`，真实桌面截图未能采集。
- 下一轮改用已缓存的 Qwen3.5:4b 对照固定原句；若结果仍不佳，进一步拆解模型能力、SVG 修复反馈和文件工具使用问题，系统提示继续保持通用。

## 2026-10-02 03:59（Asia/Shanghai）· 新建公开客户端仓库并推送

- 新建 [ljk00000/SciDevHarness-Client](https://github.com/ljk00000/SciDevHarness-Client)，确认为公开仓库、默认分支 `main`。代码提交 `0c7b56c73e47d497b1e3a1bfbd33b090b65255f2` 已推送，`git ls-remote` 与本地 SHA 一致；既有远端未改动。
- 完整 `unittest` 129 项通过；Apache-2.0。凭据特征扫描无命中，未发现超过 50 MB 的 Git 对象，`.venv`、模型权重及研究数据未上传。
- GitHub [Windows CI 首次运行成功](https://github.com/ljk00000/SciDevHarness-Client/actions/runs/36918193600)。GitHub API 直连短超时失败后才通过已配置代理创建仓库；代码推送使用 SSH 直连成功。

## 2026-10-02 03:43（Asia/Shanghai）· 工具范围缩减与提示结构对照

- 固定提示词原文未变。Qwen2.5-Coder-7B 首次生成只开放 `write_file` 后，本次请求 14 次、累计 72.8 秒、响应体约 1.18 MB；此前一次为 17 次、284.9 秒、5.32 MB。单次对照显示开销明显下降，但最终预览仍不能识别为鹈鹕骑车，仍缺全部自行车关键 ID。
- 所有 `tool_argument_bytes` 仍为 0：模型没有发出原生函数参数，文件依靠 Harness 从文本 SVG 安全恢复。已更新通用提示，明确先规划对象接触/连接关系、再添加细节；待同模型固定提示复测。

## 2026-10-02 03:29（Asia/Shanghai）· 固定提示词模型对照与 Qt 渲染故障

- 用户提示词保持精确为 `Generate an SVG of a pelican riding a bicycle`；缓存的 Qwen3.5 4.7B 未下载，Harness 请求 `max_tokens=12000`，但 Ollama 实际上下文为 4096。初稿仍未形成正确车架/骑乘关系。
- 对照进程曾以 `0xC0000409` 崩溃，Windows 事件确认故障模块为 `Qt6Core.dll`。原因是 headless 验证器未创建 `QGuiApplication`；现已修复，复测真实失败产物返回可读结构错误，含 `<text>/<tspan>` 的隔离子进程回归测试通过。
- 纯 SVG 新建时工具集缩减为 `write_file`；修复任务仍能读取、精确编辑和检查 diff。定向回归测试通过，待固定提示词重跑和全套测试确认整体效果。

## 2026-10-02 03:02（Asia/Shanghai）· 创建并公开发布新仓库

- 新建 [ljk00000/SciDevHarness-Public](https://github.com/ljk00000/SciDevHarness-Public)，公开可见，默认分支 `main`；推送提交 `527410fc8a050802703d15e3839efa064ccfc168`，远端 SHA 核验一致。
- 128 项 `unittest` 通过；当前工作树与提交历史的凭据形态扫描无命中。`.venv`、`.research`、模型权重未上传，其他 Git 远程保持不变。
- GitHub 直连超时后通过受限本地代理隧道复用现有 SSH 身份；未扩大 OAuth 授权范围。

## 2026-10-02 02:46（Asia/Shanghai）· 修复轮分类、SVG 质量复测与响应式 UI

- Harness 现在区分“创建 SVG”和“修复已有 SVG”：修复提示不再带重复的创建指令；如模型返回完整 SVG，只能回写明确指定且已存在的文件。通用系统提示要求编辑时保留未涉及内容。
- SVG 检查支持语义 ID 位于 `<g>`、分组继承与简单 CSS class；可一次反馈多项几何问题，并按部件生成局部修复指引。
- 原始输入严格为 `Generate an SVG of a pelican riding a bicycle`。本机 Qwen2.5-Coder-7B 本轮仍失败：17 次 loopback 请求，约 284.9 秒，183 KB 请求体/5.32 MB 响应体；两轮修复后最终 SVG 缺少全部 17 个语义 ID，渲染质量不合格。此为本机回环流量，不是外网传输；未下载模型。
- 新鲜离屏截图覆盖 1500×920、1180×760、940×620、820×600、780×480，编辑器/版本树 smoke 均通过；原生窗口未发现可控应用，故真实桌面交互仍未验收。
- `python -m pytest -q`：128 项、86 子测试通过；`compileall` 与 `git diff --check` 通过。修改尚未提交。

## 2026-10-02 00:56（Asia/Shanghai）· 本地模型 SVG 烟测与请求计量

- 流式计时现覆盖完整响应体，并分开统计首个可见输出、正文/推理/工具参数字节及请求/响应体积；失败诊断不保存请求正文或 shell 命令。SVG 写入/替换识别与修复路径有回归测试。
- Qwen2.5-Coder-7B 最新真实运行 15 次本地请求，请求累计耗时约 72.9 秒；请求体合计约 164 KiB，响应体约 1.28 MiB（SSE 正文约 14 KiB）。生成图因重复语义 ID 被拒绝，预览也未达可用质量；不等同于 Harness 冒报成功，也不代表远端网络流量。
- 全套 `python -m pytest -q`：115 项、86 子测试通过；`git diff --check` 通过。未提交、未推送。
- 原生窗口验收未完成：计算机使用工具返回 `apps=[]`，没有可截图的桌面应用；不以离屏图替代。

## 2026-10-01 23:51（Asia/Shanghai）· 新建公开仓库并上传

- 新建公开仓库 [ljk00000/SciDevHarness-IDE](https://github.com/ljk00000/SciDevHarness-IDE)，推送完整 Git 历史到 `master`；本地与远端 SHA 均为 `3c4cfe09fc2be9d17023ac9c0bb9d66066e6c70a`。新增 `ide-public` 远程，旧 `origin` 与 `desktop-public` 未改动。
- 107 项单测通过；常见凭据格式扫描无命中，历史中无超过 20 MiB 的文件对象；虚拟环境、模型权重与 `.research` 数据未提交。仓库沿用 Apache-2.0。代码提交 `3c4cfe0` 的 [Windows CI](https://github.com/ljk00000/SciDevHarness-IDE/actions/runs/36887597180) 在 Python 3.12/3.13/3.14 全部通过（测试、依赖 smoke、普通/125% DPI 截图、QML lint）。

## 2026-10-01 23:30（Asia/Shanghai）· 低分辨率工作台响应式适配

- 把三栏工作台最小尺寸从 940×620 降到 780×480，并加入紧凑宽度阈值；Explorer、编辑器、Agent 始终可见。窄屏总结 chip 使用短文案，同时保留完整 tooltip/accessibility 状态；Explorer 版本树入口防止副标题裁切。
- Git 页面低高度时收起非关键统计卡片，给树和详情区留空间；QML 画布依据实际尺寸隐藏第二行说明并缩短缩放提示，修复节点卡与说明重叠。
- 截图 smoke 覆盖宽、中、窄、最小窗口及宽屏短高度，在 100%/125% DPI 下通过；分支节点完整可见，标题栏、chip、入口文案无裁切。另有 107 项测试、编译、QML lint、diff 检查通过。
- Computer-use 未返回可见桌面应用；本轮使用 Qt 离屏真实控件截图，未声称完成原生桌面验收。改动当时留在本地，随后于 23:51 随提交 `3c4cfe0` 上传至 `SciDevHarness-IDE`。

## 2026-10-01 23:01（Asia/Shanghai）· 新建并验证公开桌面客户端仓库

- 创建公开仓库 [ljk00000/SciDevHarness-Desktop](https://github.com/ljk00000/SciDevHarness-Desktop)，推送 `master`；功能提交 `aeb342d1b17b091d0a32f8f532efc60c38ba4d3d` 与远端哈希一致。原有公开仓库保留为 `origin`，新仓库使用 `desktop-public` 远端。
- 107 项本地测试通过；GitHub Windows CI 在 Python 3.12、3.13、3.14 均通过测试、编译、依赖检查/smoke、普通与 125% DPI UI 截图及 QML lint。
- 发布前检查未发现已跟踪的大型模型文件或常见密钥格式；新仓库与旧项目仓库互不覆盖。

## 2026-10-01 22:41（Asia/Shanghai）· 收紧鹈鹕触把的实际几何门槛

- 人工看图发现前伸翼与车把仍有可见小间隙；把触点验收从包围框重叠改为第一段三次曲线端点落在把手轮廓附近（2 SVG 单位容差），并要求闭合填充曲线。复合 path 辐条按子路径而非元素数计数。
- 同一原始提示再次通过本机 Qwen2.5-Coder-7B + Harness：71.1 秒，8 次 loopback 请求，每次 `max_tokens=12000`；2 次 `write_file`，无文本恢复，安全/结构验收和 Qt 1200×800 渲染通过。人工复看确认翼尖接到车把；风格仍是简洁矢量图。
- 临时 Git commit `8879ed8bea707fec3c97996b1fd7884c7a749a96`、事件账本与摘要齐全，临时工作区干净。最终完整单测 107 项、编译、依赖 smoke、`pip check`、QML lint、`git diff --check` 全通过；项目修改未提交。

## 2026-10-01 22:34（Asia/Shanghai）· SVG 工具恢复与质量验收闭环

- 一次真实 Qwen 运行因重复生成大量 SVG 路径未产出文件；发现补救轮次会把整段失败回答带入新上下文，现改为压缩失败轮次文本，并保留一次有界自动恢复。多项错误一并反馈，避免修一个后才发现另一个。
- 视觉检查新增车轮/车架/前叉/辐条、鞍座与踏板连接、真实曲线触把等约束；修正复合 SVG path 的辐条计数，避免将合法的一条多子路径误判为缺失。结构和渲染单测覆盖这些回归。
- 精确原始提示本机闭环通过：Qwen2.5-Coder-7B，9 次 loopback 调用、每次 12000 tokens，耗时 247.9 秒；SVG 安全解析和语义检查通过，Qt 1200×800 实际渲染，事件账本/Git 自动提交/会话总结齐备，临时工作区干净。视觉检查为可辨认的简洁矢量图，尚非精修作品。
- 完整 107 项单测、编译、依赖 smoke、`pip check`、QML lint、`git diff --check` 通过。生成物和测试提交仅在临时目录，不进入项目工作区；项目修改未提交。

## 2026-10-01 21:54（Asia/Shanghai）· 流式 LLM、工具恢复与 SVG 实测

- OpenAI-compatible 调用改用可配置 SSE 流式响应，支持分片 tool-call 重组和 UI 实时气泡；本机 Qwen 启动器使用进程级 240 秒超时默认值。
- 修复 fenced XML 中 JSON `write_file` 未转成工具调用的问题；无 SVG 输出时最多补救一次，仍无文件则任务失败。新增安全解析、队列/UI、诊断及质量回归。
- SVG 渲染验收现检查 fill/stroke 色值、部件几何比例、头身连接、翅膀位置、长喙和闭合喉囊。104 项单测、依赖 smoke、`pip check`、QML lint、常规与 125% DPI 响应式截图通过。
- 精确 Qwen 任务能写入并渲染，但最近一次运行仍未通过视觉质量门槛；预览中鸟身比例错误、喙太短。随后加入精确路径提示，尚待新一轮模型实测。

## 2026-10-01 20:04（Asia/Shanghai）· 项目文档归并

- 将 `SUMMARY_AND_TREE.md` 的对话总结、2.5D 版本树和 IDE 工作流说明并入 `SCIDEV_HARNESS.md`，保留功能细节；Markdown 文件总数从 6 降至 5。

## 2026-10-01 19:54（Asia/Shanghai）· SVG 安全恢复与生成质量验收

- 收紧 SVG 修复目标选择：只在用户明确要求修复已有 `.svg` 时原位覆盖；普通创建仍避免覆盖已有文件。提示词增加可见眼睛、对比度等要求。
- 增强本机烟测的语义结构检查与定向修复，并新增重复 ID、短鸟喙等回归测试。完整单测 80 项通过；编译、`pip check`、依赖 smoke、QML lint 通过。
- 本机 Qwen 实测虽然通过 Harness 工具写出 SVG 并成功渲染，但画面仍难辨认且缺少可见眼睛，验收脚本正确判失败；没有把可渲染误报成生成质量合格。
- 提交 `d4840dd` 已推送至公开仓库 `ljk00000/SciDevHarness`；远端 `master` SHA 一致，仓库确认公开、工作区干净。GitHub Actions Python 3.12/3.13/3.14 全部通过：[CI #36858516069](https://github.com/ljk00000/SciDevHarness/actions/runs/36858516069)。

## 2026-10-01 19:37（Asia/Shanghai）· GitHub CI 路径兼容修复通过

- `ab23f79` 的远端 CI 在 Python 3.12/3.13/3.14 均通过单测、编译和运行时预检，但 `smoke_dependency.py` 把 `EventLedger` 解析后的路径与 Windows 临时目录未解析路径直接比较，因路径别名失败。
- 修正为两侧都解析后比较；本地 dependency smoke、78 项单测、`py_compile` 与 `git diff --check` 通过。提交 `6461244` 已推送，GitHub Actions 的 3.12/3.13/3.14 矩阵全绿：[Windows CI #36856282996](https://github.com/ljk00000/SciDevHarness/actions/runs/36856282996)。

## 2026-10-01 19:28（Asia/Shanghai）· 发布 CI 修复与 SVG 实测

- 修复 Windows CI/打包环境未把虚拟环境脚本目录加入 `PATH` 的问题，并补上回归测试及手动打包说明。
- 对明确的 SVG 创建请求，新增安全恢复：模型若只返回单个 fenced SVG/XML 代码块，且 XML 无脚本、事件处理器、外部资源等风险，Harness 将其转为受路径保护的 `write_file` 工具调用；同名文件不覆盖，事件仍进入现有 Git/账本链路。
- 新增 SVG 安全、渲染及结构检查。78 项单测、Python 编译、`pip check`、依赖 smoke、启动器预检、QML lint 和 1500×920/940×620 UI 截图通过。原始 `Generate an SVG of a pelican riding a bicycle` 本机 Qwen 实测在一次修复后仍未通过图形结构检查，故结果记为失败，不能据此声称生成质量合格。

## 2026-10-01 18:25（Asia/Shanghai）· GitHub 公开上传完成

- 创建公开仓库 [ljk00000/SciDevHarness](https://github.com/ljk00000/SciDevHarness)，推送 `master`；GitHub API 确认公开，`git ls-remote` 验证远端 SHA 与本地一致。仓库包含 Apache-2.0 LICENSE。
- 上传前审查提交树：无 >10 MiB 文件、模型权重/敏感文件名及常见 GitHub/AWS/私钥/API key 格式命中。单测 69 项连续 3 轮、Qwen 编码烟测与客户端依赖 smoke 通过；远端 Actions 是否通过尚未检查。

## 2026-10-01 14:01（Asia/Shanghai）· 本地提交与 GitHub 目标确认

- 当前工作区已提交：`c7c7ada`，包含 31 个项目文件；提交后 Git 工作区干净。提交前暂存检查通过，未发现大文件、模型权重或常见密钥格式。
- GitHub CLI 登录有效，但当前仓库无远端，认证账号下未找到同名 SciDevHarness 仓库；未擅自创建新远端。上传需用户提供目标仓库 URL，或确认在当前账号创建仓库并指定公开/私有。

## 2026-10-01 12:30（Asia/Shanghai）· Qwen 工具往返与 Windows 行尾兼容

- 本机模型实测中先后暴露三处问题：Ollama 模板会在助手消息同时含正文和工具调用时丢掉调用；Qwen 偶尔使用 `<tool_request>` 标签；Windows CRLF 文件无法匹配模型根据行文本给出的 LF 片段。现已修复消息序列化、增加仅接受已声明工具和合法参数的标签兼容，并让精确替换适配/保留源文件换行。工具说明也明确了行号标记和已授权文件编辑的执行方式。
- 修复后一次完整隔离运行通过：Qwen 实际写入并运行固定安全脚本，4 个代码任务共 18 个用例全过；既有 bug 修复真实调用 `read_file` 与 `replace_in_file`。所有模型请求仅发往回环 Ollama，`max_tokens=12000`；逐项验证 diff、自动 Git commit、对话总结及临时仓库干净。前面几次烟测曾失败，均保留为诊断事实。
- 新增消息往返、`<tool_request>` 和 CRLF 回归。69 项单测连续 3 轮；依赖/UI smoke、运行时预检、`pip check`、QML lint、Python 编译、`git diff --check` 通过。用户选择 Apache-2.0；`LICENSE` 文件已存在。无提交；远端 CI、MSVC 构建、Qt 第三方许可证人工审核和原生桌面验收待完成。

## 2026-10-01 12:07（Asia/Shanghai）· Qwen 多题验证与未保存修改保护

- 本机 Qwen 经 Harness 完成奇偶判断、正数过滤求和、区间夹取；每轮 13/13 边界样例通过，连续 3 次完整通过。首次运行曾未生成第二题目标文件并被烟测判失败；未把它藏进重试。每次成功运行包含 16 个本地请求，逐题使用 `write_file`、提交前 diff、单文件自动 commit、自动总结；AST runner 拒绝导入/调用/属性/顶层代码，并限制循环只遍历输入。
- 应用关闭时新增单个“保存全部/放弃修改/继续编辑”确认，覆盖多编辑器组；保存失败或取消不丢内容，干净窗口不弹窗。65/65 单测连续 3 轮；依赖/UI smoke 和常规/125% DPI 截图（包括确认框）通过。远端 CI、MSVC standalone、Qt 打包许可人工审查及原生桌面验收未完成；未提交。

## 2026-10-01 11:35（Asia/Shanghai）· Agent 失败/重试后的编辑器同步

- Agent 可能先写入文件、随后遇到网络重试或最终失败；此前 GUI 只在成功完成后刷新打开的文件，导致编辑器与磁盘分叉。现在重试/失败事件刷新未修改的编辑器，同时明确保留用户未保存文本。
- 新增 Qt ClientWindow 回归测试，模拟磁盘上的 Agent 部分修改并验证 retry/failure 两条路径；58/58 单测连续 3 轮，真实客户端依赖 smoke 通过。未提交。

## 2026-10-01 11:07（Asia/Shanghai）· 本机模型输出执行保护测试

- 将 Qwen 烟测的生成文件检查和执行抽为 `_execute_verified_sample`。新增单测证明固定样例能运行并返回预期输出；若模型文件内容有任何偏差则拒绝执行，且不会启动子进程。
- 57/57 单测连续 3 轮；本机 Qwen 再次实测 4 次请求均 loopback、`max_tokens=12000`，生成文件安全运行，提交仅含目标文件，最终总结产生且临时仓库干净。编译、依赖、QML lint、启动器和 diff 检查通过。未提交。

## 2026-10-01 10:48（Asia/Shanghai）· 当前 UI 双 DPI 实图复核

- 在显式保留的临时目录生成 100% 与 125% DPI 截图，并目视检查宽屏编辑器、窄屏编辑器及窄屏版本树；布局无重叠/裁切，版本树分支与详情可读，拖拽、平移、缩放和自适应断言通过。原生桌面截图通道仍不可用。
- 发布候选文件清单只读检查未发现大于 10 MB 的非忽略文件，也未命中扫描的常见密钥格式/个人 Windows 路径；此为有限模式扫描，不代表完整安全审计。项目仍未提交。

## 2026-10-01 10:44（Asia/Shanghai）· Qwen 生成代码运行复验与许可证确认

- 本机 Qwen 回环烟测通过：模型调用 `write_file` 创建 `hello_qwen.py`，脚本先严格比对固定安全内容，再运行并得到预期输出；提交前 diff、仅目标文件的自动 commit、总结和干净临时仓库均通过。4 次请求 `max_tokens=12000`，未下载模型。
- 55/55 单测连续 3 轮；依赖 smoke、`pip check`、编译、QML lint、启动器检查与 `git diff --check` 通过。项目源码采用 Apache-2.0；独立版 Qt 第三方组件审查仍为人工待办。未提交。

## 2026-10-01 10:36（Asia/Shanghai）· 本地 LLM 烟测暂存入口回归

- 新增暂存目录中的 `smoke_local_ollama.py --help` 无网络启动测试，确保 README 给出的本地验证命令在源码暂存版中可用；烟测拒绝远端 endpoint，且只在临时仓库操作。
- 最终本机 Qwen 实跑再次通过：`write_file`、提交前 diff、目标文件单文件 commit、总结事件全部存在；4 次 loopback 请求均 `max_tokens=12000`，临时工作区清洁。
- 55/55 单测连续 3 轮；100%/125% 双 DPI 截图与像素断言、依赖 smoke、编译、`pip check`、QML lint、启动器、暂存版测试和 `git diff --check` 通过。远端 CI/MSVC standalone/许可证实包审查/原生桌面验收待完成，未提交。

## 2026-10-01 10:29（Asia/Shanghai）· 本地 Qwen 编码闭环实测与可复用烟测

- 确认本机 Ollama 已缓存 `scidev-qwen2.5-coder-7b:q4_k_m`，未下载或拉取模型。实际 Agent 在外部临时 Git 仓库调用 `write_file` 创建精确脚本；提交前 diff 检查成功，自动 commit 仅包含目标文件，账本写入完成/提交事件，并生成最终对话总结；临时仓库结束前状态干净。
- 新增 `scripts/smoke_local_ollama.py` 与 3 项本地保护测试：拒绝远端 URL、回环请求、低于 10k 的 token 预算；烟测只连 loopback、为本进程设置代理绕过规则、从不执行模型下载，并只输出请求元数据。真实运行 4 次请求均为 loopback、`max_tokens=12000`。
- README 提供手动复测命令，源码暂存与 CI 编译检查包含该脚本。54/54 单测连续 3 轮通过；双 DPI 全套截图、依赖/启动器/QML/编译和 diff 检查通过。远端 CI、MSVC standalone、Qt 许可审查及原生桌面验收仍待完成；未提交。

## 2026-10-01 09:47（Asia/Shanghai）· 双 DPI 分支像素级截图断言

- 截图几何断言曾与 QML 画面不同步；现在窄屏截图会在目标卡片缩放后的位置核验失败分支强调色像素，避免仅凭 QML 属性判定卡片已绘制。
- 普通与 `QT_SCALE_FACTOR=1.25` 两套截图烟测通过并目视复核；两套图均含完整首个尝试分支和可见详情。CI 的 125% 渲染工件路径也有静态回归保护。
- 51/51 单测连续 3 轮通过；编译、依赖 smoke、`pip check`、QML lint、启动器、`git diff --check` 通过。离屏结果仍不等于原生桌面验收；未执行远端 CI/MSVC，未提交。

## 2026-10-01 09:40（Asia/Shanghai）· 高 DPI 场景图截图回归

- 125% DPI 烟测发现截图首帧仍显示 100% 旧树画面，尽管 Qt/QML 属性已经更新；`QQuickWidget` 截图前现在等待连续 3 次事件/渲染循环，画面与几何断言保持同步。
- Windows CI 新增隔离 `QT_SCALE_FACTOR=1.25` 的完整 UI 截图烟测，并将两种 DPI 的截图一起保留为定时/手动审查工件。目视复核高 DPI 窄屏图：78% 缩放、首个分支完整，详情标题/状态/说明可见。
- 51/51 单测连续 3 轮通过；125% DPI 截图、依赖 smoke、编译、`pip check`、启动器、QML lint 与 `git diff --check` 通过。截图仍为离屏渲染；未运行远端 CI、MSVC standalone 或原生桌面验收，未提交。

## 2026-10-01 09:21（Asia/Shanghai）· 窄屏版本树与全量回归

- 修复版本树窄屏详情区仅约 88px且首个尝试卡片裁切：详情区提高到约 38%，窄屏画布自适配到 78% 缩放以完整显示首个分支；回宽屏恢复原缩放/平移，并尊重紧凑模式中的手动缩放。
- 新增截图断言覆盖分栏高度、卡片可见边界、滚动详情及宽窄屏状态往返。目视 940×620 离屏图确认分支、详情标题/状态/说明均可见；拖拽、平移、滚动和 Ctrl+滚轮缩放仍通过。
- 50/50 单测连续 3 轮通过；依赖 smoke、Python 编译、`pip check`、QML lint、启动器 runtime check、`git diff --check` 通过。截图为 Qt 离屏渲染，不代替原生桌面验收；未提交 Git。

## 2026-10-01 09:10（Asia/Shanghai）· standalone 依赖与许可证审计清单

- 手动 Windows standalone workflow 过去只输出 exe 哈希与总文件数，无法直接核对实际打包的 Qt/PySide native runtime。新增 `scripts/audit_standalone.py`：输出 DLL/PYD/EXE 路径、字节数、SHA-256，以及安装分发包的许可证声明和 `License-File` 哈希；发现声明的许可文本缺失、缺 `Qt6Core.dll` 或包目录含 symlink/junction 时失败。
- workflow 只上传审计 JSON，不上传 standalone 二进制，并明确 `manual-review-required`；该清单是审查证据而非许可证结论。针对完整/不完整包、缺许可文件、实际 site-packages 元数据及 Windows 外部 junction 的回归通过。MSVC workflow 尚未实际执行。
- 原生截图 helper 本轮仍报 `native pipe unavailable`；UI 继续以宽/中/窄 Qt 离屏截图验证。Git remote 缺失；未提交。

## 2026-10-01 08:55（Asia/Shanghai）· 发布风险复核与本地模型数据隔离

- `.gitignore` 原先未排除常见本地权重/数据格式，现忽略 GGUF、safetensors、ONNX、PyTorch checkpoint 和生成的数据/产物目录；新增 `git check-ignore --no-index` 回归，确认源代码与 `.env.example` 仍可跟踪。
- 对当前可提交文件及 4 个本地提交作定向私钥/API-token 特征扫描，未命中；机器未安装 Gitleaks 等完整扫描器，故不把此结果表述为完整凭据审计。当前仓库没有 Git remote。
- 官方 Qt 文档说明 Community 版采用 LGPL/GPL/商业许可组合、个别模块仅 GPL，并建议针对实际使用的 PySide 第三方组件作归属声明；SBOM 与许可结果依具体安装/构建配置。当前 pip wheel 元数据包含许可表达式及商业许可提示，但尚无真实 standalone 产物，第三方清单仍未完成。Nuitka runtime exception 只按其条款适用于目标代码，不能替代编译器自身许可审查。
- 45/45 单测连续 3 轮；客户端 smoke、1500/1180/940px 截图复核、编译、pip、启动器、QML lint 与差异检查通过。原生桌面截图接口重置后仍报 `native pipe unavailable`；未提交 Git。

## 2026-10-01 08:24（Asia/Shanghai）· Agent Git 自动操作隐私与大文件保护

- 隔离仓库复现：未忽略的 `.env` 会进入 Agent diff/自动提交；超过 Agent diff 展示上限的二进制也可能被自动提交。修复后，常见敏感/内部路径及其改名差异不发给模型、不进入 Agent 自动提交；自动提交另跳过链接、超过 240,000 字节及二进制/非 UTF-8 文件，保留在工作区并在聊天提示。
- `.env.example` 等模板仍可用；Git 面板手动查看/提交不变。新增凭据内容/改名、密钥和大文件自动提交回归。44/44 单测连续 3 轮通过；客户端交互、1500/1180/940px 离屏截图复核、启动器、编译、pip、QML lint 与 `git diff --check` 通过。
- 截图为 Qt 离屏渲染，不替代原生桌面捕获；未提交 Git。远端 CI、MSVC 独立包及 Qt 第三方许可清单仍待真实验证。

## 2026-10-01 07:46（Asia/Shanghai）· 畸形版本树布局不再导致启动崩溃

- `tree_layout.json` 中 `NaN` 会让版本树在 `int(offset.y())` 崩溃；`Infinity`/超大坐标会污染画布与滚动高度，非法 UTF-8/超大 JSON 整数也可能在解析阶段抛错。
- 统一布局加载现设置 1 MB 文件上限、±50,000 px 坐标边界，拒绝布尔和非有限坐标，忽略损坏/无效项；旧版与 QML 版复用该加载器。新增两项 GUI 回归覆盖 NaN/Infinity/超大值以及坏编码、坏 JSON、超大整数和超限文件。
- 40/40 单测连续 3 轮通过；窗口/文件编辑交互 smoke、拖拽持久化、8 张响应式截图、启动器/编译/依赖/QML 全通过，复看宽窄截图未见新异常。新 GUI 测试已加入 Windows CI 编译列表。
- 离屏截图不是原生桌面验证；远端 CI、独立 Windows 包与第三方 Qt 许可清单仍待验证。

## 2026-10-01 07:36（Asia/Shanghai）· Windows junction 越界文件树泄露修复

- 隔离复现发现：NTFS junction 的 `is_symlink()` 为 false，递归文件树会列出 junction 外目录的文件名与大小；Agent 的 `read_file`/`write_file` 已通过根路径解析拒绝越界，但 listing 没有同等保护。
- `list_files()` 现对每个子项解析后确认仍在项目根内，并跳过 symlink/junction。新增 Windows 回归验证外部哨兵不出现在树中，且经 junction 的读取和写入均被拒绝。测试先失败复现、修复后通过。
- 38/38 单测连续 3 轮；桌面依赖 smoke、8 张响应式截图、启动器/编译/pip/QML 检查通过。真实 Qwen 持久队列隔离项目再次成功，确认 junction 外部哨兵未进入对话、只提交 `hello.py` 且总结落盘（12k tokens，无 shell）。
- 本机 UI 捕获工具未提供可控原生窗口；远端 CI、独立包与 Qt 第三方许可清单仍待验证。

## 2026-10-01 07:28（Asia/Shanghai）· Qwen 持久队列端到端复验

- Ollama `scidev-qwen2.5-coder-7b:q4_k_m` 经真实持久任务队列在临时隔离项目成功创建 `hello.py`；Harness 提交前 diff、精确文件级自动提交及 LLM 总结均落盘，任务/会话状态成功，max_tokens=12000，未执行 shell。
- 随后全量 37 项单测连续 3 轮通过。此端到端覆盖本地模型/API/队列/Agent/Git/总结链，不代表原生 GUI 点击链验证。
- 原生桌面捕获接口本轮无可控应用；远端 Actions、MSVC 独立包与第三方许可清单仍未验证。模型 E2E 临时目录已自动清理。

## 2026-10-01 07:20（Asia/Shanghai）· 回归测试环境定位修复

- 首次完整测试发现部署 dry-run 用 `Path(sys.executable).with_name(...)` 定位工具，在 Windows 实际会漏掉 `Scripts` 目录；改为按 `sys.prefix` 和平台脚本目录定位。
- 37/37 单测连续 3 轮通过；客户端交互 smoke 与 8 张宽/中/窄离屏截图通过。启动器预检、编译、依赖、QML lint、差异空白检查均通过。
- Apache-2.0 已获用户确认，`LICENSE` 完整文本与 `pyproject.toml` 声明一致。本地截图非原生桌面；远端 CI 和 Windows 独立包尚未实测。

## 2026-10-01 07:02（Asia/Shanghai）· 提交前 diff 确定性门禁

- Qwen 实测会忽略显式 `git_diff` 请求却照常结束。现在 Agent 有改动时必须先检查最新 diff 才能提交；缺少检查就由 Harness 自动执行只读 diff 并回传模型，屏蔽未验证的临时答复。后续工具修改会使已验证版本失效并触发复查。
- 回归覆盖 Harness 补检先于提交、模型在 diff 后再次修改需二次检查。36/36 单测连续 3 轮通过；真实 Ollama Qwen 经过持久任务队列写入、自动 diff、仅 `hello.py` 提交、LLM 总结落盘；未批准/执行 shell，max_tokens=12000，临时项目已清理。
- 客户端交互 smoke、1500/1180/940px 共 8 张截图、py_compile、pip check、启动器、QML lint 全通过。截图为 Qt 离屏而非原生桌面；远端与 Windows 独立包仍待验证。

## 2026-10-01 06:49（Asia/Shanghai）· Qwen 裸 JSON 工具调用与未验证完成修复

- Qwen 偶发返回完整裸 JSON 工具调用，旧兼容层未解析，Agent 错把未执行的写文件请求标记完成。兼容开关开启时现只解析整条回复、精确 envelope、已声明工具和完整参数；普通说明文本、未知工具或额外字段均不转为执行调用。
- 加强 Agent 规则：用户明确要求的工具/检查必须执行并据工具结果汇报。35/35 单测通过；本地 Qwen2.5-Coder-7B 通过隔离真实编码链路：`write_file`→`git_diff`→自动提交 `hello.py`→持久化会话总结；关闭 shell 审批且未执行 shell，max_tokens=12000。临时项目已清理。
- Action SHA 在线核验受直连网络超时影响，未使用代理；其真实 tag 对应关系、远端 CI、独立包仍未证实。

## 2026-10-01 06:20（Asia/Shanghai）· 版本树中等窗口裁切修复

- 1180px 页面下版本树 splitter 因方向变化自动切成水平 `Preferred` 策略，宽 360px 而页面宽 567px；明确设为双轴 `Expanding` 后，1500/1180/940px 分栏宽度均与页面一致，消除右侧空洞。
- 截图 smoke 新增内层 splitter 方向、填充宽度、尺寸策略和子面板边界检查。33/33 单测与 8 张截图/拖拽/平移/滚轮交互回归通过，已人工复看新版宽、中、窄图。
- 仅为 Qt 离屏渲染；原生桌面、远端 CI、Windows 独立包及第三方运行时许可清单仍待验证。

## 2026-10-01 06:01（Asia/Shanghai）· Essentials-only 隔离启动验证

- 临时 hardlink overlay 只含 PySide6-Essentials 与 Shiboken；发行包门禁确认无 Addons。应用从隔离路径成功导入，8 张 Qt 离屏截图及 UI/版本树交互 smoke 通过。
- 验证不下载文件、不改写 `.venv`，并清理了隔离目录；它仍不能替代干净 pip venv、Windows CI 或 standalone 包验证。

## 2026-10-01 05:33（Asia/Shanghai）· Essentials-only CI 门禁与完整回归

- `requirements.txt`、`pyproject.toml` 和打包约束改为 PySide6-Essentials；启动器检测对应发行元数据，CI 构建清单报告准确的 distribution 名称。实装文件清单确认所需 Qt 模块及 deploy/qmllint 工具均由 Essentials 提供。
- 移除项目对 PySide6-Addons 的直接依赖；当前 venv 中 Addons 文件合计约 456.6 MB，项目依赖解析不再要求安装它们。新增 Apache-2.0 元数据/许可证/Qt 第三方许可边界回归。32/32 单测连续 3 轮通过；源码/QML/交互 smoke、启动器、`py_compile`、`pip check` 和 QML lint 通过。
- 临时 import hook 屏蔽本机所有 Addons Python 原生模块后，完整 8 张 Qt 离屏布局/交互截图 smoke 仍通过；pip cache 禁用，未下载 wheel。该模拟不是干净安装结果，真实 Essentials-only 环境仍待远端 CI。
- 新增最小 Qt 发行包检测脚本，并挂到三版本 Windows CI 与手动打包 workflow 安装后；33/33 单测连续 3 轮、8 张布局截图 smoke 及交互/启动/编译/依赖/QML 门禁通过。当前工作机仍未执行这个“Addons 缺席”门禁，因为旧 `.venv` 有既存 Addons。
- Qt 官方说明 Essentials 与 Addons 是独立发行组件，许可只需审查实际使用的组件；仍未构建 standalone，不能据当前 venv 声称完成最终第三方许可清单。

## 2026-10-01 04:59（Asia/Shanghai）· 窄屏聊天控制项裁切修复与视觉回归

- 修正窄屏右侧聊天栏状态 chip 的截断：可见“重试”，完整含义保留在辅助名称与悬浮说明；截图 smoke 新增文字裁切断言。
- 修复后 8 张 Qt 离屏截图、版本树拖拽/平移/位置持久化/滚动/缩放检查通过；29/29 单测连续 3 轮通过，依赖 smoke、`py_compile`、`pip check`、QML lint、启动器 runtime 检查通过。源码凭据/个人绝对路径扫描未命中。
- Computer Use 返回空应用/浏览器，未取得原生窗口截图；当前图像为隔离数据上的 Qt 离屏渲染，不代表真实桌面窗口捕获。

## 2026-10-01 04:53（Asia/Shanghai）· Apache 许可证确认与本地 Qwen 端到端验证

- 用户确认采用 Apache-2.0；现有许可证文件、包元数据和 README 一致，README 区分项目代码、Qt/PySide 运行时与模型权重许可。
- 隔离临时 Git 项目中的本机 Ollama Qwen2.5-Coder-7B 实测通过：Harness 实际调用 `write_file`，生成脚本输出 `5`，写入会话总结与事件日志，并自动创建提交 `e7362eb49342f0e39773455296f87d356df9eb6e`。
- 项目 `.venv` 下 29/29 测试通过；系统 Python 环境缺少 PySide deploy 命令导致的单测失败已确认是解释器环境不匹配，不是代码故障。
- 未触发下载；该结果证明本机模型至 Harness 的编码、记录和 Git 链路，不代表 Windows standalone 打包或公开发布许可审计已完成。

## 2026-10-01 04:48（Asia/Shanghai）· 可复现 Windows standalone 构建门禁

- 新增仅手动触发的 Windows Server 2022 构建工作流：显式加载 MSVC x64 环境，使用固定 Python 3.14、PySide6 6.11.2、Nuitka 4.1.1；完成 standalone 后运行 exe 的 `--smoke-test` 检查 QML 启动。
- 工作流只上传 7 天的构建元数据清单（版本、文件数、QML 相对路径、exe 大小与 SHA-256），不传可执行文件、不创建 Release；避免第三方组件许可清单尚未完成时意外分发 Qt 二进制。
- 增加 `requirements-build.txt` 固定打包 Qt 版本，并将 pip / GitHub Actions 更新都交由每周 Dependabot；新增配置回归。
- 29/29 单测、隔离暂存/实际源码 smoke、deploy dry-run、客户端交互 smoke、编译、pip check、QML lint、启动器预检通过。[官方 PyPI 元数据](https://pypi.org/project/PySide6/6.11.2/)显示 PySide6 6.11.2 支持 Python 3.14 且提供 win_amd64 wheel；仅核验元数据，未下载。
- 本地无 MSVC，未实际构建；无 Git remote，workflow 未远端执行；Qt runtime 随包许可证仍需按真实构建产物审查；原生桌面截图接口不可用。项目未提交/发布。

## 2026-10-01 04:31（Asia/Shanghai）· 发布暂存副本实际启动测试

- 在新建隔离暂存目录运行 `scidev_client.py`，验证窗口构造、QML 版本树 Ready、root object 有效且没有 QML 错误；这覆盖发布清单和资源路径，而非原仓库副本。
- 27/27 单测、客户端交互 smoke、独立暂存运行、打包器 dry-run、Python 编译、pip check、QML lint、启动器预检及 8 张宽/中/窄 Qt 截图通过。
- 本轮尝试 Computer Use 原生窗口核验，但接口返回空应用列表；仅有离屏截图证据。MSVC/真实二进制、远端 CI、Git remote 均缺，项目未提交/发布。

## 2026-10-01 04:26（Asia/Shanghai）· Windows 发布暂存与打包配置回归

- 补全独立打包暂存清单中的依赖、启动批处理和 bootstrap；回归实际执行暂存脚本，要求文件集合精确匹配并排除 `.venv`、`.research`、`.git`。
- PySide 打包器未指定 spec 时 dry-run 会错误选用 `main.py` 和 onefile。文档现明确使用 `--config-file pysidedeploy.spec --nuitka-version=4.1.1`；新回归在隔离暂存目录实际运行 dry-run，断言 `scidev_client.py`、standalone、无控制台参数、固定 Nuitka 版本和 QML 文件。
- 26/26 单测、打包 dry-run、客户端/QML smoke、启动器、编译、依赖、QML lint 与差异检查通过；1500×920、1180×760、940×620 的 8 张 Qt 离屏截图生成并目视复核。
- 本机没有 `cl.exe`/`vswhere.exe`，未下载 Nuitka 或实际编译；无 Git remote，远端 CI 和发布仍未验证，改动未提交。
- 后续门槛：具备 MSVC 的 Windows 构建与 Qt 第三方许可审查；提供 Git remote 后实际运行 Python 3.12–3.14 CI。

## 2026-10-01 04:14（Asia/Shanghai）· 工作区标题省略与回归修复

- 发现并修复长工作区名称在响应式布局中被直接裁切的问题：侧栏名称中间省略，宽屏标题栏保留可读项目名，紧凑布局隐藏该标题栏标签但保留完整路径提示。
- 回归测试补充标签溢出/省略符检查与三栏尺寸门禁；1500×920、1180×760、940×620 共 8 张 Qt 截图生成并目视检查，分栏宽度均达标。
- 24/24 单测、客户端/QML 交互 smoke、启动器、编译、依赖、QML lint、`git diff --check` 全通过；截图并非原生桌面验收。远端 CI 与 Windows 安装包未验证；无 Git remote，未提交。

## 2026-10-01 03:47（Asia/Shanghai）· 可执行的响应式截图门禁

- `smoke_screenshot.py` 现在检查主工作区三栏最小宽度、可见性、相邻无重叠、Agent 输入区宽度和截图非空，不再只验证 PNG 保存成功。
- 三种窗口尺寸的栏宽检查通过：1500px→365/716/409，1180px→284/567/319，940px→240/410/280；24/24 单测及客户端/QML smoke、编译、依赖、lint 均通过。
- 新截图复看宽屏编辑器与窄屏版本树无明显视觉异常；原生桌面捕获仍不可用，当前截图为离屏渲染。

## 2026-10-01 03:42（Asia/Shanghai）· CI 供应链更新后复验

- Actions 固定 SHA、Python 版本矩阵防漂移及启动器测试在内共 24/24 单测通过；客户端/QML smoke、运行时预检、编译、依赖、QML lint 和 `git diff --check` 均通过。
- 重新生成 8 张响应式 Qt 截图；UI 代码未变，已在前一轮复看宽屏拖拽树与窄屏编辑器/版本树。
- 未运行 GitHub 远端 workflow 或 Dependabot；当前未配置 remote。

## 2026-10-01 03:41（Asia/Shanghai）· GitHub Actions 可复现性

- CI 使用的三个 GitHub Actions 固定为完整 commit SHA，标注对应 release 版本；新增每周 GitHub Actions Dependabot 更新计划。
- 单测覆盖 SHA 格式和更新配置，定向测试通过；GitHub 远端无配置，workflow/Dependabot 仍需首次推送后实际运行验证。

## 2026-10-01 03:36（Asia/Shanghai）· 完整回归与发布能力边界

- 最终本机 Python 3.14 验证：23/23 单测、客户端/QML 交互 smoke、启动器检查、8 张 Qt 响应式截图、编译、依赖检查、QML lint、`git diff --check` 通过；拖拽树与窄屏布局已人工复看。
- Python 3.12/3.13 仅纳入 Windows CI 矩阵，尚未运行远端工作流；本机找不到 MSVC C++ 工具链，Nuitka 安装包未构建；未获得原生桌面截图。
- 当前仓库没有 Git remote，因此没有推送或发布；所有改动保持未提交。

## 2026-10-01 03:35（Asia/Shanghai）· 支持版本矩阵防漂移

- CI 加入 Python 3.13，现逐一覆盖声明支持的 3.12–3.14；单测解析 `pyproject.toml` 与 workflow 矩阵，校验版本列表一致，定向测试通过。
- Qt 官方发行说明确认 PySide6 支持 Python 3.13，并记录 3.14 支持；本机实测仍限于 Python 3.14，远端矩阵尚未运行。

## 2026-10-01 03:32（Asia/Shanghai）· RetryQueue 停止中的重启保护

- 回归测试复现：`stop()` 等待超时后旧 worker 仍存活，`start()` 无提示返回，旧任务结束后队列停止服务。
- `start()` 现在区分运行中与停止中 worker：正常运行时重复启动仍幂等；停止未完成时明确报错，不创建并行 worker；退出后重启通过。
- 23/23 单测、客户端依赖/QML smoke、8 张响应式截图、编译、pip check、QML lint 和 `git diff --check` 通过；截图为 Qt 离屏渲染，未替代原生桌面验收。

## 2026-10-01 03:18（Asia/Shanghai）· 项目级 Python 启动器

- 增加 `scripts/bootstrap.py` 与启动器测试：支持 Python 3.12–3.14、校验 PySide6、仅按需安装依赖；不覆盖既有虚拟环境。批处理启动器与 CI 加入只读运行环境预检。
- 在隔离临时目录实测 Python 3.11 经 `py` 启动器选中 3.14 并创建 venv；实际 `start_client.bat --check-runtime` 通过，不触发额外下载。
- 单测 22/22；客户端依赖 smoke、8 张响应式 Qt 截图、编译、pip check、QML lint、`git diff --check` 均通过。已人工复看宽屏编辑器和窄屏版本树；不是原生桌面截图验收。
- 无 Git remote，未运行远端 CI 或安装包构建；改动未提交。

## 2026-10-01 02:53（Asia/Shanghai）· 原生桌面截图接口复核

- 按桌面自动化流程重新查询 Windows 应用；`sky.list_apps()` 连续两次因 native pipe 缺失失败，未取得原生窗口截图。已明确区分 Qt 离屏截图与真实桌面截图，不据此宣称原生视觉验收通过。

## 2026-10-01 02:52（Asia/Shanghai）· 队列中断恢复与完整回归

- 新增模拟“任务已写入 running 后进程中断”的恢复测试，验证队列重启后恢复执行并完成；与幂等启动/停止/重启及退避重试一起，17/17 单测通过。
- Qwen2.5-Coder-7B 经真实 RetryQueue→CodingAgent 全链路通过；工具事件、任务完成事件、仅提交目标文件与干净工作区均核验。
- 客户端交互 smoke、Python 编译、依赖检查、QML lint 和 `git diff --check` 通过；此前本轮 8 张响应式截图已人工复看。
- 原生桌面截图、Python 3.12 远端 CI、GitHub remote 与安装包构建仍待验证；`vswhere` 未发现 MSVC C++ 工具链；改动未提交。

## 2026-10-01 02:50（Asia/Shanghai）· 队列生命周期修复与模型全链路复测

- 复现并修复 RetryQueue 重复 `start()` 会恢复正在运行任务并启动第二个 worker、`stop()` 后重启未清除停止标志的问题；增加幂等启动/停止/重启回归。
- 16/16 单测通过；本机 Qwen2.5-Coder-7B 经真实 RetryQueue→CodingAgent 流程调用 `write_file`，任务成功事件、工具事件、隔离仓库单文件提交与干净工作区均验证通过。
- 客户端双击打开/Ctrl+S、工作区恢复、多窗口、8 张宽中窄布局截图及版本树拖拽/平移/滚动/缩放、编译、依赖、QML lint 和差异检查通过；截图为 Qt 离屏图，人工复看无新外观异常。
- 仍待原生 Windows 截图、Python 3.12/远端 CI、配置 Git remote 与真实安装包构建；没有提交项目改动。

## 2026-10-01 02:45（Asia/Shanghai）· 文件编辑与 Git 路径回归

- 增加 Git 中文目录/空格文件名的受限提交测试，状态枚举和提交都使用 NUL 分隔路径；15/15 单测通过，未混入其他文件。
- 宽屏编辑器、窄屏版本树 Qt 截图人工复看无新布局异常；客户端双击打开/Ctrl+S、工作区恢复、多窗口、拖拽/平移/滚动/缩放 smoke 通过。
- 本机 Qwen 在文件工具安全修复后仍完成 `write_file`、事件记录和隔离 Git 提交；编译、依赖、QML lint、差异检查通过。
- 原生桌面截图、Python 3.12 远端 CI、GitHub remote 与安装包构建仍待验证；未提交项目更改。

## 2026-10-01 02:25（Asia/Shanghai）· 安全修复后 Qwen 端到端复测

- 补齐替换结果边界检查：拒绝 NUL 内容，且替换扩容后仍受单文件写入上限约束。
- 文件工具保护改动后重新运行本机 Qwen2.5-Coder-7B；真实 `write_file` 创建文件，事件账本有记录，临时仓库自动提交仅包含 `hello.py`，工作区干净。
- 14/14 单测通过；客户端交互、8 张多尺寸 Qt 离屏截图及版本树拖拽/平移/滚动/缩放、Python 编译、依赖检查、QML lint 与 `git diff --check` 通过。
- 真实桌面截图和 GitHub 远端 CI/发布仍待具备窗口枚举接口及配置 Git remote 后验证。

## 2026-10-01 02:22（Asia/Shanghai）· 文件工具数据保护与全面回归

- 复查发现 `replace_in_file` 对空匹配串会意外插入文本；对含 NUL 的二进制或非法 UTF-8 文件则可能以替换字符解码后写回，造成静默破坏。新增拒绝空匹配、NUL、无效 UTF-8、超大既有文件及非普通文件的保护；README 补充该安全边界。
- 新增回归覆盖二进制和无效编码的替换/覆盖拒绝、原始字节不变及 NUL 文本拒绝；14/14 单测通过。
- 客户端交互 smoke、8 张宽/中/窄布局截图与分支拖动/画布平移/滚动/缩放、`py_compile`、`pip check`、QML lint、`git diff --check` 通过；此前本机 Qwen→工具事件→隔离 Git 提交闭环已实测通过。
- Windows 桌面截图接口、远端 GitHub CI、Python 3.12 与 MSVC 二进制构建仍未验证；项目修改保留未提交状态。

## 2026-10-01 02:01（Asia/Shanghai）· Qwen 实际编码闭环与最终回归

- 已安装的 `scidev-qwen2.5-coder-7b:q4_k_m` 经 Ollama 接入 Harness，真实完成 `write_file` 工具调用；临时 Git 项目记录工具事件并自动提交，提交文件仅 `hello.py`，工作区干净。未重复下载 4.7 GB 模型。
- 13/13 单测、IDE 双击打开/Ctrl+S、工作区恢复、多窗口、QML/响应式截图与版本树拖拽/平移/滚动/缩放、`py_compile`、`pip check`、QML lint、`git diff --check` 通过。
- 两次首轮烟测断言分别过严地要求末尾换行、未考虑根提交 `diff-tree` 默认不列文件；修正临时验证条件后完整闭环通过，未发现产品代码缺陷。
- 原生 Windows 窗口截图工具当前不可用，8 张图为 Qt 离屏截图；无 Git remote，未运行 GitHub CI、未构建安装包、未提交或发布。

## 2026-10-01 01:55（Asia/Shanghai）· 版本树交互与自动提交隔离

- 真实 Qt 控件烟测证明普通滚轮原先被 QQuickWidget 消费，不能滚动外层窄屏画布；已改为普通滚轮滚动、Ctrl+滚轮缩放，并验证滚动后的卡片可见。
- 分支拖拽截图发现同级尝试卡片容易重叠；增大布局槽距，并测试拖拽、位置重载和节点不遮挡。窄屏 splitter 调整后首个尝试卡片完整显示。
- 自动提交由 `git add -A` 改为会话路径 + 临时 index；13 个单测、IDE 双击打开/Ctrl+S、工作区恢复、多窗口、画布平移/滚动/缩放、分支拖动与截图/QML、编译和依赖检查通过。Computer Use 无原生应用枚举，截图为 Qt 离屏控件图；未运行远端 CI 或二进制构建。

## 2026-10-01 01:24（Asia/Shanghai）· 发布准备与响应式界面复查

- 确认项目代码许可证为 Apache-2.0；README 明确 Qt/PySide6 运行时许可独立于项目代码。
- 1500×920、1180×760、940×620 五种代表布局截图回归通过；修复窄屏资源栏状态文案溢出和少数字符图标缺字，并将状态栏宽度检查加入截图烟测。
- 本机验证通过：12/12 单测、依赖/客户端/QML 烟测、命令审批拒绝、QML lint、Python 编译及 `git diff --check`。无 Git remote；未运行远端 CI、未发布。
- 在独立目录对允许发布的源码做清单暂存与打包 dry-run；确认不包含 `.venv`、`.research`、Git/CI 数据，并通过 `--nuitka-version=4.1.1` 固定 Nuitka。缺少 MSVC `dumpbin`，因此没有执行或声称完成二进制构建。

## 2026-09-30 19:53 · 开源发布与命令安全

- 根据用户选择加入 Apache-2.0（与 Apache 官方文件逐字核验）；增加 Python 3.12/3.14 Windows CI。README 移除开发机绝对模型路径，Ollama 标签可覆盖；架构文档已与当前 Qt 实现对齐。
- Agent 文件工具现阻止项目外路径和常见凭据路径；shell 命令需逐条 UI 批准，拒绝时不会启动，并记录决策。`.env.example` 等模板保留可读性。
- 本机 Python 3.14 / PySide6 6.11.2 回归通过：12 个单测、编译、依赖检查、客户端/QML 启动与命令拒绝桥接 smoke、QML lint、`git diff --check`。
- 安全改动后再次使用本地 Qwen 经 Harness 真实创建文件、记录 `write_file` 并自动提交；纯文件编辑未请求 shell 审批，测试目录已清理。
- GitHub CI 尚未远端运行：当前没有配置 Git remote。

## 2026-09-27 17:56 · 本机 Qwen2.5-Coder-7B 接入（Asia/Shanghai）

- 从 ModelScope Qwen GGUF 仓库固定 revision `9bc02b77c486bb4b40e119970821bf8cfeaaf267` 下载 Q4_K_M 单文件（4,683,073,536 字节）；SHA-256 `509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c` 核验一致，存放于项目外本地模型目录，不随仓库分发，Apache-2.0。
- 导入 Ollama：`scidev-qwen2.5-coder-7b:q4_k_m`，上下文设为 32K；新增 `start_qwen_local.bat` 本地启动入口，不改全局环境变量或默认启动方式。
- 为该入口增加 opt-in 文本工具调用兼容：仅接受已声明工具、参数完整且无未知参数的 `<tool_call>` / JSON fenced call；其他 provider 默认不启用。
- 验证：9 个单测通过；Harness 经本机 Ollama 实际调用 `write_file`，在隔离临时项目创建文件、记入工具事件并生成 Git commit。未在正式项目内生成测试文件。

## 2026-09-27 17:16 · 客户端 UI 与布局修复（Asia/Shanghai）

- 深色配色改为蓝灰分层表面，统一字体、留白、边框和状态栏；编辑区工具收进“更多”，对话输入区改用紧凑状态标签。
- 活动栏整理为资源管理器、项目搜索、开发版本树三个有效入口；修复重复导航和错误高亮。
- 工作区最低窗口调整为 940×620；标题栏和版本树工具随窄屏缩短，手动拖动三栏后窗口缩放仍保留比例。
- 版本树卡片与说明文字提升可读性；科研总结默认输出 12000 tokens，配置上限 32000。
- 截图复核 1500×920 与 940×620 工作区及版本树；LLM 当前未配置，未发起真实模型请求。

## 2026-09-02

- 已完成科研 Harness 方向设计文档。
- 根据需求确认使用原生桌面客户端，不使用网页端。
- 已移除第一版中的“运行实验”主流程，优先实现 Codex 式编码闭环。
- 已完成 Tkinter 桌面客户端：编码会话、LLM 工具调用、文件修改、必要命令检查、任务重试、会话 transcript、Git 自动提交和项目文件树。
- 已完成 VSCode + Codex 风格 UI 重构：深色 IDE 主题、左侧资源管理器、中间任务问卷/代码工作区、右侧对话栏、任务历史和 Git 弹窗。
- 已安装 PySide6 6.11.2，完成表现层从 Tkinter 迁移到 Qt，保留现有 CodingAgent 核心。
- Qt 客户端已通过窗口构造、显示、关闭和截图布局检查；代码编辑器支持行号、基础 Python 语法高亮和 Ctrl+S 保存。
- 已创建项目 `.venv` 并在其中安装 PySide6，新增 `start_client.bat` 自动准备依赖并启动客户端。
- 已通过 4 个核心单元测试和客户端启动/关闭冒烟测试。
- 已按 VS Code + Codex 工作台重做客户端布局：活动栏、资源管理器、中央任务入口/代码编辑器、右侧 Agent 对话和底部状态栏。
- 已修复客户端中文文案编码错乱，并统一为高对比度 VS Code 深色主题；完成实际窗口截图检查。
- 已去除主窗口、任务历史和 Git 窗口的 Windows 原生标题栏，增加统一的自定义窗口控制和拖动支持。
- 已将左侧活动栏增宽并放开侧栏可调范围，三栏使用 QSplitter 响应式伸缩；文件搜索可过滤资源树，任务问卷支持 Ctrl+Enter 提交。
- 已修复双击启动时依赖当前工作目录的问题，客户端现在始终以自身目录作为项目根目录。
- 已将 UI 基准字号提升至 10pt、编辑器提升至 11pt，文件树和聊天正文同步放大；活动栏提升至 70px，左侧资源管理器范围调整为 340–520px。
- 已完成 1500px 常用窗口和 1180px 最小窗口截图检查，确认三栏无横向溢出。
- 已将 Git 做成主工作区页面，加入主线/尝试方向树、节点详情、统计卡片和失败方向直接重试；树节点来自 `.research` 任务账本，不冒充 Git 正式分支。
- 已移除任务历史和 Git 状态的常规弹窗，任务历史并入开发版本树，开发树支持随节点数量增高并纵向滚动。
- 已修复同一主线下多个尝试方向的节点重叠问题，并通过 9 节点示例树渲染检查。
