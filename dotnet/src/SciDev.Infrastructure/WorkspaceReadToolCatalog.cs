using System.Text.Json;
using System.Text.Json.Nodes;

namespace SciDev.Infrastructure;

internal static class WorkspaceReadToolCatalog
{
    internal sealed record Definition(string Name, string Description, JsonElement InputSchema);
    private static readonly Definition[] WorkspaceTools = JsonSerializer.Deserialize<Definition[]>("""
        [
          {"name":"read_file","description":"Read a UTF-8 file in the workspace, up to 400 numbered lines.","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"start_line":{"type":"integer"},"end_line":{"type":"integer"}},"required":["path"],"additionalProperties":false}},
          {"name":"list_files","description":"List visible workspace-relative file and directory paths. Use depth 1 to look at a single directory, or up to 5 levels.","inputSchema":{"type":"object","properties":{"path":{"type":"string","default":"."},"depth":{"type":"integer","minimum":1,"maximum":5,"default":3}},"additionalProperties":false}},
          {"name":"search_code","description":"Find literal text in workspace UTF-8 files and return paths, line numbers and neighboring lines. Narrow path or glob if the output reaches its limits.","inputSchema":{"type":"object","properties":{"query":{"type":"string","minLength":1,"maxLength":256},"path":{"type":"string","default":"."},"glob":{"type":"string","description":"Optional workspace-relative glob such as src/**/*.cs"},"case_sensitive":{"type":"boolean"},"max_results":{"type":"integer","minimum":1,"maximum":40},"context_lines":{"type":"integer","minimum":0,"maximum":3}},"required":["query"],"additionalProperties":false}},
          {"name":"get_project_context","description":"Summarize project manifests and declared dependencies, lockfiles, static verification candidates, scoped AGENTS.md and relevant source lines. Use before investigating an unfamiliar project. This reads workspace files and runs no commands.","inputSchema":{"type":"object","properties":{"path":{"type":"string","description":"Workspace-relative file or directory; defaults to ."},"query":{"type":"string","maxLength":4000,"description":"Task keywords for selecting source excerpts."}},"additionalProperties":false}}
        ]
        """, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase })!;

    internal static IReadOnlyList<Definition> Definitions => WorkspaceTools;

    internal static JsonArray ModelTools() => new(WorkspaceTools.Select(tool => (JsonNode)new JsonObject
    {
        ["type"] = "function",
        ["function"] = new JsonObject
        {
            ["name"] = tool.Name,
            ["description"] = tool.Description,
            ["parameters"] = JsonNode.Parse(tool.InputSchema.GetRawText()),
        },
    }).ToArray());
}
