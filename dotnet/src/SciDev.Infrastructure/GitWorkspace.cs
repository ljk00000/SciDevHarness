using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Read-only Git queries initiated by the operator.</summary>
public static class GitWorkspace
{
    public sealed record Change(string Code, string Path, string? Original)
    {
        public string Display => Original is null ? $"{Code}  {Path}" : $"{Code}  {Original} → {Path}";
    }
    public sealed record Status(string? Branch, Change[] Changes)
    {
        public string? Head { get; init; }
    }
    public sealed record Diff(string Text, bool Truncated);

    public static async Task<Status> StatusAsync(string workspace, CancellationToken token, string? approvedRepository = null)
    {
        using var query = new GitReadContext(workspace, token, approvedRepository);
        await query.ApplyLocalCoreSettingsAsync(token);
        var branch = await query.RunAsync(["symbolic-ref", "--quiet", "--short", "HEAD"], token, maxChars: 4096);
        var head = await query.RunAsync(["rev-parse", "--verify", "--quiet", "HEAD"], token, maxChars: 256);
        var changes = await ReadChangesAsync(query, token);
        return new(branch.Code == 0 ? branch.Text.TrimEnd('\r', '\n') : null, changes.Where(IsVisibleChange).ToArray())
        {
            Head = head.Code == 0 ? head.Text.TrimEnd('\r', '\n') : null,
        };
    }

    internal static async Task<Change[]> ReadChangesAsync(GitReadContext query, CancellationToken token, bool onlyHarnessState = false)
    {
        string[] args = onlyHarnessState
            ? ["status", "--porcelain=v1", "-z", "--untracked-files=no", "--ignore-submodules=all", "--no-renames", "--", ".research"]
            : ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignore-submodules=all", "--renames", "--", ".", ":(top,exclude).research"];
        var output = await query.RunAsync(args, token);
        RequireSuccess(output);
        if (output.Truncated)
            throw new ToolDeniedException("git_limit", "Git 状态输出超出读取上限。");
        return ParseChanges(output.Text);
    }

    internal static bool IsVisibleChange(Change change) => WorkspaceDocuments.IsVisible(change.Path) &&
        (change.Original is null || WorkspaceDocuments.IsVisible(change.Original));

    internal static bool IsAddition(Change change) => change.Code[0] is '?' or 'A';

    internal static async Task<bool> HasProtectedRemovalAsync(GitReadContext query, Change[] changes, CancellationToken token) =>
        changes.Any(change => !IsVisibleChange(change) && (change.Code.Contains('D') || change.Original is not null)) ||
        (await ReadChangesAsync(query, token, onlyHarnessState: true)).Any(change => change.Code.Contains('D'));

    public static async Task<Diff> DiffAsync(string workspace, string path, string? original, CancellationToken token,
        string? approvedRepository = null)
    {
        var guard = new WindowsWorkspace(workspace);
        var selected = CheckPath(guard, path);
        var paths = original is null ? new[] { selected } : new[] { CheckPath(guard, original), selected };
        using var query = new GitReadContext(workspace, token, approvedRepository);
        await query.ApplyLocalCoreSettingsAsync(token);
        var head = await query.RunAsync(["rev-parse", "--verify", "--quiet", "HEAD"], token, maxChars: 256);
        var common = new[] { "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all" };
        var result = await query.RunAsync(["--literal-pathspecs", ..common,
            head.Code == 0 ? "HEAD" : "--cached", "--", ..paths], token);
        RequireSuccess(result);
        // An index on an unborn branch can also have unstaged edits.
        if (head.Code != 0)
        {
            var unstaged = await query.RunAsync(["--literal-pathspecs", ..common, "--", ..paths], token);
            RequireSuccess(unstaged);
            result = new(result.Code, result.Text + unstaged.Text, result.Truncated || unstaged.Truncated);
        }
        if (result.Text.Length != 0)
            return new(result.Text, result.Truncated);
        var untracked = await query.RunAsync(["--literal-pathspecs", "ls-files", "--others", "--exclude-standard",
            "-z", "--", selected], token);
        RequireSuccess(untracked);
        if (!untracked.Text.Split('\0').Contains(selected, StringComparer.Ordinal))
            return new("", false);
        var added = await query.RunAsync([..common, "--no-index", "--", "/dev/null", selected], token);
        if (added.Code != 1)
            RequireSuccess(added);
        return new(added.Text, added.Truncated);
    }

    private static string CheckPath(WindowsWorkspace guard, string path)
    {
        var relative = WorkspaceDocuments.VisiblePath(path);
        using var candidate = guard.OpenOperatorPath(relative, allowMissingParents: true);
        if (candidate is not null && WindowsWorkspace.FileExists(candidate.AbsolutePath))
        {
            using var handle = WindowsWorkspace.OpenForRead(candidate.AbsolutePath, FileShare.Read);
            WindowsWorkspace.GetFileIdentity(handle, candidate.AbsolutePath);
        }
        return relative;
    }

    private static Change[] ParseChanges(string output)
    {
        if (output.Length == 0)
            return [];
        if (output[^1] != '\0')
            throw new InvalidDataException("Git 状态记录不完整。");
        var records = output.Split('\0');
        var result = new List<Change>();
        for (var i = 0; i < records.Length - 1; i++)
        {
            var record = records[i];
            if (record.Length < 4 || record[2] != ' ')
                throw new InvalidDataException("Git 状态记录无效。");
            var code = record[..2];
            var path = record[3..];
            string? original = null;
            if (code.Contains('R') || code.Contains('C'))
            {
                if (++i == records.Length - 1 || records[i].Length == 0)
                    throw new InvalidDataException("Git 重命名记录缺少原路径。");
                original = records[i];
            }
            result.Add(new(code, path, original));
        }
        return result.ToArray();
    }

    private static void RequireSuccess(GitReadContext.Output result)
    {
        if (result.Code != 0)
            throw new ToolDeniedException("git_query_failed", "Git 无法读取所选工作区的元数据。");
    }
}

internal sealed class GitReadContext : IDisposable
{
    private const int MaxRefEntries = 10_000;
    private const int MaxObjectEntries = 60_000;
    private const long MaxMetadataBytes = 96 * 1024 * 1024;
    private readonly string _root;
    private readonly string _git;
    private readonly string _stage;
    private readonly string _stageRoot;
    private readonly string _objects;
    private readonly WindowsWorkspace _source;
    private readonly GitLocations _locations;

    internal sealed record Output(int Code, string Text, bool Truncated);

    internal string IndexCopy => Path.Combine(_stage, "index");
    internal string TaskIndex => Path.Combine(_stage, "task-index");

    public GitReadContext(string workspace, CancellationToken token, string? approvedRepository, GitLocations? validated = null,
        string? scratchWorkspace = null)
    {
        _root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        var guard = new WindowsWorkspace(_root);
        var locations = validated ?? GitLocations.Resolve(_root, guard, approvedRepository);
        _locations = locations;
        var gitDir = locations.CommonDirectory;
        var source = new WindowsWorkspace(gitDir);
        var worktree = new WindowsWorkspace(locations.IndexDirectory);
        _source = source;
        _objects = Path.Combine(gitDir, "objects");
        if (!Directory.Exists(_objects) || Path.Exists(Path.Combine(gitDir, "commondir")) ||
            Path.Exists(Path.Combine(_objects, "info", "alternates")) ||
            Path.Exists(Path.Combine(_objects, "info", "http-alternates")))
            throw new ToolDeniedException("git_external_objects", "当前 Git 视图需要仓库自有的本地对象库。");
        var count = 0;
        VerifyObjects(new DirectoryInfo(_objects), ref count, token);
        _git = GitProcess.RequireExecutable();

        _stageRoot = scratchWorkspace is null ? _root : Path.TrimEndingDirectorySeparator(Path.GetFullPath(scratchWorkspace));
        var state = (scratchWorkspace is null ? guard : new WindowsWorkspace(_stageRoot)).EnsureStateDirectory();
        using var target = state.OpenPath("scidev-git-query-" + Guid.NewGuid().ToString("N"));
        _stage = Directory.CreateDirectory(target.AbsolutePath).FullName;
        try
        {
            long remaining = MaxMetadataBytes;
            Copy(worktree, "HEAD", ref remaining);
            Copy(worktree, "index", ref remaining);
            Copy(source, "packed-refs", ref remaining);
            Copy(source, "shallow", ref remaining);
            Directory.CreateDirectory(Path.Combine(_stage, "info"));
            if (Directory.Exists(Path.Combine(gitDir, "info")))
            {
                Copy(source, "info/exclude", ref remaining);
                Copy(source, "info/attributes", ref remaining);
            }
            Directory.CreateDirectory(Path.Combine(_stage, "objects"));
            Directory.CreateDirectory(Path.Combine(_stage, "refs"));
            count = 0;
            if (Directory.Exists(Path.Combine(gitDir, "refs")))
                CopyRefs(source, new DirectoryInfo(Path.Combine(gitDir, "refs")), "refs", ref remaining, ref count, token);
            foreach (var file in Directory.EnumerateFiles(locations.IndexDirectory, "sharedindex.*"))
            {
                token.ThrowIfCancellationRequested();
                if (++count > MaxRefEntries)
                    throw new ToolDeniedException("git_limit", "Git 元数据条目过多。");
                Copy(worktree, Path.GetFileName(file), ref remaining);
            }
            File.WriteAllText(Path.Combine(_stage, "config"), "[core]\n\tbare = false\n\tfilemode = false\n\tignorecase = true\n");
            File.WriteAllText(Path.Combine(_stage, "empty-config"), "");
            Directory.CreateDirectory(Path.Combine(_stage, "disabled-hooks"));
        }
        catch
        {
            Dispose();
            throw;
        }
    }

    public async Task ApplyLocalCoreSettingsAsync(CancellationToken token)
    {
        using var config = _source.OpenPath("config");
        if (!WindowsWorkspace.FileExists(config.AbsolutePath))
            return;
        using var file = WindowsWorkspace.OpenForRead(config.AbsolutePath, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(file, config.AbsolutePath);
        using var reader = new FileStream(file, FileAccess.Read);
        if (reader.Length > 1_000_000)
            throw new ToolDeniedException("git_limit", "Git 本地配置文件超出读取上限。");
        var source = await RunAsync(["config", "--file", config.AbsolutePath, "--no-includes", "--null", "--get-regexp",
            "^(core[.](repositoryformatversion|filemode|ignorecase|symlinks|autocrlf|eol)|extensions[.](objectformat|refstorage|worktreeconfig))$"],
            token, maxChars: 32_768);
        if (source.Code is not (0 or 1) || source.Truncated)
            throw new ToolDeniedException("git_config_error", "无法读取 Git 本地文件格式设置。");

        var core = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["bare"] = "false", ["filemode"] = "false", ["ignorecase"] = "true",
        };
        var extensions = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var entry in source.Text.Split('\0', StringSplitOptions.RemoveEmptyEntries))
        {
            var separator = entry.IndexOf('\n');
            var key = separator < 0 ? entry : entry[..separator];
            var value = (separator < 0 ? "true" : entry[(separator + 1)..]).ToLowerInvariant();
            switch (key)
            {
                case "core.repositoryformatversion" when value is "0" or "1":
                case "core.eol" when value is "lf" or "crlf" or "native":
                    core[key[5..]] = value;
                    break;
                case "core.autocrlf" when value == "input" || IsBoolean(value):
                case "core.filemode" or "core.ignorecase" or "core.symlinks" when IsBoolean(value):
                    core[key[5..]] = value;
                    break;
                case "extensions.objectformat" when value is "sha1" or "sha256":
                    extensions["objectformat"] = value;
                    break;
                case "extensions.refstorage" when value == "files":
                case "extensions.worktreeconfig" when value is "false" or "no" or "off" or "0":
                    break;
                default:
                    throw new ToolDeniedException("git_config_error", "当前 Git 视图不支持此仓库的文件格式配置。");
            }
        }
        var output = new StringBuilder("[core]\n");
        foreach (var (key, value) in core)
            output.Append('\t').Append(key).Append(" = ").AppendLine(value);
        if (extensions.Count > 0)
        {
            output.AppendLine("[extensions]");
            foreach (var (key, value) in extensions)
                output.Append('\t').Append(key).Append(" = ").AppendLine(value);
        }
        await File.WriteAllTextAsync(Path.Combine(_stage, "config"), output.ToString(), token);
    }

    private static bool IsBoolean(string value) => value is "true" or "false" or "yes" or "no" or "on" or "off" or "1" or "0";

    public Task<Output> RunAsync(IReadOnlyList<string> args, CancellationToken token, int maxChars = 1_000_000, string? input = null) =>
        RunGitAsync(args, token, maxChars, input, useRepository: false);

    internal Task<Output> RunOnTaskIndexAsync(IReadOnlyList<string> args, CancellationToken token, string? input = null) =>
        RunGitAsync(args, token, 1_000_000, input, useRepository: false, indexFile: TaskIndex);

    internal async Task<Output> CompareTaskSnapshotsAsync(byte[]? before, byte[] after, int limit, CancellationToken token)
    {
        var review = CreateReviewTree();
        await File.WriteAllBytesAsync(Path.Combine(review, "before"), before ?? [], token);
        await File.WriteAllBytesAsync(Path.Combine(review, "after"), after, token);
        return await RunOnReviewTreeAsync(["--no-replace-objects", "diff", "--no-index", "--no-ext-diff", "--no-textconv",
            "--no-color", "--text", "--", "before", "after"], token, limit);
    }

    private string ReviewTree => Path.Combine(_stage, "review-tree");

    internal string CreateReviewTree() => Directory.CreateDirectory(ReviewTree).FullName;

    internal Task<Output> RunOnReviewTreeAsync(IReadOnlyList<string> args, CancellationToken token, int maxChars) =>
        RunGitAsync(args, token, maxChars, input: null, useRepository: false, workTree: ReviewTree);

    internal Task<Output> RunWorktreeAsync(IReadOnlyList<string> args, CancellationToken token) =>
        RunGitAsync(["--no-replace-objects", "-c", $"core.hooksPath={Path.Combine(_stage, "disabled-hooks")}",
            "-c", $"core.attributesFile={Path.Combine(_stage, "empty-config")}", "-c", "core.fsmonitor=false",
            "-c", "submodule.recurse=false", "-c", "core.protectNTFS=true", "-c", "core.symlinks=false",
            "-c", "gc.auto=0", "-c", "maintenance.auto=false", "worktree", ..args],
            token, 1_000_000, input: null, useRepository: true, timeoutSeconds: 60);

    internal Task<Output> PublishReferenceAsync(string reference, string oid, string previousOid, string name, string email) =>
        RunGitAsync(["--no-replace-objects", "-c", $"core.hooksPath={Path.Combine(_stage, "disabled-hooks")}",
            "-c", $"user.name={name}", "-c", $"user.email={email}", "-c", "core.logAllRefUpdates=true",
            "update-ref", "--no-deref", "-m", "commit: SciDevHarness", reference, oid, previousOid],
            CancellationToken.None, 4096, input: null, useRepository: true);

    private Task<Output> RunGitAsync(IReadOnlyList<string> args, CancellationToken token, int maxChars, string? input,
        bool useRepository, int timeoutSeconds = 10, string? workTree = null, string? indexFile = null)
    {
        var start = PrepareSnapshotProcess(workTree);
        if (useRepository)
        {
            start.Environment["GIT_DIR"] = _locations.IndexDirectory;
            start.Environment["GIT_COMMON_DIR"] = _locations.CommonDirectory;
        }
        start.Environment["GIT_WORK_TREE"] = useRepository ? _root : ".";
        if (indexFile is not null)
            start.Environment["GIT_INDEX_FILE"] = Path.GetRelativePath(start.WorkingDirectory, indexFile).Replace('\\', '/');
        return GitProcess.RunAsync(start, args, token, maxChars, input, timeoutSeconds);
    }

    internal System.Diagnostics.ProcessStartInfo PrepareSnapshotProcess(string? workTree = null)
    {
        var start = GitProcess.Prepare(_git, workTree ?? _root, Path.Combine(_stage, "empty-config"));
        // Relative paths avoid Git for Windows' GIT_DIR length limit inside nested worktrees.
        start.Environment["GIT_DIR"] = Path.GetRelativePath(start.WorkingDirectory, _stage).Replace('\\', '/');
        start.Environment["GIT_WORK_TREE"] = ".";
        start.Environment["GIT_OBJECT_DIRECTORY"] = _objects;
        return start;
    }

    private void Copy(WindowsWorkspace source, string relative, ref long remaining)
    {
        using var inputPath = source.OpenPath(relative);
        if (!WindowsWorkspace.FileExists(inputPath.AbsolutePath))
            return;
        using var handle = WindowsWorkspace.OpenForRead(inputPath.AbsolutePath, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(handle, inputPath.AbsolutePath);
        using var stream = new FileStream(handle, FileAccess.Read);
        if (stream.Length > remaining)
            throw new ToolDeniedException("git_limit", "Git 元数据副本超出读取上限。");
        using var output = File.Open(Path.Combine(_stage, relative), FileMode.CreateNew, FileAccess.Write, FileShare.None);
        stream.CopyTo(output);
        remaining -= stream.Length;
    }

    private void CopyRefs(WindowsWorkspace source, DirectoryInfo directory, string relative, ref long remaining,
        ref int count, CancellationToken token)
    {
        using var lease = source.OpenPath(relative + "/scidev-query");
        Directory.CreateDirectory(Path.Combine(_stage, relative));
        foreach (var item in directory.EnumerateFileSystemInfos())
        {
            token.ThrowIfCancellationRequested();
            if (++count > MaxRefEntries)
                throw new ToolDeniedException("git_limit", "Git 引用条目过多。");
            RejectReparse(item);
            if (item is DirectoryInfo child)
                CopyRefs(source, child, relative + "/" + item.Name, ref remaining, ref count, token);
            else
                Copy(source, relative + "/" + item.Name, ref remaining);
        }
    }

    private static void VerifyObjects(DirectoryInfo directory, ref int count, CancellationToken token)
    {
        RejectReparse(directory);
        foreach (var item in directory.EnumerateFileSystemInfos())
        {
            token.ThrowIfCancellationRequested();
            if (++count > MaxObjectEntries)
                throw new ToolDeniedException("git_limit", "Git 对象条目过多。");
            RejectReparse(item);
            if (item is DirectoryInfo child)
                VerifyObjects(child, ref count, token);
        }
    }

    private static void RejectReparse(FileSystemInfo item)
    {
        if ((item.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new ToolDeniedException("git_linked_metadata", "Git 元数据包含链接，未执行查询。");
    }

    public void Dispose()
    {
        var folder = new DirectoryInfo(_stage);
        if (folder.Parent?.FullName.Equals(Path.Combine(_stageRoot, ".research"), StringComparison.OrdinalIgnoreCase) == true &&
            folder.Name.StartsWith("scidev-git-query-", StringComparison.Ordinal) &&
            (folder.Attributes & FileAttributes.ReparsePoint) == 0)
            folder.Delete(recursive: true);
    }
}
