using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal sealed partial class WorkbenchViewModel
{
    public async Task ResumePythonAsync(RunHistory.Entry entry, string? credential, WorkbenchReview review)
    {
        if (IsBusy || entry.Source != "python")
            return;
        using var stop = new CancellationTokenSource();
        _active = stop;
        IsBusy = true;
        var workspace = Workspace;
        var prompt = Prompt.Trim();
        var images = _images;
        try
        {
            Status = "正在核对 Python 会话…";
            var id = CliSession.ValidateId(entry.RunId, credential);
            var legacy = await Task.Run(() => RunHistory.InspectLegacy(workspace, id, credential, stop.Token), stop.Token);
            if (legacy.Continuation is not { } continuation)
                throw new ToolDeniedException("legacy_session_invalid", "此旧会话无法在 C# 中续跑。");
            if (prompt.Length == 0)
                prompt = legacy.Task?.Trim() ?? "";
            if (prompt.Length == 0)
                throw new ArgumentException("请在任务框输入本次要继续处理的内容。");
            var task = Prepare(prompt, credential, AutoCommit, images);
            if (!await review.ConfirmPythonResumeAsync(id, task.Settings.Endpoint, continuation, prompt, stop.Token))
            {
                Status = "已取消续跑。";
                return;
            }
            stop.Token.ThrowIfCancellationRequested();
            using var session = await Task.Run(() => CliSession.Open(workspace, id, task.Settings.Endpoint, resumePython: true,
                continuation.Revision, credential, stop.Token), stop.Token);
            string? retry = null;
            if (session.Previous is { } previous)
            {
                var prior = await Task.Run(() => RunInspection.Check(workspace, previous.RunId), stop.Token);
                if (prior.Status != "finished")
                {
                    if (!await review.ConfirmInterruptedRunAsync(previous.RunId, prior, stop.Token))
                    {
                        Status = "已取消续跑。";
                        return;
                    }
                    retry = previous.RunId;
                }
            }
            stop.Token.ThrowIfCancellationRequested();
            var runId = Guid.NewGuid().ToString("N");
            var context = await Task.Run(() => session.StartAttempt(workspace, runId, null, null, credential, retry, prompt,
                task.Settings.Images), stop.Token);
            await ExecuteOneAsync(runId, task, review, context);
            await RefreshAsync();
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { Status = "已取消续跑。"; }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            Status = ex is ArgumentException or ToolDeniedException ? ex.Message : "无法核对旧会话，请检查工作区权限。";
        }
        finally
        {
            _active = null;
            IsBusy = false;
        }
    }
}
