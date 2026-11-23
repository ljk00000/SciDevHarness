using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public enum EditorLanguage { Python, TypeScript }

public sealed record EditorDiagnostic(int Line, int Column, int EndLine, int EndColumn, string Severity,
    string Message, string Code, string Source);

/// <summary>A resident, operator-started language server for the active editor buffer.</summary>
public sealed partial class EditorLanguageServer : IAsyncDisposable
{
    public const int MaxDocumentBytes = 512 * 1024;
    private const int MaxMessagesPerOperation = 256;
    private readonly string _root;
    private readonly EditorLanguage _language;
    private readonly WindowsWorkspace _workspace;
    private readonly LocalStdioProcess _process;
    private readonly LanguageServerFrames _frames;
    private readonly SemaphoreSlim _operation = new(1, 1);
    private readonly object[] _folders;
    private (string Uri, string Text)? _document;
    private int _version;
    private int _requestId;
    private int _sync;
    private int _disposed;

    private EditorLanguageServer(string root, WindowsWorkspace workspace, EditorLanguage language, LocalStdioProcess process)
    {
        _root = root;
        _workspace = workspace;
        _language = language;
        _process = process;
        _frames = new(process.Input, process.Output);
        _folders = [new { uri = FileUri(root), name = Path.GetFileName(root) }];
    }

    public static EditorLanguage? ForFile(string path) => LanguageId(path) switch
    {
        "python" => EditorLanguage.Python,
        null => null,
        _ => EditorLanguage.TypeScript,
    };

    private static string? LanguageId(string path) => Path.GetExtension(path).ToLowerInvariant() switch
    {
        ".py" or ".pyi" => "python",
        ".ts" or ".mts" or ".cts" => "typescript",
        ".tsx" => "typescriptreact",
        ".js" or ".mjs" or ".cjs" => "javascript",
        ".jsx" => "javascriptreact",
        _ => null,
    };

    public async Task<IReadOnlyList<EditorDiagnostic>> DiagnoseAsync(string relative, string text, CancellationToken token = default)
    {
        if (ForFile(relative) != _language)
            throw new ArgumentException("文件类型和诊断服务不匹配。", nameof(relative));
        if (Encoding.UTF8.GetByteCount(text) > MaxDocumentBytes)
            throw new ToolDeniedException("diagnostic_document_limit", "编辑器诊断支持 512 KiB 以内的文本。");
        relative = WorkspaceDocuments.VisiblePath(relative);
        using var path = _workspace.OpenOperatorPath(relative)!;
        _ = WindowsWorkspace.FileExists(path.AbsolutePath);
        await _operation.WaitAsync(token).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
            WorkspaceNetworkPolicy.RequireAllowed(_root, "项目断网策略禁止向本机语言服务发送编辑内容。");
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
            deadline.CancelAfter(TimeSpan.FromSeconds(8));
            using var stop = deadline.Token.Register(_process.Abort);
            try
            {
                var uri = FileUri(path.AbsolutePath);
                var version = checked(++_version);
                await SynchronizeAsync(uri, LanguageId(relative)!, text, version, deadline.Token).ConfigureAwait(false);
                return await ReceiveDiagnosticsAsync(uri, version, deadline.Token).ConfigureAwait(false);
            }
            catch (Exception ex) when (deadline.IsCancellationRequested && ex is IOException or OperationCanceledException)
            {
                token.ThrowIfCancellationRequested();
                throw new TimeoutException("语言服务未在时限内返回诊断。", ex);
            }
        }
        finally { _operation.Release(); }
    }

    private async Task InitializeAsync(CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(8));
        using var stop = deadline.Token.Register(_process.Abort);
        var response = await RequestAsync("initialize", new
        {
            processId = Environment.ProcessId,
            rootUri = FileUri(_root),
            workspaceFolders = _folders,
            initializationOptions = _language == EditorLanguage.TypeScript
                ? (object)new { disableAutomaticTypingAcquisition = true } : new { },
            capabilities = new
            {
                general = new { positionEncodings = new[] { "utf-16" } },
                textDocument = new { publishDiagnostics = new { versionSupport = true } },
                workspace = new { workspaceFolders = true, configuration = true },
            },
        }, deadline.Token).ConfigureAwait(false);
        var capabilities = Member(response, "capabilities");
        if (Text(capabilities, "positionEncoding") is { } encoding && encoding != "utf-16")
            throw new InvalidDataException("当前编辑器只支持使用 UTF-16 列偏移的语言服务。");
        var sync = Member(capabilities, "textDocumentSync");
        _sync = Number(sync.ValueKind == JsonValueKind.Object ? Member(sync, "change") : sync) ?? 0;
        await NotifyAsync("initialized", new { }, deadline.Token).ConfigureAwait(false);
    }

    private async Task SynchronizeAsync(string uri, string language, string text, int version, CancellationToken token)
    {
        if (_document is { } previous && (previous.Uri != uri || _sync is not (1 or 2)))
        {
            await NotifyAsync("textDocument/didClose", new { textDocument = new { uri = previous.Uri } }, token).ConfigureAwait(false);
            _document = null;
        }
        if (_document is { } existing)
        {
            object change = _sync == 1 ? new { text } : new
            {
                range = new { start = new { line = 0, character = 0 }, end = EndOf(existing.Text) },
                text,
            };
            await NotifyAsync("textDocument/didChange", new { textDocument = new { uri, version }, contentChanges = new[] { change } }, token)
                .ConfigureAwait(false);
        }
        else
            await NotifyAsync("textDocument/didOpen", new { textDocument = new { uri, languageId = language, version, text } }, token)
                .ConfigureAwait(false);
        _document = (uri, text);
    }

    private async Task<IReadOnlyList<EditorDiagnostic>> ReceiveDiagnosticsAsync(string uri, int version, CancellationToken token)
    {
        for (var index = 0; index < MaxMessagesPerOperation; index++)
        {
            var packet = await _frames.ReadAsync(token).ConfigureAwait(false);
            if (await HandleServerRequestAsync(packet, token).ConfigureAwait(false) ||
                Text(packet, "method") != "textDocument/publishDiagnostics")
                continue;
            var data = Member(packet, "params");
            if (!SameFileUri(Text(data, "uri"), uri) ||
                Number(Member(data, "version")) is int actual && actual != version)
                continue;
            return ParseDiagnostics(Member(data, "diagnostics"));
        }
        throw new InvalidDataException("语言服务返回了过多无关消息。");
    }

    private async Task<JsonElement> RequestAsync(string method, object? parameters, CancellationToken token)
    {
        var id = ++_requestId;
        await _frames.SendAsync(new { jsonrpc = "2.0", id, method, @params = parameters }, token).ConfigureAwait(false);
        for (var index = 0; index < MaxMessagesPerOperation; index++)
        {
            var packet = await _frames.ReadAsync(token).ConfigureAwait(false);
            if (await HandleServerRequestAsync(packet, token).ConfigureAwait(false) || Number(Member(packet, "id")) != id)
                continue;
            var result = Member(packet, "result");
            if (result.ValueKind == JsonValueKind.Undefined)
                throw new InvalidDataException("语言服务拒绝了会话请求。");
            return result;
        }
        throw new InvalidDataException("语言服务没有返回会话响应。");
    }

    private ValueTask NotifyAsync(string method, object? parameters, CancellationToken token) =>
        _frames.SendAsync(new { jsonrpc = "2.0", method, @params = parameters }, token);

    private async ValueTask<bool> HandleServerRequestAsync(JsonElement packet, CancellationToken token)
    {
        var id = Member(packet, "id");
        var method = Text(packet, "method");
        if (id.ValueKind is not (JsonValueKind.Number or JsonValueKind.String) || method is null)
            return false;
        var items = Member(Member(packet, "params"), "items");
        object reply = method switch
        {
            "workspace/workspaceFolders" => new { jsonrpc = "2.0", id, result = _folders },
            "workspace/configuration" when items.ValueKind == JsonValueKind.Array && items.GetArrayLength() <= 64 =>
                new { jsonrpc = "2.0", id, result = new object?[items.GetArrayLength()] },
            "workspace/applyEdit" => new { jsonrpc = "2.0", id, result = new { applied = false } },
            "client/registerCapability" or "client/unregisterCapability" or "window/workDoneProgress/create" =>
                new { jsonrpc = "2.0", id, result = (object?)null },
            _ => new { jsonrpc = "2.0", id, error = new { code = -32601, message = "Method not supported" } },
        };
        await _frames.SendAsync(reply, token).ConfigureAwait(false);
        return true;
    }

    public void Abort() => _process.Abort();

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
            return;
        if (!_operation.Wait(0))
        {
            Abort();
            await _operation.WaitAsync().ConfigureAwait(false);
        }
        try
        {
            using var deadline = new CancellationTokenSource(TimeSpan.FromMilliseconds(600));
            using var stop = deadline.Token.Register(_process.Abort);
            await RequestAsync("shutdown", null, deadline.Token).ConfigureAwait(false);
            await NotifyAsync("exit", null, deadline.Token).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is IOException or InvalidDataException or JsonException or OperationCanceledException) { }
        finally
        {
            try { await _process.DisposeAsync().ConfigureAwait(false); }
            finally { _operation.Release(); }
        }
    }

    private static JsonElement Member(JsonElement node, string key) =>
        node.ValueKind == JsonValueKind.Object && node.TryGetProperty(key, out var result) ? result : default;

    private static string? Text(JsonElement node, string key) => Member(node, key) is { ValueKind: JsonValueKind.String } field
        ? field.GetString() : null;

    private static int? Number(JsonElement node) => node.ValueKind == JsonValueKind.Number && node.TryGetInt32(out var number)
        ? number : null;

    private static string FileUri(string path)
    {
        var parts = path.Replace('\\', '/').Split('/');
        return "file:///" + parts[0] + "/" + string.Join('/', parts.Skip(1).Select(Uri.EscapeDataString));
    }

    private static bool SameFileUri(string? candidate, string documentUri)
    {
        if (string.Equals(candidate, documentUri, StringComparison.OrdinalIgnoreCase))
            return true;
        if (!Uri.TryCreate(candidate, UriKind.Absolute, out var parsed) || !parsed.IsFile || parsed.Host.Length != 0 ||
            parsed.Query.Length != 0 || parsed.Fragment.Length != 0)
            return false;
        // vscode-uri serializes the Windows drive colon as %3A. Decode each URI once.
        return string.Equals(Uri.UnescapeDataString(parsed.AbsolutePath).TrimStart('/'),
            Uri.UnescapeDataString(new Uri(documentUri).AbsolutePath).TrimStart('/'), StringComparison.OrdinalIgnoreCase);
    }

    internal static object EndOf(string text)
    {
        var line = 0;
        var character = 0;
        for (var index = 0; index < text.Length; index++)
        {
            if (text[index] is '\r' or '\n')
            {
                if (text[index] == '\r' && index + 1 < text.Length && text[index + 1] == '\n')
                    index++;
                line++;
                character = 0;
            }
            else
                character++;
        }
        return new { line, character };
    }

    internal static IReadOnlyList<EditorDiagnostic> ParseDiagnostics(JsonElement payload)
    {
        if (payload.ValueKind != JsonValueKind.Array)
            throw new InvalidDataException("语言服务返回了无效的诊断列表。");
        var results = new List<EditorDiagnostic>();
        foreach (var item in payload.EnumerateArray().Take(200))
        {
            var range = Member(item, "range");
            var start = Member(range, "start");
            var end = Member(range, "end");
            if (Position(Member(start, "line")) is not int line ||
                Position(Member(start, "character")) is not int column ||
                Position(Member(end, "line")) is not int endLine ||
                Position(Member(end, "character")) is not int endColumn ||
                endLine < line || endLine == line && endColumn < column ||
                Text(item, "message") is not { Length: > 0 } message)
                continue;
            var code = Member(item, "code");
            var severity = Number(Member(item, "severity")) switch { 2 => "warning", 3 => "info", 4 => "hint", _ => "error" };
            results.Add(new(line, column, endLine, endColumn, severity,
                Limit(message, 1000), Limit(code.ValueKind == JsonValueKind.Number ? code.GetRawText() : Text(item, "code"), 80),
                Limit(Text(item, "source"), 60)));
        }
        return results;
    }

    private static int? Position(JsonElement node) => Number(node) is >= 0 and < int.MaxValue and int offset ? offset + 1 : null;

    private static string Limit(string? text, int max) => text is null ? "" : text[..Math.Min(text.Length, max)];
}
