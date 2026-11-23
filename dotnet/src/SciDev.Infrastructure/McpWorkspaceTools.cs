using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>The local tools exposed by the read-only MCP server.</summary>
public sealed class McpWorkspaceTools
{
    public sealed record Tool(string Name, string Description, JsonElement InputSchema);
    public sealed record Answer(string Text, bool IsError = false);

    private const string TempPrefix = "scidev-mcp-git-";
    private static readonly Tool[] Definitions = WorkspaceReadToolCatalog.Definitions
        .Select(tool => new Tool(tool.Name, tool.Description, tool.InputSchema))
        .Append(new("git_diff", "Read the visible Git status and text diff of the selected local repository.",
            JsonSerializer.SerializeToElement(new { type = "object", properties = new { }, additionalProperties = false })))
        .OrderBy(tool => tool.Name, StringComparer.Ordinal).ToArray();
    private static readonly Dictionary<string, Tool> ByName = Definitions.ToDictionary(tool => tool.Name, StringComparer.Ordinal);
    private readonly string _workspace;
    private readonly string? _credential;

    public McpWorkspaceTools(string workspace, string? credential)
    {
        _workspace = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        _ = new WindowsWorkspace(_workspace);
        _credential = credential;
    }

    public IReadOnlyList<Tool> Available => Definitions;
    public bool Contains(string name) => ByName.ContainsKey(name);

    public async ValueTask<Answer> CallAsync(string name, JsonElement arguments, CancellationToken token)
    {
        if (!ByName.TryGetValue(name, out var tool))
            return new("此服务仅提供列出的工作区只读工具。", true);
        if (!ValidArguments(tool.InputSchema, arguments))
            return new("参数缺失、重复、未知或超出工具约束。请按 tools/list 返回的 inputSchema 提供参数。", true);

        try
        {
            if (name == "git_diff")
            {
                var git = await ReadGitAsync(token);
                return new(git);
            }
            var files = new WorkspaceFiles(_workspace, executionMode: ExecutionMode.ReadOnly, credential: _credential);
            var result = await files.ExecuteAsync(new("mcp-read", name, arguments.GetRawText()), token);
            return new(WorkspaceFiles.RedactCredential(result.Text, _credential));
        }
        catch (ToolDeniedException ex) { return new(WorkspaceFiles.RedactCredential(ex.Message, _credential), true); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException or
            System.ComponentModel.Win32Exception or InvalidDataException)
        { return new("无法安全读取该工作区内容。", true); }
    }

    private async Task<string> ReadGitAsync(CancellationToken token)
    {
        var parent = Path.TrimEndingDirectorySeparator(Path.GetFullPath(Path.GetTempPath()));
        var relative = Path.GetRelativePath(_workspace, parent);
        if (!Path.IsPathFullyQualified(relative) && relative != ".." && !relative.StartsWith("..\\", StringComparison.Ordinal))
            throw new ToolDeniedException("git_temp_scope", "只读 Git 查询需要工作区外的系统临时目录。");
        var temp = Directory.CreateTempSubdirectory(TempPrefix);
        try
        {
            var result = await GitDiffReview.CaptureAsync(_workspace, approvedRepository: null, WorkspaceDrafts.Empty, _credential, token,
                scratchWorkspace: temp.FullName);
            return result.Text;
        }
        finally
        {
            if (temp.Name.StartsWith(TempPrefix, StringComparison.Ordinal) &&
                string.Equals(temp.Parent?.FullName, parent, StringComparison.OrdinalIgnoreCase) && (temp.Attributes & FileAttributes.ReparsePoint) == 0)
                temp.Delete(recursive: true);
        }
    }

    private static bool ValidArguments(JsonElement schema, JsonElement arguments)
    {
        if (arguments.ValueKind != JsonValueKind.Object)
            return false;
        var fields = schema.GetProperty("properties");
        var supplied = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in arguments.EnumerateObject())
        {
            if (!supplied.Add(property.Name) || !fields.TryGetProperty(property.Name, out var definition) ||
                !Satisfies(definition, property.Value))
                return false;
        }
        return !schema.TryGetProperty("required", out var required) || required.EnumerateArray().All(item => supplied.Contains(item.GetString()!));
    }

    private static bool Satisfies(JsonElement schema, JsonElement value) => schema.GetProperty("type").GetString() switch
    {
        "boolean" => value.ValueKind is JsonValueKind.True or JsonValueKind.False,
        "integer" => value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number) &&
            Within(schema, number, "minimum", "maximum"),
        "string" => value.ValueKind == JsonValueKind.String && value.GetString() is { } text &&
            Within(schema, text.EnumerateRunes().Count(), "minLength", "maxLength"),
        _ => false,
    };

    private static bool Within(JsonElement schema, int size, string minimum, string maximum) =>
        (!schema.TryGetProperty(minimum, out var min) || size >= min.GetInt32()) &&
        (!schema.TryGetProperty(maximum, out var max) || size <= max.GetInt32());
}
