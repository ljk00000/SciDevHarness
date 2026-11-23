using System.Diagnostics;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceFiles;

namespace SciDev.Infrastructure;

internal sealed class WorkspaceDiscovery(WindowsWorkspace workspace, string? credential, WorkspaceDrafts drafts, WorkspaceGitIndex? index)
{
    private const int MaxOutput = 24_000;
    private const int MaxEntries = 4_096;
    private const int MaxSearchBytes = 8 * 1024 * 1024;
    private static readonly TimeSpan ScanBudget = TimeSpan.FromSeconds(4);
    private sealed record Scope(WorkspaceDocuments.Entry? Entry, WorkspaceIgnore.Rule[] Rules, bool Ignored = false,
        bool TrackedOnly = false);

    public string Execute(string tool, JsonElement args, CancellationToken token)
    {
        var path = args.TryGetProperty("path", out _) ? RequiredString(args, "path") : ".";
        var elapsed = Stopwatch.StartNew();
        if (tool == "get_project_context")
        {
            var query = args.TryGetProperty("query", out _) ? RequiredString(args, "query") : "";
            if (query.Length > 4000)
                throw new ToolDeniedException("invalid_tool", "项目上下文查询最多 4000 个字符。");
            return Context(path, query, elapsed, token)!;
        }
        return tool == "list_files" ? List(path, Math.Clamp(OptionalInt(args, "depth", 3), 1, 5), elapsed, token) :
            Search(path, args, elapsed, token);
    }

    internal string? InitialContext(string task, CancellationToken token)
    {
        var length = Math.Min(task.Length, 4000);
        if (length < task.Length && char.IsHighSurrogate(task[length - 1]))
            length--;
        return Context(".", task[..length], Stopwatch.StartNew(), token, initial: true);
    }

    internal VerificationPlan FindVerificationPlan(IReadOnlyList<FileChange> changes, CancellationToken token)
    {
        var elapsed = Stopwatch.StartNew();
        var ancestors = changes.SelectMany(change => WorkspaceProjectContext.Ancestors(change.Path, directory: false))
            .Distinct(StringComparer.OrdinalIgnoreCase);
        var scopedFiles = new List<string>();
        foreach (var directory in ancestors)
        {
            var scope = Resolve(directory.Length == 0 ? "." : directory, token);
            if (scope.Ignored || scope.Entry is { IsDirectory: false })
                return new([], [], Complete: false);
            var (entries, limited) = Walk(scope, depth: 1, MaxEntries, elapsed, token);
            if (limited)
                return new([], [], Complete: false);
            scopedFiles.AddRange(entries.Where(entry => !entry.IsDirectory).Select(entry => entry.RelativePath));
        }

        // Tests may be in descendants. An edited file's project boundary comes from its ancestors.
        IEnumerable<string> testFiles = scopedFiles;
        if (changes.Any(change => change.Path.EndsWith(".py", StringComparison.OrdinalIgnoreCase)) ||
            scopedFiles.Any(path => DotnetTestProjects.IsProject(path) || ProjectManifest.UsesSourceTestDiscovery(Path.GetFileName(path))))
        {
            var (entries, _) = Walk(".", depth: 30, MaxEntries, elapsed, token);
            testFiles = scopedFiles.Concat(entries.Where(entry => !entry.IsDirectory).Select(entry => entry.RelativePath));
        }
        return new WorkspaceProjectContext(workspace, credential, drafts).FindVerificationPlan(scopedFiles.ToArray(), testFiles,
            changes, token);
    }

    internal VerificationPlan InspectVerificationPlan(CancellationToken token)
    {
        var (entries, limited) = Walk(".", depth: 30, MaxEntries, Stopwatch.StartNew(), token);
        var paths = entries.Where(entry => !entry.IsDirectory).Select(entry => entry.RelativePath).ToArray();
        var plan = new WorkspaceProjectContext(workspace, credential, drafts).FindVerificationPlan(paths, paths, null, token);
        return plan with { Complete = plan.Complete && !limited };
    }

    private string? Context(string path, string query, Stopwatch elapsed, CancellationToken token, bool initial = false)
    {
        var scope = Resolve(path, token);
        if (scope.Ignored)
            return "（此范围已按工作区忽略规则排除。）";
        var (entries, limited) = Walk(scope, depth: 30, MaxEntries, elapsed, token);
        var directories = WorkspaceProjectContext.Ancestors(scope.Entry?.RelativePath, scope.Entry?.IsDirectory == true).ToArray();
        var files = entries.Where(entry => !entry.IsDirectory).Select(entry => entry.RelativePath);
        foreach (var directory in directories.Where(dir => scope.Entry is not { IsDirectory: true } || dir != scope.Entry.RelativePath))
        {
            if (scope.Entry is null)
                break;
            var top = Walk(directory.Length == 0 ? "." : directory, depth: 1, maxEntries: 500, elapsed, token);
            files = files.Concat(top.Entries.Where(entry => !entry.IsDirectory).Select(entry => entry.RelativePath));
            limited |= top.Limited;
        }
        var paths = files.Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        if (initial && paths.Length == 0 && drafts.Count == 0 && index is null && !limited)
            return null;
        return new WorkspaceProjectContext(workspace, credential, drafts).Build(scope.Entry?.RelativePath ?? ".", query,
            paths, directories, limited, elapsed, ScanBudget, token, initial ? 9_000 : 16_000, index);
    }

    private string List(string path, int depth, Stopwatch elapsed, CancellationToken token)
    {
        var (entries, limited) = Walk(path, depth, maxEntries: 500, elapsed, token);
        var result = new StringBuilder();
        foreach (var entry in entries)
        {
            if (result.Length + entry.RelativePath.Length + 2 > MaxOutput)
            {
                limited = true;
                break;
            }
            result.Append(entry.RelativePath).AppendLine(entry.IsDirectory ? "/" : "");
        }
        return Format(result, limited, "（没有可列出的条目）");
    }

    private string Search(string path, JsonElement args, Stopwatch elapsed, CancellationToken token)
    {
        var query = RequiredString(args, "query");
        var glob = args.TryGetProperty("glob", out _) ? RequiredString(args, "glob") : "";
        if (string.IsNullOrWhiteSpace(query) || query.Length > 256 || query.Any(char.IsControl) ||
            glob.Length > 256 || glob.Any(char.IsControl))
            throw new ToolDeniedException("invalid_tool", "搜索字面文本必须是 1 至 256 个字符的单行文本，文件模式最多 256 个字符。");
        var pattern = glob.Length == 0 ? null : WorkspaceIgnore.Pattern(glob.Replace('\\', '/'));
        var comparison = CaseSensitive(args) ? StringComparison.Ordinal : StringComparison.OrdinalIgnoreCase;
        var maxResults = Math.Clamp(OptionalInt(args, "max_results", 20), 1, 40);
        var context = Math.Clamp(OptionalInt(args, "context_lines", 1), 0, 3);
        var (entries, limited) = Walk(path, depth: 30, MaxEntries, elapsed, token);
        var result = new StringBuilder();
        var used = 0;
        var matches = 0;
        var files = 0;
        foreach (var entry in entries)
        {
            token.ThrowIfCancellationRequested();
            if (entry.IsDirectory || pattern?.IsMatch(entry.RelativePath) == false)
                continue;
            if (++files > 768 || used >= MaxSearchBytes || elapsed.Elapsed >= ScanBudget)
            {
                limited = true;
                break;
            }
            var lines = ReadLines(entry.RelativePath, ref used);
            for (var index = 0; index < lines.Length; index++)
            {
                if ((index & 255) == 0)
                    token.ThrowIfCancellationRequested();
                var offset = lines[index].IndexOf(query, comparison);
                if (offset < 0)
                    continue;
                if (matches == maxResults || result.Length + 2_600 > MaxOutput)
                    return Format(result, limited: true, "（没有匹配）");
                matches++;
                result.Append(entry.RelativePath).Append(':').Append(index + 1)
                    .AppendLine(drafts.Get(entry.RelativePath) is null ? "" : "（未保存草稿）");
                for (var number = Math.Max(0, index - context); number <= Math.Min(lines.Length - 1, index + context); number++)
                    result.Append(number == index ? "> " : "  ").Append(number + 1).Append(": ")
                        .AppendLine(Excerpt(lines[number], number == index ? offset : 0));
                result.AppendLine();
            }
        }
        return Format(result, limited, "（没有匹配）");
    }

    private (List<WorkspaceDocuments.Entry> Entries, bool Limited) Walk(string path, int depth, int maxEntries,
        Stopwatch elapsed, CancellationToken token) => Walk(Resolve(path, token), depth, maxEntries, elapsed, token);

    private (List<WorkspaceDocuments.Entry> Entries, bool Limited) Walk(
        Scope resolved, int depth, int maxEntries,
        Stopwatch elapsed, CancellationToken token)
    {
        if (resolved.Ignored)
            return ([], false);
        var scope = resolved.Entry;
        if (scope is { IsDirectory: false })
        {
            using var lease = workspace.OpenPath(scope.RelativePath);
            using var handle = WindowsWorkspace.OpenForRead(lease.AbsolutePath, FileShare.Read);
            WindowsWorkspace.GetFileIdentity(handle, lease.AbsolutePath);
            return ([scope], false);
        }

        var folders = new Queue<(string? Path, int Level, WorkspaceIgnore.Rule[] Rules, bool TrackedOnly)>();
        folders.Enqueue((scope?.RelativePath, 1, resolved.Rules, resolved.TrackedOnly));
        var result = new List<WorkspaceDocuments.Entry>();
        var scanned = 0;
        var limited = false;
        while (folders.TryDequeue(out var folder))
        {
            token.ThrowIfCancellationRequested();
            if (elapsed.Elapsed >= ScanBudget)
                return (result, true);
            WorkspaceDocuments.Entry[] children;
            WorkspaceIgnore.Rule[] rules;
            try
            {
                rules = folder.TrackedOnly ? folder.Rules : WorkspaceIgnore.Read(workspace, folder.Path, folder.Rules);
                children = WorkspaceDocuments.List(workspace, folder.Path, token);
            }
            catch (ToolDeniedException ex) when (ex.Status == "directory_limit")
            {
                limited = true;
                continue;
            }
            catch (DirectoryNotFoundException) when (folder.Level > 1)
            {
                limited = true;
                continue;
            }
            foreach (var entry in children)
            {
                token.ThrowIfCancellationRequested();
                if (++scanned > MaxEntries || elapsed.Elapsed >= ScanBudget)
                    return (result, true);
                if (entry.Name[0] == '.' || index?.IsSubmodule(entry.RelativePath) == true)
                    continue;
                var trackedOnly = folder.TrackedOnly || WorkspaceIgnore.Excludes(rules, entry);
                if (trackedOnly && index?.Tracks(entry.RelativePath, entry.IsDirectory) != true)
                    continue;
                if (result.Count == maxEntries)
                    return (result, true);
                result.Add(entry);
                if (entry.IsDirectory && folder.Level < depth && entry.RelativePath.Length < 225)
                    folders.Enqueue((entry.RelativePath, folder.Level + 1, rules, trackedOnly));
            }
        }
        return (result, limited);
    }

    private Scope Resolve(string path, CancellationToken token)
    {
        var relative = path == "." ? null : WorkspaceDocuments.VisiblePath(path, allowHidden: false);
        var rules = WorkspaceIgnore.ReadRepositoryExcludes(workspace);
        if (relative is null)
            return new(null, rules);
        string? parent = null;
        WorkspaceDocuments.Entry? entry = null;
        var trackedOnly = false;
        foreach (var name in relative.Split('/'))
        {
            token.ThrowIfCancellationRequested();
            if (!trackedOnly)
                rules = WorkspaceIgnore.Read(workspace, parent, rules);
            var next = parent is null ? name : parent + "/" + name;
            using var target = workspace.OpenPath(next);
            var attributes = File.GetAttributes(target.AbsolutePath);
            if ((attributes & (FileAttributes.ReparsePoint | FileAttributes.Device)) != 0)
                throw new ToolDeniedException("path_denied", "项目发现只能使用工作区内的普通文件和目录。");
            entry = new(name, next, (attributes & FileAttributes.Directory) != 0);
            if (index?.IsSubmodule(next) == true)
                return new(null, [], Ignored: true);
            trackedOnly |= WorkspaceIgnore.Excludes(rules, entry);
            if (trackedOnly && index?.Tracks(next, entry.IsDirectory) != true)
                return new(null, [], Ignored: true);
            parent = next;
        }
        return new(entry, rules, TrackedOnly: trackedOnly);
    }

    private string[] ReadLines(string relative, ref int used)
    {
        try
        {
            if (drafts.Get(relative) is { } draft)
            {
                if (draft.Text is null || draft.Bytes > MaxSearchBytes - used)
                    return [];
                used += draft.Bytes;
                return SplitLines(RedactCredential(draft.Text, credential));
            }
            using var path = workspace.OpenPath(relative);
            using var snapshot = OpenSnapshot(path.AbsolutePath, FileShare.Read, maxBytes: Math.Min(256 * 1024, MaxSearchBytes - used));
            used += snapshot.Bytes.Length;
            var text = RedactCredential(Utf8.GetString(snapshot.Bytes), credential).TrimStart('\uFEFF');
            return SplitLines(text);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException or DecoderFallbackException)
        {
            // A candidate may be oversized, inaccessible, a hard link, or no longer present.
            return [];
        }
    }

    private static string[] SplitLines(string text) =>
        text.Replace("\r\n", "\n", StringComparison.Ordinal).Replace('\r', '\n').Split('\n');

    private static bool CaseSensitive(JsonElement args) => args.TryGetProperty("case_sensitive", out var value) && (value.ValueKind switch
    {
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        _ => throw new ToolDeniedException("invalid_tool", "case_sensitive 必须是布尔值。"),
    });

    private static string Excerpt(string line, int offset)
    {
        var start = Math.Clamp(offset - 75, 0, Math.Max(0, line.Length - 300));
        if (start > 0 && char.IsLowSurrogate(line[start]))
            start--;
        var end = Math.Min(line.Length, start + 300);
        if (end < line.Length && char.IsHighSurrogate(line[end - 1]))
            end--;
        var text = new string(line[start..end].Select(ch => char.IsControl(ch) && ch != '\t' ? ' ' : ch).ToArray());
        return (start > 0 ? "…" : "") + text + (end < line.Length ? "…" : "");
    }

    private static string Format(StringBuilder result, bool limited, string empty)
    {
        if (result.Length == 0)
            result.AppendLine(empty);
        if (limited)
            result.AppendLine("（输出已达上限，请缩小 path 或 glob 后重试。）");
        return result.ToString().TrimEnd();
    }
}
