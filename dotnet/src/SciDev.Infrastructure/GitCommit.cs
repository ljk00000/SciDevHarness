using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Create an operator-requested commit from a reviewed index and publish it to that exact branch.</summary>
public static class GitCommit
{
    public sealed record Preview(string Workspace, string Reference, string? Parent, string IndexHash, string[] Paths, string? Name, string? Email);
    public sealed record Result(string Status, string CommitId, string Reference, int FileCount);

    public static async Task<Preview> PreviewAsync(string workspace, CancellationToken token, string? approvedRepository = null)
    {
        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        var locations = GitLocations.Resolve(root, new WindowsWorkspace(root), approvedRepository);
        var metadata = new WindowsWorkspace(locations.IndexDirectory);
        using var index = metadata.OpenPath("index");
        RejectInProgressOperation(metadata);
        var stamp = GitIndexEditor.ReadIndexStamp(index.AbsolutePath) ?? throw Empty();
        using var query = new GitReadContext(root, token, approvedRepository, locations);
        await query.ApplyLocalCoreSettingsAsync(token);
        using var scope = new GitMetadataFiles();
        var config = scope.HoldFile(new WindowsWorkspace(locations.CommonDirectory), "config", required: true);
        var identity = await ReadIdentityAsync(query, config, token);
        var preview = await ReadPreviewAsync(root, query, stamp.Hash, identity, token);
        if (stamp != GitIndexEditor.ReadIndexStamp(index.AbsolutePath))
            throw Changed();
        return preview;
    }

    public static async Task<Result> CommitAsync(string workspace, Preview preview, string message, string name, string email,
        CancellationToken token, string? approvedRepository = null)
    {
        message = NormalizeMessage(message);
        name = ValidateIdentity(name, "姓名");
        email = ValidateIdentity(email, "邮箱");
        if (!email.Contains('@') || email.Any(char.IsWhiteSpace))
            throw new ArgumentException("请输入提交者邮箱。");

        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        if (!string.Equals(root, preview.Workspace, StringComparison.OrdinalIgnoreCase))
            throw Changed();
        var locations = GitLocations.Resolve(root, new WindowsWorkspace(root), approvedRepository);
        var local = new WindowsWorkspace(locations.IndexDirectory);
        var common = new WindowsWorkspace(locations.CommonDirectory);
        using var index = local.OpenPath("index");
        using var lockPath = local.OpenPath("index.lock");
        using var indexLock = GitIndexEditor.LockIndex(lockPath.AbsolutePath, deleteOnClose: true);
        RejectInProgressOperation(local);
        var stamp = GitIndexEditor.ReadIndexStamp(index.AbsolutePath);
        if (stamp is null || stamp.Value.Hash != preview.IndexHash)
            throw Changed();

        using var scope = new GitMetadataFiles();
        var headPath = scope.HoldFile(local, "HEAD", required: true);
        var config = scope.HoldFile(common, "config", required: true);
        scope.HoldFile(common, "packed-refs");
        using var query = new GitReadContext(root, token, approvedRepository, locations);
        await query.ApplyLocalCoreSettingsAsync(token);
        var identity = await ReadIdentityAsync(query, config, token);
        var verified = await ReadPreviewAsync(root, query, stamp.Value.Hash, identity, token);
        if (verified.Reference != preview.Reference || verified.Parent != preview.Parent || !verified.Paths.SequenceEqual(preview.Paths))
            throw Changed();
        if (ReadHead(headPath) != $"ref: {verified.Reference}")
            throw Changed();

        scope.CheckWritableRef(common, verified.Reference);
        scope.HoldFile(common, $"logs/{verified.Reference}", mayAppend: true, createParents: true);
        scope.HoldFile(local, "logs/HEAD", mayAppend: true, createParents: true);
        var tree = ParseOid(await query.RunAsync(["--no-replace-objects", "write-tree"], token, maxChars: 256));
        var parent = verified.Parent is null ? Array.Empty<string>() : new[] { "-p", verified.Parent };
        var commit = ParseOid(await query.RunAsync(["--no-replace-objects", "-c", $"user.name={name}", "-c",
            $"user.email={email}", "commit-tree", tree, ..parent, "--no-gpg-sign", "-F", "-"], token, maxChars: 256, input: message));

        if (stamp != GitIndexEditor.ReadIndexStamp(index.AbsolutePath))
            throw Changed();
        RejectInProgressOperation(local);
        token.ThrowIfCancellationRequested();
        var old = verified.Parent ?? new string('0', commit.Length);
        var update = await query.PublishReferenceAsync(verified.Reference, commit, old, name, email);
        if (update.Code != 0 || update.Truncated)
            throw new ToolDeniedException("git_ref_update_failed", "Git 无法更新分支引用，请刷新并核对提交结果。");
        return new("committed", commit, verified.Reference, verified.Paths.Length);
    }

    private static async Task<Preview> ReadPreviewAsync(string workspace, GitReadContext query, string indexHash,
        (string? Name, string? Email) identity, CancellationToken token)
    {
        var (reference, parent) = await ReadBranchAsync(query, token);
        var staged = await query.RunAsync(["--no-replace-objects", "diff", "--cached", "--name-only", "-z",
            "--ita-invisible-in-index", "--no-renames", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "--"], token);
        if (staged.Code != 0 || staged.Truncated || staged.Text.Length > 0 && !staged.Text.EndsWith('\0'))
            throw new ToolDeniedException("git_commit_preview", "无法核对完整的暂存区，请刷新后重试。");
        var paths = staged.Text.Split('\0', StringSplitOptions.RemoveEmptyEntries);
        if (paths.Length == 0)
            throw Empty();
        if (paths.Any(path => !WorkspaceDocuments.IsVisible(path)))
            throw new ToolDeniedException("git_protected_index", "暂存区含有受保护的路径，请在本地 Git 中先核对这些文件。");
        await RequireMergedIndexAsync(query, token);
        return new(workspace, reference, parent, indexHash, paths, identity.Name, identity.Email);
    }

    internal static async Task<(string Reference, string? Parent)> ReadBranchAsync(GitReadContext query, CancellationToken token)
    {
        var branch = await query.RunAsync(["symbolic-ref", "--quiet", "--no-recurse", "HEAD"], token, maxChars: 4096);
        var reference = branch.Text.TrimEnd('\r', '\n');
        if (branch.Code != 0 || branch.Truncated || !reference.StartsWith("refs/heads/", StringComparison.Ordinal) ||
            WindowsWorkspace.NormalizeOperatorPath(reference) != reference ||
            (await query.RunAsync(["check-ref-format", reference], token, maxChars: 256)).Code != 0)
            throw new ToolDeniedException("git_commit_branch", "请在普通本地分支上提交。");
        var target = await query.RunAsync(["symbolic-ref", "--quiet", "--no-recurse", reference], token, maxChars: 4096);
        var previous = await query.RunAsync(["--no-replace-objects", "rev-parse", "--verify", "--quiet", $"{reference}^{{commit}}"], token, maxChars: 256);
        var parent = previous.Code == 1 && !previous.Truncated ? null : ParseOid(previous);
        if (target.Truncated || (target.Code != 1 && !(target.Code == 128 && parent is null)))
            throw new ToolDeniedException("git_commit_branch", "当前分支的引用格式不支持此提交方式。");
        return (reference, parent);
    }

    internal static async Task RequireMergedIndexAsync(GitReadContext query, CancellationToken token)
    {
        var unmerged = await query.RunAsync(["ls-files", "--unmerged", "-z"], token, maxChars: 4096);
        if (unmerged.Code != 0 || unmerged.Text.Length > 0 || unmerged.Truncated)
            throw new ToolDeniedException("git_commit_conflict", "请先在本地 Git 中完成冲突处理。");
    }

    internal static async Task<(string? Name, string? Email)> ReadIdentityAsync(GitReadContext query, string config, CancellationToken token)
    {
        var settings = await GitRepositoryConfig.ReadAsync(query, config, token);
        var sign = settings.GetValueOrDefault("commit.gpgsign")?.ToLowerInvariant();
        if (sign is not (null or "false" or "no" or "off" or "0"))
            throw new ToolDeniedException("git_commit_signing", "此仓库配置了提交签名，请使用本地 Git 签名提交。");
        return (settings.GetValueOrDefault("user.name"), settings.GetValueOrDefault("user.email"));
    }

    internal static string ReadHead(string path)
    {
        using var reader = WorkspaceFiles.OpenSnapshot(path, FileShare.Read, maxBytes: 4096);
        return WorkspaceFiles.Utf8.GetString(reader.Bytes).TrimEnd('\r', '\n');
    }

    internal static void RejectInProgressOperation(WindowsWorkspace metadata)
    {
        foreach (var name in new[] { "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "sequencer" })
        {
            using var marker = metadata.OpenPath(name);
            if (Path.Exists(marker.AbsolutePath))
                throw new ToolDeniedException("git_commit_in_progress", "仓库正在合并、变基或拣选提交，请先在本地 Git 完成该操作。");
        }
    }

    internal static string ParseOid(GitReadContext.Output output)
    {
        var oid = output.Text.TrimEnd('\r', '\n');
        if (output.Code != 0 || output.Truncated || oid.Length is not (40 or 64) || !oid.All(Uri.IsHexDigit))
            throw new ToolDeniedException("git_commit_object", "Git 未能生成或验证提交对象。");
        return oid;
    }

    internal static string NormalizeMessage(string value)
    {
        var message = value.Trim().ReplaceLineEndings("\n");
        if (message.Length is < 1 or > 8192 || message.Any(ch => char.IsControl(ch) && ch is not ('\n' or '\t')))
            throw new ArgumentException("请提供有效的提交说明，最多 8,192 字符。");
        return message + "\n";
    }

    internal static string ValidateIdentity(string value, string label)
    {
        var identity = value.Trim();
        if (identity.Length is < 1 or > 256 || identity.Any(ch => char.IsControl(ch) || ch is '<' or '>'))
            throw new ArgumentException($"请提供有效的提交者{label}。");
        return identity;
    }

    private static ToolDeniedException Empty() => new("git_no_staged", "请先暂存要提交的文件。");
    private static ToolDeniedException Changed() => new("git_commit_changed", "暂存区或分支在确认期间已变化，请刷新后重新审核。");
}
