using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal sealed partial class WorkbenchViewModel
{
    private sealed record PreparedTask(WorkspaceRunOptions Settings, string? Credential, bool ApproveOutside, bool ApproveNetwork,
        bool ApproveHost, bool ApproveMcp);
    internal sealed record QueueItem(DesktopTaskQueue.Entry Entry, string State)
    {
        public string Prompt => Entry.Prompt;
    }

    private readonly Dictionary<string, PreparedTask> _prepared = new(StringComparer.Ordinal);
    private DesktopTaskQueue.Entry[] _pending = [];
    private string? _runningQueuedId;
    private bool _pauseQueue;

    public QueueItem[] Queue { get; private set; } = [];
    public bool HasQueue => Queue.Length > 0;
    public bool CanContinueQueue => !IsBusy && NextTask is not null;
    public string SubmitLabel => IsBusy || NextTask is not null ? "加入队列" : "发送";
    private DesktopTaskQueue.Entry? NextTask => _pending.FirstOrDefault(item => !item.Started);
    private DesktopTaskQueue QueueStore => new(Workspace);

    public void RefreshQueue()
    {
        try { ShowQueue(QueueStore.Load()); }
        catch (Exception ex) when (ExpectedFailure(ex)) { QueueFailure(ex); }
    }

    public async Task ExecuteAsync(string? credential, WorkbenchReview review)
    {
        if (string.IsNullOrWhiteSpace(Prompt))
            return;
        try
        {
            var task = Prepare(Prompt.Trim(), credential, AutoCommit, _images);
            if (!IsBusy && NextTask is null)
            {
                Prompt = "";
                ClearImages();
                await DrainAsync(task, null, review);
                return;
            }

            ValidateQueuedTask(task);
            var entry = new DesktopTaskQueue.Entry(Guid.NewGuid().ToString("N"), task.Settings.Task, AutoCommit: task.Settings.AutoCommit,
                ImageCount: task.Settings.Images?.Count ?? 0);
            var updated = QueueStore.Add(entry);
            _prepared.Add(entry.Id, task);
            ShowQueue(updated);
            Prompt = "";
            ClearImages();
        }
        catch (Exception ex) when (ExpectedFailure(ex)) { QueueFailure(ex); }
    }

    public async Task ContinueQueueAsync(Func<string?> resolveCredential, WorkbenchReview review)
    {
        if (!CanContinueQueue || NextTask is not { } entry)
            return;
        try
        {
            var task = PreparedFor(entry);
            if (task is null && entry.ImageCount > 0)
                throw new ToolDeniedException("queue_attachments_missing", "请取回队列任务并重新附加图片。");
            task ??= Prepare(entry.Prompt, resolveCredential(), entry.AutoCommit);
            ValidateQueuedTask(task);
            await DrainAsync(task, entry, review);
        }
        catch (Exception ex) when (ExpectedFailure(ex)) { QueueFailure(ex); }
    }

    public void RemoveQueued(QueueItem item, bool takeBack)
    {
        if (_runningQueuedId == item.Entry.Id)
            return;
        try
        {
            var images = takeBack ? PreparedFor(item.Entry)?.Settings.Images : null;
            ShowQueue(QueueStore.Remove(item.Entry));
            if (takeBack)
            {
                Prompt = item.Prompt;
                SetImages(images?.ToArray() ?? []);
                if (item.Entry.ImageCount > 0 && images is null)
                    Status = "任务已取回，请重新附加图片。";
            }
        }
        catch (Exception ex) when (ExpectedFailure(ex)) { QueueFailure(ex); }
    }

    private PreparedTask Prepare(string prompt, string? credential, bool autoCommit, IReadOnlyList<ImageAttachment>? images = null)
    {
        var endpoint = WorkspaceRunner.EndpointFor(ApiBase.Trim());
        if (string.IsNullOrWhiteSpace(Model))
            throw new ArgumentException("请提供模型 ID。");
        if (!endpoint.IsLoopback && string.IsNullOrEmpty(credential))
            throw new ArgumentException("远程模型需要提供 API 凭据。");
        var image = Mode == ExecutionMode.ReadOnly || string.IsNullOrWhiteSpace(SandboxImage) ? null : SandboxImage.Trim();
        var settings = new WorkspaceRunOptions(Workspace, prompt, endpoint, Model.Trim(), Mode: Mode, SandboxImage: image,
            Planning: Planning, RequireVerification: RequireVerification, AutoCommit: CanApproveOutside && autoCommit,
            AllowedCommands: CanEditAllowedCommands ? AllowedCommands : null, NetworkPolicy: NetworkPolicy,
            LegacyCommandArguments: _legacyCommandArguments, Images: images);
        WorkspaceRunner.ValidateOptions(settings, credential, CanApproveOutside && ApproveOutside,
            image is not null && Mode is ExecutionMode.Manual or ExecutionMode.WorkspaceWrite,
            networkReview: CanApproveExtraNetwork && ApproveNetwork, taskCommitReview: settings.AutoCommit,
            hostCommandReview: CanApproveExtraNetwork && ApproveHostCommands, mcpReview: CanApproveExtraNetwork && ApproveMcp);
        return new(settings, credential, CanApproveOutside && ApproveOutside, CanApproveExtraNetwork && ApproveNetwork,
            CanApproveExtraNetwork && ApproveHostCommands, CanApproveExtraNetwork && ApproveMcp);
    }

    private async Task DrainAsync(PreparedTask task, DesktopTaskQueue.Entry? entry, WorkbenchReview review)
    {
        _pauseQueue = false;
        IsBusy = true;
        try
        {
            while (!_pauseQueue)
            {
                var id = entry?.Id ?? Guid.NewGuid().ToString("N");
                if (entry is not null)
                {
                    var updated = QueueStore.Start(entry);
                    _runningQueuedId = id;
                    ShowQueue(updated);
                    entry = entry with { Started = true };
                }
                var completed = await ExecuteOneAsync(id, task, review);
                if (entry is not null)
                {
                    var updated = QueueStore.Remove(entry);
                    _runningQueuedId = null;
                    ShowQueue(updated);
                }
                await RefreshAsync();
                if (!completed || NextTask is not { } next || PreparedFor(next) is not { } prepared)
                    break;
                entry = next;
                task = prepared;
            }
        }
        finally
        {
            if (_runningQueuedId is not null)
            {
                _runningQueuedId = null;
                ShowQueue(_pending);
            }
            IsBusy = false;
        }
    }

    private static void ValidateQueuedTask(PreparedTask task)
    {
        if (!string.IsNullOrEmpty(task.Credential) && task.Settings.Task.Contains(task.Credential, StringComparison.Ordinal))
            throw new ToolDeniedException("queue_sensitive", "提示词含有当前模型密钥，请先移除密钥。");
    }

    private PreparedTask? PreparedFor(DesktopTaskQueue.Entry item) =>
        _prepared.TryGetValue(item.Id, out var task) && task.Settings.Task == item.Prompt && task.Settings.AutoCommit == item.AutoCommit &&
        (task.Settings.Images?.Count ?? 0) == item.ImageCount ? task : null;

    private void ShowQueue(DesktopTaskQueue.Entry[] entries)
    {
        _pending = entries;
        foreach (var id in _prepared.Keys.Where(id => !entries.Any(item => item.Id == id && !item.Started)).ToArray())
            _prepared.Remove(id);
        Queue = entries.Where(item => item.Id != _runningQueuedId).Select(item => new QueueItem(item,
            item.Started ? "待核对运行记录" : PreparedFor(item) is { } task
                ? $"{task.Settings.Model} · {ModeLabel(task.Settings.Mode)}"
                : item.ImageCount > 0 ? "取回任务以重新附加图片" : "待确认模型和权限")).ToArray();
        Changed(nameof(Queue));
        Changed(nameof(HasQueue));
        Changed(nameof(CanContinueQueue));
        Changed(nameof(SubmitLabel));
    }

    private static string ModeLabel(ExecutionMode mode) => mode switch
    {
        ExecutionMode.ReadOnly => "只读",
        ExecutionMode.Manual => "手动审批",
        ExecutionMode.WorkspaceWrite => "工作区可写",
        ExecutionMode.FullAccess => "完全访问",
        _ => "工作区审批",
    };

    private void QueueFailure(Exception ex)
    {
        if (ex is ToolDeniedException { Status: "queue_changed" or "edit_conflict" })
        {
            try { ShowQueue(QueueStore.Load()); }
            catch (Exception failure) when (ExpectedFailure(failure)) { }
        }
        var note = ex is ToolDeniedException or ArgumentException ? ex.Message : "无法更新任务队列，请检查工作区权限。";
        if (IsBusy)
            Output += "\n\n" + note;
        else
            Status = note;
    }

    internal void LoadQueueCapture()
    {
        ShowQueue(
        [
            new("00000000000000000000000000000001", "整理实验观察记录"),
            new("00000000000000000000000000000002", "检查模拟结果与基线的差异"),
        ]);
    }
}
