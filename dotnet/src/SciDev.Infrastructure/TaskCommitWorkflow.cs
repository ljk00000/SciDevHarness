using System.Text;
using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed record TaskCommitResult(string Status, string? CommitId = null, string? Reference = null,
    int FileCount = 0, int Omitted = 0);

internal static partial class TaskCommitWorkflow
{
    [GeneratedRegex(@"(?:不要|别|无需|不需要|请勿|切勿|禁止).{0,10}(?:自动)?\s*(?:提交|commit)(?!\s*(?:测试|实验)?报告|文档|作业|论文|表单|申请|邮件)|\b(?:do\s+not|don't|dont|never)\s+(?:auto[\s-]?)?commit\b(?!\s+(?:tests?|docs?|reports?|papers?)\b)",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex NoCommitRequest();

    internal static async Task<TaskCommitResult> RunAsync(WorkspaceRunOptions options, RunResult result,
        WorkspaceCheckpoint checkpoint, IOperationReview reviewer, Func<CancellationToken, ValueTask<string?>>? metadataPicker,
        string? credential, Action<RunEvent> record, CancellationToken token)
    {
        if (!result.Success)
            return new("skipped_failed_task");
        if (NoCommitRequest().IsMatch(options.Task))
            return new("skipped_user");
        var paths = result.FilesChanged.Where(file => file.IsWorkspaceChange()).Select(file => file.Path).Distinct(StringComparer.OrdinalIgnoreCase)
            .Order(StringComparer.OrdinalIgnoreCase).ToArray();
        if (paths.Length == 0)
            return new("nothing_to_commit");

        string Safe(string value) => WorkspaceFiles.RedactCredential(value, credential);
        try
        {
            token.ThrowIfCancellationRequested();
            var root = options.Workspace;
            using var marker = new WindowsWorkspace(root).OpenOperatorPath(".git")!;
            var directory = Directory.Exists(marker.AbsolutePath);
            var linked = !directory && WindowsWorkspace.FileExists(marker.AbsolutePath);
            if (!linked && !directory)
                return new("git_not_initialized");
            var repository = options.GitMetadataWorkspace;
            if (linked && repository is null && metadataPicker is not null)
                repository = await metadataPicker(token);
            if (linked && repository is null)
                return new("git_linked_worktree");
            var detail = "运行本机 Git 只读检查，核对本轮文件、分支和暂存区。提交前会再次确认。";
            if (linked)
                detail += "\n主仓库 " + Safe(repository!);
            var target = Safe(root);
            var id = result.RunId;
            if (!await ApproveAsync(reviewer, new(id, OperationKind.PrepareGitCommit, target, detail), record, token))
                return new("approval_denied");

            var plan = await GitTaskCommit.PrepareAsync(root, repository, checkpoint, paths, credential, token);
            if (plan.Entries.Length == 0)
                return new("nothing_to_commit", Omitted: plan.Omitted);
            var message = $"SciDevHarness task {id[..12]}";
            var summary = $"分支 {plan.Reference}\n作者 {plan.Name} <{plan.Email}>\n提交说明 {message}\n" +
                $"本地提交 {plan.Entries.Length} 个文件，跳过 {plan.Omitted} 个。";
            if (!await ApproveAsync(reviewer, new(id, OperationKind.PublishGitCommit, target, Safe(summary),
                Before: Safe(plan.Preview)), record, token))
                return new("approval_denied", Omitted: plan.Omitted);

            return await GitTaskCommit.PublishAsync(root, repository, plan, checkpoint, paths, message, credential, record, token);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { return new("cancelled"); }
        catch (ToolDeniedException ex) { return new(ex.Status); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or DecoderFallbackException or
            InvalidDataException or System.ComponentModel.Win32Exception) { return new("git_task_commit_unavailable"); }
    }

    private static async Task<bool> ApproveAsync(IOperationReview reviewer, OperationReview request,
        Action<RunEvent> record, CancellationToken token)
    {
        var review = new { id = request.CallId, operation = request.Kind.ToString() };
        record(new("git_task_commit_review_requested", 0, review));
        var outcome = await OperationApproval.DecideAsync(reviewer, request, token);
        record(new("git_task_commit_review_decided", 0, new { review, outcome }));
        token.ThrowIfCancellationRequested();
        return outcome == "approved";
    }
}
