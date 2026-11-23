using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Apply a user-selected path to a scratch index before publishing it under Git's index lock.</summary>
public static class GitIndexEditor
{
    private const int MaxIndexBytes = 96 * 1024 * 1024;
    private const int MaxBulkPaths = 256;
    public sealed record Result(string Status, IReadOnlyList<string> Paths, int Skipped = 0);

    public static Task<Result> ChangeAsync(string workspace, string path, string? original, bool stage,
        CancellationToken token, string? approvedRepository = null)
    {
        var requested = original is null ? new[] { path } : new[] { path, original };
        var selected = requested.Select(WorkspaceDocuments.VisiblePath).Distinct(StringComparer.Ordinal).ToArray();
        return ChangeIndexAsync(workspace, selected, stage, token, approvedRepository);
    }

    public static Task<Result> StageAllAsync(string workspace, CancellationToken token, string? approvedRepository = null) =>
        ChangeIndexAsync(workspace, null, true, token, approvedRepository);

    private static async Task<Result> ChangeIndexAsync(string workspace, string[]? selected, bool stage,
        CancellationToken token, string? approvedRepository)
    {
        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        var guard = new WindowsWorkspace(root);
        var locations = GitLocations.Resolve(root, guard, approvedRepository);
        var metadata = new WindowsWorkspace(locations.IndexDirectory);
        using var index = metadata.OpenPath("index");
        using var lockPath = metadata.OpenPath("index.lock");
        using var lease = LockIndex(lockPath.AbsolutePath);
        var published = false;
        var pins = new List<PinnedFile>();
        try
        {
            var initial = ReadIndexStamp(index.AbsolutePath);
            using var query = new GitReadContext(root, token, approvedRepository, locations);
            await query.ApplyLocalCoreSettingsAsync(token);
            var (paths, skipped) = selected is null ? await BulkPathsAsync(query, token) : (selected, 0);
            if (paths.Length == 0)
                return new("nothing_to_stage", [], skipped);
            var config = new[] { "--literal-pathspecs", "-c", "core.splitIndex=false" };
            var indexed = Records(await query.RunAsync([..config, "ls-files", "-z", "--", ..paths], token));
            RequireOnlySelected(indexed, paths);

            if (stage)
            {
                var present = new List<string>();
                foreach (var path in paths)
                {
                    token.ThrowIfCancellationRequested();
                    if (PinFile(guard, path) is not { } file)
                        continue;
                    pins.Add(file);
                    present.Add(path);
                }
                if (present.Count > 0)
                {
                    var attributes = Records(await query.RunAsync([..config, "check-attr", "-z", "filter", "--", ..present], token));
                    if (attributes.Length != present.Count * 3 ||
                        attributes.Where((_, index) => index % 3 == 2).Any(value => value is not ("unspecified" or "unset")))
                        throw new ToolDeniedException("git_filter_required", "所选文件需要 Git 清理过滤器，请使用本地 Git 处理。");
                    Verify(await query.RunAsync([..config, "add", "--", ..present], token));
                }
                var deleted = paths.Except(present, StringComparer.Ordinal).Where(indexed.Contains).ToArray();
                if (deleted.Length > 0)
                    Verify(await query.RunAsync([..config, "update-index", "--force-remove", "--", ..deleted], token));
                if (present.Count == 0 && deleted.Length == 0)
                    throw new ToolDeniedException("git_path_missing", "所选文件和索引条目均已不存在。");
            }
            else
            {
                var head = await query.RunAsync([..config, "rev-parse", "--verify", "--quiet", "HEAD"], token, maxChars: 256);
                if (head.Code == 0)
                {
                    var tracked = Records(await query.RunAsync([..config, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ..paths], token));
                    RequireOnlySelected(tracked, paths);
                    Verify(await query.RunAsync([..config, "reset", "--quiet", "--no-refresh", "HEAD", "--", ..paths], token));
                }
                else if (head.Code == 1)
                {
                    Verify(await query.RunAsync([..config, "rm", "--cached", "--ignore-unmatch", "--force", "--", ..paths], token));
                }
                else
                    Verify(head);
            }

            token.ThrowIfCancellationRequested();
            if (ReadIndexStamp(index.AbsolutePath) != initial)
                throw new ToolDeniedException("git_index_changed", "Git 索引在操作期间发生变化，请刷新后重试。");
            if (!File.Exists(query.IndexCopy))
                return new("no_index_change", paths, skipped);
            using (var copy = File.OpenRead(query.IndexCopy))
            {
                if (copy.Length > MaxIndexBytes)
                    throw new ToolDeniedException("git_limit", "更新后的 Git 索引超出读取上限。");
                await copy.CopyToAsync(lease, token);
            }
            lease.Flush(flushToDisk: true);
            token.ThrowIfCancellationRequested();
            if (ReadIndexStamp(index.AbsolutePath) != initial)
                throw new ToolDeniedException("git_index_changed", "Git 索引在发布前发生变化，请刷新后重试。");
            lease.Dispose();
            File.Move(lockPath.AbsolutePath, index.AbsolutePath, overwrite: initial is not null);
            published = true;
            return new(stage ? "staged" : "unstaged", paths, skipped);
        }
        finally
        {
            foreach (var pin in pins)
                pin.Dispose();
            lease.Dispose();
            if (!published)
                File.Delete(lockPath.AbsolutePath);
        }
    }

    private static async Task<(string[] Paths, int Skipped)> BulkPathsAsync(GitReadContext query, CancellationToken token)
    {
        var changes = await GitWorkspace.ReadChangesAsync(query, token);
        var pending = changes.Where(change => change.Code[1] != ' ').ToArray();
        var protectedRemoval = pending.Any(GitWorkspace.IsAddition) && await GitWorkspace.HasProtectedRemovalAsync(query, changes, token);

        var eligible = pending.Where(change => GitWorkspace.IsVisibleChange(change) &&
            change.Code is not ("DD" or "AU" or "UD" or "UA" or "DU" or "AA" or "UU") &&
            !(protectedRemoval && GitWorkspace.IsAddition(change))).ToArray();
        var paths = eligible.SelectMany(change => change.Code[1] is 'R' or 'C' && change.Original is { } previous
            ? new[] { change.Path, previous } : [change.Path]).Distinct(StringComparer.Ordinal).ToArray();
        // Keep the literal argument list within Windows' process command-line limit.
        if (paths.Length > MaxBulkPaths || paths.Sum(path => path.Length + 3) > 24_000)
            throw new ToolDeniedException("git_limit", "待暂存路径过多，请分批选择。");
        return (paths, pending.Length - eligible.Length);
    }

    internal static FileStream LockIndex(string path, bool deleteOnClose = false)
    {
        FileStream stream;
        try
        {
            stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None, bufferSize: 4096,
                deleteOnClose ? FileOptions.DeleteOnClose : FileOptions.None);
        }
        catch (IOException ex) when ((ex.HResult & 0xFFFF) is 80 or 183 or 32)
        {
            throw new ToolDeniedException("git_index_busy", "Git 索引已锁定，请在其他操作结束后重试。");
        }
        try
        {
            WindowsWorkspace.GetFileIdentity(stream.SafeFileHandle, path);
            return stream;
        }
        catch
        {
            stream.Dispose();
            if (!deleteOnClose)
                File.Delete(path);
            throw;
        }
    }

    internal static (WindowsWorkspace.FileIdentity Identity, string Hash)? ReadIndexStamp(string path)
    {
        if (!WindowsWorkspace.FileExists(path))
            return null;
        using var handle = WindowsWorkspace.OpenForRead(path, FileShare.Read);
        var identity = WindowsWorkspace.GetFileIdentity(handle, path);
        using var index = new FileStream(handle, FileAccess.Read);
        if (index.Length > MaxIndexBytes)
            throw new ToolDeniedException("git_limit", "Git 索引超出读取上限。");
        return (identity, Convert.ToHexString(SHA256.HashData(index)));
    }

    private static PinnedFile? PinFile(WindowsWorkspace workspace, string path)
    {
        var parent = workspace.OpenOperatorPath(path, allowMissingParents: true);
        if (parent is null)
            return null;
        SafeFileHandle? handle = null;
        try
        {
            if (!WindowsWorkspace.FileExists(parent.AbsolutePath))
                return null;
            handle = WindowsWorkspace.OpenForRead(parent.AbsolutePath, FileShare.Read);
            WindowsWorkspace.GetFileIdentity(handle, parent.AbsolutePath);
            var result = new PinnedFile(parent, handle);
            parent = null;
            handle = null;
            return result;
        }
        finally
        {
            handle?.Dispose();
            parent?.Dispose();
        }
    }

    private sealed record PinnedFile(WindowsWorkspace.Lease Parent, SafeFileHandle Handle) : IDisposable
    {
        public void Dispose()
        {
            Handle.Dispose();
            Parent.Dispose();
        }
    }

    private static string[] Records(GitReadContext.Output output)
    {
        Verify(output);
        if (output.Text.Length == 0)
            return [];
        if (!output.Text.EndsWith('\0'))
            throw new ToolDeniedException("git_index_error", "Git 返回了不完整的索引记录。");
        return output.Text[..^1].Split('\0');
    }

    private static void RequireOnlySelected(string[] actual, string[] selected)
    {
        if (actual.Except(selected, StringComparer.Ordinal).Any())
            throw new ToolDeniedException("git_path_scope", "Git 路径匹配了整个目录，请选择具体文件。");
    }

    private static void Verify(GitReadContext.Output output)
    {
        if (output.Code != 0 || output.Truncated)
            throw new ToolDeniedException("git_index_error", "Git 未能准备所选路径的索引更改。");
    }
}
