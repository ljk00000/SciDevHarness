using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Xml;
using Microsoft.Win32.SafeHandles;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed partial class WorkspaceFiles(string workspace, IFileEditApproval? approval = null,
    WorkspaceCheckpoint? checkpoint = null, ExecutionMode executionMode = ExecutionMode.WorkspaceApproval,
    string? credential = null, WorkspaceDrafts? drafts = null) : IFileTools
{
    internal const int MaxBytes = 512 * 1024;
    internal const int MaxReviewChars = 8192;
    internal static readonly UTF8Encoding Utf8 = new(false, true);
    private readonly WindowsWorkspace _workspace = new(workspace);
    private readonly Dictionary<string, FileStamp> _created = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, (string Digest, int Turn)> _reportedGuidance = new(StringComparer.OrdinalIgnoreCase);
    private int _toolTurn;

    public async ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var discovery = call.Name is "list_files" or "search_code" or "get_project_context";
        if (executionMode == ExecutionMode.ReadOnly && call.Name != "read_file" && !discovery)
            throw new ToolDeniedException("read_only", "只读模式禁止写入和命令执行。");
        try
        {
            if (!discovery && call.Name is not ("read_file" or "write_file" or "replace_in_file" or "apply_patch" or "rename_file"))
                throw new ToolDeniedException("unsupported_tool", "文件工具不运行命令，也不联网。");
            using var doc = JsonDocument.Parse(call.Arguments);
            var args = doc.RootElement;
            if (args.ValueKind != JsonValueKind.Object)
                throw new ToolDeniedException("invalid_tool", "工具参数必须是一个 JSON 对象。");
            if (discovery)
            {
                var text = Discovery(token).Execute(call.Name, args, token);
                return new(call.Name == "get_project_context" ? text : RedactCredential(text, credential));
            }
            var path = WorkspaceDocuments.VisiblePath(RequiredString(args, "path"), allowHidden: false);
            if (call.Name == "rename_file")
                return await RenameAsync(path, WorkspaceDocuments.VisiblePath(RequiredString(args, "destination"), allowHidden: false), token);
            if (call.Name != "read_file")
            {
                drafts?.RequireWritable(path);
                if (UpdateGuidance(path, token) is { Length: > 0 } guidance)
                    return new("本次尚未写入。先阅读目标目录说明，再按任务需要重新提交操作。\n\n" + guidance);
            }
            return call.Name switch
            {
                "read_file" => Read(path, args, token),
                "write_file" => Create(path, RequiredString(args, "content"), token),
                _ => await EditAsync(path, ReadEdits(args, call.Name == "replace_in_file"), call.Name == "apply_patch", token),
            };
        }
        catch (JsonException)
        {
            throw new ToolDeniedException("invalid_tool", "工具参数不是有效的 JSON。");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or EncoderFallbackException or DecoderFallbackException or XmlException)
        {
            throw new ToolDeniedException("file_error", "文件操作失败：文件不可访问、超出限制，或内容格式无效。", ex);
        }
    }

    private WorkspaceDiscovery Discovery(CancellationToken token) =>
        new(_workspace, credential, drafts ?? WorkspaceDrafts.Empty, WorkspaceGitIndex.Read(_workspace, token));

    internal void BeginToolBatch(int turn) => _toolTurn = turn;

    internal string? InitialContext(string task, CancellationToken token) =>
        Discovery(token).InitialContext(task, token);

    internal VerificationPlan FindVerificationPlan(IReadOnlyList<FileChange> changes, CancellationToken token) =>
        Discovery(token).FindVerificationPlan(changes, token);

    public byte[] ReadCreatedArtifact(string path)
    {
        using var target = _workspace.OpenPath(path);
        using var snapshot = OpenSnapshot(target.AbsolutePath, FileShare.Read);
        if (!_created.TryGetValue(target.RelativePath, out var expected) || expected != snapshot.Stamp)
            throw new ToolDeniedException("edit_conflict", "本轮产物已经被外部改动，不能继续验收。");
        return snapshot.Bytes;
    }

    private ToolResult Read(string path, JsonElement args, CancellationToken token)
    {
        var start = Math.Max(1L, OptionalInt(args, "start_line", 1));
        var end = Math.Min(Math.Max(start, OptionalInt(args, "end_line", 400)), start + 399);
        if (drafts?.Get(path) is { } draft)
        {
            if (draft.Text is null)
                throw new ToolDeniedException("editor_draft", "此文件的未保存草稿超出本次预算。保存后可重新读取。");
            return new(FormatLines(draft.Text, start, end, 25_000, credential) + "\n（来自任务启动时的未保存草稿，本任务只读。）"
                + GuidanceNote(path, token));
        }
        using var target = _workspace.OpenPath(path);
        using var stream = OpenSnapshot(target.AbsolutePath, FileShare.Read);
        return new(FormatLines(stream.Bytes, start, end, 25_000, credential) + GuidanceNote(path, token));
    }

    private string GuidanceNote(string path, CancellationToken token) =>
        UpdateGuidance(path, token) is { Length: > 0 } guide ? "\n\n适用目录说明（与文件原文分开）\n" + guide : "";

    private string UpdateGuidance(string path, CancellationToken token)
    {
        var slash = path.LastIndexOf('/');
        if (slash < 0)
            return "";
        var folder = path[..slash];
        var guide = new WorkspaceProjectContext(_workspace, credential, drafts ?? WorkspaceDrafts.Empty)
            .InstructionsFor(WorkspaceProjectContext.Ancestors(path, directory: false).Skip(1), 3500, token);
        var identifier = Convert.ToHexString(SHA256.HashData(Utf8.GetBytes(guide)));
        if (_reportedGuidance.TryGetValue(folder, out var reported) && reported.Digest == identifier)
            return _toolTurn > 0 && reported.Turn == _toolTurn ? guide : "";
        _reportedGuidance[folder] = (identifier, _toolTurn);
        return guide;
    }

    internal static string RedactCredential(string text, string? credential) => string.IsNullOrEmpty(credential) ? text :
        text.Replace(credential, "[redacted]", StringComparison.Ordinal);

    internal static string FormatLines(byte[] bytes, long start, long end, int maxChars, string? credential = null) =>
        FormatLines(Utf8.GetString(bytes), start, end, maxChars, credential);

    private static string FormatLines(string content, long start, long end, int maxChars, string? credential)
    {
        var source = RedactCredential(content, credential);
        using var reader = new StringReader(source.Length > 0 && source[0] == '\uFEFF' ? source[1..] : source);
        var text = new StringBuilder();
        for (long number = 1; number <= end && reader.ReadLine() is { } line; number++)
        {
            if (number < start)
                continue;
            if (text.Length + line.Length > maxChars)
                break;
            text.Append(number).Append(": ").Append(line).Append('\n');
        }
        return text.Length == 0 ? "（无可读行）" : text.ToString().TrimEnd('\n');
    }

    private ToolResult Create(string path, string content, CancellationToken token)
    {
        using var target = _workspace.OpenPath(path);
        if (WindowsWorkspace.FileExists(target.AbsolutePath))
            throw new ToolDeniedException("approval_required", "write_file 只能新建文件；已有文件请用 apply_patch。");

        var bytes = Encode(target.AbsolutePath, content);
        _created[target.RelativePath] = Save(target.AbsolutePath, bytes, token,
            prepare: stamp => checkpoint?.Prepare(target.RelativePath, null, null, stamp, token));
        return new($"已写入 {target.RelativePath}（{bytes.Length} bytes）", new(target.RelativePath, "write"));
    }

    private async ValueTask<ToolResult> EditAsync(string path, Edit[] edits, bool reviewed, CancellationToken token)
    {
        string relative;
        string original;
        FileStamp stamp;
        bool unchangedTaskFile;
        using (var target = _workspace.OpenPath(path))
        {
            relative = target.RelativePath;
            var tracked = _created.TryGetValue(relative, out var created);
            if (!reviewed && !tracked)
                throw new ToolDeniedException("approval_required", "已有文件必须通过 apply_patch 提交审批。");
            using var initial = OpenSnapshot(target.AbsolutePath, FileShare.Read);
            stamp = initial.Stamp;
            unchangedTaskFile = tracked && created == stamp;
            if (!reviewed && !unchangedTaskFile)
                throw new ToolDeniedException("edit_conflict", "本轮新建的文件已被外部改动，请通过 apply_patch 重新提交。");
            original = Utf8.GetString(initial.Bytes);
        }

        var revised = original;
        var position = 0;
        foreach (var edit in edits)
            revised = ReplaceExact(revised, edit.Before, edit.After, out position);
        if (original == revised)
            return new("内容相同，未修改。");

        if (reviewed)
        {
            var line = 1 + original.AsSpan(0, position).Count('\n');
            var edit = edits[0];
            if (edit.Before.Length > MaxReviewChars || edit.After.Length > MaxReviewChars)
                throw new ToolDeniedException("invalid_tool", "补丁预览过长，请拆分成较小的修改。");
            if (approval is null || !await approval.ApproveAsync(new(relative, line, edit.Before, edit.After), token))
                throw new ToolDeniedException("approval_denied", "补丁未获批准；没有写入文件。");
        }

        token.ThrowIfCancellationRequested();
        using var destination = _workspace.OpenPath(path);
        var after = Encode(destination.AbsolutePath, revised);
        using var current = OpenSnapshot(destination.AbsolutePath, FileShare.Read | FileShare.Delete);
        if (current.Stamp != stamp)
            throw new ToolDeniedException("edit_conflict", "文件内容或身份在审阅期间已变化；没有写入文件。");

        var updated = Save(destination.AbsolutePath, after, token, stamp,
            next => checkpoint?.Prepare(relative, current.Stamp, current.Bytes, next, token));
        if (unchangedTaskFile)
            _created[relative] = updated;
        else
            _created.Remove(relative);
        return new($"已更新 {relative}。", new(relative, "edit"));
    }

    internal static string ReplaceExact(string original, string before, string after, out int position)
    {
        position = original.IndexOf(before, StringComparison.Ordinal);
        if (position < 0 || original.IndexOf(before, position + 1, StringComparison.Ordinal) >= 0)
            throw new ToolDeniedException("edit_conflict", "每项 old_text 必须精确匹配且只出现一次；本次没有写入。");
        if (original.Length - before.Length + (long)after.Length > MaxBytes)
            throw new ToolDeniedException("file_error", "修改后的文件超过大小限制。");
        return string.Concat(original.AsSpan(0, position), after, original.AsSpan(position + before.Length));
    }

    internal static Snapshot OpenSnapshot(string path, FileShare share, bool exclusiveWrite = false, int maxBytes = MaxBytes,
        bool allowBinary = false)
    {
        var handle = exclusiveWrite ? WindowsWorkspace.OpenForRecovery(path, delete: false) : WindowsWorkspace.OpenForRead(path, share);
        return ReadSnapshot(handle, path, exclusiveWrite ? FileAccess.ReadWrite : FileAccess.Read, maxBytes, allowBinary);
    }

    internal static Snapshot ReadSnapshot(SafeFileHandle handle, string path, FileAccess access, int maxBytes, bool allowBinary)
    {
        try
        {
            var identity = WindowsWorkspace.GetFileIdentity(handle, path);
            var stream = new FileStream(handle, access);
            try
            {
                if (stream.Length > maxBytes)
                    throw new ToolDeniedException("file_error", "文件超出读取大小限制。");
                var bytes = new byte[(int)stream.Length];
                stream.ReadExactly(bytes);
                if (!allowBinary && bytes.AsSpan().Contains((byte)0))
                    throw new ToolDeniedException("file_error", "文件包含 NUL，无法按 UTF-8 文本处理。");
                return new Snapshot(stream, bytes, Stamp(identity, bytes));
            }
            catch
            {
                stream.Dispose();
                throw;
            }
        }
        catch
        {
            handle.Dispose();
            throw;
        }
    }

    internal static byte[] Encode(string path, string content)
    {
        var bytes = Utf8.GetBytes(content);
        if (bytes.Length > MaxBytes || content.Contains('\0'))
            throw new ToolDeniedException("file_error", "文本超出大小限制或包含 NUL。");
        if (path.EndsWith(".svg", StringComparison.OrdinalIgnoreCase))
        {
            using var input = new MemoryStream(bytes);
            using var reader = XmlReader.Create(input, new XmlReaderSettings
            {
                DtdProcessing = DtdProcessing.Prohibit,
                XmlResolver = null,
                MaxCharactersInDocument = MaxBytes,
            });
            if (reader.MoveToContent() != XmlNodeType.Element || reader.LocalName != "svg" ||
                reader.NamespaceURI != "http://www.w3.org/2000/svg")
                throw new ToolDeniedException("file_error", "生成文件缺少 SVG 根元素。");
            while (reader.Read()) { }
        }
        return bytes;
    }

    internal static FileStamp Save(string path, byte[] bytes, CancellationToken token,
        FileStamp? expected = null, Action<FileStamp>? prepare = null, int maxCurrentBytes = MaxBytes)
    {
        var temporary = Path.Combine(Path.GetDirectoryName(path)!, $".scidev-{Guid.NewGuid():N}.tmp");
        var createdTemporary = false;
        try
        {
            FileStamp stamp;
            using (var output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                createdTemporary = true;
                output.Write(bytes);
                output.Flush(flushToDisk: true);
                stamp = Stamp(WindowsWorkspace.GetFileIdentity(output.SafeFileHandle, temporary), bytes);
            }
            token.ThrowIfCancellationRequested();
            prepare?.Invoke(stamp);
            token.ThrowIfCancellationRequested();
            if (expected is { } original)
                ReplaceChecked(temporary, path, original, token, maxCurrentBytes);
            else
                File.Move(temporary, path, overwrite: false);
            createdTemporary = false;
            return stamp;
        }
        finally
        {
            if (createdTemporary)
                File.Delete(temporary);
        }
    }

    private static void ReplaceChecked(string temporary, string path, FileStamp expected, CancellationToken token, int maxCurrentBytes)
    {
        const int sharingViolation = 32, unableToRemoveReplaced = 1175;
        for (var retry = 0; ; retry++)
        {
            token.ThrowIfCancellationRequested();
            using var current = OpenSnapshot(path, FileShare.Read | FileShare.Delete, maxBytes: maxCurrentBytes);
            if (current.Stamp != expected)
                throw new ToolDeniedException("edit_conflict", "替换前发现文件内容或身份变化，未写入文件。");
            try
            {
                File.Replace(temporary, path, destinationBackupFileName: null);
                return;
            }
            catch (IOException ex) when (retry < 3 && (ex.HResult & 0xFFFF) is sharingViolation or unableToRemoveReplaced)
            {
                // Both names remain available for these errors. Recheck the approved stamp on every attempt.
            }
            if (token.WaitHandle.WaitOne(25 << retry))
                token.ThrowIfCancellationRequested();
        }
    }

    internal static FileStamp Stamp(WindowsWorkspace.FileIdentity identity, byte[] bytes) =>
        new(identity, Convert.ToHexString(SHA256.HashData(bytes)));

    internal static string RequiredString(JsonElement args, string key) =>
        args.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()! : throw new ToolDeniedException("invalid_tool", $"工具参数 {key} 必须是字符串。");

    private static Edit[] ReadEdits(JsonElement args, bool allowBatch)
    {
        Edit[] edits;
        if (args.TryGetProperty("edits", out var batch))
        {
            if (!allowBatch || args.TryGetProperty("old_text", out _) || args.TryGetProperty("new_text", out _) ||
                batch.ValueKind != JsonValueKind.Array || batch.GetArrayLength() is < 1 or > 32 ||
                batch.EnumerateArray().Any(item => item.ValueKind != JsonValueKind.Object))
                throw new ToolDeniedException("invalid_tool", "批量修改必须是 1 至 32 项，且仅可修改本轮新建文件。");
            edits = batch.EnumerateArray().Select(ParseEdit).ToArray();
        }
        else
            edits = [ParseEdit(args)];
        if (edits.Any(edit => edit.Before.Length == 0))
            throw new ToolDeniedException("invalid_tool", "old_text 不能为空。");
        return edits;
    }

    private static Edit ParseEdit(JsonElement value) =>
        new(RequiredString(value, "old_text"), RequiredString(value, "new_text"));

    internal static int OptionalInt(JsonElement args, string key, int fallback) =>
        !args.TryGetProperty(key, out var value) ? fallback :
        value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number) ? number :
        throw new ToolDeniedException("invalid_tool", $"工具参数 {key} 必须是整数。");

    internal sealed record Snapshot(FileStream Stream, byte[] Bytes, FileStamp Stamp) : IDisposable
    {
        public void Dispose() => Stream.Dispose();
    }

    internal readonly record struct FileStamp(WindowsWorkspace.FileIdentity Identity, string ContentHash);
    private readonly record struct Edit(string Before, string After);
}
