using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal enum DevelopmentState { Main, Active, Failed, Retry, Unknown }

internal sealed record DevelopmentVersion(string Id, string? ParentId, DevelopmentState State, string Title,
    string Description, string Record, RunHistory.Entry? Run = null)
{
    internal const string MainRoot = "root";
    internal bool IsMain => State == DevelopmentState.Main;
    internal string Status => State switch
    {
        DevelopmentState.Main => "当前主线",
        DevelopmentState.Failed => "已取消 / 失败",
        DevelopmentState.Active => "进行中",
        DevelopmentState.Retry => "等待重试",
        _ => "记录不可读",
    };

    internal string Badge => State switch
    {
        DevelopmentState.Main => "主线",
        DevelopmentState.Failed => "失败",
        DevelopmentState.Active => "进行中",
        DevelopmentState.Retry => "重试",
        _ => "未知",
    };

    internal static DevelopmentVersion Root => new(MainRoot, null, DevelopmentState.Main, "当前主线",
        "项目当前持续发展的编码主线。成功完成的编码任务会接在这里。", "暂无开发记录");

    internal static DevelopmentVersion[] FromHistory(IEnumerable<RunHistory.Entry> history,
        IEnumerable<WorkbenchViewModel.QueueItem> queue)
    {
        var result = new List<DevelopmentVersion> { Root };
        var parent = MainRoot;
        foreach (var run in history.OrderBy(entry => entry.UpdatedAt).ThenBy(entry => entry.RunId, StringComparer.Ordinal))
        {
            var state = run.Status switch
            {
                "completed" or "success" or "succeeded" => DevelopmentState.Main,
                "running" or "queued" or "in_progress" or "retryable" => DevelopmentState.Active,
                "retry_wait" => DevelopmentState.Retry,
                "unreadable" => DevelopmentState.Unknown,
                _ => DevelopmentState.Failed,
            };
            var id = $"{run.Source}:{run.RunId}";
            var detail = state switch
            {
                DevelopmentState.Main => "该编码任务已完成，修改结果进入当前主线。",
                DevelopmentState.Active => "Agent 正在尝试这个方向。",
                DevelopmentState.Unknown => "无法读取此开发记录。",
                _ => "这个方向没有进入当前主线。",
            };
            result.Add(new(id, parent, state, TitleFor(run.Task), detail, $"{run.UpdatedAt:yyyy-MM-dd HH:mm} · {run.Status}", run));
            if (state == DevelopmentState.Main)
                parent = id;
        }
        foreach (var item in queue.Where(item => !item.Entry.Started))
            result.Add(new($"queue:{item.Entry.Id}", parent, DevelopmentState.Active, TitleFor(item.Prompt),
                "任务已排队，尚未成为主线的一部分。", "等待执行"));
        return [.. result];
    }

    private static string TitleFor(string? task)
    {
        var title = task?.Split(['\r', '\n'], 2)[0].Trim() ?? "";
        const string prefix = "编码目标：";
        if (title.StartsWith(prefix, StringComparison.Ordinal))
            title = title[prefix.Length..].Trim();
        return title.Length == 0 ? "编码任务" : title;
    }
}
