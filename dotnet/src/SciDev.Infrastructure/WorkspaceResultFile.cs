using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Publish a CLI result at an operator-selected path in the active workspace.</summary>
public static class WorkspaceResultFile
{
    private static readonly HashSet<string> StateNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "settings.json", "provider.json", "sessions", "checkpoints", "worktrees", "runs.db", "runs.db-wal", "runs.db-shm",
        "runs.db-journal", "mcp.json", DesktopPreferences.FileName, "desktop-queue.json",
    };

    public static void Write(string workspace, string path, string json) => Publish(workspace, path, _ => json, WorkspaceFiles.MaxBytes);

    public static void ValidatePath(string workspace, string path) => _ = RelativePath(workspace, path);

    public static string Publish(string workspace, string path, Func<string?, string> payload, int maxBytes)
    {
        var root = Path.GetFullPath(workspace);
        var relative = RelativePath(root, path);
        var parts = relative.Split('/');
        var guard = new WindowsWorkspace(root);
        var prefix = "";
        foreach (var part in parts[..^1])
        {
            prefix += part;
            using var directory = guard.OpenOperatorPath(prefix)!;
            Directory.CreateDirectory(directory.AbsolutePath);
            prefix += '/';
        }

        using var target = guard.OpenOperatorPath(relative)!;
        WorkspaceFiles.FileStamp? existing = null;
        string? previous = null;
        if (WindowsWorkspace.FileExists(target.AbsolutePath))
        {
            using var snapshot = WorkspaceFiles.OpenSnapshot(target.AbsolutePath, FileShare.Read, maxBytes: maxBytes);
            existing = snapshot.Stamp;
            previous = Encoding.UTF8.GetString(snapshot.Bytes);
        }
        var json = payload(previous);
        var bytes = Encoding.UTF8.GetBytes(json + "\n");
        if (bytes.Length > maxBytes)
            throw new ToolDeniedException("file_error", "结果文件超过大小限制。");
        WorkspaceFiles.Save(target.AbsolutePath, bytes, CancellationToken.None, existing, maxCurrentBytes: maxBytes);
        return json;
    }

    private static string RelativePath(string workspace, string path)
    {
        var root = Path.GetFullPath(workspace);
        var target = Path.GetFullPath(path, root);
        var relative = WindowsWorkspace.NormalizeOperatorPath(Path.GetRelativePath(root, target));
        var parts = relative.Split('/');
        if (parts.Length > 1 && parts[0].Equals(".research", StringComparison.OrdinalIgnoreCase))
        {
            _ = WorkspaceDocuments.VisiblePath(relative[(parts[0].Length + 1)..]);
            if (StateNames.Contains(parts[1]) || parts[1].StartsWith("csharp-", StringComparison.OrdinalIgnoreCase) ||
                parts[1].StartsWith("scidev-", StringComparison.OrdinalIgnoreCase))
                throw new ToolDeniedException("path_denied", "结果文件不可覆盖工作区内部状态。");
        }
        else
            _ = WorkspaceDocuments.VisiblePath(relative);
        return relative;
    }
}
