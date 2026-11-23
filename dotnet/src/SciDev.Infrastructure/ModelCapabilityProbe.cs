using System.Net;
using System.Net.Http.Headers;
using System.Text.Json;

namespace SciDev.Infrastructure;

/// <summary>Query model metadata and request a synthetic tool call. The tool is never executed.</summary>
public static class ModelCapabilityProbe
{
    public sealed record Result(bool ToolsSupported, int? ModelCount, int? ContextLength, bool? Vision, string Detail);
    private const int MetadataLimit = 512 * 1024;
    private const int ProbeLimit = 128 * 1024;

    public static async Task<Result> CheckAsync(Uri chat, string model, string? credential, CancellationToken token)
    {
        const string suffix = "/chat/completions";
        if ((chat.Scheme != "https" && !(chat.IsLoopback && chat.Scheme == "http")) || chat.UserInfo.Length > 0 ||
            chat.Query.Length > 0 || chat.Fragment.Length > 0 || !chat.AbsoluteUri.EndsWith(suffix, StringComparison.Ordinal))
            throw new ArgumentException("模型探测地址无效。");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(15));
        using var client = new HttpClient(new SocketsHttpHandler
        {
            AllowAutoRedirect = false, UseProxy = false, UseCookies = false, MaxResponseDrainSize = 0,
            ConnectTimeout = TimeSpan.FromSeconds(5),
        }) { Timeout = Timeout.InfiniteTimeSpan };
        if (!string.IsNullOrEmpty(credential))
            client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", credential);
        int? count = null;
        int? context = null;
        bool? vision = null;
        var detail = new List<string>(2);
        try
        {
            using var metadata = new HttpRequestMessage(HttpMethod.Get, chat.AbsoluteUri[..^suffix.Length] + "/models");
            using var reply = await client.SendAsync(metadata, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
            if (reply.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
                return new(false, null, null, null, $"模型目录返回 HTTP {(int)reply.StatusCode}。");
            if (reply.IsSuccessStatusCode)
            {
                try
                {
                    using var json = await ReadJsonAsync(reply, MetadataLimit, deadline.Token);
                    (count, context, vision) = Metadata(json.RootElement, model);
                }
                catch (Exception ex) when (ex is InvalidDataException or JsonException)
                {
                    detail.Add("模型目录格式无效或超出限制。");
                }
            }
            else
                detail.Add($"模型目录返回 HTTP {(int)reply.StatusCode}。");

            using var request = new HttpRequestMessage(HttpMethod.Post, chat)
            {
                Content = new ByteArrayContent(JsonSerializer.SerializeToUtf8Bytes(new
                {
                    model, stream = false, max_tokens = 12_000,
                    messages = new[] { new { role = "user", content = "Call the ping tool once with an empty object. Do not answer with prose." } },
                    tools = new[] { new { type = "function", function = new
                    {
                        name = "ping", description = "Synthetic capability probe.",
                        parameters = new { type = "object", properties = new { }, additionalProperties = false },
                    } } },
                    tool_choice = "auto",
                })),
            };
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
            using var completion = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
            if (!completion.IsSuccessStatusCode)
            {
                detail.Add($"工具探测返回 HTTP {(int)completion.StatusCode}。");
                return new(false, count, context, vision, string.Join(' ', detail));
            }
            using var result = await ReadJsonAsync(completion, ProbeLimit, deadline.Token);
            return new(HasPing(result.RootElement), count, context, vision, string.Join(' ', detail));
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { detail.Add("模型探测超时。"); }
        catch (Exception ex) when (ex is HttpRequestException or IOException) { detail.Add("无法直连模型服务。"); }
        catch (Exception ex) when (ex is InvalidDataException or JsonException) { detail.Add("工具探测响应格式无效或超出限制。"); }
        return new(false, count, context, vision, string.Join(' ', detail));
    }

    private static async Task<JsonDocument> ReadJsonAsync(HttpResponseMessage reply, int limit, CancellationToken token)
    {
        if (reply.Content.Headers.ContentLength > limit)
            throw new InvalidDataException();
        await using var body = await reply.Content.ReadAsStreamAsync(token);
        var buffer = new byte[limit + 1];
        var read = await body.ReadAtLeastAsync(buffer, buffer.Length, false, token);
        if (read > limit)
            throw new InvalidDataException();
        return JsonDocument.Parse(buffer.AsMemory(0, read), new JsonDocumentOptions { MaxDepth = 24 });
    }

    private static (int?, int?, bool?) Metadata(JsonElement root, string model)
    {
        if (Field(root, "data") is not { ValueKind: JsonValueKind.Array } list)
            return (null, null, null);
        foreach (var item in list.EnumerateArray())
        {
            if (Text(item, "id") != model)
                continue;
            var context = new[] { "context_length", "max_context_length", "context_window", "max_input_tokens" }
                .Select(name => PositiveInt(Field(item, name))).FirstOrDefault(value => value is not null)
                ?? PositiveInt(Field(Field(item, "meta"), "n_ctx_train"));
            var vision = Field(item, "capabilities") is { ValueKind: JsonValueKind.Array } caps &&
                caps.EnumerateArray().Any(value => value.ValueKind == JsonValueKind.String &&
                    value.GetString()!.ToLowerInvariant() is "vision" or "image" or "image_input");
            return (list.GetArrayLength(), context, vision ? true : null);
        }
        return (list.GetArrayLength(), null, null);
    }

    private static bool HasPing(JsonElement root)
    {
        if (Field(root, "choices") is not { ValueKind: JsonValueKind.Array } choices || choices.GetArrayLength() == 0 ||
            Field(Field(choices[0], "message"), "tool_calls") is not { ValueKind: JsonValueKind.Array } calls ||
            calls.GetArrayLength() != 1)
            return false;
        var function = Field(calls[0], "function");
        if (Text(function, "name") != "ping" || string.IsNullOrWhiteSpace(Text(calls[0], "id")) ||
            Text(function, "arguments") is not { Length: > 0 and <= 64 } args)
            return false;
        using var json = JsonDocument.Parse(args);
        return json.RootElement.ValueKind == JsonValueKind.Object && !json.RootElement.EnumerateObject().Any();
    }

    private static JsonElement Field(JsonElement item, string name) => item.ValueKind == JsonValueKind.Object &&
        item.TryGetProperty(name, out var found) ? found : default;
    private static string? Text(JsonElement item, string name) =>
        Field(item, name) is { ValueKind: JsonValueKind.String } field ? field.GetString() : null;
    private static int? PositiveInt(JsonElement value) =>
        (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var size) ||
         value.ValueKind == JsonValueKind.String && int.TryParse(value.GetString(), out size)) && size > 0 ? size : null;
}
