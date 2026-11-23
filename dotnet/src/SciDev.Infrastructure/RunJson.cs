using System.Text;
using System.Text.Json;

namespace SciDev.Infrastructure;

public static class RunJson
{
    public static string SerializeRedacted(object value, JsonSerializerOptions options, string? credential)
    {
        var json = JsonSerializer.Serialize(value, options);
        if (string.IsNullOrEmpty(credential) || !json.Contains(JsonSerializer.Serialize(credential, options)[1..^1], StringComparison.Ordinal))
            return json;

        using var document = JsonDocument.Parse(json);
        return RedactElement(document.RootElement, credential, options.Encoder);
    }

    internal static string RedactElement(JsonElement element, string? credential,
        System.Text.Encodings.Web.JavaScriptEncoder? encoder = null)
    {
        if (string.IsNullOrEmpty(credential))
            return element.GetRawText();
        return SanitizeElement(element, text => WorkspaceFiles.RedactCredential(text, credential), encoder);
    }

    internal static string SanitizeElement(JsonElement element, Func<string, string> sanitize,
        System.Text.Encodings.Web.JavaScriptEncoder? encoder = null)
    {
        using var output = new MemoryStream();
        using (var writer = new Utf8JsonWriter(output, new() { Encoder = encoder }))
            WriteRedacted(element, writer, sanitize);
        return Encoding.UTF8.GetString(output.GetBuffer(), 0, (int)output.Length);
    }

    private static void WriteRedacted(JsonElement element, Utf8JsonWriter writer, Func<string, string> sanitize)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                writer.WriteStartObject();
                foreach (var property in element.EnumerateObject())
                {
                    writer.WritePropertyName(sanitize(property.Name));
                    WriteRedacted(property.Value, writer, sanitize);
                }
                writer.WriteEndObject();
                break;
            case JsonValueKind.Array:
                writer.WriteStartArray();
                foreach (var value in element.EnumerateArray())
                    WriteRedacted(value, writer, sanitize);
                writer.WriteEndArray();
                break;
            case JsonValueKind.String:
                writer.WriteStringValue(sanitize(element.GetString()!));
                break;
            default:
                element.WriteTo(writer);
                break;
        }
    }
}
