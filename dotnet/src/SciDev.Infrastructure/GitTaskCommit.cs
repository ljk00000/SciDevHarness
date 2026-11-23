using System.Text;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceFiles;

namespace SciDev.Infrastructure;

internal static class GitTaskCommit
{
    private const int MaxFiles = 64;
    private const int MaxFileBytes = 240_000;
    private const int MaxTotalBytes = 4 * 1024 * 1024;
    private const int MaxPreviewChars = 64_000;
    private static readonly string[] Pathspec = ["--no-replace-objects", "--literal-pathspecs"];
    private static readonly string[] IndexUpdate = ["-c", "core.splitIndex=false", "update-index", "-z", "--index-info"];

    internal sealed record Entry(string Path, string Mode, string ObjectId, FileStamp? Stamp);
    internal sealed record Plan(string Reference, string? Parent, (WindowsWorkspace.FileIdentity Identity, string Hash)? Index,
        string Name, string Email, Entry[] Entries, int Omitted, string Preview);

    internal static async Task<Plan> PrepareAsync(string root, string? repository, WorkspaceCheckpoint checkpoint,
        string[] paths, string? credential, CancellationToken token)
    {
        var location = GitLocations.Resolve(root, new WindowsWorkspace(root), repository);
        using var index = new WindowsWorkspace(location.IndexDirectory).OpenPath("index");
        var stamp = GitIndexEditor.ReadIndexStamp(index.AbsolutePath);
        using var query = new GitReadContext(root, token, repository, location);
        var plan = await ReadPlanAsync(root, location, query, checkpoint, paths, credential, token);
        if (plan.Index != stamp)
            throw Changed();
        return plan;
    }

    internal static async Task<TaskCommitResult> PublishAsync(string root, string? repository, Plan reviewed,
        WorkspaceCheckpoint checkpoint, string[] paths, string message, string? credential,
        Action<RunEvent> record, CancellationToken token)
    {
        var workspace = new WindowsWorkspace(root);
        var location = GitLocations.Resolve(root, workspace, repository);
        var local = new WindowsWorkspace(location.IndexDirectory);
        var common = new WindowsWorkspace(location.CommonDirectory);
        using var index = local.OpenPath("index");
        using var lockPath = local.OpenPath("index.lock");
        using var indexLock = GitIndexEditor.LockIndex(lockPath.AbsolutePath);
        var indexPublished = false;
        try
        {
            using var metadata = new GitMetadataFiles();
            var head = metadata.HoldFile(local, "HEAD", required: true);
            metadata.HoldFile(common, "config", required: true);
            metadata.HoldFile(common, "packed-refs");
            using var query = new GitReadContext(root, token, repository, location);
            var verified = await ReadPlanAsync(root, location, query, checkpoint, paths, credential, token);
            if (verified.Reference != reviewed.Reference || verified.Parent != reviewed.Parent || verified.Index != reviewed.Index ||
                verified.Name != reviewed.Name || verified.Email != reviewed.Email || verified.Omitted != reviewed.Omitted ||
                verified.Preview != reviewed.Preview || !verified.Entries.SequenceEqual(reviewed.Entries) ||
                GitCommit.ReadHead(head) != $"ref: {reviewed.Reference}")
                throw Changed();

            metadata.CheckWritableRef(common, reviewed.Reference);
            metadata.HoldFile(common, $"logs/{reviewed.Reference}", mayAppend: true, createParents: true);
            metadata.HoldFile(local, "logs/HEAD", mayAppend: true, createParents: true);
            foreach (var file in reviewed.Entries)
            {
                using var path = workspace.OpenOperatorPath(file.Path)!;
                if (file.Stamp is null)
                {
                    EnsureAbsent(path.AbsolutePath);
                    continue;
                }
                using var snapshot = OpenSnapshot(path.AbsolutePath, FileShare.Read, maxBytes: MaxFileBytes);
                if (snapshot.Stamp != file.Stamp ||
                    await HashBlobAsync(query, file.Path, snapshot.Bytes, token, write: true) != file.ObjectId)
                    throw Changed();
            }

            Require(await query.RunOnTaskIndexAsync(["--no-replace-objects", "read-tree", reviewed.Parent ?? "--empty"], token));
            var indexInfo = string.Concat(reviewed.Entries.Select(file => $"{file.Mode} {file.ObjectId}\t{file.Path}\0"));
            Require(await query.RunOnTaskIndexAsync(IndexUpdate, token, indexInfo));
            var tree = GitCommit.ParseOid(await query.RunOnTaskIndexAsync(["--no-replace-objects", "write-tree"], token));
            var parents = reviewed.Parent is null ? Array.Empty<string>() : new[] { "-p", reviewed.Parent };
            var commit = GitCommit.ParseOid(await query.RunAsync(["--no-replace-objects", "-c", $"user.name={reviewed.Name}", "-c", $"user.email={reviewed.Email}",
                "commit-tree", tree, ..parents, "--no-gpg-sign", "-F", "-"], token, maxChars: 256, input: GitCommit.NormalizeMessage(message)));

            // Refresh only the committed entries in the copy of the user's original index.
            Require(await query.RunAsync(IndexUpdate, token, input: indexInfo));
            using (var copy = File.OpenRead(query.IndexCopy))
            {
                if (copy.Length > 96 * 1024 * 1024)
                    throw new ToolDeniedException("git_limit", "更新后的 Git 索引超出读取上限。");
                await copy.CopyToAsync(indexLock, token);
            }
            indexLock.Flush(flushToDisk: true);
            foreach (var file in reviewed.Entries)
            {
                using var path = workspace.OpenOperatorPath(file.Path)!;
                if (file.Stamp is null)
                {
                    EnsureAbsent(path.AbsolutePath);
                    continue;
                }
                using var snapshot = OpenSnapshot(path.AbsolutePath, FileShare.Read, maxBytes: MaxFileBytes);
                if (snapshot.Stamp != file.Stamp)
                    throw Changed();
            }
            if (GitIndexEditor.ReadIndexStamp(index.AbsolutePath) != reviewed.Index)
                throw Changed();
            GitCommit.RejectInProgressOperation(local);
            token.ThrowIfCancellationRequested();

            var old = reviewed.Parent ?? new string('0', commit.Length);
            record(new("git_task_commit_publishing", 0, new { reviewed.Reference, reviewed.Parent, commit,
                paths = reviewed.Entries.Select(file => file.Path).ToArray() }));
            try
            {
                var update = await query.PublishReferenceAsync(reviewed.Reference, commit, old, reviewed.Name, reviewed.Email);
                if (update.Code != 0 || update.Truncated)
                    return new("git_task_commit_uncertain", commit, reviewed.Reference, reviewed.Entries.Length, reviewed.Omitted);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException or System.ComponentModel.Win32Exception)
            {
                return new("git_task_commit_uncertain", commit, reviewed.Reference, reviewed.Entries.Length, reviewed.Omitted);
            }
            try
            {
                indexLock.Dispose();
                File.Move(lockPath.AbsolutePath, index.AbsolutePath, overwrite: reviewed.Index is not null);
                indexPublished = true;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                // A compare-and-swap rollback never overwrites an intervening branch update.
                try
                {
                    var reverted = await query.PublishReferenceAsync(reviewed.Reference, old, commit, reviewed.Name, reviewed.Email);
                    if (reverted.Code == 0 && !reverted.Truncated)
                        return new("git_task_index_write_failed", Omitted: reviewed.Omitted);
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException or ToolDeniedException or System.ComponentModel.Win32Exception) { }
                return new("git_task_index_repair_needed", commit, reviewed.Reference, reviewed.Entries.Length, reviewed.Omitted);
            }
            return new("committed", commit, reviewed.Reference, reviewed.Entries.Length, reviewed.Omitted);
        }
        finally
        {
            indexLock.Dispose();
            if (!indexPublished)
                File.Delete(lockPath.AbsolutePath);
        }
    }

    private static async Task<Plan> ReadPlanAsync(string root, GitLocations location, GitReadContext query,
        WorkspaceCheckpoint checkpoint, string[] paths, string? credential, CancellationToken token)
    {
        var workspace = new WindowsWorkspace(root);
        var local = new WindowsWorkspace(location.IndexDirectory);
        using var indexPath = local.OpenPath("index");
        GitCommit.RejectInProgressOperation(local);
        var index = GitIndexEditor.ReadIndexStamp(indexPath.AbsolutePath);
        using var metadata = new GitMetadataFiles();
        var config = metadata.HoldFile(new WindowsWorkspace(location.CommonDirectory), "config", required: true);
        await query.ApplyLocalCoreSettingsAsync(token);
        await GitCommit.RequireMergedIndexAsync(query, token);
        var (reference, parent) = await GitCommit.ReadBranchAsync(query, token);
        var identity = await GitCommit.ReadIdentityAsync(query, config, token);
        string name, email;
        try
        {
            name = GitCommit.ValidateIdentity(identity.Name ?? "SciDevHarness", "姓名");
            email = GitCommit.ValidateIdentity(identity.Email ?? "scidev@localhost", "邮箱");
        }
        catch (ArgumentException)
        {
            throw new ToolDeniedException("git_task_identity", "Git 提交身份需要核对。");
        }
        if (!email.Contains('@') || email.Any(char.IsWhiteSpace) || (!string.IsNullOrEmpty(credential) &&
            (name.Contains(credential, StringComparison.Ordinal) || email.Contains(credential, StringComparison.Ordinal))))
            throw new ToolDeniedException("git_task_identity", "Git 提交身份需要核对。");
        var taskChanges = checkpoint.ReadTaskChanges(token, out var renames);
        var eligible = paths.Where(path => WorkspaceDocuments.IsVisible(path) &&
                !path.Split('/').Any(component => component.Equals(".gitattributes", StringComparison.OrdinalIgnoreCase)) &&
                (string.IsNullOrEmpty(credential) || !path.Contains(credential, StringComparison.Ordinal)))
            .Take(MaxFiles).ToArray();
        var entries = new List<(Entry File, string Preview)>();
        var previewLength = 0;
        var unchanged = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (eligible.Length != 0)
        {
            var staged = await query.RunAsync([..Pathspec, "ls-files", "--stage", "-z", "--", ..eligible], token);
            var indexEntries = ParseEntries(staged, fromTree: false);
            var indexFlags = Split(await query.RunAsync([..Pathspec, "ls-files", "-v", "-z", "--", ..eligible], token));
            var specialEntries = indexFlags.Where(flag => !flag.StartsWith("H ", StringComparison.Ordinal))
                .Select(flag => flag.Length > 2 ? flag[2..] : throw Changed()).ToHashSet(StringComparer.Ordinal);
            var headEntries = parent is null ? new Dictionary<string, Blob>(StringComparer.Ordinal) : ParseEntries(await query.RunAsync(
                [..Pathspec, "ls-tree", "-r", "--full-tree", "-z", parent, "--", ..eligible], token), fromTree: true);
            var changes = await GitWorkspace.ReadChangesAsync(query, token);
            var protectNew = changes.Any(GitWorkspace.IsAddition) && await GitWorkspace.HasProtectedRemovalAsync(query, changes, token);
            var remaining = MaxTotalBytes;
            foreach (var path in eligible)
            {
                token.ThrowIfCancellationRequested();
                if (!taskChanges.TryGetValue(path, out var task))
                    throw Changed();
                if (task.Before is null && task.After is null)
                    continue;
                var original = headEntries.GetValueOrDefault(path);
                if (specialEntries.Contains(path) || indexEntries.GetValueOrDefault(path) != original ||
                    (original is null) != (task.Before is null) ||
                    original is not null && original.Mode is not ("100644" or "100755") ||
                    original is null && (protectNew || await IsIgnoredAsync(query, path, token)))
                    continue;

                using var visible = workspace.OpenOperatorPath(path)!;
                if (task.After is null)
                    EnsureAbsent(visible.AbsolutePath);
                using var current = task.After is null ? null : ReadSnapshot(
                    WindowsWorkspace.OpenForRead(visible.AbsolutePath, FileShare.Read), visible.AbsolutePath, FileAccess.Read,
                    MaxBytes, allowBinary: true);
                if (current?.Stamp != task.After)
                    throw Changed();
                var bytes = current?.Bytes ?? [];
                var examined = current?.Bytes ?? task.Original ?? [];
                if (examined.Length > Math.Min(MaxFileBytes, remaining) || !CanPreview(examined, credential) ||
                    (task.Original is not null && !CanPreview(task.Original, credential)))
                    continue;
                var attrs = Split(await query.RunAsync([..Pathspec, "check-attr", "-z", "filter", "--", path], token));
                if (attrs.Length != 3 || attrs[0] != path || attrs[1] != "filter" || attrs[2] is not ("unspecified" or "unset"))
                    continue;
                if (original is not null && (task.Original is null ||
                    await HashBlobAsync(query, path, task.Original, token) != original.Oid))
                    continue;
                var movedFrom = task.MovedFrom is null ? null : headEntries.GetValueOrDefault(task.MovedFrom);
                if (task.MovedFrom is not null && movedFrom is not { Mode: "100644" or "100755" })
                    continue;
                var mode = current is null ? "0" : movedFrom?.Mode ?? original?.Mode ?? "100644";
                var revised = current is null ? new string('0', original!.Oid.Length) : await HashBlobAsync(query, path, bytes, token);
                if (revised == original?.Oid && mode == original.Mode)
                {
                    unchanged.Add(path);
                    continue;
                }
                var action = current is null ? "删除" : original is null ? "新增" : "修改";
                var heading = $"{action} {path}  ({revised}, {mode})\n";
                var diffBudget = MaxPreviewChars - previewLength - heading.Length - 2;
                if (diffBudget <= 0)
                    break;
                var diff = await query.CompareTaskSnapshotsAsync(task.Original, bytes, diffBudget, token);
                if (diff.Truncated)
                    continue;
                var hunk = diff.Text.IndexOf("\n@@ ", StringComparison.Ordinal);
                if (diff.Code is not (0 or 1) || diff.Code == 1 && hunk < 0)
                    throw Changed();
                var snippet = heading + (hunk >= 0 ? diff.Text[(hunk + 1)..] : "") + "\n\n";
                previewLength += snippet.Length;
                entries.Add((new(path, mode, revised, task.After), snippet));
                remaining -= examined.Length;
            }
        }
        // A skipped name (for example an ignored target or a staged source) excludes the entire move.
        var included = entries.Select(entry => entry.File.Path).ToHashSet(StringComparer.OrdinalIgnoreCase);
        foreach (var group in renames)
            if (group.Any(path => !included.Contains(path) && !unchanged.Contains(path) &&
                (taskChanges[path].Before is not null || taskChanges[path].After is not null)))
                included.ExceptWith(group);
        var accepted = entries.Where(entry => included.Contains(entry.File.Path)).ToArray();
        if (GitIndexEditor.ReadIndexStamp(indexPath.AbsolutePath) != index)
            throw Changed();
        return new(reference, parent, index, name, email, accepted.Select(entry => entry.File).ToArray(),
            paths.Length - accepted.Length, string.Concat(accepted.Select(entry => entry.Preview)));
    }

    private static void EnsureAbsent(string path)
    {
        if (WindowsWorkspace.FileExists(path))
            throw Changed();
    }

    private static bool CanPreview(byte[] bytes, string? credential)
    {
        if (bytes.AsSpan().Contains((byte)0) || (!string.IsNullOrEmpty(credential) &&
            bytes.AsSpan().IndexOf(Encoding.UTF8.GetBytes(credential)) >= 0))
            return false;
        try { _ = Utf8.GetCharCount(bytes); return true; }
        catch (DecoderFallbackException) { return false; }
    }

    private static async Task<bool> IsIgnoredAsync(GitReadContext query, string path, CancellationToken token)
    {
        var ignored = await query.RunAsync(["--no-replace-objects", "check-ignore", "-q", "--", path], token);
        if (ignored.Truncated || ignored.Code is not (0 or 1))
            throw Changed();
        return ignored.Code == 0;
    }

    private static async Task<string> HashBlobAsync(GitReadContext query, string path, byte[] bytes, CancellationToken token, bool write = false)
    {
        string[] args = write ? ["hash-object", "-w", $"--path={path}", "--stdin"] : ["hash-object", $"--path={path}", "--stdin"];
        return GitCommit.ParseOid(await query.RunAsync(args, token, maxChars: 256, input: Utf8.GetString(bytes)));
    }

    private sealed record Blob(string Mode, string Oid);

    private static Dictionary<string, Blob> ParseEntries(GitReadContext.Output output, bool fromTree)
    {
        var entries = new Dictionary<string, Blob>(StringComparer.Ordinal);
        foreach (var record in Split(output))
        {
            var separator = record.IndexOf('\t');
            var header = separator < 0 ? [] : record[..separator].Split(' ');
            var oid = header.Length == 3 ? header[fromTree ? 2 : 1] : "";
            if (oid.Length is not (40 or 64) || !oid.All(char.IsAsciiHexDigit) ||
                (!fromTree && header[2] != "0") || !entries.TryAdd(record[(separator + 1)..], new(header[0], oid)))
                throw Changed();
        }
        return entries;
    }

    private static string[] Split(GitReadContext.Output result)
    {
        Require(result);
        if (result.Text.Length == 0)
            return [];
        if (!result.Text.EndsWith('\0'))
            throw Changed();
        return result.Text[..^1].Split('\0');
    }

    private static void Require(GitReadContext.Output result)
    {
        if (result.Code != 0 || result.Truncated)
            throw Changed();
    }

    private static ToolDeniedException Changed() =>
        new("git_task_commit_changed", "任务文件、分支或暂存区已变化，保留改动以便再次核对。");
}
