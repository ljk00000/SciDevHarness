using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class ApprovedGitTools(string workspace, string? metadataWorkspace, IFileTools inside, IOperationReview? reviewer,
    Uri endpoint, Action<RunEvent> record, WorkspaceDrafts drafts, string? credential,
    Func<CancellationToken, ValueTask<string?>>? metadataPicker = null) : IFileTools
{
    public async ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token)
    {
        if (call.Name != "git_diff")
            return await inside.ExecuteAsync(call, token);
        token.ThrowIfCancellationRequested();
        if (reviewer is null)
            throw new ToolDeniedException("approval_denied", "Agent Git 查询需要交互审批。");
        ValidateArguments(call.Arguments);

        var root = new WindowsWorkspace(workspace);
        using var workspaceLease = root.OpenOperatorPath(".git")!;
        var directory = Directory.Exists(workspaceLease.AbsolutePath);
        if (!directory && !WindowsWorkspace.FileExists(workspaceLease.AbsolutePath))
            throw new ToolDeniedException("git_not_initialized", "请选择 Git 仓库或链接工作树的根目录。");
        using var metadataDirectory = directory ? new WindowsWorkspace(workspaceLease.AbsolutePath).OpenPath("scidev-review") : null;
        using var marker = directory ? null : WindowsWorkspace.OpenForRead(workspaceLease.AbsolutePath, FileShare.Read);
        if (marker is not null)
            WindowsWorkspace.GetFileIdentity(marker, workspaceLease.AbsolutePath);
        var linked = marker is not null;
        var metadata = metadataWorkspace;
        if (linked && metadata is null && metadataPicker is not null)
        {
            metadata = await metadataPicker(token);
            if (metadata is null)
                throw new ToolDeniedException("approval_denied", "未选择链接工作树的主仓库。");
        }
        if (linked && metadata is null)
            throw new ToolDeniedException("git_linked_worktree", "链接工作树的 Agent Git 查询需要预先指定主仓库。");
        var safeWorkspace = WorkspaceFiles.RedactCredential(workspace, credential);
        var destination = WorkspaceFiles.RedactCredential(endpoint.GetLeftPart(UriPartial.Path), credential);
        var detail = "批准一次本机 Git 只读查询。仅运行固定的状态、索引和差异查询。";
        if (linked)
            detail += "\n本次查询读取指定主仓库的 Git 元数据，发送差异前将展示并另行审批。\n主仓库 " +
                WorkspaceFiles.RedactCredential(metadata!, credential);
        else
            detail += "\n可见文件的文本差异将发给当前模型。";
        await ApproveAsync(new(call.Id, OperationKind.RunGitDiff, safeWorkspace, detail,
            Destination: linked ? null : destination), token);

        GitDiffReview.Result result;
        try { result = await GitDiffReview.CaptureAsync(workspace, metadata, drafts, credential, token); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or DecoderFallbackException or
            InvalidDataException or System.ComponentModel.Win32Exception)
        {
            throw new ToolDeniedException("git_review_unavailable", "无法读取工作区的 Git 差异。", ex);
        }
        var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(result.Text)));
        if (linked)
            await ApproveAsync(new(call.Id, OperationKind.ShareGitDiff, safeWorkspace,
                "将下方已核对的链接工作树差异发送给当前模型。", destination, Before: result.Text), token, digest);
        record(new("git_review_delivered", 0, new { id = call.Id, linked, result.Included, result.Omitted, result.Truncated, digest }));
        return new(result.Text, ExternalDataShared: linked);
    }

    private async ValueTask ApproveAsync(OperationReview request, CancellationToken token, string? digest = null)
    {
        var audit = new { id = request.CallId, operation = request.Kind.ToString(), request.Destination, digest };
        record(new("git_review_requested", 0, audit));
        var outcome = await OperationApproval.DecideAsync(reviewer!, request, token);
        record(new("git_review_decided", 0, new { review = audit, outcome }));
        token.ThrowIfCancellationRequested();
        if (outcome != "approved")
            throw new ToolDeniedException("approval_denied", "本次 Git 审阅操作未获批准。");
    }

    private static void ValidateArguments(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind == JsonValueKind.Object && !document.RootElement.EnumerateObject().Any())
                return;
        }
        catch (JsonException) { }
        throw new ToolDeniedException("invalid_tool", "git_diff 接受空对象，查询范围是当前工作区。");
    }
}
