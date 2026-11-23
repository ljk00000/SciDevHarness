using System.Globalization;
using System.Text;
using System.Text.Json;

namespace SciDev.Infrastructure;

internal sealed record McpToolDefinition(string ModelName, string RemoteName, JsonElement ModelDefinition)
{
    private static readonly JsonElement EmptyParameters = JsonSerializer.SerializeToElement(new { type = "object", properties = new { } });

    public static McpToolDefinition? Read(string server, JsonElement tool)
    {
        var name = McpRpc.StringField(tool, "name");
        if (string.IsNullOrWhiteSpace(name) || name.Length > 128 || name.Any(ch => char.IsControl(ch) ||
            CharUnicodeInfo.GetUnicodeCategory(ch) is UnicodeCategory.Format or UnicodeCategory.LineSeparator or UnicodeCategory.ParagraphSeparator))
            return null;
        var model = McpLocalConfiguration.ToolName(server, name);
        var description = PreviewText(McpRpc.StringField(tool, "description") ?? "", 500);
        var schema = tool.TryGetProperty("inputSchema", out var parameters) && parameters.ValueKind == JsonValueKind.Object &&
            McpRpc.StringField(parameters, "type") == "object" && Encoding.UTF8.GetByteCount(parameters.GetRawText()) <= 8192
            ? parameters : EmptyParameters;
        return new(model, name, JsonSerializer.SerializeToElement(new
        {
            type = "function",
            function = new { name = model, description = $"[MCP {server}] {description}", parameters = schema },
        }));
    }

    public static string PreviewText(string value, int limit)
    {
        value = value.ReplaceLineEndings("\n");
        var length = Math.Min(limit, value.Length);
        if (length > 0 && char.IsHighSurrogate(value[length - 1]))
            length--;
        return string.Create(length, value, (destination, text) =>
        {
            for (var i = 0; i < destination.Length; i++)
            {
                var ch = text[i];
                destination[i] = ch is '\n' or '\t' || (!char.IsControl(ch) && CharUnicodeInfo.GetUnicodeCategory(ch) != UnicodeCategory.Format) ? ch : '�';
            }
        });
    }
}
