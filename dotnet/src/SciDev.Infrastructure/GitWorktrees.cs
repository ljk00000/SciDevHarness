using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Manage explicitly requested Git worktrees rooted in the workspace state directory.</summary>
public static class GitWorktrees
{
    private const string BranchPrefix = "scidev/agent-";
    private const string Registry = "worktrees";

    public sealed record Entry(string Path, string? Branch, string? Head, bool Locked, bool Prunable)
    {
        public string Display => $"{Branch ?? "分离 HEAD"} · {Path}" + (Prunable ? "（路径失效）" : Locked ? "（已锁定）" : "");
    }
    public sealed record Created(string Path, string Branch, string BaseSha, string Repository);
    public sealed record Removed(string Status, string Path, string Branch);

    public static async Task<Created> CreateAsync(string workspace, string id, CancellationToken token,
        string? approvedRepository = null)
    {
        ValidateId(id);
        var root = Normalize(workspace);
        var guard = new WindowsWorkspace(root);
        var locations = GitLocations.Resolve(root, guard, approvedRepository);
        var repository = Path.GetDirectoryName(locations.CommonDirectory)!;
        var metadata = new WindowsWorkspace(locations.CommonDirectory);
        var branch = BranchPrefix + id;
        var reference = $"refs/heads/{branch}";
        using var query = new GitReadContext(root, token, approvedRepository, locations);
        using var leases = new GitMetadataFiles();
        var config = await LoadConfigAsync(query, metadata, leases, token);
        RequireNoExternalFilters(config);

        var check = await query.RunAsync(["check-ref-format", reference], token, maxChars: 256);
        if (check.Code != 0 || check.Truncated)
            throw new ArgumentException("工作树 ID 不能组成有效分支名。");
        var parent = await query.RunAsync(["--no-replace-objects", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            token, maxChars: 256);
        var baseSha = parent.Text.TrimEnd('\r', '\n');
        if (parent.Code != 0 || parent.Truncated || !IsOid(baseSha))
            throw new ToolDeniedException("git_worktree_no_base", "请先创建基线提交。");
        await ValidateTreeAsync(query, baseSha, config, token);

        var branchExists = await query.RunAsync(["show-ref", "--verify", "--quiet", reference], token, maxChars: 256);
        if (branchExists.Code == 0)
            throw new ToolDeniedException("git_worktree_branch_exists", "工作树分支已存在，请选择新的 ID。");
        if (branchExists.Code != 1)
            throw new ToolDeniedException("git_worktree_registry", "无法核对工作树分支。");

        var state = guard.EnsureStateDirectory();
        using var container = state.OpenPath(Registry);
        Directory.CreateDirectory(container.AbsolutePath);
        var worktrees = new WindowsWorkspace(container.AbsolutePath);
        using var destination = worktrees.OpenPath(id);
        if (Path.Exists(destination.AbsolutePath))
            throw new ToolDeniedException("git_worktree_path_exists", "工作树目录已存在，请选择新的 ID。");
        if (destination.AbsolutePath.Length >= 243)
            throw new ArgumentException("工作树目录过长，请使用较短的工作区路径或 ID。");

        HoldRegistry(metadata, leases, create: true, token);
        leases.CheckWritableRef(metadata, reference);
        leases.HoldFile(metadata, $"logs/{reference}", mayAppend: true, createParents: true);
        token.ThrowIfCancellationRequested();
        var result = await query.RunWorktreeAsync(["add", "-b", branch, "--", destination.AbsolutePath, baseSha], token);
        if (result.Code != 0 || result.Truncated)
            throw new ToolDeniedException("git_worktree_create_failed", "Git 未完成创建。请查看工作树列表并核对目标目录。");
        CheckOpenPath(destination.AbsolutePath, repository);
        using var created = new GitReadContext(destination.AbsolutePath, token, repository, scratchWorkspace: root);
        await created.ApplyLocalCoreSettingsAsync(token);
        var head = await created.RunAsync(["rev-parse", "--verify", "--quiet", "HEAD"], token, maxChars: 256);
        var selected = await created.RunAsync(["symbolic-ref", "--quiet", "--no-recurse", "HEAD"], token, maxChars: 4096);
        if (head.Code != 0 || selected.Code != 0 || head.Text.TrimEnd('\r', '\n') != baseSha ||
            selected.Text.TrimEnd('\r', '\n') != reference)
            throw new ToolDeniedException("git_worktree_incomplete", "工作树已注册，请核对分支和基线。");
        return new(destination.AbsolutePath, branch, baseSha, repository);
    }

    public static async Task<Entry[]> ListAsync(string workspace, CancellationToken token, string? approvedRepository = null)
    {
        var root = Normalize(workspace);
        var locations = GitLocations.Resolve(root, new WindowsWorkspace(root), approvedRepository);
        var metadata = new WindowsWorkspace(locations.CommonDirectory);
        using var leases = new GitMetadataFiles();
        using var query = new GitReadContext(root, token, approvedRepository, locations);
        _ = await LoadConfigAsync(query, metadata, leases, token);
        HoldRegistry(metadata, leases, create: false, token);
        var output = await query.RunWorktreeAsync(["list", "--porcelain", "-z"], token);
        if (output.Code != 0 || output.Truncated || !output.Text.EndsWith('\0'))
            throw new ToolDeniedException("git_worktree_registry", "无法读取 Git 工作树列表。");
        return ParseList(output.Text);
    }

    public static string CheckOpenPath(string path, string approvedRepository)
    {
        var target = Normalize(path);
        var repository = Normalize(approvedRepository);
        var locations = GitLocations.Resolve(target, new WindowsWorkspace(target), repository);
        if (!string.Equals(locations.CommonDirectory, Path.Combine(repository, ".git"), StringComparison.OrdinalIgnoreCase))
            throw new ToolDeniedException("git_worktree_mismatch", "选中的目录已不属于此仓库。");
        return target;
    }

    public static string? ManagedId(string workspace, Entry item)
    {
        var expected = Path.Combine(Normalize(workspace), ".research", Registry);
        var parent = Path.GetDirectoryName(item.Path);
        var id = Path.GetFileName(item.Path);
        return string.Equals(expected, parent, StringComparison.OrdinalIgnoreCase) && item.Branch == BranchPrefix + id &&
            !item.Locked && !item.Prunable ? id : null;
    }

    internal static string ManagedPath(string workspace, string id)
    {
        ValidateId(id);
        return Path.Combine(Normalize(workspace), ".research", Registry, id);
    }

    public static async Task<Created> OpenManagedAsync(string workspace, string id, string baseSha, CancellationToken token,
        string? approvedRepository = null)
    {
        var path = ManagedPath(workspace, id);
        if (!IsOid(baseSha))
            throw new ToolDeniedException("git_worktree_mismatch", "会话的旧工作树基线无效。");
        var entry = (await ListAsync(workspace, token, approvedRepository)).SingleOrDefault(item =>
            ManagedId(workspace, item) == id && item.Path.Equals(path, StringComparison.OrdinalIgnoreCase));
        if (entry is null)
            throw new ToolDeniedException("git_worktree_mismatch", "会话原工作树已移除、锁定或更换分支。");
        var root = Normalize(workspace);
        var repository = Path.GetDirectoryName(GitLocations.Resolve(root, new WindowsWorkspace(root), approvedRepository).CommonDirectory)!;
        return new(CheckOpenPath(path, repository), entry.Branch!, baseSha, repository);
    }

    public static async Task<Removed> RemoveAsync(string workspace, string id, CancellationToken token,
        string? approvedRepository = null)
    {
        ValidateId(id);
        var root = Normalize(workspace);
        var guard = new WindowsWorkspace(root);
        var locations = GitLocations.Resolve(root, guard, approvedRepository);
        var repository = Path.GetDirectoryName(locations.CommonDirectory)!;
        var common = new WindowsWorkspace(locations.CommonDirectory);
        using var target = guard.OpenOperatorPath($".research/{Registry}/{id}")!;
        var path = target.AbsolutePath;
        var branch = BranchPrefix + id;
        var linked = GitLocations.Resolve(path, new WindowsWorkspace(path), repository);
        if (!string.Equals(linked.CommonDirectory, locations.CommonDirectory, StringComparison.OrdinalIgnoreCase) ||
            string.Equals(linked.IndexDirectory, linked.CommonDirectory, StringComparison.OrdinalIgnoreCase))
            throw new ToolDeniedException("git_worktree_mismatch", "只可回收此仓库创建的链接工作树。");

        using var query = new GitReadContext(root, token, approvedRepository, locations);
        using var leases = new GitMetadataFiles();
        var config = await LoadConfigAsync(query, common, leases, token);
        RequireNoExternalFilters(config);
        HoldRegistry(common, leases, create: false, token, removing: linked.IndexDirectory);
        var records = await query.RunWorktreeAsync(["list", "--porcelain", "-z"], token);
        if (records.Code != 0 || records.Truncated ||
            !ParseList(records.Text).Any(item => string.Equals(item.Path, path, StringComparison.OrdinalIgnoreCase) &&
                ManagedId(root, item) == id))
            throw new ToolDeniedException("git_worktree_mismatch", "请确认目标仍是未锁定的托管工作树。");
        using (var check = new GitReadContext(path, token, repository, linked, scratchWorkspace: root))
        {
            await check.ApplyLocalCoreSettingsAsync(token);
            var dirty = await check.RunAsync(["status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignored=traditional", "--ignore-submodules=none"], token, maxChars: 16384);
            if (dirty.Code != 0 || dirty.Truncated || dirty.Text.Length > 0)
                throw new ToolDeniedException("git_worktree_dirty", "工作树含改动、未跟踪文件或运行记录，请先保留这些内容。");
        }
        CheckOpenPath(path, repository);
        ValidateRemovalTree(new DirectoryInfo(path), token);
        token.ThrowIfCancellationRequested();
        var removed = await query.RunWorktreeAsync(["remove", "--", path], token);
        if (removed.Code != 0 || removed.Truncated || Path.Exists(path) || Directory.Exists(linked.IndexDirectory))
            throw new ToolDeniedException("git_worktree_remove_failed", "Git 未完成工作树回收，请重新核对目录和登记状态。");
        return new("removed", path, branch);
    }

    public static void ValidateId(string id)
    {
        if (id.Length is < 1 or > 40 || id[0] is '.' or '-' || id[^1] == '.' ||
            !id.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is '.' or '-' or '_'))
            throw new ArgumentException("工作树 ID 使用 1 到 40 个字母、数字、点、短横线或下划线。");
        _ = WindowsWorkspace.NormalizeOperatorPath(id);
    }

    private static async Task<Dictionary<string, string>> LoadConfigAsync(GitReadContext query, WindowsWorkspace metadata,
        GitMetadataFiles leases, CancellationToken token)
    {
        var config = leases.HoldFile(metadata, "config", required: true);
        await query.ApplyLocalCoreSettingsAsync(token);
        return await GitRepositoryConfig.ReadAsync(query, config, token);
    }

    private static void HoldRegistry(WindowsWorkspace metadata, GitMetadataFiles leases, bool create, CancellationToken token,
        string? removing = null)
    {
        using var registry = metadata.OpenPath(Registry);
        if (create)
            Directory.CreateDirectory(registry.AbsolutePath);
        if (!Directory.Exists(registry.AbsolutePath))
            return;
        var records = new WindowsWorkspace(registry.AbsolutePath);
        leases.HoldFile(records, "scidev-worktree-check");
        var count = 0;
        foreach (var entry in new DirectoryInfo(registry.AbsolutePath).EnumerateFileSystemInfos())
        {
            token.ThrowIfCancellationRequested();
            if ((entry.Attributes & FileAttributes.ReparsePoint) != 0 || ++count > 4096)
                throw new ToolDeniedException("git_worktree_registry", "工作树登记目录包含链接或超出读取上限。");
            if (entry is not DirectoryInfo)
                continue;
            if (string.Equals(entry.FullName, removing, StringComparison.OrdinalIgnoreCase))
                continue;
            foreach (var marker in new[] { "HEAD", "commondir", "gitdir", "locked" })
                leases.HoldFile(records, $"{entry.Name}/{marker}");
        }
    }

    private static void RequireNoExternalFilters(Dictionary<string, string> config)
    {
        if (config.Any(item => item.Key.StartsWith("filter.", StringComparison.OrdinalIgnoreCase) &&
            (item.Key.EndsWith(".clean", StringComparison.OrdinalIgnoreCase) ||
             item.Key.EndsWith(".smudge", StringComparison.OrdinalIgnoreCase) ||
             item.Key.EndsWith(".process", StringComparison.OrdinalIgnoreCase) ||
             item.Key.EndsWith(".required", StringComparison.OrdinalIgnoreCase)) &&
            !string.IsNullOrWhiteSpace(item.Value)))
            throw new ToolDeniedException("git_worktree_filter", "此仓库配置了工作树过滤器，请使用本地 Git。");
    }

    private static void ValidateRemovalTree(DirectoryInfo root, CancellationToken token)
    {
        var directories = new Stack<DirectoryInfo>();
        directories.Push(root);
        var entries = 0;
        while (directories.TryPop(out var directory))
        {
            if ((directory.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new ToolDeniedException("git_worktree_path", "工作树含文件链接，请使用本地 Git 核对。");
            foreach (var file in directory.EnumerateFileSystemInfos())
            {
                token.ThrowIfCancellationRequested();
                if (++entries > 60_000 || (file.Attributes & (FileAttributes.ReparsePoint | FileAttributes.Device)) != 0)
                    throw new ToolDeniedException("git_worktree_path", "工作树含链接或条目过多，请使用本地 Git 核对。");
                if (file is DirectoryInfo child)
                    directories.Push(child);
            }
        }
    }

    private static async Task ValidateTreeAsync(GitReadContext query, string baseSha, Dictionary<string, string> config,
        CancellationToken token)
    {
        var listed = await query.RunAsync(["--no-replace-objects", "ls-tree", "-r", "-z", "--full-tree", baseSha], token);
        if (listed.Code != 0 || listed.Truncated || listed.Text.Length > 0 && !listed.Text.EndsWith('\0'))
            throw new ToolDeniedException("git_worktree_tree", "无法核对基线提交的完整文件列表。");
        var windowsPaths = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in listed.Text.Split('\0', StringSplitOptions.RemoveEmptyEntries))
        {
            token.ThrowIfCancellationRequested();
            var separator = entry.IndexOf('\t');
            if (separator < 0)
                throw new ToolDeniedException("git_worktree_tree", "Git 文件列表格式无效。");
            if (entry.StartsWith("120000 ", StringComparison.Ordinal) &&
                config.GetValueOrDefault("core.symlinks")?.ToLowerInvariant() is "true" or "yes" or "on" or "1")
                throw new ToolDeniedException("git_worktree_symlink", "此仓库要求检出文件链接，请使用本地 Git。");
            var path = entry[(separator + 1)..];
            if (WindowsWorkspace.NormalizeOperatorPath(path) != path ||
                path.Split('/').Contains(".git", StringComparer.OrdinalIgnoreCase))
                throw new ToolDeniedException("git_worktree_tree", "提交包含不适合 Windows 工作树的路径。");
            var prefix = "";
            foreach (var part in path.Split('/'))
            {
                prefix = prefix.Length == 0 ? part : $"{prefix}/{part}";
                if (windowsPaths.TryGetValue(prefix, out var prior) && prior != prefix)
                    throw new ToolDeniedException("git_worktree_tree", "提交包含在 Windows 上重叠的文件名。");
                windowsPaths[prefix] = prefix;
            }
        }
    }

    private static Entry[] ParseList(string text)
    {
        var result = new List<Entry>();
        string? path = null, branch = null, head = null;
        var locked = false;
        var prunable = false;
        foreach (var field in text.Split('\0'))
        {
            var separator = field.IndexOf(' ');
            var key = separator < 0 ? field : field[..separator];
            var value = separator < 0 ? "" : field[(separator + 1)..];
            switch (key)
            {
                case "worktree": path = Normalize(value); break;
                case "branch" when value.StartsWith("refs/heads/", StringComparison.Ordinal): branch = value["refs/heads/".Length..]; break;
                case "HEAD" when IsOid(value) && value.Any(ch => ch != '0'): head = value; break;
                case "locked": locked = true; break;
                case "prunable": prunable = true; break;
                case "":
                    if (path is not null)
                        result.Add(new(path, branch, head, locked, prunable));
                    path = branch = head = null;
                    locked = prunable = false;
                    break;
            }
        }
        return result.ToArray();
    }

    private static string Normalize(string path) => Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
    private static bool IsOid(string oid) => oid.Length is (40 or 64) && oid.All(Uri.IsHexDigit);
}
