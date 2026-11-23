using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class EvaluationSuite
{
    internal sealed record Pricing(decimal InputPerMillion = 0, decimal OutputPerMillion = 0);
    internal sealed record TaskSpec(string Id, string Prompt, Dictionary<string, string> Setup, string[] Check, int Timeout, bool Verify,
        IReadOnlySet<string> EditableTests)
    {
        internal bool CanEdit(string path) => !IsChecker(Check, path) &&
            (EditableTests.Contains(path) || SandboxVerification.EvidenceCategory(Check) != "test" || !IsTestPath(path));
    }
    private sealed record Manifest(string? Name, string? Workspace, TaskEntry[]? Tasks, Dictionary<string, string>? Environment, Pricing? Pricing);
    private sealed record TaskEntry(string? Id, string? Prompt, Dictionary<string, string>? Setup, string[]? Check, double? Timeout, bool Verify,
        string[]? EditableTests);
    private static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, AllowDuplicateProperties = false };

    internal string Name { get; }
    internal string? Template { get; }
    internal TaskSpec[] Tasks { get; }
    internal Pricing Prices { get; }
    internal Dictionary<string, string> Environment { get; }

    private EvaluationSuite(string name, string? template, TaskSpec[] tasks, Pricing pricing, Dictionary<string, string> environment) =>
        (Name, Template, Tasks, Prices, Environment) = (name, template, tasks, pricing, environment);

    internal static EvaluationSuite Load(string path, string? credential)
    {
        try
        {
            using var file = WindowsWorkspace.OpenAbsoluteFilePath(Path.GetFullPath(path));
            using var snapshot = WorkspaceFiles.OpenSnapshot(file.AbsolutePath, FileShare.Read, maxBytes: 8 * 1024 * 1024);
            var bytes = snapshot.Bytes.AsSpan();
            if (bytes.StartsWith(Encoding.UTF8.Preamble))
                bytes = bytes[Encoding.UTF8.Preamble.Length..];
            var manifest = JsonSerializer.Deserialize<Manifest>(bytes, Json);
            if (manifest is not { Tasks.Length: >= 1 and <= 50 })
                throw Invalid("套件须包含 1 到 50 个任务。");

            var template = manifest.Workspace switch
            {
                null or "" => null,
                "." => Path.GetDirectoryName(file.AbsolutePath),
                var relative => Path.Combine(Path.GetDirectoryName(file.AbsolutePath)!, WorkspaceDocuments.VisiblePath(relative, allowHidden: false)),
            };
            if (template is not null)
                _ = new WindowsWorkspace(template);

            var name = manifest.Name ?? Path.GetFileNameWithoutExtension(file.AbsolutePath);
            if (string.IsNullOrWhiteSpace(name) || name.Length > 160 || name.Any(char.IsControl))
                throw Invalid("套件名称无效。");
            var ids = new HashSet<string>(StringComparer.Ordinal);
            var tasks = manifest.Tasks.Select(task => ValidateTask(task, ids, credential)).ToArray();
            var prices = manifest.Pricing ?? new();
            if (prices.InputPerMillion < 0 || prices.OutputPerMillion < 0 || prices.InputPerMillion > 1_000_000 || prices.OutputPerMillion > 1_000_000)
                throw Invalid("套件的价格配置无效。");
            var environment = manifest.Environment ?? [];
            if (environment.Keys.Any(key => key is not ("SCIDEV_MAX_AGENT_TURNS" or "SCIDEV_REQUEST_TIMEOUT_SECONDS" or "SCIDEV_TEXT_TOOL_CALL_FALLBACK")))
                throw Invalid("套件环境只接受轮次、请求超时和文本工具兼容选项。模型地址和凭据应由操作员配置。");
            return new(name, template, tasks, prices, environment);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or ToolDeniedException)
        {
            throw new ArgumentException("无法读取评测套件、模板或其中的路径无效。", ex);
        }
    }

    private static TaskSpec ValidateTask(TaskEntry? task, HashSet<string> ids, string? credential)
    {
        if (task is not { Id: { } id, Prompt: { } prompt, Check: { Length: >= 1 and <= 64 } arguments } ||
            string.IsNullOrWhiteSpace(id) || id.Length > 128 || id.Any(char.IsControl) || !ids.Add(id) ||
            string.IsNullOrWhiteSpace(prompt) || prompt.Length > 64_000 ||
            arguments.Any(arg => arg is null || arg.Contains('\0')) || string.IsNullOrWhiteSpace(arguments[0]) || arguments[0].StartsWith('-') ||
            arguments.Sum(arg => (long)arg.Length) > 4096)
            throw Invalid("任务 ID、提示词或检查命令无效。");
        var setup = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var size = 0L;
        foreach (var (path, content) in task.Setup ?? [])
        {
            var visible = WorkspaceDocuments.VisiblePath(path, allowHidden: false);
            if (content is null || !setup.TryAdd(visible, content))
                throw Invalid("任务准备文件存在重复或无效内容。");
            size += Encoding.UTF8.GetByteCount(content);
        }
        if (size > 2_000_000)
            throw Invalid("任务准备文件超过大小上限。");
        var editableTests = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var raw in task.EditableTests ?? [])
        {
            if (raw is null)
                throw Invalid("可编辑测试须指定准备文件的相对路径。");
            var path = WorkspaceDocuments.VisiblePath(raw, allowHidden: false);
            if (!setup.ContainsKey(path) || !IsTestPath(path) || IsChecker(arguments, path) ||
                SandboxVerification.EvidenceCategory(arguments) != "test" || !editableTests.Add(path))
                throw Invalid("可编辑测试须为唯一的已准备测试文件，且与直接验收入口分开。");
        }
        if (!string.IsNullOrEmpty(credential) &&
            (prompt.Contains(credential, StringComparison.Ordinal) || arguments.Concat(setup.Values).Any(value => value.Contains(credential, StringComparison.Ordinal))))
            throw Invalid("评测套件包含当前模型凭据。");
        return new(id, prompt, setup, arguments, (int)Math.Ceiling(Math.Clamp(task.Timeout is null or 0 ? 180 : task.Timeout.Value, 5, 1800)),
            task.Verify, editableTests);
    }

    private static bool IsChecker(string[] check, string path) => check.Any(arg => arg.Replace('\\', '/').TrimStart('.', '/')
        .Equals(path, StringComparison.OrdinalIgnoreCase));

    private static bool IsTestPath(string path)
    {
        var name = Path.GetFileName(path);
        return name.StartsWith("test_", StringComparison.OrdinalIgnoreCase) || name.EndsWith("_test.py", StringComparison.OrdinalIgnoreCase) ||
            name.Contains(".test.", StringComparison.OrdinalIgnoreCase) || name.Contains(".spec.", StringComparison.OrdinalIgnoreCase);
    }

    private static ArgumentException Invalid(string message) => new(message);
}
