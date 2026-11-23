using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed partial class WorkspaceFiles
{
    private async ValueTask<ToolResult> RenameAsync(string path, string destination, CancellationToken token)
    {
        if (string.Equals(path, destination, StringComparison.OrdinalIgnoreCase))
            throw new ToolDeniedException("rename_conflict", "源路径和目标路径必须是不同的文件名。");
        if (!string.IsNullOrEmpty(credential) && (path.Contains(credential, StringComparison.Ordinal) ||
            destination.Contains(credential, StringComparison.Ordinal)))
            throw new ToolDeniedException("checkpoint_credential", "文件路径包含当前模型凭据。");

        drafts?.RequireWritable(path);
        drafts?.RequireWritable(destination);
        var sourceGuide = UpdateGuidance(path, token);
        var destinationGuide = UpdateGuidance(destination, token);
        if (sourceGuide.Length != 0 || destinationGuide.Length != 0)
            return new("本次尚未移动文件。先阅读源目录和目标目录说明，再重新提交操作。\n\n" +
                string.Join("\n\n", new[] { sourceGuide, destinationGuide }.Where(guide => guide.Length != 0).Distinct()));

        FileStamp expected;
        using (var source = _workspace.OpenPath(path))
        using (var target = _workspace.OpenPath(destination))
        {
            RequireVacantRenameTarget(target.AbsolutePath);
            using var original = ReadSnapshot(WindowsWorkspace.OpenForRead(source.AbsolutePath, FileShare.Read),
                source.AbsolutePath, FileAccess.Read, MaxBytes, allowBinary: true);
            expected = original.Stamp;
        }

        if (approval is null || !await approval.ApproveAsync(new(path, 0, path, destination, destination), token))
            throw new ToolDeniedException("approval_denied", "重命名未获批准。");

        token.ThrowIfCancellationRequested();
        using var currentSource = _workspace.OpenPath(path);
        using var currentTarget = _workspace.OpenPath(destination);
        RequireVacantRenameTarget(currentTarget.AbsolutePath);
        if (!WindowsWorkspace.FileExists(currentSource.AbsolutePath))
            throw new ToolDeniedException("edit_conflict", "源文件在审阅期间已移走。");
        using var current = ReadSnapshot(WindowsWorkspace.OpenForRecovery(currentSource.AbsolutePath, delete: true),
            currentSource.AbsolutePath, FileAccess.Read, MaxBytes, allowBinary: true);
        if (current.Stamp != expected)
            throw new ToolDeniedException("edit_conflict", "源文件内容或身份在审阅期间已变化。");
        token.ThrowIfCancellationRequested();
        checkpoint?.PrepareRename(path, destination, current.Stamp, current.Bytes, token);
        WindowsWorkspace.RenameWithoutReplace(current.Stream.SafeFileHandle, currentTarget.AbsolutePath);

        _created.Remove(destination);
        if (_created.Remove(path, out var owned) && owned == current.Stamp)
            _created.Add(destination, owned);
        return new($"已重命名 {path} → {destination}。", new(path, "delete"),
            RelatedChanges: [new(destination, "write")]);
    }

    private static void RequireVacantRenameTarget(string path)
    {
        if (WindowsWorkspace.FileExists(path))
            throw new ToolDeniedException("rename_conflict", "重命名目标已存在。");
    }
}
