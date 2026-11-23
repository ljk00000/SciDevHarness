using System.Text;
using System.Text.Json;
using System.Xml;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceFiles;

namespace SciDev.Infrastructure;

public sealed class OutsideWorkspaceTools(string workspace, IFileTools inside, IOperationReview? reviewer, Uri modelEndpoint,
    Action<RunEvent> record, string? credential = null) : IFileTools
{
    private readonly string _prefix = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace)) + Path.DirectorySeparatorChar;

    public async ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token)
    {
        if (call.Name is not ("read_external_file" or "edit_external_file"))
            return await inside.ExecuteAsync(call, token);
        token.ThrowIfCancellationRequested();
        if (reviewer is null)
            throw new ToolDeniedException("approval_denied", "区外文件操作需要交互式审批。");
        try
        {
            using var doc = JsonDocument.Parse(call.Arguments);
            var args = doc.RootElement;
            if (args.ValueKind != JsonValueKind.Object)
                throw new ToolDeniedException("invalid_tool", "区外工具参数必须为 JSON 对象。");
            var path = WindowsWorkspace.ValidateAbsoluteFilePath(RequiredString(args, "path"));
            if (path.StartsWith(_prefix, StringComparison.OrdinalIgnoreCase))
                throw new ToolDeniedException("path_denied", "这个目标位于工作区内，请使用工作区文件工具。");
            ProtectCredential(path);
            return call.Name == "read_external_file"
                ? await ShareAsync(call.Id, path, args, token)
                : await EditAsync(call.Id, path, args, token);
        }
        catch (JsonException)
        {
            throw new ToolDeniedException("invalid_tool", "区外工具参数不是有效 JSON。");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or DecoderFallbackException or
            EncoderFallbackException or XmlException)
        {
            throw new ToolDeniedException("external_file_error", "区外文件操作失败，请核对路径、权限和编码。", ex);
        }
    }

    private async ValueTask<ToolResult> ShareAsync(string id, string path, JsonElement args, CancellationToken token)
    {
        var start = OptionalInt(args, "start_line", 1);
        var end = OptionalInt(args, "end_line", (int)Math.Min(int.MaxValue, start + 199L));
        if (start < 1 || end < start || end - (long)start >= 200)
            throw new ToolDeniedException("invalid_tool", "区外读取需指定正数行号，每次最多 200 行。");
        var scope = $"第 {start}–{end} 行，最多 8,000 字符";
        await RequireApprovalAsync(new(id, OperationKind.OpenExternalText, path,
            $"只在本机打开 UTF-8 文件，生成预览（{scope}）。"), token);

        var (original, stamp) = ReadApproved(path);
        var excerpt = FormatLines(original, start, end, 8_000);
        ProtectCredential(excerpt);
        await RequireApprovalAsync(new(id, OperationKind.ShareExternalText, path,
            $"将下方带行号的预览发送给当前模型（{scope}）。模型的回复可能引用这些内容。",
            modelEndpoint.GetLeftPart(UriPartial.Path), Before: excerpt), token);

        using var target = WindowsWorkspace.OpenAbsoluteFilePath(path);
        using var current = OpenSnapshot(target.AbsolutePath, FileShare.Read);
        RequireSame(stamp, current);
        return new(excerpt, ExternalDataShared: true);
    }

    private async ValueTask<ToolResult> EditAsync(string id, string path, JsonElement args, CancellationToken token)
    {
        var before = RequiredString(args, "old_text");
        var after = RequiredString(args, "new_text");
        if (before.Length == 0 || before.Length > MaxReviewChars || after.Length > MaxReviewChars)
            throw new ToolDeniedException("invalid_tool", "区外修改需要一项非空的精确匹配，修改前后各最多 8,192 字符。");
        ProtectCredential(before);
        ProtectCredential(after);
        await RequireApprovalAsync(new(id, OperationKind.OpenExternalText, path,
            "只在本机打开 UTF-8 文件，核对所提替换是否只匹配一次。"), token);

        var (original, stamp) = ReadApproved(path);
        var originalText = Utf8.GetString(original);
        var updated = ReplaceExact(originalText, before, after, out var position);
        if (updated == originalText)
            return new("内容相同，未修改。");
        var revised = Encode(path, updated);
        var line = 1 + originalText.AsSpan(0, position).Count('\n');
        await RequireApprovalAsync(new(id, OperationKind.EditExternalText, path,
            $"应用从第 {line} 行开始的精确替换。工作区检查点的恢复范围不包含此文件。",
            Before: before, After: after), token);

        using var target = WindowsWorkspace.OpenAbsoluteFilePath(path);
        using var current = OpenSnapshot(target.AbsolutePath, FileShare.None, exclusiveWrite: true);
        RequireSame(stamp, current);
        token.ThrowIfCancellationRequested();
        try { WriteAndFlush(current.Stream, revised); }
        catch (IOException)
        {
            WriteAndFlush(current.Stream, original);
            throw;
        }
        return new("已应用审阅通过的区外替换。", new(path, "external_edit"));
    }

    private static (byte[] Bytes, FileStamp Stamp) ReadApproved(string path)
    {
        using var target = WindowsWorkspace.OpenAbsoluteFilePath(path);
        using var snapshot = OpenSnapshot(target.AbsolutePath, FileShare.Read);
        return (snapshot.Bytes, snapshot.Stamp);
    }

    private static void RequireSame(FileStamp expected, Snapshot actual)
    {
        if (expected != actual.Stamp)
            throw new ToolDeniedException("edit_conflict", "区外文件在复核期间发生变化，本次操作没有继续。");
    }

    private static void WriteAndFlush(FileStream file, byte[] content)
    {
        file.Position = 0;
        file.Write(content);
        file.SetLength(content.Length);
        file.Flush(flushToDisk: true);
    }

    private void ProtectCredential(string value)
    {
        if (!string.IsNullOrEmpty(credential) && value.Contains(credential, StringComparison.Ordinal))
            throw new ToolDeniedException("external_credential", "内容包含当前模型凭据，已拒绝区外操作。");
    }

    private async ValueTask RequireApprovalAsync(OperationReview request, CancellationToken token)
    {
        var audit = new { id = request.CallId, action = request.Kind.ToString(), path = request.Target, request.Destination };
        record(new("outside_review_requested", 0, audit));
        var outcome = await OperationApproval.DecideAsync(reviewer!, request, token);
        record(new("outside_review_decided", 0, new { review = audit, outcome }));
        token.ThrowIfCancellationRequested();
        if (outcome != "approved")
            throw new ToolDeniedException("approval_denied", "区外操作未获本次确认。");
    }
}
