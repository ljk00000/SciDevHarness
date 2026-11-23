using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class WorkspaceGitSummary
{
    private const int MaxPaths = 160;
    private const int MaxFileBytes = 256 * 1024;
    private const int MaxTotalBytes = 2 * 1024 * 1024;

    internal static void Append(StringBuilder output, WindowsWorkspace workspace, WorkspaceGitIndex index, WorkspaceDrafts drafts,
        string scope, string[] paths, Stopwatch elapsed, TimeSpan budget, CancellationToken token)
    {
        output.AppendLine().Append("Git 索引");
        if (index.Branch is { } branch)
            output.Append(" · ").Append(branch);
        if (index.Unavailable is { } note)
        {
            output.Append(" · ").AppendLine(note);
            return;
        }
        var tracked = index.Entries.Where(entry => InScope(entry.Path, scope)).ToArray();
        var untracked = paths.Where(path => InScope(path, scope) && !index.Tracks(path, false)).ToArray();
        output.Append(" · 索引路径 ").Append(tracked.Length).Append(" · 扫描到的未跟踪文件 ").AppendLine(untracked.Length.ToString());
        output.AppendLine("仅比较工作树原始文件与索引，尚未比较提交或应用 Git 的内容过滤。");
        var shown = 0;
        var checkedPaths = 0;
        var bytes = 0;
        var skipped = 0;
        foreach (var entry in tracked)
        {
            token.ThrowIfCancellationRequested();
            if (checkedPaths >= MaxPaths || bytes >= MaxTotalBytes || shown >= 10 || elapsed.Elapsed >= budget)
                break;
            checkedPaths++;
            if (drafts.Get(entry.Path) is not null || entry.Sparse)
            {
                skipped++;
                continue;
            }
            if (entry.Conflict || entry.Intent)
            {
                output.Append(entry.Conflict ? "  合并索引条目 · " : "  意向添加 · ").AppendLine(entry.Path);
                shown++;
                continue;
            }
            var state = Compare(workspace, entry, ref bytes);
            if (state is null)
                skipped++;
            else if (state.Length > 0)
            {
                output.Append("  ").Append(state).Append(" · ").AppendLine(entry.Path);
                shown++;
            }
        }
        if (checkedPaths < tracked.Length || skipped > 0)
            output.AppendLine("  比对未覆盖全部索引文件（预算、草稿、稀疏或不可读路径）。");
        var visibleNew = untracked.Where(path => drafts.Get(path) is null).Take(8);
        foreach (var path in visibleNew)
            output.Append("  未跟踪 · ").AppendLine(path);
    }

    private static bool InScope(string path, string scope) => scope == "." ||
        path.Equals(scope, StringComparison.OrdinalIgnoreCase) || path.StartsWith(scope + "/", StringComparison.OrdinalIgnoreCase);

    private static string? Compare(WindowsWorkspace workspace, WorkspaceGitIndex.Entry entry, ref int total)
    {
        try
        {
            using var path = workspace.OpenOperatorPath(entry.Path, allowMissingParents: true);
            if (path is null || !WindowsWorkspace.FileExists(path.AbsolutePath))
                return "工作树缺失";
            using var handle = WindowsWorkspace.OpenForRead(path.AbsolutePath, FileShare.Read);
            WindowsWorkspace.GetFileIdentity(handle, path.AbsolutePath);
            using var stream = new FileStream(handle, FileAccess.Read);
            if (stream.Length > Math.Min(MaxFileBytes, MaxTotalBytes - total))
                return null;
            var bytes = new byte[(int)stream.Length];
            stream.ReadExactly(bytes);
            total += bytes.Length;
            using var digest = IncrementalHash.CreateHash(entry.ObjectId.Length == 20 ? HashAlgorithmName.SHA1 : HashAlgorithmName.SHA256);
            digest.AppendData(Encoding.ASCII.GetBytes("blob " + bytes.Length.ToString(CultureInfo.InvariantCulture) + "\0"));
            digest.AppendData(bytes);
            return digest.GetHashAndReset().AsSpan().SequenceEqual(entry.ObjectId) ? "" : "原始内容与索引不同";
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException)
        {
            return null;
        }
    }
}
