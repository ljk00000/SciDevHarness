using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed record GitLocations(string CommonDirectory, string IndexDirectory)
{
    public static GitLocations Resolve(string workspace, WindowsWorkspace guard, string? approvedRepository)
    {
        var local = Path.Combine(workspace, ".git");
        if (Directory.Exists(local))
            return new(local, local);
        if (!File.Exists(local))
            throw new ToolDeniedException("git_not_initialized", "请选择 Git 仓库或链接工作树的根目录。");
        if (approvedRepository is null)
            throw new ToolDeniedException("git_linked_worktree", "链接工作树需要选择主仓库以批准本次 Git 读取。");

        var line = ReadMarker(guard, ".git");
        const string marker = "gitdir: ";
        if (!line.StartsWith(marker, StringComparison.OrdinalIgnoreCase))
            throw InvalidLink();
        var repository = Path.GetFullPath(approvedRepository);
        var common = Path.Combine(repository, ".git");
        var location = Path.TrimEndingDirectorySeparator(Path.GetFullPath(line[marker.Length..], workspace));
        var prefix = Path.Combine(common, "worktrees") + Path.DirectorySeparatorChar;
        if (!location.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ||
            location.Length == prefix.Length || location[prefix.Length..].Contains(Path.DirectorySeparatorChar))
            throw InvalidLink();
        _ = new WindowsWorkspace(repository);
        var worktree = new WindowsWorkspace(location);
        if (!Same(Path.GetFullPath(ReadMarker(worktree, "commondir"), location), common) ||
            !Same(Path.GetFullPath(ReadMarker(worktree, "gitdir"), location), local))
            throw InvalidLink();
        return new(common, location);
    }

    private static string ReadMarker(WindowsWorkspace root, string name)
    {
        using var target = root.OpenOperatorPath(name)!;
        using var data = WorkspaceFiles.OpenSnapshot(target.AbsolutePath, FileShare.Read, maxBytes: 4096);
        var text = WorkspaceFiles.Utf8.GetString(data.Bytes).Trim();
        if (text.Length == 0 || text.Any(char.IsControl))
            throw InvalidLink();
        return text;
    }

    private static bool Same(string a, string b) => string.Equals(Path.TrimEndingDirectorySeparator(a),
        Path.TrimEndingDirectorySeparator(b), StringComparison.OrdinalIgnoreCase);

    private static ToolDeniedException InvalidLink() =>
        new("git_worktree_mismatch", "链接工作树与所批准的主仓库不匹配。");
}
