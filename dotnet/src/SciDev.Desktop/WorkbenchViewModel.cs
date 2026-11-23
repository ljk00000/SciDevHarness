using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal sealed partial class WorkbenchViewModel(string workspace, Func<WorkspaceDrafts> captureDrafts) : INotifyPropertyChanged
{
    private string _workspace = workspace;
    private string _output = "";
    private string _submittedPrompt = "";
    private string[] _previewPaths = [];
    private string _status = "就绪";
    private bool _isBusy;
    private ExecutionMode _mode = ExecutionMode.WorkspaceApproval;
    private AgentNetworkPolicy _networkPolicy = AgentNetworkPolicy.Ask;
    private bool _legacyCommandArguments;
    private PlanningMode _planning = PlanningMode.Auto;
    private string _prompt = "";
    private string _apiBase = Environment.GetEnvironmentVariable("SCIDEV_API_BASE") ?? "";
    private string _model = Environment.GetEnvironmentVariable("SCIDEV_MODEL") ?? "";
    private RunHistory.Entry[] _history = [];
    private CancellationTokenSource? _active;
    private int _historyVersion;
    private int _selectionVersion;

    public event PropertyChangedEventHandler? PropertyChanged;
    public string Workspace
    {
        get => _workspace;
        private set
        {
            Set(ref _workspace, value);
            Changed(nameof(WorkspaceName));
        }
    }
    public string WorkspaceName => Path.GetFileName(Path.TrimEndingDirectorySeparator(Workspace));
    public string ApiBase
    {
        get => _apiBase;
        set
        {
            Set(ref _apiBase, value);
            Changed(nameof(ModelLabel));
            Changed(nameof(HasModelAddress));
        }
    }
    public string Model
    {
        get => _model;
        set
        {
            Set(ref _model, value);
            Changed(nameof(ModelLabel));
            Changed(nameof(HasModelAddress));
        }
    }
    public bool HasModelAddress => !string.IsNullOrWhiteSpace(Model) && !string.IsNullOrWhiteSpace(ApiBase);
    public string ModelLabel => HasModelAddress ? Model.Trim() : "未配置模型";
    public string SandboxImage { get; set; } = Environment.GetEnvironmentVariable("SCIDEV_SANDBOX_IMAGE") ?? "";
    public string Prompt
    {
        get => _prompt;
        set
        {
            Set(ref _prompt, value);
            Changed(nameof(DraftContextLabel));
        }
    }
    public string DraftContextLabel => $"上下文 · 草稿 {Prompt.Length} 字";
    public bool ApproveOutside { get; set; }
    public bool ApproveNetwork { get; set; }
    public bool ApproveHostCommands { get; set; }
    public bool ApproveMcp { get; set; }
    public string AllowedCommands { get; set; } = "";
    public PlanningMode Planning { get => _planning; set => Set(ref _planning, value); }
    public bool RequireVerification { get; set; } = true;
    public bool AutoCommit { get; set; }
    public string PlanTitle { get; private set; } = "";
    public string PlanText { get; private set; } = "";
    public bool HasPlan => PlanText.Length != 0;
    public string Output
    {
        get => _output;
        private set
        {
            if (Set(ref _output, value))
                Changed(nameof(HasExchange));
        }
    }
    public string SubmittedPrompt
    {
        get => _submittedPrompt;
        private set
        {
            if (Set(ref _submittedPrompt, value))
                Changed(nameof(HasExchange));
        }
    }
    public bool HasExchange => SubmittedPrompt.Length != 0 || Output.Length != 0;
    public string[] PreviewPaths { get => _previewPaths; private set => Set(ref _previewPaths, value); }
    public string Status { get => _status; private set => Set(ref _status, value); }
    public RunHistory.Entry[] History { get => _history; private set => Set(ref _history, value); }
    public bool CanStart => !IsBusy;
    public bool CanApproveOutside => Mode != ExecutionMode.ReadOnly;
    public bool CanApproveExtraNetwork => CanApproveOutside && NetworkPolicy == AgentNetworkPolicy.Ask;
    public bool CanEditAllowedCommands => Mode == ExecutionMode.WorkspaceWrite;

    public void UseSavedProvider(SavedProvider provider)
    {
        ApiBase = provider.Address;
        Model = provider.Model;
    }

    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            Set(ref _isBusy, value);
            Changed(nameof(CanStart));
            Changed(nameof(CanContinueQueue));
            Changed(nameof(SubmitLabel));
        }
    }

    public ExecutionMode Mode
    {
        get => _mode;
        set
        {
            Set(ref _mode, value);
            Changed(nameof(CanApproveOutside));
            Changed(nameof(CanApproveExtraNetwork));
            Changed(nameof(CanEditAllowedCommands));
        }
    }

    public AgentNetworkPolicy NetworkPolicy
    {
        get => _networkPolicy;
        set
        {
            if (_networkPolicy == value)
                return;
            Set(ref _networkPolicy, value);
            if (value == AgentNetworkPolicy.Deny)
            {
                ApproveNetwork = ApproveHostCommands = ApproveMcp = false;
                Changed(nameof(ApproveNetwork));
                Changed(nameof(ApproveHostCommands));
                Changed(nameof(ApproveMcp));
                _prepared.Clear();
                RefreshQueue();
            }
            Changed(nameof(CanApproveExtraNetwork));
        }
    }

    internal void ApplyLegacyPolicy(LegacyExecutionPolicy policy, ExecutionMode mode)
    {
        Mode = mode;
        NetworkPolicy = policy.EffectiveNetwork;
        SandboxImage = policy.ImageFor(mode) ?? "";
        AllowedCommands = policy.PrefixesFor(mode, SandboxImage.Length > 0 ? SandboxImage : null)?.Replace('\n', ';') ?? "";
        _legacyCommandArguments = true;
        ClearApprovals();
        _prepared.Clear();
        RefreshQueue();
        Status = "已应用本项目权限";
    }

    private void ClearApprovals()
    {
        ApproveOutside = ApproveNetwork = ApproveHostCommands = ApproveMcp = AutoCommit = false;
        Changed(nameof(ApproveOutside));
        Changed(nameof(ApproveNetwork));
        Changed(nameof(ApproveHostCommands));
        Changed(nameof(ApproveMcp));
        Changed(nameof(AutoCommit));
        Changed(nameof(SandboxImage));
        Changed(nameof(AllowedCommands));
    }

    public async Task SelectWorkspaceAsync(string path)
    {
        Workspace = path;
        Mode = ExecutionMode.WorkspaceApproval;
        NetworkPolicy = AgentNetworkPolicy.Ask;
        SandboxImage = Environment.GetEnvironmentVariable("SCIDEV_SANDBOX_IMAGE") ?? "";
        AllowedCommands = "";
        _legacyCommandArguments = false;
        ClearImages();
        ClearApprovals();
        _selectionVersion++;
        History = [];
        SubmittedPrompt = "";
        Output = "";
        PreviewPaths = [];
        ShowPlan(null);
        Status = "就绪";
        _prepared.Clear();
        RefreshQueue();
        await RefreshAsync();
    }

    public async Task RefreshAsync()
    {
        var version = ++_historyVersion;
        var path = Workspace;
        try
        {
            var sessions = await Task.Run(() => RunHistory.Recent(path, 30, CancellationToken.None));
            if (version == _historyVersion)
                History = sessions;
        }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (version == _historyVersion && !IsBusy)
                Status = "无法读取工作区会话，请检查工作区路径和权限。";
        }
    }

    public async Task OpenAsync(RunHistory.Entry entry)
    {
        if (IsBusy)
            return;
        var version = ++_selectionVersion;
        var path = Workspace;
        try
        {
            var selected = await Task.Run(() =>
            {
                if (entry.Source == "python")
                {
                    var legacy = RunHistory.ReadLegacy(path, entry.RunId);
                    return (legacy.Task, legacy.Summary, Status: legacy.FinalStatus ?? legacy.Status, legacy.Plan, legacy.Verification,
                        GitCommit: (TaskCommitResult?)null);
                }
                var current = RunInspection.Check(path, entry.RunId);
                return (current.Task, current.Summary,
                    Status: current.Status == "finished" ? current.FinalStatus ?? current.Status : current.Status, current.Plan,
                    current.Verification, current.GitCommit);
            });
            if (version == _selectionVersion && !IsBusy)
            {
                PreviewPaths = [];
                SubmittedPrompt = selected.Task ?? "";
                Output = (selected.Summary ?? "") + FormatVerification(selected.Verification) + FormatCommit(selected.GitCommit);
                ShowPlan(selected.Plan);
                Status = LocalStatus(selected.Status);
            }
        }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (version == _selectionVersion && !IsBusy)
                Status = "会话无法打开。";
        }
    }

    private async Task<bool> ExecuteOneAsync(string id, PreparedTask task, WorkbenchReview review,
        SessionRunContext? sessionContext = null)
    {
        _selectionVersion++;
        using var stop = new CancellationTokenSource();
        _active = stop;
        var (settings, credential, approveOutside, approveNetwork, approveHost, approveMcp) = task;
        Status = "正在准备任务…";
        SubmittedPrompt = Redact(settings.Task, credential);
        Output = "";
        PreviewPaths = [];
        ShowPlan(null);
        try
        {
            var drafts = captureDrafts();
            var outside = approveOutside ? review : null;
            var progress = new Progress<RunEvent>(entry =>
            {
                if (ReferenceEquals(_active, stop))
                {
                    if (entry.Data is TaskPlanUpdate update)
                        ShowPlan(update.Plan, credential);
                    UpdateProgress(entry);
                }
            });
            var run = await Task.Run(() => WorkspaceRunner.RunAsync(id, settings, credential,
                stop.Token, callback => review.PatchReviewer(callback, credential), outside, ((IProgress<RunEvent>)progress).Report,
                commandReview: settings is { Mode: ExecutionMode.Manual or ExecutionMode.WorkspaceWrite, SandboxImage: not null }
                    ? review : null, networkReview: approveNetwork ? review : null, drafts: drafts,
                gitReview: settings.Mode != ExecutionMode.ReadOnly ? review : null, gitMetadataPicker: review.SelectGitRepositoryAsync,
                taskCommitReview: settings.AutoCommit ? review : null, hostCommandReview: approveHost ? review : null,
                mcpReview: approveMcp ? review : null, sessionContext: sessionContext));
            var response = Redact(run.Result.Text, credential);
            ShowPlan(run.Result.Plan, credential);
            PreviewPaths = run.Result.FilesChanged.Where(change => change.IsWorkspaceChange())
                .GroupBy(change => change.Path, StringComparer.OrdinalIgnoreCase).Select(group => group.Last())
                .Where(change => change.Operation != "delete" && PreviewDocument.CanOpen(change.Path))
                .Select(change => change.Path).ToArray();
            var otherFiles = run.Result.FilesChanged.Select(change => change.Path).Except(PreviewPaths, StringComparer.OrdinalIgnoreCase).ToArray();
            if (otherFiles.Length > 0)
                response += "\n\n" + Redact(string.Join('\n', otherFiles), credential);
            Output = (response + Redact(FormatVerification(run.Result.Verification), credential) +
                Redact(FormatCommit(run.GitCommit), credential)).Trim();
            Status = LocalStatus(run.Result.Status);
            return run.Result.Success;
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { Status = "任务已停止"; }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            Status = "运行失败";
            Output = ex is ArgumentException or ToolDeniedException ? Redact(ex.Message, credential) : "检查工作区目录和文件权限。";
        }
        finally
        {
            _active = null;
        }
        return false;
    }

    public void Cancel()
    {
        _pauseQueue = true;
        if (_active is not { } active)
            return;
        Status = "正在停止…";
        active.Cancel();
    }

    private void UpdateProgress(RunEvent entry)
    {
        if (!IsBusy || _active?.IsCancellationRequested != false)
            return;
        Status = entry.Type switch
        {
            "model_requested" => $"第 {entry.Turn} 轮 · 模型生成中",
            "model_retry_scheduled" => "模型接口暂时不可用，正在重试",
            "tool_started" => $"第 {entry.Turn} 轮 · 执行工具",
            "verification_followup" => "等待验证最终代码",
            "verification_auto_started" => "正在运行离线检查",
            "edit_review_requested" or "outside_review_requested" or "command_review_requested" or "network_review_requested"
                or "git_review_requested" or "git_task_commit_review_requested" or "host_review_requested" or "mcp_review_requested" => "等待你的审批",
            "mcp_catalog_unavailable" => "有 MCP 服务的工具未能加入本次任务",
            _ => Status,
        };
    }

    private static string FormatCommit(TaskCommitResult? result) => result?.Status switch
    {
        null => "",
        "committed" => $"\n\n本地提交 {result.CommitId} · {result.FileCount} 个文件，跳过 {result.Omitted} 个",
        "nothing_to_commit" => "\n\n没有符合条件的本轮 Git 改动",
        "approval_denied" or "cancelled" => "\n\n已取消任务提交，工作区改动可继续使用",
        "skipped_user" => "\n\n已按任务要求跳过本地提交",
        "skipped_failed_task" => "\n\n任务未通过，已跳过本地提交",
        "git_not_initialized" => "\n\n当前工作区尚未初始化 Git 仓库",
        "git_linked_worktree" => "\n\n链接工作树需要指定主仓库",
        "git_task_commit_uncertain" or "git_task_index_repair_needed" => $"\n\n请核对分支、暂存区和提交 {result.CommitId}",
        _ => $"\n\n任务未提交，工作区改动可继续使用（{result.Status}）",
    };

    private static string FormatVerification(VerificationReport? report)
    {
        if (report is null)
            return "";
        var status = report.Status switch
        {
            "passed" => "验证通过",
            "failed" => "验证失败",
            "incomplete" => "部分项目待验证",
            "stale" => "代码已变更，待重新验证",
            "partial" => "验证仅覆盖部分文件",
            "scope_unavailable" => "未能确认需要验证的项目范围",
            "no_tests" => "测试命令没有执行有效测试",
            "test_result_unconfirmed" => "未能确认测试执行结果",
            "timed_out" => "验证超时",
            "start_failed" => "验证未能启动",
            "not_run" when report.Reason == VerificationReport.ContainerNotConfigured => "尚未运行代码检查（容器镜像为空）",
            _ => "尚未验证",
        };
        var lines = new List<string> { status + (report.Category is null ? "" : " · " + report.Category) };
        if (report.PassedDirectories is { Count: > 0 } passed && (passed.Count > 1 || report.PendingDirectories is { Count: > 0 }))
            lines.Add("已验证 · " + string.Join("、", passed.Take(6)) + (passed.Count > 6 ? " 等" : ""));
        if (report.PendingDirectories is { Count: > 0 } pending)
            lines.Add("待验证 · " + string.Join("、", pending.Take(6)) + (pending.Count > 6 ? " 等" : ""));
        lines.AddRange((report.Diagnostics ?? []).Take(6).Select(item => item.ToString()));
        return "\n\n" + string.Join('\n', lines);
    }

    private void ShowPlan(TaskPlan? plan, string? credential = null)
    {
        PlanTitle = plan is null ? "" : $"任务计划 · {plan.Completed}/{plan.Steps.Count}";
        PlanText = plan is null ? "" : Redact(string.Join("\n", plan.Steps.Select(step =>
            (step.Status switch
            {
                "in_progress" => "进行中",
                "completed" => "完成",
                "blocked" => "受阻",
                "skipped" => "跳过",
                _ => "待处理",
            }) + " · " + step.Step)) + (plan.Explanation.Length == 0 ? "" : "\n\n" + plan.Explanation), credential);
        Changed(nameof(PlanTitle));
        Changed(nameof(PlanText));
        Changed(nameof(HasPlan));
    }

    internal static string LocalStatus(string status) => status switch
    {
        "completed" or "success" => "任务已完成",
        "cancelled" => "任务已停止",
        "approval_denied" => "审批未通过",
        "mcp_output_not_shared" => "MCP 结果未获准发送",
        "editor_draft" => "文件有未保存的编辑",
        "plan_incomplete" => "任务计划未完成",
        "verification_required" => "代码改动待验证",
        "host_output_not_shared" => "宿主命令已运行，输出未分享",
        "host_command_timeout" => "宿主命令已超时",
        "interrupted" or "needs_inspection" => "任务中断，待复核",
        "no_changes" => "没有文件改动",
        _ => status,
    };

    private static bool ExpectedFailure(Exception ex) => ex is ArgumentException or ToolDeniedException or IOException or
        UnauthorizedAccessException or InvalidOperationException or InvalidDataException or JsonException or KeyNotFoundException;

    private static string Redact(string value, string? credential) => string.IsNullOrEmpty(credential) ? value :
        value.Replace(credential, "[redacted]", StringComparison.Ordinal);

    private bool Set<T>(ref T storage, T value, [CallerMemberName] string name = "")
    {
        if (EqualityComparer<T>.Default.Equals(storage, value))
            return false;
        storage = value;
        Changed(name);
        return true;
    }

    private void Changed(string name) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));

    public void LoadCaptureSample()
    {
        ApiBase = "https://opencode.ai/inference/go/openai/v1";
        Model = "deepseek-v4.1-flash";
        SandboxImage = "";
        Prompt = "根据阶段报告，补充实验结果和复现步骤。";
        Changed(nameof(SandboxImage));
        History =
        [
            new("csharp", "capture-a", DateTimeOffset.UtcNow, "已完成", "绘制鹈鹕骑自行车的 SVG"),
            new("csharp", "capture-b", DateTimeOffset.UtcNow, "已完成", "查看项目回归记录"),
            new("python", "capture-c", DateTimeOffset.UtcNow, "待复核", "核对旧实验记录"),
        ];
        SubmittedPrompt = "绘制鹈鹕骑自行车的 SVG";
        Output = "已在工作区生成 pelican.svg。SVG 已通过结构和几何检验。\n\npelican.svg";
        Status = "任务已完成";
    }

    internal void LoadEmptyCapture()
    {
        ApiBase = Model = SandboxImage = "";
    }

    internal void ShowOpenedFile(string path) => Status = $"已打开 {Path.GetFileName(path)}";

    internal void BeginDraft()
    {
        _selectionVersion++;
        SubmittedPrompt = "";
        Output = "";
        PreviewPaths = [];
        ShowPlan(null);
        Status = "就绪";
    }

    internal void LoadDocumentCapture(string path)
    {
        PreviewPaths = [path, "运行记录.txt"];
        SubmittedPrompt = "整理这次实验的结果，输出一份 Markdown 报告。";
        Output = "已整理 24 次实验记录，并把关键指标与复核步骤写入报告。";
        Status = "任务完成";
    }

    internal void LoadPlanCapture()
    {
        ShowPlan(new(
            [new("查看项目结构和任务要求", "completed"), new("更新实验脚本与说明", "in_progress"),
             new("运行相关检查", "pending")], "", 2, DateTimeOffset.UnixEpoch));
        Status = "第 2 轮 · 模型生成中";
        Output = "已查看实验目录与运行说明。";
    }
}
