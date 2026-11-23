using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceFiles;

namespace SciDev.Infrastructure;

internal sealed class WorkspaceProjectContext(WindowsWorkspace workspace, string? credential, WorkspaceDrafts drafts)
{
    private const int MaxReadBytes = 2 * 1024 * 1024;
    private const RegexOptions Safe = RegexOptions.NonBacktracking | RegexOptions.CultureInvariant;
    private static readonly Regex Terms = new("""[\p{L}\p{N}_-]{2,80}""", Safe);
    private static readonly Regex KeyBlock = new("""-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\z)""", Safe);
    private static readonly Regex CredentialUrl = new("""(?i)\b(?:https?|git\+https?)://[^\s/@]+@[^\s"'<>]+""", Safe);
    private static readonly Regex Secret = new(
        """(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|private[_-]?key)\b["']?[ \t]*[:=][ \t]*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[A-Za-z0-9_+./=-]{12,})""", Safe);
    private static readonly Regex Tokens = new("""\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,})\b""", Safe);
    private static readonly Regex PythonVersion = new("""\A\d+\.\d+(?:\.\d+)?\z""", Safe);
    private static readonly HashSet<string> TextExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".cs", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".cpp",
        ".c", ".h", ".hpp", ".swift", ".ps1", ".sh", ".xaml", ".html", ".css", ".md", ".rst", ".txt",
    };
    private int _bytesRead;
    private bool _limited;
    private sealed record Excerpt(string Path, List<(int Number, string Text)> Lines);

    internal string Build(string scope, string query, string[] paths, string[] ancestors, bool limited, Stopwatch elapsed,
        TimeSpan budget, CancellationToken token, int maxOutput, WorkspaceGitIndex? gitIndex)
    {
        _limited = limited;
        var output = new StringBuilder();
        output.AppendLine("当前工作区 · 范围 " + scope);
        output.AppendLine($"Harness 运行时 .NET {Environment.Version} · {RuntimeInformation.ProcessArchitecture}");
        output.AppendLine("以下信息来自本地文件。依赖和命令仅为声明或静态候选，实际环境与执行结果尚未核验。");
        AppendDrafts(output, paths);
        if (gitIndex is not null)
            WorkspaceGitSummary.Append(output, workspace, gitIndex, drafts, scope, paths, elapsed, budget, token);

        var groups = GroupDirectories(paths);
        var testEvidence = InspectTests(paths, token);

        output.AppendLine().AppendLine("项目清单与验证候选");
        var manifests = paths.Where(path => ProjectManifest.IsManifest(Path.GetFileName(path)))
            .OrderByDescending(path => ancestors.Contains(DirectoryPart(path), StringComparer.OrdinalIgnoreCase)).Take(25).ToArray();
        if (manifests.Length == 0)
            output.AppendLine("（未发现常见清单）");
        foreach (var path in manifests)
        {
            token.ThrowIfCancellationRequested();
            if (elapsed.Elapsed >= budget || output.Length > 5_000)
            {
                _limited = true;
                break;
            }
            AppendManifest(output, path, ManifestSiblings(path, groups), testEvidence);
        }
        var locks = paths.Where(path => ProjectManifest.IsLock(Path.GetFileName(path))).Take(20);
        output.AppendLine("锁文件 · " + (string.Join(", ", locks) is { Length: > 0 } present ? Clip(present, 1_000) : "未发现"));
        AppendPythonEnvironments(output);
        var words = Terms.Matches(query).Select(match => match.Value).Distinct(StringComparer.OrdinalIgnoreCase).Take(12).ToArray();
        var excerpts = FindSources(paths, scope, words, elapsed, budget, token);

        output.AppendLine().AppendLine("适用 AGENTS.md（文件内容及作用域）");
        var instructionDirs = ancestors.Concat(excerpts.SelectMany(source => Ancestors(source.Path, directory: false)))
            .Distinct(StringComparer.OrdinalIgnoreCase);
        var instructions = InstructionsFor(instructionDirs, 7500, token);
        output.AppendLine(instructions.Length == 0 ? "（未发现）" : instructions);

        output.AppendLine().AppendLine("相关源文件与行号");
        foreach (var source in excerpts)
        {
            output.Append('[').Append(source.Path).AppendLine("]");
            foreach (var (number, line) in source.Lines)
                output.Append(number).Append(": ").AppendLine(line);
        }
        if (excerpts.Count == 0)
            output.AppendLine("（未命中；可用 search_code 搜索字面文本。）");
        output.AppendLine().AppendLine("文件索引（前 80 项）");
        foreach (var path in paths.Take(80))
            output.AppendLine(path);
        var result = RedactCredential(output.ToString().TrimEnd(), credential);
        const string notice = "\n（已达上下文预算，请缩小 path 或使用 search_code / read_file。）";
        return _limited || result.Length > maxOutput ? Clip(result, maxOutput - notice.Length, "") + notice : result;
    }

    internal VerificationPlan FindVerificationPlan(string[] scopedFiles, IEnumerable<string> testFiles,
        IReadOnlyList<FileChange>? changes, CancellationToken token)
    {
        var groups = GroupDirectories(scopedFiles);
        var knownFiles = testFiles.Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        var evidence = InspectTests(knownFiles, token);
        var dotnetProjects = new Lazy<DotnetTestProjects>(() => new(knownFiles, path => Read(path, 64 * 1024), token));
        var manifests = scopedFiles.Where(path => ProjectManifest.IsManifest(Path.GetFileName(path)))
            .ToLookup(DirectoryPart, StringComparer.OrdinalIgnoreCase);
        string ProjectDirectory(string path) => Ancestors(path, directory: false).LastOrDefault(manifests.Contains) ?? "";
        var scopes = changes is null ? manifests.Select(group => group.Key).Concat(evidence.PythonDirectories) :
            changes.Select(change => ProjectDirectory(change.Path));
        var directories = scopes.Distinct(StringComparer.OrdinalIgnoreCase).Order(StringComparer.OrdinalIgnoreCase).ToArray();
        var pythonDirectories = (changes is null ? evidence.PythonDirectories :
            changes.Where(change => change.Path.EndsWith(".py", StringComparison.OrdinalIgnoreCase))
                .Select(change => ProjectDirectory(change.Path))).ToHashSet(StringComparer.OrdinalIgnoreCase);
        if (directories.Length == 0)
            return new(["."], []);

        var priority = new[] { "test", "lint", "typecheck" };
        var planned = new List<(VerificationCandidate Candidate, int Slot)>();
        var inspected = changes is null ? directories.Take(128).ToArray() : directories;
        var cargoScopes = changes is null ? inspected : changes.Where(change => CargoWorkspaceTests.IsCargoSource(change.Path))
            .Select(change => ProjectDirectory(change.Path)).Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        var cargoWorkspaces = CargoWorkspaceTests.Find(scopedFiles, cargoScopes, evidence.RustDirectories,
            path => Read(path, 64 * 1024), token);
        var cargoRoots = cargoWorkspaces.Select(candidate => candidate.Directory).ToHashSet(StringComparer.OrdinalIgnoreCase);
        foreach (var directory in inspected)
        {
            token.ThrowIfCancellationRequested();
            var selected = new Dictionary<string, VerificationCandidate>(StringComparer.Ordinal);
            foreach (var path in manifests[directory].Order(StringComparer.OrdinalIgnoreCase).Take(50))
            {
                token.ThrowIfCancellationRequested();
                if (_bytesRead >= MaxReadBytes || selected.Count == priority.Length)
                    break;
                var summary = ReadManifest(path, ManifestSiblings(path, groups), evidence, out _);
                if (summary is null)
                    continue;
                foreach (var check in summary.Checks)
                {
                    if ((check.Arguments is not ["cargo", "test"] || !cargoRoots.Contains(ScopeName(directory))) &&
                        SandboxVerification.Classify(check.Arguments) == check.Category)
                        selected.TryAdd(check.Category, new(check.Category, path, ScopeName(directory), check.Arguments));
                }
            }
            if (!selected.ContainsKey("test") && pythonDirectories.Contains(directory) && evidence.PythonSource(directory) is { } source &&
                evidence.PythonChecks(directory, declaredPytest: false).FirstOrDefault(check => SandboxVerification.Classify(check.Arguments) == check.Category) is { } python)
                selected.Add("test", new("test", source, ScopeName(directory), python.Arguments));
            if (!selected.ContainsKey("test") && manifests[directory].Where(DotnetTestProjects.IsProject).Take(2).ToArray() is [var subject] &&
                SandboxVerification.Classify(["dotnet", "test", subject, "--no-restore"]) == "test")
                planned.AddRange(dotnetProjects.Value.Find(subject).Take(3).Select((candidate, slot) => (candidate, slot)));
            planned.AddRange(priority.Where(selected.ContainsKey).Select((category, slot) => (selected[category], slot)));
        }
        return new(inspected.Select(ScopeName).ToArray(),
            cargoWorkspaces.Concat(planned.OrderBy(item => item.Slot).ThenBy(item => Array.IndexOf(priority, item.Candidate.Category))
                .Select(item => item.Candidate)).ToArray(),
            Complete: changes is not null || inspected.Length == directories.Length && _bytesRead < MaxReadBytes);
    }

    private static string ScopeName(string directory) => directory.Length == 0 ? "." : directory;

    private static Dictionary<string, HashSet<string>> GroupDirectories(string[] paths) =>
        paths.GroupBy(DirectoryPart, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.Select(path => Path.GetFileName(path)).ToHashSet(StringComparer.OrdinalIgnoreCase),
                StringComparer.OrdinalIgnoreCase);

    private static IReadOnlySet<string> ManifestSiblings(string path, Dictionary<string, HashSet<string>> groups)
    {
        if (Path.GetFileName(path).Equals("package.json", StringComparison.OrdinalIgnoreCase))
            foreach (var directory in Ancestors(path, directory: false).Reverse())
                if (groups.TryGetValue(directory, out var names) && names.Any(ProjectManifest.IsNodeLock))
                    return names;
        return groups[DirectoryPart(path)];
    }

    private ProjectTestEvidence InspectTests(IEnumerable<string> files, CancellationToken token)
    {
        var limit = _bytesRead + 512 * 1024;
        return new(files, path => _bytesRead >= limit ? null : Read(path, Math.Min(64 * 1024, limit - _bytesRead)), token);
    }

    internal string InstructionsFor(IEnumerable<string> directories, int maxChars, CancellationToken token)
    {
        var selected = new StringBuilder();
        foreach (var directory in directories)
        {
            token.ThrowIfCancellationRequested();
            var path = Join(directory, "AGENTS.md");
            if (Read(path, 64 * 1024) is not { } text)
                continue;
            selected.Append('[').Append(path).Append("；作用域 ").Append(directory.Length == 0 ? "." : directory).AppendLine("]");
            selected.AppendLine(Clip(Redact(text), 2_400, "…（请用 read_file 查看原文）"));
            if (selected.Length >= maxChars)
            {
                _limited = true;
                break;
            }
        }
        return Clip(selected.ToString().TrimEnd(), maxChars, "…（请用 read_file 查看完整说明）");
    }

    private void AppendDrafts(StringBuilder output, string[] paths)
    {
        var visible = paths.ToHashSet(StringComparer.OrdinalIgnoreCase);
        var dirty = drafts.VisiblePaths.Where(visible.Contains).Take(16).ToArray();
        if (dirty.Length == 0)
            return;
        output.AppendLine("未保存编辑（启动时快照，以下文件本任务只读）");
        foreach (var path in dirty)
            output.Append(path).AppendLine(drafts.Get(path)?.Text is null ? " · 超出快照预算，旧磁盘内容已排除" : " · 草稿快照");
    }

    private void AppendManifest(StringBuilder output, string path, IReadOnlySet<string> siblings, ProjectTestEvidence evidence)
    {
        output.AppendLine(path);
        var summary = ReadManifest(path, siblings, evidence, out var problem);
        if (summary is null)
        {
            output.Append("  ").AppendLine(problem);
            return;
        }
        if (summary.Declaration is not null)
            output.Append("  运行时 · ").AppendLine(summary.Declaration);
        if (summary.Dependencies.Length > 0)
            output.Append("  依赖声明 · ").AppendLine(Clip(string.Join(", ", summary.Dependencies), 900));
        foreach (var candidate in summary.Checks.Take(6))
            output.Append("  静态候选 ").Append(candidate.Category).Append("（目录 ")
                .Append(DirectoryPart(path) is { Length: > 0 } directory ? directory : ".").Append("） ")
                .AppendLine(JsonSerializer.Serialize(candidate.Arguments));
    }

    private ProjectManifest.Summary? ReadManifest(string path, IReadOnlySet<string> siblings, ProjectTestEvidence evidence, out string? problem)
    {
        problem = "清单不可读取或超出限制。";
        var text = Read(path, 64 * 1024);
        if (text is null)
            return null;
        try { return ProjectManifest.Describe(Path.GetFileName(path), text, siblings, evidence, DirectoryPart(path)); }
        catch (Exception ex) when (ex is JsonException or XmlException or InvalidOperationException)
        {
            problem = "清单格式不可解析。";
            return null;
        }
    }

    private void AppendPythonEnvironments(StringBuilder output)
    {
        foreach (var directory in new[] { ".venv", "venv", "env" })
        {
            if (Read(directory + "/pyvenv.cfg", 4096, internalPath: true) is not { } text)
                continue;
            var settings = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var line in text.Split('\n'))
            {
                var separator = line.IndexOf('=');
                if (separator > 0)
                    settings[line[..separator].Trim()] = line[(separator + 1)..].Trim();
            }
            var version = settings.GetValueOrDefault("version", settings.GetValueOrDefault("version_info", ""));
            var shared = settings.GetValueOrDefault("include-system-site-packages", "");
            output.Append("Python 虚拟环境配置 · ").Append(directory);
            if (PythonVersion.IsMatch(version))
                output.Append(" · Python ").Append(version);
            if (bool.TryParse(shared, out var share))
                output.Append(share ? " · 共享系统包" : " · 不共享系统包");
            output.AppendLine("（解释器及依赖未核验）");
        }
    }

    private List<Excerpt> FindSources(string[] paths, string scope, string[] words, Stopwatch elapsed,
        TimeSpan budget, CancellationToken token)
    {
        var ranked = paths.Where(path => TextExtensions.Contains(Path.GetExtension(path)))
            .OrderByDescending(path => path.Equals(scope, StringComparison.OrdinalIgnoreCase))
            .ThenByDescending(path => words.Count(word => path.Contains(word, StringComparison.OrdinalIgnoreCase)));
        var excerpts = new List<Excerpt>();
        foreach (var path in ranked.Take(100))
        {
            token.ThrowIfCancellationRequested();
            if (elapsed.Elapsed >= budget || _bytesRead >= MaxReadBytes)
            {
                _limited = true;
                break;
            }
            if (words.Length == 0 && !path.Equals(scope, StringComparison.OrdinalIgnoreCase))
                continue;
            if (Read(path, 72 * 1024) is not { } source)
                continue;
            using var lines = new StringReader(Redact(source));
            var hits = new List<(int Number, string Text)>();
            var first = new List<(int Number, string Text)>();
            for (var lineNumber = 1; lines.ReadLine() is { } line && hits.Count < 3; lineNumber++)
            {
                var offset = words.Select(word => line.IndexOf(word, StringComparison.OrdinalIgnoreCase)).FirstOrDefault(index => index >= 0, -1);
                if (offset < 0)
                {
                    if (first.Count < 3 && path.Equals(scope, StringComparison.OrdinalIgnoreCase) && !string.IsNullOrWhiteSpace(line))
                        first.Add((lineNumber, Clip(line, 220)));
                    continue;
                }
                var start = Math.Max(0, offset - 60);
                if (start > 0 && char.IsLowSurrogate(line[start]))
                    start--;
                hits.Add((lineNumber, (start > 0 ? "…" : "") + Clip(line[start..], 220)));
            }
            if (hits.Count == 0)
                hits = first;
            if (hits.Count != 0)
                excerpts.Add(new(path, hits));
            if (excerpts.Count == 8)
                break;
        }
        return excerpts;
    }

    private string? Read(string relative, int cap, bool internalPath = false)
    {
        if (_bytesRead >= MaxReadBytes)
        {
            _limited = true;
            return null;
        }
        if (!internalPath && drafts.Get(relative) is { } draft)
        {
            if (draft.Text is null || draft.Bytes > Math.Min(cap, MaxReadBytes - _bytesRead))
                return null;
            _bytesRead += draft.Bytes;
            return draft.Text;
        }
        try
        {
            using var target = internalPath ? workspace.OpenOperatorPath(relative, allowMissingParents: true) : workspace.OpenPath(relative);
            if (target is null || !WindowsWorkspace.FileExists(target.AbsolutePath))
                return null;
            using var snapshot = OpenSnapshot(target.AbsolutePath, FileShare.Read, maxBytes: Math.Min(cap, MaxReadBytes - _bytesRead));
            _bytesRead += snapshot.Bytes.Length;
            return Utf8.GetString(snapshot.Bytes).TrimStart('\uFEFF');
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException or DecoderFallbackException)
        {
            return null;
        }
    }

    private string Redact(string text)
    {
        text = RedactCredential(text, credential);
        text = KeyBlock.Replace(text, match => "[redacted]" +
            new string('\n', match.Value.Replace("\r\n", "\n", StringComparison.Ordinal).Count(ch => ch is '\n' or '\r')));
        text = CredentialUrl.Replace(text, "[redacted URL]");
        text = Secret.Replace(text, "$1[redacted]");
        return Tokens.Replace(text, "[redacted]");
    }

    private static string Clip(string text, int maximum, string suffix = "…")
    {
        if (text.Length <= maximum)
            return text;
        var end = maximum - suffix.Length;
        return text[..(char.IsHighSurrogate(text[end - 1]) ? end - 1 : end)] + suffix;
    }

    internal static string DirectoryPart(string path)
    {
        var index = path.LastIndexOf('/');
        return index < 0 ? "" : path[..index];
    }
    private static string Join(string directory, string name) => directory.Length == 0 ? name : directory + "/" + name;

    internal static IEnumerable<string> Ancestors(string? path, bool directory)
    {
        yield return "";
        if (path is null)
            yield break;
        if (directory)
            path += "/";
        for (var i = path.IndexOf('/'); i >= 0; i = path.IndexOf('/', i + 1))
            yield return path[..i];
    }
}
