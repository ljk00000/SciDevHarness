using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class TextToolCalls
{
    private const int MaxCalls = 16;
    private const int MaxText = 1024 * 1024;
    private const string Instruction = "\n\nUse the declared tools for workspace actions and wait for their results. " +
        "If native tool calls are unavailable, emit <tool_call>{\"name\":\"declared_function_name\",\"arguments\":{...}}</tool_call> with valid JSON.";
    private const RegexOptions Explicit = RegexOptions.Singleline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking;
    private static readonly Regex NumberedLine = new(@"(?m)^[ \t]*[0-9]+[ \t]*:[ \t]?", RegexOptions.NonBacktracking);
    private static readonly Regex Tagged = new(@"<tool_call>\s*(?<body>.*?)\s*</tool_call>|<tool_request>\s*(?<body>.*?)\s*</tool_request>", Explicit);
    private static readonly Regex Wrapped = new(@"\A\s*<tool_response>\s*(?<body>.*?)\s*</tool_response>\s*\z", Explicit);
    private static readonly Regex Fenced = new(@"\A\s*```(?:json|xml)\s*(?<body>.*?)```\s*\z", Explicit);

    internal static IReadOnlyList<Message> Advertise(IReadOnlyList<Message> messages)
    {
        var copy = messages.ToList();
        var index = copy.FindIndex(message => message.Role == "system");
        if (index < 0)
            copy.Insert(0, new("system", Instruction.Trim()));
        else
            copy[index] = copy[index] with { Content = copy[index].Content + Instruction };
        return copy;
    }

    internal static ModelTurn Normalize(ModelTurn turn, JsonNode tools)
    {
        if (turn.Calls.Count != 0 || turn.Text is not { Length: > 0 and <= MaxText } content)
            return turn;
        var catalog = tools.AsArray().Select(tool => tool?["function"]).OfType<JsonObject>()
            .Where(function => function["name"] is not null).ToDictionary(function => function["name"]!.GetValue<string>(),
                function => function["parameters"] as JsonObject, StringComparer.Ordinal);
        var parseText = NumberedLine.Replace(content, "");
        var candidates = Tagged.Matches(parseText);
        if (candidates.Count == 0)
        {
            var envelope = Wrapped.Match(parseText);
            if (!envelope.Success)
                envelope = Fenced.Match(parseText);
            var body = envelope.Success ? envelope.Groups["body"].Value : parseText.Trim();
            var calls = ParseBatch(body, catalog, allowMultiple: envelope.Success);
            return calls.Length > 0 ? turn with { Text = null, Calls = calls } : turn;
        }

        var accepted = new List<ToolCall>();
        var retained = new StringBuilder();
        var offset = 0;
        foreach (Match candidate in candidates)
        {
            var calls = ParseBatch(candidate.Groups["body"].Value, catalog, allowMultiple: true);
            if (calls.Length == 0 || accepted.Count + calls.Length > MaxCalls)
                return turn;
            accepted.AddRange(calls);
            retained.Append(parseText, offset, candidate.Index - offset);
            offset = candidate.Index + candidate.Length;
        }
        retained.Append(parseText, offset, parseText.Length - offset);
        var text = retained.ToString().Trim();
        return turn with { Text = text.Length == 0 ? null : text, Calls = accepted };
    }

    private static ToolCall[] ParseBatch(string input, Dictionary<string, JsonObject?> catalog, bool allowMultiple)
    {
        var body = input.Trim();
        if (!body.StartsWith('{'))
            return [];
        var bytes = Encoding.UTF8.GetBytes(body);
        var calls = new List<ToolCall>();
        var offset = 0;
        try
        {
            while (offset < bytes.Length)
            {
                if (calls.Count == MaxCalls)
                    return [];
                var reader = new Utf8JsonReader(bytes.AsSpan(offset), new JsonReaderOptions { MaxDepth = 32 });
                using var document = JsonDocument.ParseValue(ref reader);
                if (ReadCall(document.RootElement, catalog) is not { } call)
                    return [];
                calls.Add(call);
                offset += (int)reader.BytesConsumed;
                while (offset < bytes.Length && bytes[offset] is (byte)' ' or (byte)'\t' or (byte)'\r' or (byte)'\n')
                    offset++;
                if (offset == bytes.Length || calls.Count == 1 && offset == bytes.Length - 1 && bytes[offset] == (byte)'}')
                    break;
                if (!allowMultiple)
                    return [];
                if (bytes[offset] == (byte)',')
                {
                    offset++;
                    if (offset == bytes.Length)
                        return [];
                }
            }
        }
        catch (JsonException) { return []; }
        return calls.ToArray();
    }

    private static ToolCall? ReadCall(JsonElement envelope, Dictionary<string, JsonObject?> catalog)
    {
        if (envelope.ValueKind != JsonValueKind.Object || envelope.EnumerateObject().Count() != 2 ||
            !envelope.TryGetProperty("name", out var name) || name.ValueKind != JsonValueKind.String ||
            !catalog.TryGetValue(name.GetString()!, out var schema) || !envelope.TryGetProperty("arguments", out var arguments))
            return null;
        using var decoded = arguments.ValueKind == JsonValueKind.String ?
            JsonDocument.Parse(arguments.GetString()!, new JsonDocumentOptions { MaxDepth = 32 }) : null;
        arguments = decoded?.RootElement ?? arguments;
        if (arguments.ValueKind != JsonValueKind.Object || !Matches(arguments, schema))
            return null;
        return new("text-" + Guid.NewGuid().ToString("N"), name.GetString()!, arguments.GetRawText());
    }

    private static bool Matches(JsonElement value, JsonObject? schema)
    {
        if (schema?["type"] is JsonValue type && !HasType(value, type.GetValue<string>()) ||
            schema?["type"] is JsonArray types && !types.Any(item => item is JsonValue kind && HasType(value, kind.GetValue<string>())))
            return false;
        if (schema?["enum"] is JsonArray options && !options.Any(option => JsonNode.DeepEquals(option, JsonNode.Parse(value.GetRawText()))))
            return false;
        switch (value.ValueKind)
        {
            case JsonValueKind.Object:
                if (schema?["required"] is JsonArray required && required.Any(item => !value.TryGetProperty(item!.GetValue<string>(), out _)))
                    return false;
                var known = schema?["properties"] as JsonObject;
                var extra = schema?["additionalProperties"];
                var seen = new HashSet<string>(StringComparer.Ordinal);
                foreach (var property in value.EnumerateObject())
                {
                    if (!seen.Add(property.Name))
                        return false;
                    if (known?.TryGetPropertyValue(property.Name, out var entry) == true)
                    {
                        if (!Matches(property.Value, entry as JsonObject))
                            return false;
                    }
                    else if (extra is JsonObject other ? !Matches(property.Value, other) :
                        extra?.GetValueKind() != JsonValueKind.True && schema is not null)
                        return false;
                }
                return true;
            case JsonValueKind.Array:
                return InRange(value.GetArrayLength(), schema, "minItems", "maxItems") &&
                    value.EnumerateArray().All(item => Matches(item, schema?["items"] as JsonObject));
            case JsonValueKind.String:
                return InRange(value.GetString()!.Length, schema, "minLength", "maxLength");
            case JsonValueKind.Number:
                return value.TryGetDecimal(out var number) && InRange(number, schema, "minimum", "maximum");
            default:
                return true;
        }
    }

    private static bool InRange(decimal value, JsonObject? schema, string minimum, string maximum) =>
        (schema?[minimum] is not JsonValue min || !min.TryGetValue<decimal>(out var lower) || value >= lower) &&
        (schema?[maximum] is not JsonValue max || !max.TryGetValue<decimal>(out var upper) || value <= upper);

    private static bool HasType(JsonElement value, string type) => type switch
    {
        "object" => value.ValueKind == JsonValueKind.Object,
        "array" => value.ValueKind == JsonValueKind.Array,
        "string" => value.ValueKind == JsonValueKind.String,
        "integer" => value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out _),
        "number" => value.ValueKind == JsonValueKind.Number,
        "boolean" => value.ValueKind is JsonValueKind.True or JsonValueKind.False,
        "null" => value.ValueKind == JsonValueKind.Null,
        _ => false,
    };
}
