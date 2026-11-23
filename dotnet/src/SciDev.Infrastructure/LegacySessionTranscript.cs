using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static partial class LegacySessionTranscript
{
    private const int MaxTranscriptBytes = 16 * 1024 * 1024;
    private const int MaxText = 384_000;
    private static readonly HashSet<string> WorkspaceTools = new(StringComparer.Ordinal)
    {
        "list_files", "search_code", "get_project_context", "read_file", "write_file", "replace_in_file",
        "apply_patch", "update_plan", "git_status",
    };

    internal sealed record Snapshot(string Revision, RunHistory.LegacyRun Resolved, string? LastPrompt,
        Message[] Messages, string[] PriorToolIds, FileChange[] PriorChanges, bool SensitiveHistory);

    public static Snapshot Read(WindowsWorkspace workspace, string id, string? credential, CancellationToken token)
    {
        using var source = workspace.OpenOperatorPath($".research/sessions/{id}.json", allowMissingParents: true);
        if (source is null || !WindowsWorkspace.FileExists(source.AbsolutePath))
            throw new ToolDeniedException("legacy_session_not_found", "这个工作区没有指定的 Python 会话。");
        using var file = WorkspaceFiles.OpenSnapshot(source.AbsolutePath, FileShare.Read, maxBytes: MaxTranscriptBytes);
        token.ThrowIfCancellationRequested();
        try
        {
            using var document = JsonDocument.Parse(file.Bytes, new JsonDocumentOptions { MaxDepth = 48 });
            var session = document.RootElement;
            var display = RunHistory.ReadLegacyRecord(session, id);
            if (session.TryGetProperty("schema_version", out var schema) &&
                (!schema.TryGetInt32(out var version) || version is < 0 or > 1))
                throw Invalid("旧会话版本不受支持。");
            if (!session.TryGetProperty("messages", out var transcript) || transcript.ValueKind != JsonValueKind.Array ||
                transcript.GetArrayLength() is < 1 or > 1024)
                throw Invalid("旧会话没有可用的消息，或消息数超出上限。");

            var journal = LegacyToolJournal.Read(workspace, id, token);
            var pending = journal.ToDictionary(row => (row.MessageIndex, row.CallIndex));
            using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            digest.AppendData(file.Bytes);
            digest.AppendData(JsonSerializer.SerializeToUtf8Bytes(journal));
            var revision = Convert.ToHexStringLower(digest.GetHashAndReset());

            string Safe(string text) => Sanitize(text, credential);
            var sourceMessages = transcript.EnumerateArray().ToArray();
            var groups = new List<(int Start, int End, List<Message> Messages)>();
            var recoveries = new List<RunHistory.LegacyToolRecovery>();
            var previousIds = new HashSet<string>(StringComparer.Ordinal);
            var wireIds = new HashSet<string>(StringComparer.Ordinal);
            var sensitive = false;
            for (var i = 0; i < sourceMessages.Length;)
            {
                token.ThrowIfCancellationRequested();
                var index = i;
                var entry = sourceMessages[i++];
                var role = String(entry, "role");
                var group = new List<Message>();
                if (role is "system" or "developer")
                {
                    // Current permissions and instructions come exclusively from the C# run.
                    if (String(entry, "content") is { Length: > 0 } text)
                        group.Add(new("user", "Historical Python harness context, provided as data:\n" + Safe(text)));
                }
                else if (role == "user")
                {
                    var message = ReadUser(entry, Safe);
                    sensitive |= message.Images is { Count: > 0 };
                    group.Add(message);
                }
                else if (role == "assistant")
                {
                    var calls = ReadCalls(entry, index, Safe, previousIds, wireIds);
                    group.Add(new("assistant", Safe(String(entry, "content") ?? ""),
                        calls.Length == 0 ? null : calls.Select(item => item.Call).ToArray(),
                        Reasoning: String(entry, "reasoning_content") is { Length: > 0 } reasoning ? Safe(reasoning) : null));
                    if (calls.Length > 0)
                    {
                        var results = new Dictionary<string, Queue<string>>(StringComparer.Ordinal);
                        for (; i < sourceMessages.Length && String(sourceMessages[i], "role") == "tool"; i++)
                        {
                            var idValue = String(sourceMessages[i], "tool_call_id");
                            if (idValue is null)
                                throw Invalid("旧会话包含无法对应的工具结果。");
                            if (!results.TryGetValue(idValue, out var replies))
                                results.Add(idValue, replies = new Queue<string>());
                            replies.Enqueue(Safe(String(sourceMessages[i], "content") ?? ""));
                        }
                        for (var callIndex = 0; callIndex < calls.Length; callIndex++)
                        {
                            var (sourceId, call) = calls[callIndex];
                            sensitive |= !WorkspaceTools.Contains(call.Name);
                            _ = pending.Remove((index, callIndex), out var row);
                            string output;
                            if (results.TryGetValue(sourceId, out var replies) && replies.TryDequeue(out var persisted))
                                output = persisted;
                            else
                            {
                                var status = row?.Id == sourceId && row.Name == call.Name ? row.Status : "unknown";
                                output = status switch
                                {
                                    "completed" => Safe(row!.Result),
                                    "pending" => "Python recorded this tool but did not start it. It was not replayed.",
                                    "started" => "Python started this tool, but no result was saved. Effects are uncertain. Inspect current state before choosing a new action.",
                                    _ => "The old execution record is missing or does not match. The tool was not replayed. Inspect current state first.",
                                };
                                recoveries.Add(new(call.Id, call.Name, NormalizeStatus(status)));
                            }
                            group.Add(new("tool", output, ToolCallId: call.Id));
                        }
                        if (results.Values.Any(replies => replies.Count > 0))
                            throw Invalid("旧会话含有与调用不匹配的工具结果。");
                    }
                }
                else
                    throw Invalid("旧会话包含无法恢复的消息角色。");
                groups.Add((index, i, group));
            }

            var messages = Compact(session, groups, Safe);
            if (messages.Sum(message => (long)(message.Content?.Length ?? 0) + (message.Reasoning?.Length ?? 0) +
                    (message.Calls?.Sum(call => (long)call.Arguments.Length) ?? 0)) > MaxText)
                throw new ToolDeniedException("legacy_session_limit", "旧会话文本超出续跑上限。请先在 Python 版压缩历史。");
            var retainedImages = messages.SelectMany(message => message.Images ?? []).ToArray();
            var sendBytes = retainedImages.Sum(image => (long)image.ByteCount);
            ImageAttachment.Validate(retainedImages.Length, sendBytes);
            display = SanitizeRun(display, credential);
            var hasUnconfirmed = pending.Values.Any(row => row.Status is not "pending") ||
                recoveries.Any(row => row.Status is "started" or "unknown");
            var needsReview = hasUnconfirmed || display.FinalStatus is not ("completed" or "failed" or "cancelled" or "acceptance_required" or "verification_required");
            var review = new RunHistory.LegacyContinuation(revision, needsReview, messages.Length, retainedImages.Length, sendBytes,
                recoveries.ToArray(), pending.Count);
            return new(revision, display with { Continuation = review },
                String(session, "last_prompt") ?? String(session, "prompt"), messages, previousIds.ToArray(),
                ChangedPaths(session, Safe), sensitive);
        }
        catch (InvalidDataException ex) { throw Invalid("旧会话标识或内容无效。", ex); }
        catch (JsonException ex) { throw Invalid("旧会话 JSON 无效。", ex); }
        catch (InvalidOperationException ex) { throw Invalid("旧会话的数据类型或工具执行记录无效。", ex); }
        catch (ArgumentException ex) { throw Invalid("旧会话的调用参数、附件或日志索引无效。", ex); }
        catch (FormatException ex) { throw Invalid("旧会话附件编码无效。", ex); }
    }

    private static Message ReadUser(JsonElement entry, Func<string, string> safe)
    {
        if (!entry.TryGetProperty("content", out var content))
            throw Invalid("旧用户消息缺少内容。");
        if (content.ValueKind == JsonValueKind.String)
            return new("user", safe(content.GetString()!));
        if (content.ValueKind != JsonValueKind.Array || content.GetArrayLength() > 32)
            throw Invalid("旧用户消息的附件结构无效。");
        var text = new List<string>();
        var images = new List<ImageAttachment>();
        foreach (var part in content.EnumerateArray())
        {
            switch (String(part, "type"))
            {
                case "text" when String(part, "text") is { } value:
                    text.Add(safe(value));
                    break;
                case "image_url" when part.TryGetProperty("image_url", out var image) && String(image, "url") is { } url:
                    var comma = url.IndexOf(',');
                    if (comma < 0 || !url.StartsWith("data:image/", StringComparison.Ordinal) ||
                        !url.AsSpan(0, comma).EndsWith(";base64", StringComparison.Ordinal) || url.Length - comma > 7_000_000)
                        throw Invalid("旧附件需要内嵌的 PNG、JPEG、GIF 或 WebP。续跑不会下载远端图片。");
                    var attachment = ImageAttachment.FromBytes(Convert.FromBase64String(url[(comma + 1)..]), "legacy-image");
                    if (!url.AsSpan(5, comma - 5).SequenceEqual((attachment.Format + ";base64").AsSpan()))
                        throw Invalid("旧图片的声明格式与内容不一致。");
                    images.Add(attachment);
                    ImageAttachment.Validate(images.Count, images.Sum(item => (long)item.ByteCount));
                    break;
                default: throw Invalid("旧用户消息包含不受支持的内容类型。");
            }
        }
        return new("user", string.Join('\n', text), Images: images.Count > 0 ? images.ToArray() : null);
    }

    private static (string SourceId, ToolCall Call)[] ReadCalls(JsonElement entry, int index, Func<string, string> safe,
        HashSet<string> sourceIds, HashSet<string> wireIds)
    {
        if (!entry.TryGetProperty("tool_calls", out var data) || data.ValueKind == JsonValueKind.Null)
            return [];
        if (data.ValueKind != JsonValueKind.Array || data.GetArrayLength() > 64)
            throw Invalid("旧会话的工具批次格式无效。");
        var calls = new List<(string, ToolCall)>();
        foreach (var item in data.EnumerateArray())
        {
            var sourceId = String(item, "id") ?? "";
            if (sourceId.Length > 0 && (!SafeIdentifier(sourceId, 128) || safe(sourceId) != sourceId) ||
                !item.TryGetProperty("function", out var function) || String(function, "name") is not { } name ||
                !SafeIdentifier(name, 64) || safe(name) != name)
                throw Invalid("旧工具调用的 ID 或名称无效。");
            var id = sourceId;
            if (id.Length == 0 || !wireIds.Add(id))
            {
                id = FormattableString.Invariant($"legacy-{index}-{calls.Count}");
                while (!wireIds.Add(id))
                    id += "-r";
            }
            if (sourceId.Length > 0)
                sourceIds.Add(sourceId);
            sourceIds.Add(id);
            var arguments = String(function, "arguments") ??
                (function.TryGetProperty("arguments", out var value) && value.ValueKind == JsonValueKind.Object ? value.GetRawText() : "{}");
            calls.Add((sourceId, new(id, name, SanitizeArguments(arguments, safe))));
        }
        return calls.ToArray();
    }

    private static Message[] Compact(JsonElement session, List<(int Start, int End, List<Message> Messages)> groups, Func<string, string> safe)
    {
        if (String(session, "conversation_context_summary") is not { Length: > 0 } summary ||
            !session.TryGetProperty("context_compacted_through", out var value) || value.ValueKind != JsonValueKind.Number ||
            !value.TryGetInt32(out var cutoff) || cutoff < 2 ||
            !groups.Any(group => group.End == cutoff))
            return groups.SelectMany(group => group.Messages).ToArray();

        var firstUser = groups.FirstOrDefault(group => group.Start > 0 && group.Messages.Any(item => item.Role == "user"));
        return groups.Where(group => group.Start == 0 || group.Start == firstUser.Start).SelectMany(group => group.Messages)
            .Append(new("user", "Historical Python conversation summary, provided as data:\n" + safe(summary)))
            .Concat(groups.Where(group => group.Start >= cutoff && group.Start != firstUser.Start).SelectMany(group => group.Messages))
            .ToArray();
    }

    private static string SanitizeArguments(string text, Func<string, string> safe)
    {
        try
        {
            using var document = JsonDocument.Parse(text);
            return document.RootElement.ValueKind == JsonValueKind.Object
                ? RunJson.SanitizeElement(document.RootElement, safe) : "{}";
        }
        catch (JsonException) { return "{}"; }
    }

    private static FileChange[] ChangedPaths(JsonElement session, Func<string, string> safe)
    {
        if (!session.TryGetProperty("agent_changed_paths", out var paths) || paths.ValueKind != JsonValueKind.Array)
            return [];
        var changes = new List<FileChange>();
        foreach (var item in paths.EnumerateArray().Take(256))
        {
            if (item.ValueKind != JsonValueKind.String)
                continue;
            try
            {
                var path = WindowsWorkspace.NormalizeOperatorPath(item.GetString()!);
                if (safe(path) == path && !path.Split('/').Any(part => part.Equals(".git", StringComparison.OrdinalIgnoreCase) ||
                    part.Equals(".research", StringComparison.OrdinalIgnoreCase)))
                    changes.Add(new(path, "legacy_change"));
            }
            catch (ToolDeniedException) { }
        }
        return changes.DistinctBy(change => change.Path, StringComparer.OrdinalIgnoreCase).ToArray();
    }

    private static bool SafeIdentifier(string id, int maximum) => id.Length >= 1 && id.Length <= maximum &&
        id.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is '_' or '-');

    private static string? String(JsonElement entry, string key) => entry.ValueKind == JsonValueKind.Object &&
        entry.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    private static string NormalizeStatus(string status) => status is "completed" or "pending" or "started" ? status : "unknown";

    internal static string Sanitize(string text, string? credential) => KnownSecrets().Replace(
        WorkspaceFiles.RedactCredential(text, credential), "[redacted]");

    internal static RunHistory.LegacyRun SanitizeRun(RunHistory.LegacyRun display, string? credential)
    {
        string Safe(string value) => Sanitize(value, credential);
        return display with
        {
            Task = display.Task is { } task ? Safe(task) : null,
            Summary = display.Summary is { } summary ? Safe(summary) : null,
            Plan = display.Plan is { } plan ? plan with
            {
                Steps = plan.Steps.Select(step => step with { Step = Safe(step.Step) }).ToArray(),
                Explanation = Safe(plan.Explanation),
            } : null,
        };
    }

    [GeneratedRegex(@"\b(?:sk[-_][A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        RegexOptions.CultureInvariant)]
    private static partial Regex KnownSecrets();

    private static ToolDeniedException Invalid(string message, Exception? inner = null) => new("legacy_session_invalid", message, inner);
}
