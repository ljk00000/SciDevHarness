using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class GitDiffReview
{
    private const int MaxFiles = 32;
    private const int MaxFileBytes = 128 * 1024;
    private const int MaxSnapshotBytes = 1024 * 1024;
    private const int MaxDiffChars = 30_000;
    private static readonly string[] DiffArgs =
        ["--no-replace-objects", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=all"];

    internal sealed record Result(string Text, int Included, int Omitted, bool Truncated);

    internal static async Task<Result> CaptureAsync(string workspace, string? approvedRepository, WorkspaceDrafts drafts,
        string? credential, CancellationToken token, string? scratchWorkspace = null)
    {
        var root = new WindowsWorkspace(workspace);
        using var query = new GitReadContext(workspace, token, approvedRepository, scratchWorkspace: scratchWorkspace);
        await query.ApplyLocalCoreSettingsAsync(token);
        var changes = await GitWorkspace.ReadChangesAsync(query, token);
        var protectedRemoval = changes.Any(GitWorkspace.IsAddition) && await GitWorkspace.HasProtectedRemovalAsync(query, changes, token);
        var stage = query.CreateReviewTree();
        var entries = new List<GitWorkspace.Change>();
        var files = new Dictionary<string, byte[]?>(StringComparer.Ordinal);
        var remaining = MaxSnapshotBytes;
        var secret = string.IsNullOrEmpty(credential) ? [] : Encoding.UTF8.GetBytes(credential);

        foreach (var change in changes)
        {
            token.ThrowIfCancellationRequested();
            if (!GitWorkspace.IsVisibleChange(change) || protectedRemoval && GitWorkspace.IsAddition(change))
                continue;
            string[] paths = change.Original is null ? [change.Path] : [change.Original, change.Path];
            var fresh = paths.Where(path => !files.ContainsKey(path)).Distinct(StringComparer.Ordinal).ToArray();
            if (files.Count + fresh.Length > MaxFiles || paths.Any(path => drafts.Get(path) is not null))
                continue;
            var pending = new List<(string Path, byte[]? Data)>();
            var budget = remaining;
            try
            {
                foreach (var path in fresh)
                {
                    var bytes = Read(root, path, Math.Min(budget, MaxFileBytes));
                    if (bytes is not null && secret.Length > 0 && bytes.AsSpan().IndexOf(secret) >= 0)
                        break;
                    budget -= bytes?.Length ?? 0;
                    pending.Add((path, bytes));
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException)
            {
                continue;
            }
            if (pending.Count != fresh.Length)
                continue;
            if (change.Code == "??")
            {
                var content = pending.FirstOrDefault(file => file.Path == change.Path).Data ?? files.GetValueOrDefault(change.Path);
                if (content is null || !IsUtf8Text(content))
                    continue;
            }
            foreach (var (path, data) in pending)
            {
                if (data is not null)
                    WriteCopy(stage, path, data);
                files.Add(path, data);
            }
            remaining = budget;
            entries.Add(change);
        }

        var output = new StringBuilder("Git 工作区状态（只读快照）\n");
        foreach (var entry in entries)
            output.AppendLine(entry.Display);
        var omitted = changes.Length - entries.Count;
        if (omitted > 0)
            output.AppendLine($"已省略 {omitted} 项受保护、不可读、草稿或超出预算的改动。");
        if (entries.Count == 0)
            return new(output.Append("（没有可共享的差异）").ToString(), 0, omitted, false);

        var tracked = entries.Where(change => change.Code != "??").SelectMany(change =>
            change.Original is null ? new[] { change.Path } : [change.Original, change.Path]).Distinct(StringComparer.Ordinal).ToArray();
        CopyAttributes(root, stage, tracked, files, drafts, secret, token);
        var truncated = false;
        if (tracked.Length != 0)
        {
            // Read past the visible cutoff so a credential straddling it can still be redacted as a unit.
            var readLimit = MaxDiffChars + (credential?.Length ?? 0);
            var head = await query.RunAsync(["rev-parse", "--verify", "--quiet", "HEAD"], token, maxChars: 256);
            if (head.Code is not (0 or 1) || head.Truncated)
                throw Unavailable();
            RequireExactPaths(await query.RunAsync(["--literal-pathspecs", "ls-files", "-z", "--", ..tracked], token), tracked);
            if (head.Code == 0)
                RequireExactPaths(await query.RunAsync(["--no-replace-objects", "--literal-pathspecs", "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ..tracked], token), tracked);
            output.AppendLine().AppendLine("Git 文本差异");
            var patch = await query.RunOnReviewTreeAsync(["--literal-pathspecs", ..DiffArgs, head.Code == 0 ? "HEAD" : "--cached", "--", ..tracked],
                token, readLimit);
            CheckExit(patch);
            output.Append(patch.Text);
            truncated = patch.Truncated;
            if (head.Code != 0 && output.Length < MaxDiffChars && !truncated)
            {
                var unstaged = await query.RunOnReviewTreeAsync(["--literal-pathspecs", ..DiffArgs, "--", ..tracked], token, readLimit);
                CheckExit(unstaged);
                output.Append(unstaged.Text);
                truncated = unstaged.Truncated;
            }
        }
        foreach (var entry in entries.Where(change => change.Code == "??"))
        {
            token.ThrowIfCancellationRequested();
            if (output.Length >= MaxDiffChars)
            {
                truncated = true;
                break;
            }
            AppendNewFile(output, entry.Path, files[entry.Path]!);
        }
        var safe = WorkspaceFiles.RedactCredential(output.ToString(), credential);
        if (safe.Length >= MaxDiffChars)
        {
            var cut = char.IsHighSurrogate(safe[MaxDiffChars - 1]) ? MaxDiffChars - 1 : MaxDiffChars;
            safe = safe[..cut];
            truncated = true;
        }
        if (truncated)
            safe += "\n（差异已达输出上限。）";
        return new(safe, entries.Count, omitted, truncated);
    }

    private static void CopyAttributes(WindowsWorkspace root, string stage, string[] tracked,
        IReadOnlyDictionary<string, byte[]?> files, WorkspaceDrafts drafts, byte[] secret, CancellationToken token)
    {
        var remaining = 128 * 1024;
        var attributes = tracked.SelectMany(path => WorkspaceProjectContext.Ancestors(path, directory: false))
            .Distinct(StringComparer.Ordinal).Select(directory => directory.Length == 0 ? ".gitattributes" : directory + "/.gitattributes");
        foreach (var path in attributes)
        {
            token.ThrowIfCancellationRequested();
            if (files.ContainsKey(path))
                continue;
            if (drafts.Get(path) is not null)
                throw new ToolDeniedException("git_review_attributes", "适用的 Git 属性文件有未保存草稿，保存后可重新审阅。");
            var data = Read(root, path, Math.Min(remaining, 32_768));
            if (data is null)
                continue;
            if (secret.Length > 0 && data.AsSpan().IndexOf(secret) >= 0)
                throw Unavailable();
            WriteCopy(stage, path, data);
            remaining -= data.Length;
        }
    }

    private static byte[]? Read(WindowsWorkspace root, string path, int maxBytes)
    {
        using var lease = root.OpenOperatorPath(path, allowMissingParents: true);
        if (lease is null || !WindowsWorkspace.FileExists(lease.AbsolutePath))
            return null;
        using var snapshot = WorkspaceFiles.OpenSnapshot(lease.AbsolutePath, FileShare.Read, maxBytes: maxBytes);
        return snapshot.Bytes;
    }

    private static void WriteCopy(string stage, string path, byte[] data)
    {
        var destination = Path.Combine(stage, WorkspaceDocuments.VisiblePath(path));
        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        File.WriteAllBytes(destination, data);
    }

    private static bool IsUtf8Text(byte[] data)
    {
        if (data.AsSpan().Contains((byte)0))
            return false;
        try { _ = WorkspaceFiles.Utf8.GetCharCount(data); return true; }
        catch (DecoderFallbackException) { return false; }
    }

    private static void AppendNewFile(StringBuilder output, string path, byte[] data)
    {
        var text = WorkspaceFiles.Utf8.GetString(data).ReplaceLineEndings("\n");
        output.Append("\n--- /dev/null\n+++ b/").AppendLine(path);
        if (text.Length == 0)
            return;
        var lines = text.Split('\n');
        var hasFinalNewline = text.EndsWith('\n');
        var count = hasFinalNewline ? lines.Length - 1 : lines.Length;
        output.Append("@@ -0,0 +1,").Append(count).AppendLine(" @@");
        for (var index = 0; index < count && output.Length < MaxDiffChars; index++)
            output.Append('+').AppendLine(lines[index]);
        if (!hasFinalNewline && output.Length < MaxDiffChars)
            output.AppendLine(@"\ No newline at end of file");
    }

    private static void RequireExactPaths(GitReadContext.Output output, string[] selected)
    {
        CheckExit(output);
        if (output.Truncated || output.Text.Length != 0 && !output.Text.EndsWith('\0') ||
            output.Text.Split('\0', StringSplitOptions.RemoveEmptyEntries).Except(selected, StringComparer.Ordinal).Any())
            throw new ToolDeniedException("git_review_scope", "Git 路径范围已变化，请重新审阅。");
    }

    private static void CheckExit(GitReadContext.Output output)
    {
        if (output.Code != 0)
            throw Unavailable();
    }

    private static ToolDeniedException Unavailable() => new("git_review_unavailable", "无法生成可安全分享的 Git 差异。");
}
